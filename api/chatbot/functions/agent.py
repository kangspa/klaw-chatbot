from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from datetime import datetime

from api.load_variable import AgentState, LAW_TOOLS
from langgraph.prebuilt import ToolNode, tools_condition
from api.chatbot.functions.tools import web_search, page_crawl4ai, search_entity, search_history, search_memory
from api.chatbot.functions.summary import memory_manager_graph
from api.chatbot.functions.llm_utils import (
    resilient_ainvoke, is_empty_answer, finish_reason_of, empty_answer_notice,
)

# === 도구 분류 및 ToolNode 구성 ===
# 로컬 도구(직접 정의) 및 MCP 법률 도구(LAW_TOOLS) 분리
local_tools = [web_search, page_crawl4ai, search_entity, search_history, search_memory]
LOCAL_TOOL_NAMES = {t.name for t in local_tools}

# 노드별 독립적 도구 처리를 위한 ToolNode 분리
local_tool_node = ToolNode(local_tools)
law_tool_node = ToolNode(LAW_TOOLS)

# === 도구별 사용 한도 ===
DEFAULT_TOOL_LIMIT = 3       # 한도 미지정 로컬 도구 폴백
MCP_TOOL_LIMIT = 5           # korean-law-mcp 도구 공통 기본 한도

LOCAL_TOOL_LIMITS = {
    "web_search": 2,
    "page_crawl4ai": 999,    # 무제한
    "search_entity": 3,
    "search_history": 3,
    "search_memory": 2,
}

MCP_TOOL_LIMIT_OVERRIDES = {
    "chain_full_research": 5,   # 종합 검색 체인 도구
}


def get_tool_limit(tool_name: str) -> int:
    """도구 이름으로 사용 한도를 결정. MCP 도구는 기본/오버라이드를 적용한다."""
    if tool_name in LOCAL_TOOL_NAMES:
        return LOCAL_TOOL_LIMITS.get(tool_name, DEFAULT_TOOL_LIMIT)
    return MCP_TOOL_LIMIT_OVERRIDES.get(tool_name, MCP_TOOL_LIMIT)


LOCAL_TOOL_PROMPTS = {
    "web_search": "- 실시간 정보(뉴스/날씨/최신 소식)는 'web_search'\n",
    "page_crawl4ai": "- 검색된 URL 본문 파싱은 'page_crawl4ai'\n",
    "search_memory": "- 전체 대화 흐름/요약 조회는 'search_memory'\n",
    "search_history": "- 과거 대화의 구체 팩트 검색은 'search_history' (entity_filter로 특정 엔티티 한정)\n",
    "search_entity": "- 대화 중 등장한 객체/주제 정보는 'search_entity'\n",
}


def build_mcp_tool_catalog(mcp_tools, max_desc: int = 220) -> str:
    """korean-law-mcp 도구를 'name: description' 목록으로 렌더링."""
    lines = []
    for t in mcp_tools:
        desc = " ".join((t.description or "").split())
        if len(desc) > max_desc:
            desc = desc[:max_desc] + "…"
        lines.append(f"- {t.name}: {desc}" if desc else f"- {t.name}")
    return "\n".join(lines)


# ==========================================
# 1. 법률 관련 메시지 이진 분류 노드 (classify_intent)
# ==========================================
async def classify_intent(state: AgentState, config: RunnableConfig):
    """
    호출 시 가장 먼저 사용자 메시지가 '법과 관련된 메시지'인지 이진 분류하는 노드.
    """
    print("--- 1. 분류 노드 (classify_intent) 입장 ---")
    params = config.get("configurable", {})
    model_name = params.get("model", "gemini/gemini-2.5-flash-lite")

    user_msg = ""
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, HumanMessage):
            user_msg = msg.content
            break

    turn_list = state.get('turn_memory', [])
    summary_content = (
        "이전 대화 요약:\n" + "\n".join([f"- {turn}" for turn in turn_list])
        if turn_list else "이전 대화 기록 없음."
    )

    prompt = f"""
당신은 대화분석 에이전트입니다. 사용자 메시지와 이전 대화 맥락을 바탕으로, 사용자 메시지가 '법과 관련된 메시지'인지 여부를 이진 분류하세요.

[판단 기준]
- 법률 관련 메시지 (True): 법률, 법령, 조례, 판례, 행정규칙, 법적 권리/의무, 소송, 고소, 계약서 검토, 손해배상, 노동법, 형사/민사/행정적 문제, 징계 등 법적 조언 및 조회가 필요한 문의.
- 법률 무관 메시지 (False): 인사말, 일상 대화, 날씨, 프로그래밍/기술 문의, 일반 상식, 법과 관련 없는 일반 질문.

[이전 대화 요약]
{summary_content}

[사용자 메시지]
{user_msg}

답변은 설명 없이 오직 표준 JSON 형태로만 응답하세요:
{{"is_law_related": true}} 또는 {{"is_law_related": false}}
"""
    is_law = False
    try:
        response = await resilient_ainvoke(model_name, [], [SystemMessage(content=prompt)])
        if response and response.content:
            text = response.content.lower().strip()
            if "true" in text:
                is_law = True
            elif "false" in text:
                is_law = False
            else:
                law_keywords = ["법", "조문", "판례", "소송", "고소", "계약", "처분", "위법", "손해배상", "조례", "과태료", "변호사"]
                is_law = any(k in user_msg for k in law_keywords)
    except Exception as e:
        print(f"--- [classify_intent] 오류 발생 ({e}), 기본값(False)으로 진행 ---")
        is_law = False

    print(f"--- 분류 결과: is_law_related = {is_law} ---")
    return {
        "is_law_related": is_law,
        "law_search_count": 0
    }


def route_by_law_intent(state: AgentState) -> str:
    """분류 결과에 따라 비법률 노드(general_chat) 또는 법률 검색 노드(law_search_agent)로 분기."""
    if state.get("is_law_related", False):
        return "law_search_agent"
    return "general_chat"


# ==========================================
# 2. 비법률 일반 대화 노드 (general_chat)
# ==========================================
async def general_chat(state: AgentState, config: RunnableConfig):
    """
    비법률 일반 메시지 처리 노드.
    LAW_TOOLS를 사용하지 않고 local_tools만 사용하여 에이전트 자체 판단으로 진행.
    """
    print("--- 2. 비법률 일반 대화 노드 (general_chat) 입장 ---")
    params = config.get("configurable", {})
    all_counts = state.get("tool_counts", {"general_chat": {}})
    current_counts = all_counts.get("general_chat", {})

    # local_tools만 사용 (LAW_TOOLS 제외)
    available_tools = []
    for tool in local_tools:
        current_usage = current_counts.get(tool.name, 0)
        if current_usage < get_tool_limit(tool.name):
            available_tools.append(tool)

    model_name = params.get("model", "gemini/gemini-2.5-flash-lite")

    client_iso = params.get("isotime")
    if client_iso:
        iso_timestamp = client_iso
        unix_timestamp = datetime.fromisoformat(client_iso).timestamp()
    else:
        now_obj = datetime.now().astimezone()
        iso_timestamp = now_obj.isoformat()
        unix_timestamp = now_obj.timestamp()

    main_prompt = f"""
[현재 시각]: {iso_timestamp} (Unix: {unix_timestamp})

당신은 친절하고 유용한 AI 어시스턴트입니다.
사용자의 일반 질문, 대화, 요청 사항에 대해 명확하고 친절하게 답변하세요.
"""
    if available_tools:
        main_prompt += "\n[이용 가능한 로컬 도구]\n"
        for tool in available_tools:
            line = LOCAL_TOOL_PROMPTS.get(tool.name)
            if line:
                main_prompt += line

    system_prompt = SystemMessage(content=main_prompt)

    turn_list = state.get('turn_memory', [])
    summary_content = (
        "# 이전 대화별 요약\n" + "\n".join([f"- {turn}" for turn in turn_list])
        if turn_list else "이전 대화 기록이 아직 없습니다."
    )
    summary_message = SystemMessage(content=summary_content)
    final_prompt = [system_prompt, summary_message] + state["messages"]

    response = await resilient_ainvoke(model_name, available_tools, final_prompt)

    if response is None or is_empty_answer(response):
        fr = finish_reason_of(response) if response is not None else "no_response"
        print(f"--- [general_chat] 응답 비어있음 안내 메시지로 대체 (finish_reason={fr}) ---")
        response = AIMessage(content=empty_answer_notice(fr))

    return {"messages": [response]}


async def update_general_tool_counts(state: AgentState):
    """general_chat 도구 사용 횟수 카운팅."""
    all_counts = dict(state.get("tool_counts", {}))
    current_counts = dict(all_counts.get("general_chat", {}))

    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        for call in last_message.tool_calls:
            tool_name = call["name"]
            current_counts[tool_name] = current_counts.get(tool_name, 0) + 1
    all_counts["general_chat"] = current_counts
    return {"tool_counts": all_counts}


# ==========================================
# 3. 법률 정보 필수 검색 노드 (law_search_agent)
# ==========================================
async def law_search_agent(state: AgentState, config: RunnableConfig):
    """
    법률 검색 노드.
    이전 대화 내역과 현재 메시지를 바탕으로 무조건 LAW_TOOLS를 이용하여 관련된 법률 정보 검색.
    답변 가능한 정보가 검색될 때까지 최대 5번, LAW_TOOLS를 사용해서 확실한 정보를 검색함.
    결과가 불충분할 경우 쿼리를 수정하여 재검색 또는 추가 정보를 요청함.
    """
    current_search_count = state.get("law_search_count", 0)
    print(f"--- 3. 법률 검색 노드 (law_search_agent) 입장 (시도 횟수: {current_search_count}/5) ---")

    # 최대 5회 시도 초과 또는 LAW_TOOLS가 없으면 더 이상 도구 호출을 수행하지 않고 완료 메시지 반환
    if current_search_count >= 5 or not LAW_TOOLS:
        print(f"--- [law_search_agent] 검색 종료 (시도: {current_search_count}/5 또는 LAW_TOOLS 없음) ---")
        return {"messages": [AIMessage(content="[법률 정보 검색 완료]")]}

    params = config.get("configurable", {})
    all_counts = state.get("tool_counts", {"law_search_agent": {}})
    current_counts = all_counts.get("law_search_agent", {})

    available_law_tools = []
    for tool in LAW_TOOLS:
        current_usage = current_counts.get(tool.name, 0)
        if current_usage < get_tool_limit(tool.name):
            available_law_tools.append(tool)

    if not available_law_tools:
        print("--- [law_search_agent] 이용 가능한 LAW_TOOLS 사용 한도 소진 ---")
        return {"messages": [AIMessage(content="[법률 정보 검색 완료]")]}

    model_name = params.get("model", "gemini/gemini-2.5-flash-lite")

    search_prompt = f"""
당신은 한국 법률 정보 전용 검색 에이전트입니다.
사용자의 질문 및 대화 맥락에 대해 답변할 수 있는 확실한 법률 정보(법령, 조문, 판례, 행정규칙, 조례 등)를 아래 [korean-law-mcp 법률 도구]를 사용해 **무조건 검색**하세요.

[검색 원칙 및 지침]
1. 사용자 질문에 답변 가능한 확실한 정보가 검색될 때까지 LAW_TOOLS를 사용하여 검색을 진행합니다.
2. 현재까지 진행된 법률 검색 횟수: {current_search_count + 1} / 5회 (최대 5회 시도 가능)
3. 1회 도구 사용 후 검색된 법률 정보가 사용자 질문에 답변하기 부족하거나 명확하지 않다고 판단되면, 살짝 변경된 쿼리로 다시 요청하거나 추가 정보 획득을 위한 쿼리로 **재검색 도구 호출**을 수행하세요.
4. 사용자에게 최종 설명을 작성하지 말고, 필수 법률 정보 수집을 위한 **LAW_TOOLS 도구 호출(Tool Call)**을 반드시 생성하세요.
5. 질문에 대한 법률 정보가 충분히 수집되어 더 이상 추가 검색이 필요 없는 경우에만, 도구 호출 없이 "[법률 정보 검색 완료]" 텍스트로만 응답하세요.

[korean-law-mcp 법률 도구 목록]
{build_mcp_tool_catalog(available_law_tools)}
"""
    system_prompt = SystemMessage(content=search_prompt)

    turn_list = state.get('turn_memory', [])
    summary_content = (
        "# 이전 대화별 요약\n" + "\n".join([f"- {turn}" for turn in turn_list])
        if turn_list else "이전 대화 기록이 아직 없습니다."
    )
    summary_message = SystemMessage(content=summary_content)
    final_prompt = [system_prompt, summary_message] + state["messages"]

    response = await resilient_ainvoke(model_name, available_law_tools, final_prompt)

    if response is None or is_empty_answer(response):
        fr = finish_reason_of(response) if response is not None else "no_response"
        print(f"--- [law_search_agent] 응답 비어있음 (finish_reason={fr}) ---")
        response = AIMessage(content="[법률 정보 검색 완료]")

    return {"messages": [response]}


async def update_law_tool_counts(state: AgentState):
    """law_search_agent 도구 카운팅 및 5회 시도 횟수 증가."""
    all_counts = dict(state.get("tool_counts", {}))
    current_counts = dict(all_counts.get("law_search_agent", {}))

    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        for call in last_message.tool_calls:
            tool_name = call["name"]
            current_counts[tool_name] = current_counts.get(tool_name, 0) + 1
    all_counts["law_search_agent"] = current_counts

    next_count = state.get("law_search_count", 0) + 1
    return {
        "tool_counts": all_counts,
        "law_search_count": next_count
    }


def route_law_search(state: AgentState) -> str:
    """법률 검색 도구 호출 여부 및 시도 횟수(최대 5회) 체크하여 분기."""
    last_msg = state["messages"][-1]
    count = state.get("law_search_count", 0)

    # 5회 미만이고 도구 호출이 남아있으면 update_law_tool_counts -> law_tool_node 진행
    if hasattr(last_msg, "tool_calls") and last_msg.tool_calls and count < 5:
        return "update_law_tool_counts"
    # 도구 호출이 없거나 5회 도달 시 최종 응답 노드(law_response_agent)로 이동
    return "law_response_agent"


# ==========================================
# 4. 법률 상담 응답 노드 (law_response_agent)
# ==========================================
async def law_response_agent(state: AgentState, config: RunnableConfig):
    """
    법률 상담 최종 응답 노드.
    LAW_TOOLS는 맵핑되지 않으며, local_tools (인터넷 검색, 메모리 도구 등) 사용 가능.
    앞선 law_search_agent 노드에서 수집한 법률 정보를 바탕으로 명확한 최종 답변 생성.
    """
    print("--- 4. 법률 상담 응답 노드 (law_response_agent) 입장 ---")
    params = config.get("configurable", {})
    all_counts = state.get("tool_counts", {"law_response_agent": {}})
    current_counts = all_counts.get("law_response_agent", {})

    # local_tools만 사용 (LAW_TOOLS 미포함)
    available_tools = []
    for tool in local_tools:
        current_usage = current_counts.get(tool.name, 0)
        if current_usage < get_tool_limit(tool.name):
            available_tools.append(tool)

    model_name = params.get("model", "gemini/gemini-2.5-flash-lite")

    client_iso = params.get("isotime")
    if client_iso:
        iso_timestamp = client_iso
        unix_timestamp = datetime.fromisoformat(client_iso).timestamp()
    else:
        now_obj = datetime.now().astimezone()
        iso_timestamp = now_obj.isoformat()
        unix_timestamp = now_obj.timestamp()

    main_prompt = f"""
[현재 시각]: {iso_timestamp} (Unix: {unix_timestamp})

당신은 한국 법률 상담 어시스턴트입니다.
이전 법률 검색 단계에서 수집된 법령·판례·조례 등 조회 결과를 바탕으로 사용자에게 최선의 명확하고 객관적인 답변을 제공하세요.

[답변 작성 원칙]
- 이전 단계에서 실제로 검색/조회된 법령 및 판례 조문만 근거로 사용하여 답변을 작성하세요 (환각 금지).
- 법률 조문 원문을 장문으로 복사하지 말고, 핵심 요지와 취지를 알기 쉬운 언어로 서술하세요.
- 일반인이 쉽게 이해할 수 있는 언어로 설명하되 조/항 명칭은 정확히 유지하세요.
- 필요시 최신 실시간 정보나 과거 대화 맥락 확인을 위해 이용 가능한 로컬 도구(인터넷 검색, 메모리 도구 등)를 활용할 수 있습니다.
- 답변 말미에는 반드시 아래 면책 조항을 포함하세요:
  "본 답변은 일반 정보 제공 목적이며, 구체 사건은 변호사 상담이 필요합니다."
"""
    if available_tools:
        main_prompt += "\n[이용 가능한 로컬 도구]\n"
        for tool in available_tools:
            line = LOCAL_TOOL_PROMPTS.get(tool.name)
            if line:
                main_prompt += line

    system_prompt = SystemMessage(content=main_prompt)

    turn_list = state.get('turn_memory', [])
    summary_content = (
        "# 이전 대화별 요약\n" + "\n".join([f"- {turn}" for turn in turn_list])
        if turn_list else "이전 대화 기록이 아직 없습니다."
    )
    summary_message = SystemMessage(content=summary_content)
    final_prompt = [system_prompt, summary_message] + state["messages"]

    response = await resilient_ainvoke(model_name, available_tools, final_prompt)

    if response is None or is_empty_answer(response):
        fr = finish_reason_of(response) if response is not None else "no_response"
        print(f"--- [law_response_agent] 응답 비어있음 안내 메시지로 대체 (finish_reason={fr}) ---")
        response = AIMessage(content=empty_answer_notice(fr))

    return {"messages": [response]}


async def update_law_response_tool_counts(state: AgentState):
    """law_response_agent 도구 사용 횟수 카운팅."""
    all_counts = dict(state.get("tool_counts", {}))
    current_counts = dict(all_counts.get("law_response_agent", {}))

    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        for call in last_message.tool_calls:
            tool_name = call["name"]
            current_counts[tool_name] = current_counts.get(tool_name, 0) + 1
    all_counts["law_response_agent"] = current_counts
    return {"tool_counts": all_counts}


# ==========================================
# 5. 메모리 세이프 노드 래퍼
# ==========================================
async def memory_manager_safe(state: AgentState, config: RunnableConfig):
    """
    메모리 관리 서브그래프의 best-effort 래퍼.
    """
    try:
        return await memory_manager_graph.ainvoke(state, config)
    except Exception as e:
        print(f"[memory_manager] 실패 — 메모리 갱신 건너뜀: {type(e).__name__}: {e}")
        return {}


# ==========================================
# 6. LangGraph 그래프 구성 (Workflow)
# ==========================================
workflow = StateGraph(AgentState)

# 노드 등록
workflow.add_node("classify_intent", classify_intent)
workflow.add_node("general_chat", general_chat)
workflow.add_node("update_general_tool_counts", update_general_tool_counts)
workflow.add_node("law_search_agent", law_search_agent)
workflow.add_node("update_law_tool_counts", update_law_tool_counts)
workflow.add_node("law_response_agent", law_response_agent)
workflow.add_node("update_law_response_tool_counts", update_law_response_tool_counts)

# ToolNode 등록 (로컬 도구 / MCP 법률 도구 분리)
workflow.add_node("local_tool_node", local_tool_node)
workflow.add_node("law_tool_node", law_tool_node)

# 메모리 관리 노드 등록
workflow.add_node("memory_manager", memory_manager_safe)

# 진입점 설정: 분류 노드로 시작
workflow.set_entry_point("classify_intent")

# 분류 노드 조건부 분기 (법률 / 비법률)
workflow.add_conditional_edges(
    "classify_intent",
    route_by_law_intent,
    {
        "general_chat": "general_chat",
        "law_search_agent": "law_search_agent"
    }
)

# 1) 비법률 일반 대화 흐름
workflow.add_conditional_edges(
    "general_chat",
    tools_condition,
    {
        "tools": "update_general_tool_counts",
        "__end__": "memory_manager"
    }
)
workflow.add_edge("update_general_tool_counts", "local_tool_node")
workflow.add_edge("local_tool_node", "general_chat")

# 2) 법률 검색 흐름 (최대 5회 도구 검색 루프)
workflow.add_conditional_edges(
    "law_search_agent",
    route_law_search,
    {
        "update_law_tool_counts": "update_law_tool_counts",
        "law_response_agent": "law_response_agent"
    }
)
workflow.add_edge("update_law_tool_counts", "law_tool_node")
workflow.add_edge("law_tool_node", "law_search_agent")

# 3) 법률 최종 응답 흐름 (local_tools만 사용 가능)
workflow.add_conditional_edges(
    "law_response_agent",
    tools_condition,
    {
        "tools": "update_law_response_tool_counts",
        "__end__": "memory_manager"
    }
)
workflow.add_edge("update_law_response_tool_counts", "local_tool_node")
workflow.add_edge("local_tool_node", "law_response_agent")

# 최종 종료 edge
workflow.add_edge("memory_manager", END)


def build_chatbot_agent(checkpointer):
    """
    main.py의 lifespan startup에서 호출하여 에이전트를 컴파일한다.
    """
    return workflow.compile(checkpointer=checkpointer)
