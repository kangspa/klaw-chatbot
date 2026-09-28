"""
호출 1건의 토큰 비용 계산.

단가는 DB(llm_model_prices)의 현재 가격을 registry.ModelSpec.prices로 전달받는다 (USD / 1M 토큰).
가격 변경은 사람이 DB를 직접 고쳐 반영한다 — 날짜 기준 자동 전환은 하지 않는다.
한 모델의 단가 행 중 입력 토큰 수가 above_input_tokens를 '초과'하는 가장 높은 구간을 적용한다
(대부분의 모델은 기본 구간 1행뿐이다).
"""
from dataclasses import dataclass
from typing import Optional, Sequence

# 자체 서버에서 돌리는 로컬 런타임은 토큰 과금이 없다 (단가 행이 없어도 0원).
FREE_PROVIDERS = {"ollama", "ollama_chat", "lmstudio", "vllm"}


@dataclass(frozen=True)
class PriceRow:
    """USD / 1M 토큰. cache_read/cache_write가 None이면 일반 입력 단가로 계산한다."""
    input: float
    output: float
    cache_read: Optional[float] = None
    cache_write: Optional[float] = None
    above_input_tokens: int = 0


def select_rate(prices: Sequence[PriceRow], input_tokens: int) -> Optional[PriceRow]:
    tiers = [p for p in prices if p.above_input_tokens == 0 or input_tokens > p.above_input_tokens]
    return max(tiers, key=lambda p: p.above_input_tokens) if tiers else None


def compute_cost(model_id: str, prices: Sequence[PriceRow], usage: dict) -> Optional[float]:
    """
    LangChain 표준 usage_metadata로 비용(USD)을 계산한다.
    적용할 단가가 없는 유료 모델이면 None을 반환한다(호출부에서 경고 후 0으로 집계).

    usage_metadata 규약: input_tokens는 캐시 토큰을 포함한 전체 입력,
    output_tokens는 추론(thinking) 토큰을 포함한 전체 출력.
    """
    input_tokens = usage.get("input_tokens", 0) or 0
    rate = select_rate(prices, input_tokens)
    if rate is None:
        return 0.0 if model_id.split("/", 1)[0] in FREE_PROVIDERS else None

    output_tokens = usage.get("output_tokens", 0) or 0
    details = usage.get("input_token_details") or {}
    cache_read = details.get("cache_read", 0) or 0
    cache_write = details.get("cache_creation", 0) or 0
    uncached = max(input_tokens - cache_read - cache_write, 0)

    cr = rate.cache_read if rate.cache_read is not None else rate.input
    cw = rate.cache_write if rate.cache_write is not None else rate.input
    total = (uncached * rate.input + cache_read * cr + cache_write * cw
             + output_tokens * rate.output)
    return total / 1_000_000
