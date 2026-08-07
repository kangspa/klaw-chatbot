"""
에이전트 1턴 실행 공통 루틴.

/chatbot/chat (새 메시지 전송)과 /chatbot/sessions/.../regenerate (다시 실행)가
동일한 경로로 그래프를 호출하도록 여기로 모았다. 두 경우 모두
configurable.turn_id 를 내려주므로 Qdrant point가 같은 규칙으로 기록/덮어쓰기 된다.
"""

import threading
import traceback
from typing import Any, Optional

import litellm
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult


class LiteLLMCostCallback(BaseCallbackHandler):
    """
    LiteLLM 내장 가격표(litellm.cost_per_token)를 사용해 호출별 비용을 누적.
    OpenAI / Anthropic / Gemini / Ollama / HuggingFace 등 LiteLLM이 지원하는
    모든 모델의 비용을 자동 계산 (Gemini 200K 임계 등 티어 가격도 LiteLLM 측에서 처리).

    각 LLM 호출이 끝날 때 AIMessage.usage_metadata에서 토큰 수,
    response_metadata에서 모델명을 추출해 비용으로 환산.
    """

    def __init__(self, default_model: Optional[str] = None) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.default_model = default_model  # response_metadata에 model이 없을 때 폴백
        self.total_input = 0
        self.total_output = 0
        self.total_cost = 0.0
        self.per_call: list[dict[str, Any]] = []

    @staticmethod
    def _resolve_model(msg: AIMessage, fallback: Optional[str]) -> Optional[str]:
        # ChatLiteLLM은 response_metadata에 'model_name' 또는 'model'을 넣음 (구현/버전 차이)
        meta = msg.response_metadata or {}
        return meta.get("model_name") or meta.get("model") or fallback

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        try:
            generation = response.generations[0][0]
        except IndexError:
            return
        if not isinstance(generation, ChatGeneration):
            return
        msg = generation.message
        if not isinstance(msg, AIMessage) or not msg.usage_metadata:
            return

        input_tokens = msg.usage_metadata.get("input_tokens", 0)
        output_tokens = msg.usage_metadata.get("output_tokens", 0)
        model_name = self._resolve_model(msg, self.default_model) or "unknown"

        cost = 0.0
        try:
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=model_name,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
            )
            cost = float(prompt_cost) + float(completion_cost)
        except Exception as e:
            # 모델이 LiteLLM 가격 DB에 없거나 모델명 형식이 달라 매칭 실패한 경우
            print(f"[cost] '{model_name}' 비용 계산 실패: {e}")

        with self._lock:
            self.total_input += input_tokens
            self.total_output += output_tokens
            self.total_cost += cost
            self.per_call.append({
                "model": model_name,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
            })


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

    cb = LiteLLMCostCallback(default_model=model)
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
