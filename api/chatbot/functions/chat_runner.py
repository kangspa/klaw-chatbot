"""
에이전트 1턴 실행 공통 루틴.

/chatbot/chat (새 메시지 전송)과 /chatbot/sessions/.../regenerate (다시 실행)가
동일한 경로로 그래프를 호출하도록 여기로 모았다. 두 경우 모두
configurable.turn_id 를 내려주므로 Qdrant point가 같은 규칙으로 기록/덮어쓰기 된다.
"""

import traceback
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage

from api.llm_gateway import CostTrackingCallback
from api.load_variable import MODEL_REGISTRY


async def resolve_model(model: Optional[str]) -> str:
    """
    API로 받은 모델 ID를 DB 등록 모델로 확인한다. 비어 있으면 기본 모델(is_default).
    미등록·비활성 모델이면 ModelNotAvailableError (엔드포인트에서 400으로 변환).
    """
    return (await MODEL_REGISTRY.resolve(model)).id


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
      Postgres의 chat_messages.graph_message_id 와 state 상의 메시지를 1:1로 묶어둘 수 있다.
      → 나중에 삭제/재생성 시 state에서 정확히 어느 지점부터 잘라낼지 알 수 있다.
    """
    config = {
        "configurable": {
            "user_id": user_id,
            "thread_id": session_id,
            "isotime": isotime,
            "model": model,
            # save_conversation이 Qdrant point id로 사용 (= Postgres chat_messages.turn_id)
            "turn_id": turn_id,
        }
    }

    input_message = HumanMessage(content=message, id=user_graph_message_id)

    cb = CostTrackingCallback(MODEL_REGISTRY)
    result = await agent.ainvoke(
        {"messages": [input_message]},
        config={**config, "callbacks": [cb]},
    )
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
    print(traceback.format_exc())
    return f"[{err_type}] {err_repr}"
