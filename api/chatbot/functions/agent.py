import re
import uuid

from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from datetime import datetime

from api.load_variable import AgentState, LAW_TOOLS
from langgraph.prebuilt import ToolNode, tools_condition
from api.chatbot.functions.tools import web_search, page_crawl4ai, search_entity, search_history, search_memory
from api.chatbot.functions.summary import memory_manager_graph
from api.chatbot.functions.llm_utils import (
    resilient_ainvoke, is_empty_answer, finish_reason_of, empty_answer_notice,
)
from api.chatbot.functions.law_auth import find_auth_failure, looks_like_auth_failure
from api.llm_gateway.chat_model import content_to_text

# === 도구 분류 및 ToolNode 구성 ===
# 로컬 도구(직접 정의) 및 MCP 법률 도구(LAW_TOOLS) 분리
local_tools = [web_search, page_crawl4ai, search_entity, search_history, search_memory]
LOCAL_TOOL_NAMES = {t.name for t in local_tools}
LAW_TOOL_NAMES = {t.name for t in LAW_TOOLS}


# law_search_agent가 검색 단계를 끝내며 남기는 내부 표식.
# 사용자에게 보이는 문장이 아니라 law_response_agent가 읽는 신호다.
SEARCH_DONE_MARKER = "[법률 정보 검색 완료]"
SEARCH_FAILED_MARKER = "[법률 정보 검색 실패]"
SEARCH_AUTH_ERROR_MARKER = "[법률 검색 시스템 인증 오류]"

# 재검색 라운드별 temperature 에스컬레이션.
# 첫 시도는 결정적으로(0.2), 실패가 쌓일수록 탐색적으로(0.5 → 0.8 → 1.0) 올려
# 같은 검색어를 반복 생성하는 고착을 깬다. 소진 후에는 1.0으로 반복.
# 주의: Gemini 3+ / GPT-5+ / Claude 5+ 는 temperature=1 고정이라 get_model이 무시한다(기본 1.0).
TEMP_SCHEDULE = (0.2, 0.5, 0.8, 1.0)


def escalated_temperature(failed_count: int) -> float:
    return TEMP_SCHEDULE[min(failed_count, len(TEMP_SCHEDULE) - 1)]

# 법률 답변에 반드시 붙어야 하는 면책 문구.
# 프롬프트로만 지시하면 모델이 종종 빠뜨리므로, 최종 답변에 코드로 보장한다.
LEGAL_DISCLAIMER = "본 답변은 일반 정보 제공 목적이며, 구체 사건은 변호사 상담이 필요합니다."


def ensure_disclaimer(msg: AIMessage) -> AIMessage:
    """최종 법률 답변에 면책 조항이 없으면 덧붙인다. 도구 호출 메시지는 건드리지 않는다."""
    if getattr(msg, "tool_calls", None):
        return msg
    content = getattr(msg, "content", None)
    if not isinstance(content, str) or not content.strip():
        return msg
    body = content.strip()
    if "변호사 상담이 필요합니다" not in body:
        body = f"{body}\n\n{LEGAL_DISCLAIMER}"
        print("--- [law_response_agent] 면책 조항 누락 → 자동 추가 ---")
    msg.content = body   # 앞뒤 공백까지 정리 (모델이 종종 빈 줄로 시작함)
    return msg


def handle_tool_error(e: Exception) -> str:
    """
    도구 실행 중 발생한 예외를 ToolMessage 내용으로 되돌려준다.

    이 핸들러가 없으면 안 되는 이유:
      LangGraph의 기본 처리(_default_handle_tool_errors)는 인자 검증 오류만 ToolMessage로 바꾸고
      나머지 예외는 그대로 재전파한다. 그런데 korean-law-mcp는 '검색 결과 0건'을 에러 결과로 돌려주고,
      langchain_mcp_adapters가 그것을 ToolException으로 바꾼다.
      → 기본값 그대로면 검색 결과가 없다는 이유만으로 그래프 전체가 죽어 500이 난다.

      게다가 그 예외 메시지에는 '키워드를 줄여서 재시도하라'는 힌트가 담겨 있다.
      이건 LLM이 읽고 다음 행동을 정하라고 만든 문구이므로, 예외로 터뜨릴 게 아니라
      ToolMessage로 모델에게 그대로 전달해야 law_search_agent의 재검색 루프가 의미를 갖는다.

    인자를 `Exception`으로 annotate한 것은 의도적이다. ToolNode는 핸들러의 타입 힌트로
    처리 대상 예외를 추론하므로(_infer_handled_types), 이렇게 해야 ToolException까지 포함된다.
    """
    return str(e) or repr(e)


# 노드별 독립적 도구 처리를 위한 ToolNode 분리
local_tool_node = ToolNode(local_tools, handle_tool_errors=handle_tool_error)
law_tool_node = ToolNode(LAW_TOOLS, handle_tool_errors=handle_tool_error)

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
    # v4.4.0에서 chain_* 8개가 legal_research(task 파라미터)로 통합 노출됨.
    # (기존 chain_full_research 항목은 노출 목록에 없어 무의미해 제거)
    "legal_research": 5,   # 종합 검색 통합 진입점
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
# 법률 검색 이력 추적
# ==========================================
# korean-law-mcp가 '데이터를 얻지 못함'을 알릴 때 응답 맨 앞에 붙이는 표식 (src/lib/errors.ts ErrorCodes)
#   [NOT_FOUND]        : 검색 결과 없음
#   [UPSTREAM_NO_DATA] : 법제처가 자료를 주지 않음 (부존재 증명 아님 — v4.13+)
#   [ANNEX_BODY_UNAVAILABLE] : 별표는 찾았으나 본문 텍스트를 얻지 못함
MCP_FAILURE_LABELS = ("[NOT_FOUND]", "[LAW_NOT_FOUND]", "[UPSTREAM_NO_DATA]", "[ANNEX_BODY_UNAVAILABLE]")


def current_turn_messages(messages: list) -> list:
    """
    이번 턴(마지막 HumanMessage 이후)의 메시지만 잘라낸다.
    체크포인트에는 직전 턴들의 ToolMessage도 남아 있어서, 그것까지 '이번 검색 시도'로
    집계하면 재검색 전략이 엉뚱한 이력을 보고 판단하게 된다.
    """
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return messages[i + 1:]
    return list(messages)


def summarize_law_search(messages: list) -> tuple[list[dict], bool]:
    """
    이번 턴에 수행한 법률 도구 호출의 성공/실패 이력을 정리한다.

    반환값: (attempts, any_success)
      attempts    : [{"tool": 도구명, "args": 인자dict, "ok": 성공여부, "reason": 실패요약}]
      any_success : 한 번이라도 실제 법률 정보를 받아왔는지

    실패 판정 기준:
      - ToolNode가 예외를 처리하며 붙인 status == "error" (MCP가 isError로 돌려준 실패 전부)
      - 또는 응답이 MCP의 실패 표식([NOT_FOUND] 등)으로 '시작'하는 경우
        (도구가 예외 대신 정상 응답으로 '없음'을 알리는 경우까지 커버)
        본문 중간에 표식이 섞인 것은 실패가 아니다 — legal_research 같은 종합 리서치는
        일부 하위 검색이 실패해도 나머지 결과와 함께 정상 응답하며, 그 안에 [NOT_FOUND]가 섞인다.
    """
    turn = current_turn_messages(messages)

    # tool_call_id → (도구명, 인자) 매핑을 먼저 만든다. ToolMessage에는 인자가 없기 때문.
    call_info: dict[str, tuple[str, dict]] = {}
    for m in turn:
        for call in (getattr(m, "tool_calls", None) or []):
            if call.get("name") in LAW_TOOL_NAMES and call.get("id"):
                call_info[call["id"]] = (call["name"], call.get("args") or {})

    attempts: list[dict] = []
    any_success = False
    for m in turn:
        if not isinstance(m, ToolMessage):
            continue
        info = call_info.get(m.tool_call_id)
        if not info:
            continue  # 로컬 도구 호출은 집계 대상이 아님
        tool_name, args = info
        # MCP 도구 응답은 콘텐츠 블록 리스트([{'type': 'text', ...}])로 오므로 텍스트만 이어 붙인다
        content = content_to_text(m.content)
        # 실패 판정: ToolNode의 error status / 실패 표식으로 시작하는 응답 / 법제처 인증 실패 페이지
        # (인증 실패는 API가 정상 응답처럼 안내 페이지를 돌려주는 경우가 있어 별도 검사)
        failed = (
            getattr(m, "status", None) == "error"
            or content.lstrip().startswith(MCP_FAILURE_LABELS)
            or looks_like_auth_failure(content)
        )

        attempts.append({
            "tool": tool_name,
            "args": args,
            "ok": not failed,
            # 전문을 보관한다. MCP가 메시지 끝에 붙여주는 '재시도 제안'을 뽑아 쓰기 때문에
            # 여기서 잘라내면 안 된다. 프롬프트에 넣을 때만 render 단계에서 줄인다.
            "reason": " ".join(content.split()) if failed else "",
        })
        if not failed:
            any_success = True

    return attempts, any_success


def render_search_attempts(attempts: list[dict]) -> str:
    """시도 이력을 프롬프트에 넣을 수 있는 목록 문자열로 변환."""
    if not attempts:
        return "아직 검색을 시도하지 않았습니다."
    lines = []
    for i, a in enumerate(attempts, start=1):
        args = ", ".join(f'{k}="{v}"' for k, v in a["args"].items())
        mark = "성공" if a["ok"] else f"실패 ({a['reason'][:120]})"
        lines.append(f"{i}. {a['tool']}({args}) → {mark}")
    return "\n".join(lines)


# ==========================================
# 재검색 강제 (검색어 축소 → 유사어 치환)
# ==========================================
# 모델에게 "다시 검색해"라고 지시하는 방식은 신뢰할 수 없다.
# korean-law-mcp의 오류 메시지 자체가 "사용자에게 '검색 실패'를 보고하고"라고 지시하고 있어서,
# 모델은 시스템 프롬프트보다 눈앞의 ToolMessage를 따라 곧바로 답변을 써버린다.
# 따라서 재검색은 설득하지 않고 코드가 직접 도구 호출을 만들어 넣는다.

# 검색어로 쓰기에 의미가 없는 조사·일반어 (축소 단계에서 우선 제거)
STOPWORDS = {"관련", "규정", "내용", "방법", "경우", "문제", "질문", "신고", "알려줘", "어떻게", "무엇"}

# 일상어 → 법령에서 실제로 쓰는 용어
LEGAL_SYNONYMS = {
    "프리랜서": "인적용역",
    "알바": "단시간근로자",
    "아르바이트": "단시간근로자",
    "월세": "차임",
    "전세": "임대차",
    "보증금": "임대차보증금",
    "해고": "해고제한",
    "퇴직금": "퇴직급여",
    "산재": "산업재해보상",
    "위자료": "손해배상",
    "이혼": "재판상 이혼",
    "층간소음": "공동주택 소음",
    "음주운전": "음주운전 처벌",
    "개인정보": "개인정보 보호",
}


def extract_retry_suggestions(reason: str) -> list[str]:
    """MCP 오류 메시지 끝의 `재시도 제안: "A" 또는 "B"` 에서 후보 검색어를 뽑는다."""
    m = re.search(r"재시도 제안\s*[:：]\s*(.+)$", reason)
    if not m:
        return []
    return [s.strip() for s in re.findall(r'["\'"“”]([^"\'“”]+)["\'“”]', m.group(1)) if s.strip()]


def shrink_query(query: str) -> str | None:
    """
    법제처 API는 공백 구분 키워드를 AND로 처리하므로 키워드가 많을수록 0건이 되기 쉽다.
    의미 없는 일반어를 먼저 버리고, 그래도 남으면 뒤에서부터 한 단어씩 덜어낸다.
    """
    parts = query.split()
    if len(parts) <= 1:
        return None
    trimmed = [p for p in parts if p not in STOPWORDS]
    if trimmed and len(trimmed) < len(parts):
        return " ".join(trimmed)
    return " ".join(parts[:-1])


def substitute_query(query: str) -> str | None:
    """일상어를 법령 용어로 바꾼 검색어를 만든다."""
    for common, legal in LEGAL_SYNONYMS.items():
        if common in query:
            replaced = query.replace(common, legal)
            if replaced != query:
                return replaced
    return None


async def ask_alternative_keyword(model_name: str, question: str, tried: list[str],
                                  temperature: float = 1.0) -> str | None:
    """
    유사 법령 용어를 LLM에게 물어본다. 도구 호출이 아니라 '검색어 한 줄'만 요구하므로
    도구 호출을 유도하는 것보다 훨씬 안정적으로 원하는 결과를 얻을 수 있다.
    """
    prompt = f"""
당신은 한국 법령 검색 전문가입니다. 아래 질문에 대한 법령을 법제처에서 찾으려 했지만 모두 실패했습니다.

[사용자 질문]
{question}

[이미 시도했지만 결과가 없던 검색어]
{", ".join(tried) if tried else "(없음)"}

법제처 법령 검색에서 결과가 나올 만한 **새로운 검색어 하나**를 제안하세요.

[규칙]
- 법령 제목에 실제로 등장할 법한 용어를 쓰세요 (예: 소득세법, 근로기준법, 주택임대차보호법, 인적용역).
- 일상어가 아니라 법률 용어여야 합니다.
- 공백으로 구분된 키워드는 최대 2개까지만 사용하세요 (AND 조건이라 많을수록 결과가 줄어듭니다).
- 이미 시도한 검색어는 절대 반복하지 마세요.
- 설명·따옴표·문장부호 없이 **검색어만** 한 줄로 출력하세요.
"""
    try:
        res = await resilient_ainvoke(
            model_name, [], [SystemMessage(content=prompt)],
            temperature=temperature, max_retries=1,
        )
    except Exception as e:
        print(f"--- [재검색] 대체 검색어 제안 실패: {type(e).__name__}: {e} ---")
        return None
    if res is None or is_empty_answer(res) or not isinstance(res.content, str):
        return None
    # 모델이 따옴표나 앞말을 붙이는 경우가 있어 첫 줄만 취하고 다듬는다
    keyword = res.content.strip().splitlines()[0].strip().strip('"\'`').strip()
    return keyword or None


def pick_search_tool(available_law_tools):
    """검색어(query)를 받는 도구를 고른다. search_law가 있으면 그것을 우선한다."""
    for tool in available_law_tools:
        if tool.name == "search_law":
            return tool
    for tool in available_law_tools:
        try:
            if "query" in tool.args:
                return tool
        except Exception:
            continue
    return None


async def force_next_search(model_name, question, attempts, available_law_tools,
                            temperature: float = 1.0):
    """
    모델이 재검색을 포기했을 때, 다음 검색을 코드가 직접 만들어 도구 호출 메시지로 돌려준다.

    후보 검색어 우선순위:
      1. MCP가 오류 메시지에 담아준 '재시도 제안'  (가장 신뢰도 높음 — 서버가 직접 알려준 값)
      2. 직전 검색어 축소 (불용어 제거 → 뒤에서부터 한 단어씩 제거)
      3. 일상어 → 법령 용어 치환 (내장 사전)
      4. LLM이 제안한 대체 법령 용어

    반환: tool_calls가 담긴 AIMessage, 또는 후보를 못 만들면 None
    """
    tool = pick_search_tool(available_law_tools)
    if tool is None:
        return None

    tried = failed_queries(attempts)
    tried_norm = {q.strip() for q in tried}

    def fresh(candidate: str | None) -> str | None:
        if not candidate:
            return None
        candidate = candidate.strip()
        return candidate if candidate and candidate not in tried_norm else None

    # 1) MCP의 재시도 제안 (최근 실패부터)
    for a in reversed(attempts):
        if a["ok"]:
            continue
        for suggestion in extract_retry_suggestions(a["reason"]):
            if (picked := fresh(suggestion)):
                return _make_search_call(tool, picked, "MCP 재시도 제안")

    last_query = tried[-1] if tried else question

    # 2) 검색어 축소 (한 단어가 남을 때까지)
    cursor = last_query
    while (cursor := shrink_query(cursor)):
        if (picked := fresh(cursor)):
            return _make_search_call(tool, picked, "검색어 축소")

    # 3) 일상어 → 법령 용어 치환
    for source in (last_query, question):
        if (picked := fresh(substitute_query(source))):
            return _make_search_call(tool, picked, "유사 법령 용어 치환")

    # 4) LLM에게 대체 검색어 요청 (에스컬레이션된 temperature로 — 새로운 후보를 얻는 게 목적)
    if (picked := fresh(await ask_alternative_keyword(model_name, question, tried, temperature))):
        return _make_search_call(tool, picked, "LLM 제안 대체 검색어")

    return None


def _make_search_call(tool, query: str, strategy: str) -> AIMessage:
    print(f"--- [재검색 강제] {strategy}: {tool.name}(query=\"{query}\") ---")
    return AIMessage(
        content="",
        tool_calls=[{
            "name": tool.name,
            "args": {"query": query},
            "id": f"forced_{uuid.uuid4().hex[:12]}",
            "type": "tool_call",
        }],
    )


def failed_queries(attempts: list[dict]) -> list[str]:
    """실패한 검색어들을 중복 없이 수집 (사용자 안내 문구에 사용)."""
    seen, result = set(), []
    for a in attempts:
        if a["ok"]:
            continue
        for value in a["args"].values():
            text = str(value).strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
    return result


# ==========================================
# 법률 질문 신호 감지 (분류 보정용)
# ==========================================
# 확실한 법률 신호가 있는데도 모델이 '일반 대화'로 분류하면, 법령 검색 없이
# 모델의 기억만으로 답하게 되어 근거 없는 답변이 나간다. 그 사고를 막는 안전망이다.

# '…법'으로 끝나지만 법률과 무관한 흔한 낱말 (오탐 방지)
NOT_A_LAW_WORD = {
    "방법", "사용법", "문법", "어법", "화법", "기법", "수법", "요법", "용법",
    "작법", "창법", "주법", "해법", "비법", "필법", "타법", "공법", "서법",
}

# 조문 번호 표기 (예: 제750조, 제 60 조)
ARTICLE_PATTERN = re.compile(r"제\s*\d+\s*조")
# 법령 이름으로 보이는 토큰 (예: 근로기준법, 소득세법, 주택임대차보호법, 도로교통법 시행령)
LAW_NAME_PATTERN = re.compile(r"[가-힣]{2,}(?:법률|법|시행령|시행규칙|조례|규칙)\b")

# 법률 문의임이 비교적 분명한 낱말들 ('법'처럼 오탐이 큰 단어는 제외)
LEGAL_KEYWORDS = (
    "법령", "법률", "조문", "판례", "소송", "고소", "고발", "기소", "구속",
    "손해배상", "위법", "불법", "합의금", "과태료", "벌금", "형사", "민사", "행정심판",
    "계약서", "임대차", "전세금", "보증금", "근로기준", "최저임금", "연차휴가", "퇴직금",
    "부당해고", "산업재해", "저작권", "특허", "상표", "이혼", "상속", "유언",
    "명예훼손", "횡령", "배임", "사기죄", "조례", "변호사", "법원", "검찰", "징계",
    "권리", "의무", "처벌", "배상", "소득세", "부가가치세", "종합소득세",
)


def looks_legal(text: str) -> bool:
    """법령명·조문 번호·법률 용어가 보이면 법률 질문으로 간주한다."""
    if not text:
        return False

    if ARTICLE_PATTERN.search(text):
        return True

    for match in LAW_NAME_PATTERN.finditer(text):
        word = match.group(0)
        if len(word) >= 3 and word not in NOT_A_LAW_WORD:
            return True

    return any(k in text for k in LEGAL_KEYWORDS)


# ==========================================
# 1. 법률 관련 메시지 이진 분류 노드 (classify_intent)
# ==========================================
async def classify_intent(state: AgentState, config: RunnableConfig):
    """
    호출 시 가장 먼저 사용자 메시지가 '법과 관련된 메시지'인지 이진 분류하는 노드.
    """
    print("--- 1. 분류 노드 (classify_intent) 입장 ---")
    params = config.get("configurable", {})
    model_name = params.get("model")

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
- 법률 관련 메시지 (True)
  · 법령·조문·판례·조례·행정규칙의 **내용을 단순히 물어보는 경우도 포함**합니다.
    법적 조언 요청이 아니어도, 답을 하려면 법령을 찾아봐야 한다면 True입니다.
  · 특정 법률 이름이나 조문 번호가 언급된 경우 (예: "근로기준법 연차휴가 규정", "민법 제750조")
  · 권리·의무, 처벌·제재, 신고·납세 의무, 계약, 소송·고소, 손해배상, 노동, 임대차, 상속 등
- 법률 무관 메시지 (False)
  · 인사말, 일상 대화, 날씨, 프로그래밍/기술 문의
  · 법과 전혀 관계없는 일반 상식

[판단 예시]
- "근로기준법 연차휴가 규정 알려줘" → true   (법령 조회가 필요함. 단순 조회여도 true)
- "전세금을 못 받고 있는데 어떻게 하나요?" → true
- "프리랜서 종합소득세 신고 방법" → true   (세법상 의무에 관한 질문)
- "오늘 날씨 어때?" → false
- "파이썬 리스트 정렬하는 법" → false   ('법'자가 들어가도 법률과 무관)

[애매할 때의 원칙]
판단이 애매하면 **true**로 분류하세요. 법령을 찾아보고 답하는 편이,
법률 질문을 일반 대화로 처리해 근거 없이 답하는 것보다 안전합니다.

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
                is_law = looks_legal(user_msg)
    except Exception as e:
        print(f"--- [classify_intent] 오류 발생 ({e}), 키워드 판정으로 대체 ---")
        is_law = looks_legal(user_msg)

    # 결정적 보정: 법령명이나 조문 번호처럼 확실한 신호가 있으면 모델 판단을 뒤집는다.
    # 모델이 "근로기준법 연차휴가 규정 알려줘" 같은 단순 조회를 일반 상식으로 보고 false를 내는 사례가
    # 재현되며, 그 경우 법률 질문이 검색 없이 general_chat으로 빠져 근거 없는 답이 나간다.
    if not is_law and looks_legal(user_msg):
        print("--- [classify_intent] 법령명/조문 신호 감지 → 법률 질문으로 보정 ---")
        is_law = True

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

    model_name = params.get("model")

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

    # 이번 턴의 검색 이력 (재검색 전략 수립 + 실패 시 안내 문구 작성에 사용)
    attempts, any_success = summarize_law_search(state["messages"])

    # 인증 실패는 '검색 결과 없음'과 다르다 — 검색어를 아무리 바꿔도 소용없으므로
    # 재검색을 즉시 중단하고 응답 노드에 '시스템 오류'를 알린다.
    auth_err = find_auth_failure(attempts)
    if auth_err:
        print(f"--- [law_search_agent] 법제처 인증 실패 감지 → 재검색 중단: {auth_err[:80]} ---")
        return {"messages": [AIMessage(content=SEARCH_AUTH_ERROR_MARKER)]}

    # 재검색 검색어를 코드로 만들 때 기준이 되는 원 질문
    user_question = next(
        (m.content for m in reversed(state["messages"])
         if isinstance(m, HumanMessage) and isinstance(m.content, str)),
        "",
    )

    # 최대 5회 시도 초과 또는 LAW_TOOLS가 없으면 더 이상 도구 호출을 수행하지 않고 종료 마커 반환
    if current_search_count >= 5 or not LAW_TOOLS:
        marker = SEARCH_DONE_MARKER if any_success else SEARCH_FAILED_MARKER
        print(f"--- [law_search_agent] 검색 종료 (시도: {current_search_count}/5) → {marker} ---")
        return {"messages": [AIMessage(content=marker)]}

    params = config.get("configurable", {})
    all_counts = state.get("tool_counts", {"law_search_agent": {}})
    current_counts = all_counts.get("law_search_agent", {})

    available_law_tools = []
    for tool in LAW_TOOLS:
        current_usage = current_counts.get(tool.name, 0)
        if current_usage < get_tool_limit(tool.name):
            available_law_tools.append(tool)

    if not available_law_tools:
        marker = SEARCH_DONE_MARKER if any_success else SEARCH_FAILED_MARKER
        print(f"--- [law_search_agent] 이용 가능한 LAW_TOOLS 사용 한도 소진 → {marker} ---")
        return {"messages": [AIMessage(content=marker)]}

    model_name = params.get("model")

    # 실패 횟수에 따라 '다음에 시도할 단계'를 지정해준다.
    # 모델이 매번 비슷한 검색어로 재시도하며 5회를 소진하는 것을 막기 위해,
    # 좁히기 → 법령 용어로 치환 → 상위 법령명 → 도구 교체 순으로 검색 범위를 단계적으로 넓힌다.
    failed_count = sum(1 for a in attempts if not a["ok"])
    ESCALATION = [
        "1단계 (첫 시도): 사용자 질문에서 핵심 법률 쟁점을 뽑아 2~3개 키워드로 검색하세요.",
        "2단계 (좁히기): 직전 검색어에서 수식어·부가 조건을 덜어내고 **핵심 법률 용어 1~2개만** 남겨 검색하세요. "
        "예) '프리랜서 사업소득 종합소득세' → '사업소득' 또는 '종합소득세'",
        "3단계 (유사어 치환): 일상어를 **법령에서 실제로 쓰는 용어**로 바꿔 검색하세요. "
        "예) '프리랜서' → '인적용역' / '사업소득', '알바' → '단시간근로자', '월세' → '차임'",
        "4단계 (상위 법령): 개별 쟁점 대신 그 쟁점을 담고 있는 **법령 이름 자체**로 검색하세요. "
        "예) '소득세법', '근로기준법', '주택임대차보호법'",
        "5단계 (도구 교체): 지금까지와 다른 종류의 도구(판례·행정규칙·조례 등)로 검색 축을 바꾸세요.",
    ]
    next_step = ESCALATION[min(failed_count, len(ESCALATION) - 1)]

    # 실패가 쌓일수록 temperature를 0.2 → 0.5 → 0.8 → 1.0으로 올려
    # 같은 검색어를 반복 생성하는 고착을 깬다 (Gemini 3+는 get_model에서 무시됨)
    temperature = escalated_temperature(failed_count)
    if failed_count > 0:
        print(f"--- [law_search_agent] temperature 에스컬레이션: {temperature} (실패 {failed_count}회) ---")

    search_prompt = f"""
당신은 한국 법률 정보 전용 검색 에이전트입니다.
사용자의 질문 및 대화 맥락에 대해 답변할 수 있는 확실한 법률 정보(법령, 조문, 판례, 행정규칙, 조례 등)를 아래 [korean-law-mcp 법률 도구]를 사용해 **무조건 검색**하세요.

[검색 원칙 및 지침]
1. 사용자 질문에 답변 가능한 확실한 정보가 검색될 때까지 LAW_TOOLS를 사용하여 검색을 진행합니다.
2. 현재까지 진행된 법률 검색 횟수: {current_search_count + 1} / 5회 (최대 5회 시도 가능)
3. 사용자에게 최종 설명을 작성하지 말고, 필수 법률 정보 수집을 위한 **LAW_TOOLS 도구 호출(Tool Call)**을 반드시 생성하세요.
4. 질문에 대한 법률 정보가 충분히 수집되어 더 이상 추가 검색이 필요 없는 경우에만, 도구 호출 없이 "[법률 정보 검색 완료]" 텍스트로만 응답하세요.

[검색 실패 시 대응 — 매우 중요]
- 도구가 '결과 없음(NOT_FOUND)'이나 오류를 돌려주는 것은 **정상적인 상황**입니다. 포기하지 말고 **검색어를 바꿔 반드시 재시도**하세요.
- '[UPSTREAM_NO_DATA]'는 법제처가 자료를 **보내주지 않았다**는 뜻이지 그 법령·판례가 **없다는 뜻이 아닙니다.** 같은 ID로 반복하지 말고 search_law 등으로 식별자(mst/lawId)를 다시 확인하거나 다른 도구로 조회하세요.
- get_law_text의 efYd(시행일자)에는 오늘 날짜를 넣지 마세요. 현행 조문은 efYd 없이 조회합니다.
- 법제처 API는 공백으로 나뉜 키워드를 **AND 조건**으로 처리합니다. 키워드가 많을수록 결과가 급격히 줄어듭니다.
- 도구가 '재시도 제안'을 함께 돌려줬다면 그 제안을 최우선으로 따르세요.
- **이미 시도해서 실패한 검색어를 그대로 반복하지 마세요.** 반드시 다른 검색어를 사용해야 합니다.
- 검색이 실패해도 법령 내용을 추측하거나 지어내지 마세요.

[지금까지 시도한 검색 이력]
{render_search_attempts(attempts)}

[이번에 시도할 단계]
{next_step}

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

    response = await resilient_ainvoke(
        model_name, available_law_tools, final_prompt, temperature=temperature,
    )

    def has_tool_calls(msg) -> bool:
        return bool(msg is not None and getattr(msg, "tool_calls", None))

    # 모델이 첫 실패만 보고 곧바로 포기하는 일이 잦다(도구 호출 대신 사과문을 생성).
    # 프롬프트로 설득하는 방식은 통하지 않는다 — MCP의 오류 메시지 자체가
    # "사용자에게 '검색 실패'를 보고하고"라고 지시하고 있어, 모델이 그쪽을 따르기 때문이다.
    # 그래서 아직 기회가 남아 있고 성공한 검색이 없다면 다음 검색을 코드가 직접 만들어 넣는다.
    if not has_tool_calls(response) and not any_success:
        print(f"--- [law_search_agent] 모델이 재검색 포기 (실패 {failed_count}회) → 코드가 재검색 강제 ---")
        forced = await force_next_search(
            model_name, user_question, attempts, available_law_tools, temperature=temperature,
        )
        if forced is not None:
            response = forced
        else:
            print("--- [재검색 강제] 더 시도할 검색어 후보가 없습니다 ---")

    # 검색 노드는 사용자에게 보일 문장을 남기지 않는다.
    # 이 노드의 산출물이 대화 이력에 섞이면, 다음 응답 노드가 그 문장을 '이미 한 말'로 취급해
    # 답변 첫머리를 통째로 생략해버린다(검색 실패 안내가 사라지는 원인).
    # 따라서 도구 호출이 아니면 내부 표식으로 치환한다.
    if not has_tool_calls(response):
        if response is None or is_empty_answer(response):
            fr = finish_reason_of(response) if response is not None else "no_response"
            print(f"--- [law_search_agent] 응답 비어있음 (finish_reason={fr}) ---")
        marker = SEARCH_DONE_MARKER if any_success else SEARCH_FAILED_MARKER
        response = AIMessage(content=marker)

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

    model_name = params.get("model")

    client_iso = params.get("isotime")
    if client_iso:
        iso_timestamp = client_iso
        unix_timestamp = datetime.fromisoformat(client_iso).timestamp()
    else:
        now_obj = datetime.now().astimezone()
        iso_timestamp = now_obj.isoformat()
        unix_timestamp = now_obj.timestamp()

    # 검색이 한 번도 성공하지 못했는지 확인.
    # 근거가 하나도 없는 상태에서 평소 프롬프트로 답하게 두면 모델이 법령을 지어낼 위험이 크므로,
    # 이 경우에는 '검색 실패를 알리고 다시 물어보도록 안내'하는 전용 프롬프트로 전환한다.
    attempts, any_success = summarize_law_search(state["messages"])
    search_exhausted = bool(attempts) and not any_success

    # 인증 실패는 재질문 안내와 별개다 — 사용자 질문이 아니라 시스템이 문제이므로,
    # 질문을 바꾸라는 안내 대신 '시스템 점검 중' 안내를 LLM 호출 없이 즉시 반환한다.
    # (근거 없이 LLM에게 맡기면 원인을 사용자 탓으로 돌리는 문장이 나올 수 있다)
    if find_auth_failure(attempts):
        print("--- [law_response_agent] 법제처 인증 오류 → 시스템 안내문 반환 (LLM 미호출) ---")
        notice = (
            "죄송합니다. 지금 법령 검색 시스템에 일시적인 문제(법제처 API 인증 오류)가 있어 "
            "법령·판례를 확인할 수 없습니다.\n\n"
            "질문에는 문제가 없으니 잠시 후 같은 질문으로 다시 시도해주세요. "
            "문제가 계속되면 관리자에게 문의해주시기 바랍니다.\n\n"
            f"{LEGAL_DISCLAIMER}"
        )
        return {"messages": [AIMessage(content=notice)]}

    if search_exhausted:
        tried = failed_queries(attempts)
        tried_text = ", ".join(f"'{q}'" for q in tried[:8]) if tried else "(검색어 기록 없음)"
        print(f"--- [law_response_agent] 검색 {len(attempts)}회 모두 실패 → 재질문 안내 모드 ---")

        main_prompt = f"""
[현재 시각]: {iso_timestamp} (Unix: {unix_timestamp})

당신은 한국 법률 상담 어시스턴트입니다.
이번 질문에 대해 법령·판례 검색을 {len(attempts)}회 시도했지만 **관련 자료를 하나도 찾지 못했습니다.**
따라서 지금은 법률적 판단을 내릴 근거가 없습니다.

[시도했지만 결과가 없던 검색어]
{tried_text}

[중요] 직전 assistant 메시지의 "[법률 정보 검색 실패]"는 내부 처리용 표식이며 사용자에게 보인 적이 없습니다.
이미 말한 내용으로 취급해 생략하지 말고, 아래 형식에 따라 답변을 처음부터 온전히 작성하세요.

[답변 형식 — 아래 4단 구성을 반드시 이 순서로 지키세요]
① **첫 문장**은 반드시 다음 문장으로 시작합니다 (변형 금지):
   "요청하신 내용과 관련된 법령·판례를 찾지 못했습니다."
② 이어서 검색이 왜 빗나갔을지 한 문장으로 짚어줍니다 (예: 표현이 법령 용어와 달랐을 수 있음, 쟁점이 여러 개 섞여 있었음 등).
③ **어떻게 다시 질문하면 좋을지** 질문 맥락에 맞게 3~4가지를 불릿으로 제안합니다. 아래를 참고하세요.
   - 알고 있는 법령 이름이나 조문 번호가 있다면 함께 적어달라고 요청
   - 상황을 좁힐 수 있는 사실관계 (누가/언제/어떤 계약·처분인지, 금액, 기간 등)
   - 일상어 대신 법률에서 쓰는 용어로 바꿔 물어보기 (예: '프리랜서' → '사업소득' 또는 '인적용역')
   - 한 번에 여러 쟁점을 묶지 말고 쟁점 하나씩 나눠서 질문하기
④ 사용자가 **그대로 복사해 쓸 수 있는 다시 질문 예시 문장 2개**를 제시합니다.

[반드시 지킬 것]
- **법령 조문, 조/항 번호, 판례를 절대 지어내지 마세요.** 검색으로 확인된 근거가 하나도 없습니다.
  기억에 의존해 "○○법 제○조에 따르면"과 같이 서술하는 것은 금지입니다.
- 사용자가 언급한 법령 이름이 실제로 존재하는 것처럼 설명하지 마세요. 확인되지 않았다고만 하세요.
- 법률 지식 없이도 말할 수 있는 일반적인 절차 안내(관할 기관 문의, 상담 창구 등)는 덧붙여도 되지만,
  그것이 법령 해석이 아니라는 점이 드러나게 쓰세요.
- 사과를 반복하지 말고, 차분하고 실용적인 톤으로 작성하세요.
- 답변 말미에는 반드시 아래 면책 조항을 포함하세요:
  "본 답변은 일반 정보 제공 목적이며, 구체 사건은 변호사 상담이 필요합니다."
"""
    else:
        main_prompt = f"""
[현재 시각]: {iso_timestamp} (Unix: {unix_timestamp})

당신은 한국 법률 상담 어시스턴트입니다.
이전 법률 검색 단계에서 수집된 법령·판례·조례 등 조회 결과를 바탕으로 사용자에게 최선의 명확하고 객관적인 답변을 제공하세요.

[중요] 직전 assistant 메시지의 "[법률 정보 검색 완료]"는 내부 처리용 표식이며 사용자에게 보인 적이 없습니다.
이미 말한 내용으로 취급해 생략하지 말고, 사용자 질문에 대한 답변을 처음부터 온전히 작성하세요.

[답변 작성 원칙]
- 이전 단계에서 실제로 검색/조회된 법령 및 판례 조문만 근거로 사용하여 답변을 작성하세요 (환각 금지).
- 검색에 실패한 항목이 섞여 있다면 그 부분은 답변 근거로 쓰지 말고, 확인되지 않았다고 밝히세요.
- 법률 조문 원문을 장문으로 복사하지 말고, 핵심 요지와 취지를 알기 쉬운 언어로 서술하세요.
- 일반인이 쉽게 이해할 수 있는 언어로 설명하되 조/항 명칭은 정확히 유지하세요.
- 필요시 최신 실시간 정보나 과거 대화 맥락 확인을 위해 이용 가능한 로컬 도구(인터넷 검색, 메모리 도구 등)를 활용할 수 있습니다.
- 답변 말미에는 반드시 아래 면책 조항을 포함하세요:
  "본 답변은 일반 정보 제공 목적이며, 구체 사건은 변호사 상담이 필요합니다."
"""
    # 검색이 전부 실패한 경우에는 도구를 붙이지 않는다.
    # 웹 검색 결과를 법령 근거처럼 인용할 여지를 없애고, 재질문 안내를 한 번에 끝내기 위함.
    tools_for_call = [] if search_exhausted else available_tools

    if tools_for_call:
        main_prompt += "\n[이용 가능한 로컬 도구]\n"
        for tool in tools_for_call:
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

    response = await resilient_ainvoke(model_name, tools_for_call, final_prompt)

    if response is None or is_empty_answer(response):
        fr = finish_reason_of(response) if response is not None else "no_response"
        print(f"--- [law_response_agent] 응답 비어있음 안내 메시지로 대체 (finish_reason={fr}) ---")
        response = AIMessage(content=empty_answer_notice(fr))

    return {"messages": [ensure_disclaimer(response)]}


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
