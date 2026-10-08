"""
호출 1건의 토큰 비용 계산 — LiteLLM 가격표 사용.

LiteLLM은 서버 시작 시 GitHub의 최신 가격표(model_prices_and_context_window.json)를 받아 오고,
받지 못하면 설치된 버전에 내장된 가격표를 쓴다. 그래서 새 모델·가격 변경을 직접 관리하지 않아도 된다.
캐시 토큰 할인과 장문 입력 구간 단가(예: Gemini 3.1 Pro >200K, GPT-6 >272K)도 LiteLLM이 계산한다.
"""
from typing import Optional

import litellm

from api.llm_gateway.providers import LOCAL_PROVIDERS, litellm_model_name, split_model_id


def compute_cost(model_id: str, usage: dict) -> Optional[float]:
    """
    LangChain 표준 usage_metadata로 비용(USD)을 계산한다.
    로컬 런타임은 0원, LiteLLM 가격표에 없는 유료 모델은 None (호출부에서 경고 후 0으로 집계).

    usage_metadata 규약: input_tokens는 캐시 토큰을 포함한 전체 입력,
    output_tokens는 추론(thinking) 토큰을 포함한 전체 출력 (LiteLLM의 prompt/completion_tokens와 같은 의미).
    """
    if split_model_id(model_id)[0] in LOCAL_PROVIDERS:
        return 0.0
    details = usage.get("input_token_details") or {}
    try:
        prompt_cost, completion_cost = litellm.cost_per_token(
            model=litellm_model_name(model_id),
            prompt_tokens=usage.get("input_tokens", 0) or 0,
            completion_tokens=usage.get("output_tokens", 0) or 0,
            cache_read_input_tokens=details.get("cache_read", 0) or 0,
            cache_creation_input_tokens=details.get("cache_creation", 0) or 0,
        )
    except Exception:
        return None
    return float(prompt_cost) + float(completion_cost)
