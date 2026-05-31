from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from datetime import datetime

from api.load_variable import MODEL_MANAGER, AgentState, LAW_TOOLS
from langgraph.prebuilt import ToolNode, tools_condition
from api.chatbot.functions.tools import web_search, page_crawl4ai, search_entity, search_history, search_memory
from api.chatbot.functions.summary import memory_manager_graph

# === 도구 통합 ===
# 로컬에서 직접 정의한 도구 + korean-law-mcp(도커)에서 받아온 도구
local_tools = [web_search, page_crawl4ai, search_entity, search_history, search_memory]
tools = local_tools + LAW_TOOLS  # MCP 서버 다운 시 LAW_TOOLS는 빈 리스트로 안전 폴백
tool_node = ToolNode(tools)

# === 도구별 사용 한도 ===
# 명시되지 않은 도구는 DEFAULT_TOOL_LIMIT 적용
DEFAULT_TOOL_LIMIT = 3
TOOL_LIMITS = {
    # --- 로컬 도구 ---
    "web_search": 2,
    "page_crawl4ai": 999,         # 무제한
    "search_entity": 3,
    "search_history": 3,
    "search_memory": 2,

    # --- korean-law-mcp 도구 (실제 서버 노출 17개와 정확히 일치) ---
    # 법령 조회 (3개)
    "search_law": 5,              # 법령 검색 → mst 획득 (자주 호출)
    "get_law_text": 8,            # 조문 본문 (한 사건에서 여러 조문 인용 가능)
    "get_annexes": 3,             # 별표/서식 (금액표·요율표·별지)

    # 판례·결정례 통합 (2개, 17개 도메인 커버)
    "search_decisions": 5,        # 판례·헌재·조세심판·공정위·노동위·해석례 등 통합 검색
    "get_decision_text": 5,       # 결정례 전문

    # 체인 도구 (8개) — 한 번 호출로 여러 RPC 자동 실행, 무거우므로 보수적 한도
    "chain_full_research": 1,     # 가장 무거움: AI검색+법령+판례+해석례 종합
    "chain_law_system": 2,        # 법체계: 3단비교(법률→시행령→시행규칙), 위임구조
    "chain_action_basis": 2,      # 처분·허가 근거 (penalty 시나리오: 과태료 기준+감경)
    "chain_dispute_prep": 2,      # 쟁송 대비 (판례+행심+전문결정례 병렬)
    "chain_amendment_track": 2,   # 개정 추적 (신구대조+조문이력, timeline 시나리오)
    "chain_ordinance_compare": 2, # 조례 비교 (compliance 시나리오: 상위법 적합성)
    "chain_procedure_detail": 2,  # 절차·비용·서식 (manual 시나리오)
    "chain_document_review": 2,   # 계약서·약관 리스크 분석

    # 킬러·검증·메타 (4개)
    "verify_citations": 3,        # 환각 방지 인용 검증 (답변 마무리 필수)
    "impact_map": 2,              # 조문 영향 그래프 (인용 판례·해석·자치법규 역방향 탐색 + mermaid, v4.0)
    "discover_tools": 2,          # 미노출 76개 전문 도구 탐색
    "execute_tool": 5,            # 전문 도구 프록시 실행
}

async def general_chat(state: AgentState, config: RunnableConfig):
    """
    한국 법률 상담 답변 노드.
    korean-law-mcp 도구로 법령/판례를 실제 조회하여 환각 없이 답변.
    """
    print("--- 법률 상담 답변 노드 입장 ---")
    params = config.get("configurable", {})
    all_counts = state.get("tool_counts", {"general_chat": {}})
    current_counts = all_counts.get("general_chat", {})

    # 1. 도구별 한도 필터링
    available_tools = []
    for tool in tools:
        current_usage = current_counts.get(tool.name, 0)
        limit = TOOL_LIMITS.get(tool.name, DEFAULT_TOOL_LIMIT)
        if current_usage < limit:
            available_tools.append(tool)

    # 2. 모델 로드
    base_llm = MODEL_MANAGER.get_model(params.get("model", "gemini/gemini-2.5-flash-lite"))
    llm_with_tools = base_llm.bind_tools(available_tools) if available_tools else base_llm

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
- 답변 말미에 "본 답변은 일반 정보 제공 목적이며, 구체 사건은 변호사 상담이 필요합니다." 류의 안내를 포함하세요.
- 사용자가 법률 외 질문을 하면 자연스럽게 응대하되, 본분(법률 상담)으로 부드럽게 유도하세요.
"""

    # 5. 도구 사용 지침 (각 도구 description은 자동으로 LLM에게 전달되므로, 핵심 도구만 사용 패턴 안내)
    if available_tools:
        prompt_map = {
            # --- 로컬 도구 ---
            "web_search": "- 실시간 정보(뉴스/날씨/최신 소식)는 'web_search'\n",
            "page_crawl4ai": "- 검색된 URL 본문 파싱은 'page_crawl4ai'\n",
            "search_memory": "- 전체 대화 흐름/요약 조회는 'search_memory'\n",
            "search_history": "- 과거 대화의 구체 팩트 검색은 'search_history' (entity_filter로 특정 엔티티 한정)\n",
            "search_entity": "- 대화 중 등장한 객체/주제 정보는 'search_entity'\n",

            # --- 법률 MCP: 단일 조회 (5개) ---
            "search_law": "- 법령명으로 법령 검색 → mst/lawId 획득: 'search_law' (약칭 자동 인식: '화관법' → '화학물질관리법')\n",
            "get_law_text": "- 특정 법령의 조문 본문: 'get_law_text' (jo='제74조' 한글 입력 가능, 또는 '007400' 6자리 코드)\n",
            "get_annexes": "- 별표/서식(금액표·요율표·과태료 기준표·별지 양식): 'get_annexes'\n",
            "search_decisions": "- 판례·헌재·조세심판·공정위·노동위·개보위·해석례 등 17개 도메인 통합 검색: 'search_decisions' (domain 파라미터로 도메인 지정)\n",
            "get_decision_text": "- 결정례 전문: 'get_decision_text' (domain + id 필요. 긴 본문은 자동 축약, full=true로 전문)\n  ※ 판례는 'search_decisions(domain=\"precedent\", options={\"includeText\": true})'로 목록+상위 판례 본문을 한 번에 받을 수도 있음(2단계 생략)\n",

            # --- 법률 MCP: 체인 도구 (8개) — 복합 리서치를 한 방에 ---
            "chain_full_research": "- 종합 리서치(어디서 시작할지 모를 때): 'chain_full_research' — AI검색→법령→판례→해석례 자동 체이닝\n",
            "chain_law_system": "- 법체계 분석(법률→시행령→시행규칙 3단 비교, 위임구조): 'chain_law_system' (scenario='delegation'/'impact'로 확장)\n",
            "chain_action_basis": "- 처분/허가/과태료/벌칙 근거: 'chain_action_basis' (scenario='penalty'로 처분기준표+감경 행심+벌칙 종합 / scenario='action_plan'으로 '이럴 땐 이렇게' 5단계 안내: 상황진단→권리·구제→기관·기한→서류→주의)\n",
            "chain_dispute_prep": "- 쟁송·불복·소송 대비: 'chain_dispute_prep' (판례+행심+전문결정례 병렬)\n",
            "chain_amendment_track": "- 개정 이력 추적(신구대조, 연혁): 'chain_amendment_track' (scenario='timeline'로 판례·해석례 시계열 / scenario='time_travel'로 두 시점 본문 자동 diff: 예 '개인정보보호법 2020-01-01 vs 2025-11-01')\n",
            "chain_ordinance_compare": "- 조례 비교(상위법 vs 자치법규): 'chain_ordinance_compare' (scenario='compliance'로 위헌·취소 사례 포함)\n",
            "chain_procedure_detail": "- 절차/비용/서식 안내: 'chain_procedure_detail' (scenario='manual'로 공무원 처리 매뉴얼)\n",
            "chain_document_review": "- 계약서·약관 리스크 분석: 'chain_document_review' (문서 텍스트 입력 시 자동 분류+리스크 탐지)\n",

            # --- 법률 MCP: 킬러·검증·메타 (4개) ---
            "verify_citations": "- ★답변 작성 후 조문/판례 인용 검증 필수: 'verify_citations' — 가짜 조문/없는 항 탐지 (예: '형법 제9999조' → ✗)\n",
            "impact_map": "- 조문 하나의 파급효과 그래프: 'impact_map' — 특정 조문을 인용한 판례·헌재·해석례·행심·자치법규 역방향 탐색 + mermaid 그래프 (예: '민법 제103조 인용한 판례')\n",
            "discover_tools": "- 노출된 17개로 부족할 때 전문 도구(용어·연혁·조약·학칙·공단규정 등 76개) 검색: 'discover_tools'\n",
            "execute_tool": "- discover_tools로 찾은 전문 도구 실행: 'execute_tool' (toolName + params 전달)\n",
        }
        tool_prompt = "\n[도구 사용 지침]\n"
        for tool in available_tools:
            line = prompt_map.get(tool.name)
            if line:
                tool_prompt += line

        tool_prompt += (
            "\n[법률 상담 권장 흐름]\n"
            "1) **사실관계 정리**: 사용자에게 부족한 정보가 있으면 짧게 되묻기 (장소/시점/당사자/금액 등)\n"
            "2) **도구 선택 — 질문 복잡도에 맞춰**:\n"
            "   • 단순 조회(특정 조문만): search_law → get_law_text\n"
            "   • 판례 찾기: search_decisions(domain='precedent') → get_decision_text\n"
            "   • **복합 사건(상황 → 법적 대응) → 체인 도구 1회 호출이 가장 효율적**\n"
            "     - 처분/과태료/벌칙 근거 → chain_action_basis (일상 고민형 '이럴 땐?' → scenario='action_plan' 5단계 안내)\n"
            "     - 절차·서식·비용 → chain_procedure_detail\n"
            "     - 쟁송·불복 대비 → chain_dispute_prep\n"
            "     - 조례 vs 상위법 → chain_ordinance_compare\n"
            "     - 개정 이력 → chain_amendment_track (두 시점 본문 비교 → scenario='time_travel' 자동 diff)\n"
            "     - 계약서/약관 리뷰 → chain_document_review\n"
            "     - 법체계 파악 → chain_law_system\n"
            "     - 어디서 시작할지 모를 때 → chain_full_research\n"
            "   • 특정 조문의 파급효과(인용 판례·결정례·자치법규 관계망) → impact_map (mermaid 그래프 제공)\n"
            "3) **인용 검증(필수)**: 답변 초안에 인용한 조문·항·판례번호를 verify_citations로 일괄 검증\n"
            "   → ✗(없는 조문) 또는 ⚠(불명확) 발견 시 답변에서 제거 또는 수정\n"
            "4) **답변 작성**: 일반인 언어 + 정확한 조문 번호/판례번호 + 면책 안내\n"
            "\n[자치법규 → 상위법령 Fallback]\n"
            "조례/규칙에서 못 찾으면 상위법령으로 우회 (예: 휴직/복무/징계 → 지방공무원법 제63조·제48조·제69조)\n"
            "\n[환각 방지 핵심]\n"
            "- 조문 번호는 절대 추측 금지. get_law_text 결과만 인용.\n"
            "- '제N조 제M항'을 인용할 때는 항 번호도 verify_citations로 확인 (없는 항을 만들지 말 것).\n"
            "- 체인 도구 응답에 '[NOT_FOUND]' 또는 '[HALLUCINATION_DETECTED]' 마커가 있으면 해당 부분은 답변에서 제외.\n"
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

    # 7. 모델 호출
    response = await llm_with_tools.ainvoke(final_prompt)

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

# --- 그래프 구성 ---
workflow = StateGraph(AgentState)

# 노드 추가
workflow.add_node("general_chat", general_chat)
workflow.add_node("update_tool_counts", update_tool_counts)
workflow.add_node("tool_node", tool_node)
workflow.add_node("memory_manager", memory_manager_graph)

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
