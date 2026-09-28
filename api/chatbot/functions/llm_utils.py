"""
LLM 호출 회복탄력성 유틸 (일시 장애 재시도 + 빈 응답 폴백).

agent.py(답변 노드)와 summary.py(메모리 노드)가 함께 재사용한다.
두 모듈이 서로 import하면 순환 참조가 되므로, 공용 로직을 여기로 분리했다.
"""
import os
import asyncio

from api.llm_gateway import ModelNotAvailableError, is_transient_error
from api.load_variable import MODEL_MANAGER

# 503(과부하)/429(한도)/500(내부오류)/네트워크·타임아웃 = 일시 오류로 간주해 재시도.
# (400/401 같은 비일시 오류는 재시도해도 소용없으므로 제외한다. 판정은 llm_gateway.errors)
MAX_RETRIES = 3          # 모델당 일시 오류 재시도 횟수(기본)
BACKOFF_BASE = 1.0       # 지수 백오프 기준(초): 1 → 2 → 4

# 빈 응답(RECITATION/SAFETY 등)이나 일시 오류 시 순차로 시도할 폴백 모델 (DB llm_models에 등록·활성이어야 함).
# .env에 CHAT_FALLBACK_MODELS="openai/gpt-6-luna,anthropic/claude-haiku-4-5-20251001" 형태로 지정.
# (해당 provider API 키가 있어야 실제로 폴백됨. 미지정 시 폴백 없이 처리.)
FALLBACK_MODELS = [m.strip() for m in os.getenv("CHAT_FALLBACK_MODELS", "").split(",") if m.strip()]


# 프롬프트가 AI 메시지로 끝나거나 시스템 메시지만 있는 경우의 보정(Gemini 3.x·Anthropic 400 회피)은
# 모든 호출이 지나가는 GatewayChatModel이 처리한다 (api/llm_gateway/chat_model.py).


def finish_reason_of(msg) -> str:
    """AIMessage에서 종료 사유(stop/length/content_filter/RECITATION/SAFETY 등)를 추출."""
    meta = getattr(msg, "response_metadata", {}) or {}
    return meta.get("finish_reason") or meta.get("finishReason") or "unknown"


def is_empty_answer(msg) -> bool:
    """내용도 없고 툴 호출도 없는 '진짜 빈 응답'인지 판별. (툴 호출만 있는 응답은 빈 게 아님)"""
    content = getattr(msg, "content", None)
    has_text = bool(content.strip()) if isinstance(content, str) else bool(content)
    has_tool_calls = bool(getattr(msg, "tool_calls", None))
    return not has_text and not has_tool_calls


def empty_answer_notice(finish_reason: str) -> str:
    """빈 응답일 때 사용자에게 돌려줄 안내 메시지."""
    reason_map = {
        "content_filter": "안전/저작권 필터에 의해 응답이 차단되었습니다.",
        "RECITATION": "법령 원문과 지나치게 유사해 저작권 필터(RECITATION)에 의해 차단되었습니다.",
        "SAFETY": "안전 필터에 의해 응답이 차단되었습니다.",
        "length": "응답 길이 제한으로 답변이 생성되지 못했습니다.",
    }
    detail = reason_map.get(finish_reason, f"모델이 빈 응답을 반환했습니다 (사유: {finish_reason}).")
    return (
        f"죄송합니다. {detail}\n\n"
        "질문을 조금 더 구체적으로 바꾸거나, 잠시 후 다시 시도하시거나, 다른 모델을 선택해 주세요.\n"
        "본 답변은 일반 정보 제공 목적이며, 구체적 사건은 변호사 상담이 필요합니다."
    )


async def resilient_ainvoke(model_name: str, available_tools, prompt,
                            temperature: float = 0.2, max_retries: int = MAX_RETRIES):
    """
    LLM 호출을 회복탄력적으로 수행한다.
      1) 일시 오류(503/429/500/네트워크)는 지수 백오프로 모델당 max_retries회 재시도
      2) 그래도 실패하면 FALLBACK_MODELS의 다음 모델로 전환
      3) 응답이 '빈 응답'(RECITATION/SAFETY 등)이면 다음 폴백 모델로 전환
    정상 응답(또는 툴 호출)이면 즉시 반환. 모든 후보가 빈 응답이면 마지막 빈 응답을,
    모든 후보가 일시 오류면 마지막 예외를 raise한다.

    available_tools=None이면 툴 바인딩 없이 호출한다(메모리 요약 등).
    max_retries=1로 주면 재시도 없이 후보별 1회만 시도(지연 최소화, 메모리 노드용).
    """
    candidates = [model_name] + [m for m in FALLBACK_MODELS if m != model_name]
    last_empty = None
    last_error = None

    for cand in candidates:
        try:
            llm = await MODEL_MANAGER.get_model(cand, temperature)
        except ModelNotAvailableError as e:
            if cand == model_name:
                raise   # 요청한 모델 자체가 미등록·비활성이면 폴백하지 않고 알린다
            print(f"--- [폴백] {e} — 건너뜀 ---")
            continue
        bound = llm.bind_tools(available_tools) if available_tools else llm

        for attempt in range(1, max_retries + 1):
            try:
                response = await bound.ainvoke(prompt)
            except Exception as e:
                if not is_transient_error(e):
                    raise
                last_error = e
                wait = BACKOFF_BASE * (2 ** (attempt - 1))
                print(f"--- [{cand}] 일시 오류 {type(e).__name__} — {attempt}/{max_retries} 재시도 (대기 {wait:.0f}s) ---")
                if attempt < max_retries:
                    await asyncio.sleep(wait)
                    continue
                break  # 이 모델 재시도 소진 → 다음 폴백 모델로
            # --- 응답 수신 성공 ---
            fr = finish_reason_of(response)
            if is_empty_answer(response):
                print(f"--- [{cand}] 빈 응답 (finish_reason={fr}) → 폴백 시도 ---")
                last_empty = response
                break  # 다음 폴백 모델로
            if fr not in ("stop", "STOP", "tool_calls", "length", "end_turn", "tool_use", "unknown"):
                print(f"--- [{cand}] finish_reason={fr} ---")
            return response  # 정상 답변 또는 툴 호출

    if last_empty is not None:
        return last_empty   # 전부 빈 응답 → 상위에서 안내/스킵 처리
    if last_error is not None:
        raise last_error    # 전부 일시 오류 → 상위에서 처리(답변 노드는 500, 메모리 노드는 skip)
    return None
