"""
모델 ID('접두사/모델명') → LiteLLM 채팅 모델(ChatLiteLLM).

호출과 토큰 단가 계산은 LiteLLM에 맡긴다. 우리 쪽 접두사를 LiteLLM 표기로 바꾸고,
로컬 런타임의 주소·옵션만 채워 넘긴다.

  우리 접두사        LiteLLM 표기       대상 / 환경 변수
  gemini/<모델>      gemini/            Google Gemini API      GEMINI_API_KEY
  openai/<모델>      openai/            OpenAI API             OPENAI_API_KEY
  anthropic/<모델>   anthropic/         Anthropic API          ANTHROPIC_API_KEY
  ollama/<태그>      ollama_chat/       Ollama (도구 호출 지원 chat API)   OLLAMA_BASE_URL (기본 http://localhost:11434)
  lmstudio/<모델>    lm_studio/         LM Studio              LMSTUDIO_BASE_URL (기본 http://localhost:1234/v1)
  vllm/<모델>        hosted_vllm/       vLLM                   VLLM_BASE_URL (기본 http://localhost:8000/v1)
  huggingface/<모델> huggingface/       HF Inference Providers HF_TOKEN (또는 HUGGINGFACEHUB_API_TOKEN)

DB(llm_models.options)의 추가 인자는 LiteLLM 호출 인자로 그대로 넘긴다 (예: Ollama num_ctx).
"""
import os
from typing import Any, Optional

from langchain_litellm import ChatLiteLLM

# LiteLLM 자체 재시도는 끈다. 재시도·폴백은 llm_utils.resilient_ainvoke가 로그와 함께 일괄 처리한다.
_SDK_MAX_RETRIES = 0

# (LiteLLM 접두사, base URL 환경 변수, 기본 URL, API 키 환경 변수들, 기본 키)
# 환경 변수는 모델 생성 시점에 읽는다 (import 순서와 무관하게 .env 값이 반영되도록).
_PROVIDERS: dict[str, tuple[str, Optional[str], Optional[str], tuple[str, ...], Optional[str]]] = {
    "gemini": ("gemini", None, None, (), None),
    "openai": ("openai", None, None, (), None),
    "anthropic": ("anthropic", None, None, (), None),
    "ollama": ("ollama_chat", "OLLAMA_BASE_URL", "http://localhost:11434", ("OLLAMA_API_KEY",), None),
    "ollama_chat": ("ollama_chat", "OLLAMA_BASE_URL", "http://localhost:11434", ("OLLAMA_API_KEY",), None),
    # 로컬 서버는 키를 검사하지 않지만 OpenAI 호환 클라이언트가 값을 요구하므로 더미 키를 넣는다.
    "lmstudio": ("lm_studio", "LMSTUDIO_BASE_URL", "http://localhost:1234/v1", ("LMSTUDIO_API_KEY",), "lm-studio"),
    "vllm": ("hosted_vllm", "VLLM_BASE_URL", "http://localhost:8000/v1", ("VLLM_API_KEY",), "EMPTY"),
    "huggingface": ("huggingface", None, None, ("HF_TOKEN", "HUGGINGFACEHUB_API_TOKEN"), None),
}

# 로컬 런타임 (요청 타임아웃을 두지 않고, 비용은 0으로 본다)
LOCAL_PROVIDERS = {"ollama", "ollama_chat", "lmstudio", "vllm"}


def _cloud_timeout() -> float:
    """클라우드 API 요청 타임아웃(초). 초과 시 일시 오류로 분류되어 재시도·폴백된다."""
    return float(os.getenv("LLM_REQUEST_TIMEOUT", "300"))


def split_model_id(model_id: str) -> tuple[str, str]:
    """'anthropic/claude-sonnet-5' → ('anthropic', 'claude-sonnet-5'). 모델명 안의 '/'는 보존한다."""
    provider, sep, name = (model_id or "").partition("/")
    if not sep or not name:
        raise ValueError(f"모델 ID는 '접두사/모델명' 형식이어야 합니다: {model_id!r}")
    if provider not in _PROVIDERS:
        raise ValueError(f"지원하지 않는 접두사 '{provider}/' (지원: {', '.join(sorted(_PROVIDERS))})")
    return provider, name


def litellm_model_name(model_id: str) -> str:
    """우리 모델 ID → LiteLLM 모델 문자열 (예: 'ollama/qwen3.8:27b' → 'ollama_chat/qwen3.8:27b')."""
    provider, name = split_model_id(model_id)
    return f"{_PROVIDERS[provider][0]}/{name}"


def build_provider_model(model_id: str, temperature: Optional[float],
                         options: Optional[dict[str, Any]] = None) -> ChatLiteLLM:
    provider, _ = split_model_id(model_id)
    _, url_env, default_url, key_envs, default_key = _PROVIDERS[provider]
    options = dict(options or {})
    if provider in ("ollama", "ollama_chat"):
        # 로컬 모델 기본 컨텍스트(수천 토큰)는 법령 도구 결과를 담기에 부족하므로 늘려서 로드한다.
        options.setdefault("num_ctx", int(os.getenv("OLLAMA_NUM_CTX", "32768")))
    # DB options는 ChatLiteLLM 필드면 필드로, 아니면 LiteLLM 호출 인자(model_kwargs)로 넘긴다
    fields = {k: v for k, v in options.items() if k in ChatLiteLLM.model_fields}
    kwargs: dict[str, Any] = {
        "model": litellm_model_name(model_id),
        "max_retries": _SDK_MAX_RETRIES,
        "model_kwargs": {k: v for k, v in options.items() if k not in fields},
        **fields,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if provider not in LOCAL_PROVIDERS:
        kwargs["request_timeout"] = _cloud_timeout()
    if url_env:
        kwargs["api_base"] = os.getenv(url_env, default_url)
    api_key = next((os.getenv(k) for k in key_envs if os.getenv(k)), default_key)
    if api_key:
        kwargs["api_key"] = api_key
    return ChatLiteLLM(**kwargs)
