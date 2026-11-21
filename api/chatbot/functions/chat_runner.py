"""
에이전트 1턴 실행 공통 루틴.

/chatbot/chat (새 메시지 전송)과 /chatbot/sessions/.../regenerate (다시 실행)가
동일한 경로로 그래프를 호출하도록 여기로 모았다. 두 경우 모두
configurable.turn_id 를 내려주므로 Qdrant point가 같은 규칙으로 기록/덮어쓰기 된다.
"""

import asyncio
import traceback
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage

from api.chatbot.functions.insights import LawLookupCallback, record_turn
from api.llm_gateway import CostTrackingCallback, ModelNotAvailableError
from api.llm_gateway.local_models import PULL_MANAGER, installed_ollama_models, is_installed, ollama_name
from api.load_variable import MODEL_REGISTRY


async def resolve_model(model: Optional[str]) -> str:
    """
    API로 받은 모델 ID를 DB 등록 모델로 확인한다. 비어 있으면 기본 모델(is_default).
    미등록·비활성 모델이면 ModelNotAvailableError (엔드포인트에서 400으로 변환).

    마이 페이지의 '사용 안 함' 설정으로는 막지 않는다. 다만 로컬 모델이 아직 없으면
    그래프 깊숙한 곳에서 500으로 터지는 대신 어떻게 해야 하는지 알려주는 오류로 바꾼다.
    """
    spec = await MODEL_REGISTRY.resolve(model)
    from api.chatbot.keys import missing_key_for
    key = missing_key_for(spec.id)
    if key and not model:
        # 모델을 지정하지 않았는데 기본 모델의 키가 없으면, 받아 둔 로컬 모델이 있을 때 그걸 쓴다
        installed = await installed_ollama_models() or set()
        local = next((s for s in await MODEL_REGISTRY.list_enabled()
                      if ollama_name(s.id) and is_installed(ollama_name(s.id), installed)), None)
        if local is not None:
            print(f"[chat] 기본 모델 {spec.id}의 API 키({key})가 없어 받아 둔 로컬 모델 {local.id}로 답합니다")
            spec, key = local, None
    if key:
        raise ModelNotAvailableError(
            f"'{spec.label}'을(를) 쓰려면 {key} API 키가 필요합니다. 마이 페이지 > API 키에서 넣거나, "
            "모델 선택에서 받아 둔 로컬 모델(Ollama)을 고르세요."
        )
    name = ollama_name(spec.id)
    if name is not None:
        from api.llm_gateway.ollama_upgrade import UPGRADER
        if UPGRADER.running:
            raise ModelNotAvailableError(
                f"로컬 모델 서버(Ollama)를 {UPGRADER.job.target} 버전으로 바꾸는 중입니다. 완료된 뒤 다시 시도하세요."
            )
        pull = PULL_MANAGER.get(spec.id)
        if pull is not None and pull.status == "pulling":
            pct = pull.to_dict()["percent"]
            raise ModelNotAvailableError(
                f"로컬 모델 '{spec.label}'을 다운로드하는 중입니다"
                f"{f' ({pct:.0f}%)' if pct is not None else ''}. 완료된 뒤 다시 시도하세요."
            )
        installed = await installed_ollama_models()
        if installed is None:
            from api import services
            if await asyncio.to_thread(services.ensure_ollama):
                installed = await installed_ollama_models()
            if installed is None:
                raise ModelNotAvailableError(services.ollama_unavailable_message())
        if not is_installed(name, installed):
            raise ModelNotAvailableError(
                f"로컬 모델 '{spec.label}'이 아직 다운로드되지 않았습니다. 마이 페이지 > 모델 설정에서 다운로드하세요."
            )
    return spec.id


async def run_agent_turn(
    agent,
    *,
    user_id: str,
    session_id: str,
    isotime: Optional[str],
    message: str,
    model: Optional[str],
    turn_id: str,
    user_graph_message_id: str,
) -> tuple[str, Optional[str]]:
    """
    사용자 메시지 1건으로 그래프를 1턴 실행하고 (AI 응답 텍스트, AI 메시지의 state id)를 반환한다.

    user_graph_message_id: HumanMessage에 직접 부여할 id.
      LangGraph의 add_messages 리듀서는 id가 이미 있으면 그대로 유지하므로,
      DB의 chat_messages.graph_message_id 와 state 상의 메시지를 1:1로 묶어둘 수 있다.
      → 나중에 삭제/재생성 시 state에서 정확히 어느 지점부터 잘라낼지 알 수 있다.
    """
    config = {
        "configurable": {
            "user_id": user_id,
            "thread_id": session_id,
            "isotime": isotime,
            "model": model,
            # save_conversation이 Qdrant point id로 사용 (= DB chat_messages.turn_id)
            "turn_id": turn_id,
        }
    }

    input_message = HumanMessage(content=message, id=user_graph_message_id)

    cb = CostTrackingCallback(MODEL_REGISTRY)
    law_cb = LawLookupCallback()
    result = None
    try:
        result = await agent.ainvoke(
            {"messages": [input_message]},
            config={**config, "callbacks": [cb, law_cb]},
        )
    finally:
        # 대시보드 기록 — 답변이 실패해도 이미 발생한 비용은 남긴다
        await record_turn(user_id=user_id, session_id=session_id, turn_id=turn_id,
                          cost_cb=cb, law_cb=law_cb, state=result)
        # SQLite 체크포인터는 단계마다 상태 전체를 쌓으므로 최근 몇 개만 남긴다
        from api.chatbot.functions.graph_ops import prune_thread
        await prune_thread(agent, session_id)
    print(f"Per-call breakdown: {cb.per_call}")
    print(f"Total Input: {cb.total_input} / Output: {cb.total_output}")
    print(f"Estimated cost: ${cb.total_cost:.6f}")

    # 결과에서 마지막 AI 메시지(실제 답변 내용이 있는 것) 추출
    answer = ""
    ai_graph_id: Optional[str] = None
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content:
            answer = msg.content
            ai_graph_id = getattr(msg, "id", None)
            break

    return answer, ai_graph_id


def describe_exception(e: Exception) -> str:
    """일부 MCP/RPC 예외는 str(e)가 비어 원인 추적이 불가능하므로 타입 + traceback을 함께 남긴다."""
    err_type = type(e).__name__
    err_repr = repr(e)
    print(f"Chat Error [{err_type}]: {err_repr}")
    print(traceback.format_exc())   # 자세한 내용은 로그에만
    # 화면에는 짧게: LiteLLM 오류 문자열에 섞여 오는 traceback·버그 신고 링크는 뺀다
    msg = str(e) or err_repr
    for cut in ("\nTraceback (most recent call last)", "Traceback (most recent call last)", "This looks like a bug in LiteLLM"):
        msg = msg.split(cut, 1)[0]
    msg = msg.strip()
    low = msg.lower()
    if "api key" in low and ("missing" in low or "invalid" in low or "incorrect" in low):
        return f"[{err_type}] 모델 API 키가 없거나 올바르지 않습니다. 마이 페이지 > API 키를 확인하거나 다른 모델을 고르세요. ({msg[:160]})"
    return f"[{err_type}] {msg[:400]}"


_PROVIDER_NAMES = {"gemini": "Google Gemini", "openai": "OpenAI", "anthropic": "Anthropic"}


def friendly_error(e: Exception, model_id: str = "") -> tuple[str, str]:
    """
    답변 실패를 화면에 AI 말풍선처럼 보여 줄 안내문으로 바꾼다. (kind, 문장)
      auth  : API 키가 틀렸거나 만료·권한 없음 (401/403)
      quota : 사용 한도·요금 부족 (429, 결제)
      other : 그 밖의 오류 (짧게 줄인 원문)
    """
    name = type(e).__name__
    raw = str(e) or repr(e)
    low = (name + " " + raw).lower()
    provider = _PROVIDER_NAMES.get(model_id.split("/", 1)[0], "")
    who = f"{provider} " if provider else ""
    if ("authenticationerror" in low or "permissiondenied" in low or " 401" in low or "invalid api key" in low
            or "incorrect api key" in low or "api key not valid" in low or "api_key_invalid" in low
            or "invalid x-api-key" in low):
        return "auth", (f"등록된 {who}API 키가 올바르지 않거나 만료되어 답변하지 못했어요.\n\n"
                        "**마이 페이지 > API 키**에서 키를 다시 확인하고 '확인' 버튼으로 바로 검사해 보세요. "
                        "모델 선택에서 다른 모델(예: 받아 둔 로컬 모델)로 바꿔 보내도 돼요.")
    if "missing" in low and "api key" in low:
        return "auth", (f"{who}API 키가 등록되어 있지 않아 답변하지 못했어요. **마이 페이지 > API 키**에서 키를 넣거나, "
                        "받아 둔 로컬 모델(Ollama)을 골라 보내 주세요.")
    if "ratelimit" in low or " 429" in low or "quota" in low or "insufficient_quota" in low or "credit" in low:
        return "quota", (f"{who}API 사용 한도를 넘었거나 크레딧이 부족해 답변하지 못했어요. 잠시 뒤 다시 보내거나, "
                         "해당 회사 콘솔에서 사용량·결제 상태를 확인해 주세요. 다른 모델로 바꿔 보내도 돼요.")
    msg = raw
    for cut in ("\nTraceback (most recent call last)", "Traceback (most recent call last)", "This looks like a bug in LiteLLM"):
        msg = msg.split(cut, 1)[0]
    return "other", f"답변 중 오류가 발생했어요: [{name}] {msg.strip()[:300]}"
