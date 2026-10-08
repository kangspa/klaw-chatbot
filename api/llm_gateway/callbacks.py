"""그래프 1턴 동안의 LLM 호출 토큰·비용을 누적하는 LangChain 콜백."""
import threading
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from api.llm_gateway.pricing import compute_cost
from api.llm_gateway.registry import ModelRegistry  # noqa: F401  (호출부 호환용 인자 타입)


class CostTrackingCallback(BaseCallbackHandler):
    """
    게이트웨이 모델은 metadata에 gateway_model(모델 ID)을 싣고 있어 on_chat_model_start에서
    호출(run_id)별 모델을 기억해 두고, on_llm_end에서 usage_metadata(입력·출력·캐시 토큰)에
    LiteLLM 가격표의 단가를 적용해 비용을 누적한다 (pricing.compute_cost).
    gateway_model이 없는 호출(게이트웨이를 거치지 않은 모델)은 집계하지 않는다.
    """

    def __init__(self, registry: ModelRegistry) -> None:
        super().__init__()
        self.registry = registry
        self._lock = threading.Lock()
        self._run_models: dict[UUID, str] = {}
        self.total_input = 0
        self.total_output = 0
        self.total_cost = 0.0
        self.per_call: list[dict[str, Any]] = []

    def on_chat_model_start(self, serialized: Any, messages: Any, *, run_id: UUID,
                            metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
        model_id = (metadata or {}).get("gateway_model")
        if model_id:
            with self._lock:
                self._run_models[run_id] = model_id

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        with self._lock:
            self._run_models.pop(run_id, None)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        with self._lock:
            model_id = self._run_models.pop(run_id, None)
        if not model_id:
            return
        try:
            generation = response.generations[0][0]
        except IndexError:
            return
        if not isinstance(generation, ChatGeneration):
            return
        msg = generation.message
        if not isinstance(msg, AIMessage) or not msg.usage_metadata:
            return

        usage = msg.usage_metadata
        cost = compute_cost(model_id, usage)
        if cost is None:
            print(f"[cost] '{model_id}'이(가) LiteLLM 가격표에 없음 — 비용 0으로 집계")
            cost = 0.0

        details = usage.get("input_token_details") or {}
        with self._lock:
            self.total_input += usage.get("input_tokens", 0)
            self.total_output += usage.get("output_tokens", 0)
            self.total_cost += cost
            self.per_call.append({
                "model": model_id,
                "input_tokens": usage.get("input_tokens", 0),
                "cache_read_tokens": details.get("cache_read", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cost_usd": cost,
            })
