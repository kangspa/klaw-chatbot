"""
LLM 게이트웨이: '접두사/모델명' 한 줄로 각 제공자의 모델을 LangChain 채팅 모델로 호출한다.

모델 호출과 토큰 단가 계산은 LiteLLM이 맡고, 호출 가능한 모델 목록은 Postgres(llm_models)에서 관리한다.
  - ModelRegistry : DB 목록을 캐시하고 ttl마다 다시 읽는다 → 서버 재시작 없이 모델·단가 변경 반영
  - ModelManager  : 요청 모델 ID를 레지스트리로 확인한 뒤 GatewayChatModel을 만들어 캐시
  - CostTrackingCallback : 레지스트리의 단가로 호출별 비용 집계

    llm = await MODEL_MANAGER.get_model("anthropic/claude-sonnet-5")
    await llm.bind_tools(tools).ainvoke(messages, {"callbacks": [CostTrackingCallback(registry)]})

지원 접두사와 환경 변수는 providers.py, DB 초기 데이터는 seed_models.json 참고.
"""
from typing import Any, Optional

from cachetools import TTLCache

from api.llm_gateway.callbacks import CostTrackingCallback
from api.llm_gateway.chat_model import GatewayChatModel, TRAILING_USER_TURN
from api.llm_gateway.embeddings import LiteLLMEmbeddings
from api.llm_gateway.errors import is_transient_error
from api.llm_gateway.pricing import compute_cost
from api.llm_gateway.providers import build_provider_model, split_model_id
from api.llm_gateway.registry import ModelNotAvailableError, ModelRegistry, ModelSpec

__all__ = [
    "get_chat_model", "ModelManager", "ModelRegistry", "ModelSpec", "ModelNotAvailableError",
    "GatewayChatModel", "CostTrackingCallback", "LiteLLMEmbeddings",
    "is_transient_error", "compute_cost", "split_model_id", "TRAILING_USER_TURN",
]


def get_chat_model(model_id: str, temperature: Optional[float] = None,
                   options: Optional[dict[str, Any]] = None) -> GatewayChatModel:
    """레지스트리를 거치지 않고 모델을 바로 만든다 (스크립트·테스트용). 앱에서는 ModelManager를 쓴다."""
    return GatewayChatModel(model_id=model_id, base=build_provider_model(model_id, temperature, options),
                            metadata={"gateway_model": model_id})


class ModelManager:
    """
    요청 모델을 레지스트리로 확인하고, 인스턴스를 (모델 ID, temperature, 설정 지문) 단위로 캐시한다.
    DB에서 options·fixed_temperature를 바꾸면 설정 지문이 달라져 새 인스턴스가 만들어진다.
    """

    def __init__(self, registry: ModelRegistry, ttl: int = 3600, maxsize: int = 20):
        self.registry = registry
        self.cache = TTLCache(maxsize=maxsize, ttl=ttl)

    async def get_model(self, model_id: Optional[str], temperature: float = 0.2) -> GatewayChatModel:
        """model_id가 None이면 기본 모델. 미등록·비활성 모델이면 ModelNotAvailableError."""
        spec = await self.registry.resolve(model_id)
        # temperature=1만 허용하는 모델은 요청 temperature를 무시하고 보내지 않는다.
        effective = None if spec.fixed_temperature else temperature
        cache_key = (spec.id, effective, spec.build_version)
        if cache_key not in self.cache:
            label = "temperature 미지정" if effective is None else f"temperature={effective}"
            print(f"--- [Memory] New model instance: {spec.id} ({label}) ---")
            self.cache[cache_key] = get_chat_model(spec.id, effective, spec.options)
        return self.cache[cache_key]
