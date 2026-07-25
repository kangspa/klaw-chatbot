from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, AIMessage
from langchain_core.runnables import RunnableConfig
from datetime import datetime

from api.load_variable import AgentState, LAW_TOOLS
from langgraph.prebuilt import ToolNode, tools_condition
from api.chatbot.functions.tools import web_search, page_crawl4ai, search_entity, search_history, search_memory
from api.chatbot.functions.summary import memory_manager_graph
from api.chatbot.functions.llm_utils import (
    resilient_ainvoke, is_empty_answer, finish_reason_of, empty_answer_notice,
)

# === 도구 통합 ===
# 로컬 도구(직접 정의) + korean-law-mcp 법률 도구.
# 법률 도구(LAW_TOOLS)는 load_variable.py에서 langchain_mcp_adapters의
# MCP_CLIENT.get_tools()로 '런타임에 동적 로드'된다. 즉 서버가 도구를
# 추가/이름변경/삭제해도 이 파일에 도구 이름을 하드코딩하지 않으므로
# 코드 수정 없이 그대로 반영된다.
local_tools = [web_search, page_crawl4ai, search_entity, search_history, search_memory]
tools = local_tools + LAW_TOOLS  # MCP 서버 다운 시 LAW_TOOLS는 빈 리스트로 안전 폴백
tool_node = ToolNode(tools)

# 로컬 도구 이름 집합 (MCP 도구와 구분하기 위한 기준)
LOCAL_TOOL_NAMES = {t.name for t in local_tools}

# === 도구별 사용 한도 ===
# 로컬 도구는 우리가 직접 관리하므로 이름별 명시 한도를 둔다.
# MCP(법률) 도구는 이름을 하드코딩하지 않고 '공통 기본 한도'를 적용한다.
#   → korean-law-mcp가 도구를 추가/변경해도 자동으로 한도가 적용된다.
DEFAULT_TOOL_LIMIT = 3       # 한도 미지정 로컬 도구 폴백
MCP_TOOL_LIMIT = 5           # korean-law-mcp 도구 공통 기본 한도

LOCAL_TOOL_LIMITS = {
    "web_search": 2,
    "page_crawl4ai": 999,    # 무제한
    "search_entity": 3,
    "search_history": 3,
    "search_memory": 2,
}

# (선택) 특정 MCP 도구만 한도를 따로 주고 싶을 때의 오버라이드.
# 여기에 없는 MCP 도구는 MCP_TOOL_LIMIT을 쓰며, 서버에서 사라진 이름이
# 남아 있어도 단순히 무시되므로(=stale 키여도) 안전하다. 즉 필수가 아니라 '튜닝'용.
MCP_TOOL_LIMIT_OVERRIDES = {
    "chain_full_research": 5,   # 가장 무거움(AI검색+법령+판례+해석 종합)이라 보수적으로 1회
}


def get_tool_limit(tool_name: str) -> int:
    """도구 이름으로 사용 한도를 결정. MCP 도구는 이름 하드코딩 없이 기본/오버라이드를 적용한다."""
    if tool_name in LOCAL_TOOL_NAMES:
        return LOCAL_TOOL_LIMITS.get(tool_name, DEFAULT_TOOL_LIMIT)
    # 로컬 도구가 아니면 korean-law-mcp 도구로 간주
    return MCP_TOOL_LIMIT_OVERRIDES.get(tool_name, MCP_TOOL_LIMIT)


# 로컬 도구 사용 지침 (우리가 만든 도구라 큐레이션 유지).
# MCP 법률 도구는 서버가 준 description을 그대로 노출하므로 여기 넣지 않는다.
LOCAL_TOOL_PROMPTS = {
    "web_search": "- 실시간 정보(뉴스/날씨/최신 소식)는 'web_search'\n",
    "page_crawl4ai": "- 검색된 URL 본문 파싱은 'page_crawl4ai'\n",
    "search_memory": "- 전체 대화 흐름/요약 조회는 'search_memory'\n",
    "search_history": "- 과거 대화의 구체 팩트 검색은 'search_history' (entity_filter로 특정 엔티티 한정)\n",
    "search_entity": "- 대화 중 등장한 객체/주제 정보는 'search_entity'\n",
}


def build_mcp_tool_catalog(mcp_tools, max_desc: int = 220) -> str:
    """
    현재 바인딩된 korean-law-mcp 도구를 'name: description' 목록으로 렌더링.
    설명은 서버가 제공한 tool.description을 그대로 사용하므로,
    도구가 늘거나 바뀌어도 코드 수정 없이 LLM에게 최신 목록이 전달된다.
    """
    lines = []
    for t in mcp_tools:
        desc = " ".join((t.description or "").split())  # 개행/중복 공백 정리
        if len(desc) > max_desc:
            desc = desc[:max_desc] + "…"
        lines.append(f"- {t.name}: {desc}" if desc else f"- {t.name}")
    return "\n".join(lines)


async def general_chat(state: AgentState, config: RunnableConfig):
    """
    한국 법률 상담 답변 노드.
    korean-law-mcp 도구로 법령/판례를 실제 조회하여 환각 없이 답변.
    """
    print("--- 법률 상담 답변 노드 입장 ---")
    params = config.get("configurable", {})
    all_counts = state.get("tool_counts", {"general_chat": {}})
    current_counts = all_counts.get("general_chat", {})

    # 1. 도구별 한도 필터링 (MCP 도구는 이름 하드코딩 없이 get_tool_limit로 동적 적용)
    available_tools = []
    for tool in tools:
        current_usage = current_counts.get(tool.name, 0)
        if current_usage < get_tool_limit(tool.name):
            available_tools.append(tool)

    # 2. 사용할 모델명 결정 (실제 호출/바인딩은 하단 resilient_ainvoke에서 재시도·폴백과 함께 수행)
    model_name = params.get("model", "gemini/gemini-2.5-flash-lite")

    # 3. 시간 정보
    client_iso = params.get("isotime")
    if client_iso:
        iso_timestamp = client_iso
        unix_timestamp = datetime.fromisoformat(client_iso).timestamp()
    else:
        now_obj = datetime.now().astimezone()
        iso_timestamp = now_obj.isoformat()
        unix_timestamp = now_obj.timestamp()

    # 4. 페르소나 + 원칙
    main_prompt = f"""
[현재 시각]: {iso_timestamp} (Unix: {unix_timestamp})

당신은 한국 법률 상담을 보조하는 AI 어시스턴트입니다.
법제처 Open API를 백엔드로 하는 korean-law-mcp 서버에 직접 접근할 수 있으며,
법령·판례·행정규칙·자치법규·헌재결정·조세심판·해석례 등을 실시간 조회할 수 있습니다.

[원칙]
- 사용자의 사실관계를 차분히 청취하고, 적용 가능한 법령·판례를 근거로 답변하세요.
- 법령/판례를 인용할 때는 반드시 korean-law-mcp 도구로 **실제 조회한 결과만** 사용하세요.
  LLM 자체 지식으로 조문 번호나 판례를 만들어내지 마세요(환각 위험).
- 답변은 일반인이 이해할 수 있는 평이한 표현으로 작성하되, 법조문 인용은 정확한 조항/명칭을 유지하세요.
- 법령·판례 원문을 길게 그대로 복사하지 말고, 조/항 번호와 짧은 요지만 인용한 뒤 핵심은 쉬운 말로 재서술하세요.
  (원문을 장문 그대로 복붙하면 Gemini의 저작권 필터(RECITATION)에 걸려 응답이 통째로 차단될 수 있습니다.)
- 답변 말미에 "본 답변은 일반 정보 제공 목적이며, 구체 사건은 변호사 상담이 필요합니다." 류의 안내를 포함하세요.
- 사용자가 법률 외 질문을 하면 자연스럽게 응대하되, 본분(법률 상담)으로 부드럽게 유도하세요.
"""

    # 5. 도구 사용 지침
    #    - 로컬 도구: 우리가 만든 것이라 큐레이션된 한 줄 지침 유지
    #    - MCP(법률) 도구: 서버가 준 name/description을 그대로 노출
    #      → 도구 이름을 하드코딩하지 않으므로 korean-law-mcp 업데이트가 자동 반영됨
    #    (각 도구의 상세 스키마/description은 bind_tools로 이미 LLM에 전달되며,
    #     아래 목록은 LLM이 한눈에 도구를 고르도록 돕는 개요 역할)
    if available_tools:
        tool_prompt = "\n[로컬 도구]\n"
        for tool in available_tools:
            line = LOCAL_TOOL_PROMPTS.get(tool.name)
            if line:
                tool_prompt += line

        mcp_available = [t for t in available_tools if t.name not in LOCAL_TOOL_NAMES]
        if mcp_available:
            tool_prompt += "\n[korean-law-mcp 법률 도구 — 각 도구 설명을 보고 상황에 맞게 선택]\n"
            tool_prompt += build_mcp_tool_catalog(mcp_available) + "\n"

        tool_prompt += (
            "\n[법률 상담 권장 흐름]\n"
            "1) **사실관계 정리**: 사용자에게 부족한 정보가 있으면 짧게 되묻기 (장소/시점/당사자/금액 등)\n"
            "2) **도구 선택 — 위 도구 설명을 참고해 질문 복잡도에 맞춰**:\n"
            "   • 단순 조회(특정 조문): 법령 검색으로 식별자(mst/lawId) 확보 → 조문 본문 조회\n"
            "   • 판례/결정례: 통합 결정례 검색 → 전문 조회\n"
            "   • **복합 사건(상황→법적 대응): 'chain_'으로 시작하는 체인 도구 1회 호출이 가장 효율적**\n"
            "     (처분/벌칙·절차/서식·쟁송·조례비교·개정이력·계약서검토·법체계 등 상황별 체인 제공)\n"
            "   • 특정 조문의 파급효과(인용 판례·결정례·자치법규 관계망): 영향 그래프 도구\n"
            "   • 노출 도구로 부족하면 전문 도구 탐색(discover) → 프록시 실행(execute) 도구 활용\n"
            "3) **인용 검증(필수)**: 답변 초안에 인용한 조문·항·판례번호를 인용 검증 도구로 일괄 검증\n"
            "   → ✗(없는 조문) 또는 ⚠(불명확) 발견 시 답변에서 제거 또는 수정\n"
            "4) **답변 작성**: 일반인 언어 + 정확한 조문 번호/판례번호 + 면책 안내\n"
            "\n[자치법규 → 상위법령 Fallback]\n"
            "조례/규칙에서 못 찾으면 상위법령으로 우회 (예: 휴직/복무/징계 → 지방공무원법 제63조·제48조·제69조)\n"
            "\n[환각 방지 핵심]\n"
            "- 조문 번호/판례는 절대 추측 금지. 법률 도구로 실제 조회한 결과만 인용.\n"
            "- '제N조 제M항'을 인용할 때는 항 번호도 인용 검증 도구로 확인 (없는 항을 만들지 말 것).\n"
            "- 도구 응답에 '[NOT_FOUND]' 또는 '[HALLUCINATION_DETECTED]' 마커가 있으면 해당 부분은 답변에서 제외.\n"
        )
        main_prompt += tool_prompt

    system_prompt = SystemMessage(content=main_prompt)

    # 6. 이전 대화 요약 컨텍스트
    turn_list = state.get('turn_memory', [])
    summary_content = (
        "# 이전 대화별 요약\n" + "\n".join([f"- {turn}" for turn in turn_list])
        if turn_list else "이전 대화 기록이 아직 없습니다."
    )
    summary_message = SystemMessage(content=summary_content)
    final_prompt = [system_prompt, summary_message] + state["messages"]

    # 7. 모델 호출 — 일시 오류(503/429/500) 자동 재시도 + 빈 응답 시 폴백 모델 순차 시도
    response = await resilient_ainvoke(model_name, available_tools, final_prompt)

    # 빈 응답(RECITATION/SAFETY 또는 모든 폴백 실패) → 사용자 안내 메시지로 대체
    if response is None or is_empty_answer(response):
        fr = finish_reason_of(response) if response is not None else "no_response"
        print(f"--- 최종 답변이 비어 안내 메시지로 대체 (finish_reason={fr}) ---")
        response = AIMessage(content=empty_answer_notice(fr))

    return {"messages": [response]}

async def update_tool_counts(state: AgentState):
    # 1. 전체 구조를 가져오되, 안전하게 기본값 생성
    all_counts = dict(state.get("tool_counts", {}))
    # 2. 해당 노드의 카운터 딕셔너리를 새로 생성(복사)
    current_counts = dict(all_counts.get("general_chat", {}))

    last_message = state["messages"][-1]

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        for call in last_message.tool_calls:
            tool_name = call["name"]
            current_counts[tool_name] = current_counts.get(tool_name, 0) + 1
    all_counts["general_chat"] = current_counts

    return {"tool_counts": all_counts}


async def memory_manager_safe(state: AgentState, config: RunnableConfig):
    """
    메모리 관리 서브그래프의 best-effort 래퍼.
    메모리 갱신(요약/엔티티/Qdrant 저장)은 '부차적' 작업이므로, 여기서 어떤 예외가 나도
    (예: LLM 503, 임베딩 오류) 삼켜서 이미 생성된 사용자 답변이 유실되지 않게 한다.
    → 답변 노드가 성공했다면 메모리가 실패해도 요청은 500이 아니라 정상 응답으로 끝난다.
    """
    try:
        return await memory_manager_graph.ainvoke(state, config)
    except Exception as e:
        print(f"[memory_manager] 실패 — 메모리 갱신 건너뜀(답변은 유지): {type(e).__name__}: {e}")
        return {}


# --- 그래프 구성 ---
workflow = StateGraph(AgentState)

# 노드 추가
workflow.add_node("general_chat", general_chat)
workflow.add_node("update_tool_counts", update_tool_counts)
workflow.add_node("tool_node", tool_node)
workflow.add_node("memory_manager", memory_manager_safe)

# 진입점 설정
workflow.set_entry_point("general_chat")

# 일반 대화 노드에서 도구 사용 여부에 따른 분기
workflow.add_conditional_edges(
    "general_chat",
    tools_condition,
    {
        "tools": "update_tool_counts",
        "__end__": "memory_manager"
    }
)
# 카운터 관리 후 실제 도구 활용 노드로 연결, 도구 활용 후 다시 general_chat으로 복귀
workflow.add_edge("update_tool_counts", "tool_node")
workflow.add_edge("tool_node", "general_chat")

workflow.add_edge("memory_manager", END)


def build_chatbot_agent(checkpointer):
    """
    main.py의 lifespan startup에서 호출.
    AsyncPostgresSaver는 이벤트 루프 안에서만 생성 가능하므로
    compile도 같은 시점에 수행한다.
    반환된 객체는 app.state.chatbot_agent에 저장되어 endpoint에서 꺼내 쓴다.
    """
    return workflow.compile(checkpointer=checkpointer)
