"""
접두사("provider/") → LangChain 채팅 모델 생성기 레지스트리.

  gemini/<모델>       Google Gemini API           GEMINI_API_KEY (또는 GOOGLE_API_KEY)
  openai/<모델>       OpenAI API                  OPENAI_API_KEY
  anthropic/<모델>    Anthropic API               ANTHROPIC_API_KEY
  ollama/<태그>       Ollama (로컬)               OLLAMA_BASE_URL  (기본 http://localhost:11434)
  lmstudio/<모델>     LM Studio (OpenAI 호환)     LMSTUDIO_BASE_URL (기본 http://localhost:1234/v1)
  vllm/<모델>         vLLM (OpenAI 호환)          VLLM_BASE_URL    (기본 http://localhost:8000/v1)
  huggingface/<모델>  HF Inference Providers      HF_TOKEN (또는 HUGGINGFACEHUB_API_TOKEN)

새 제공자는 _BUILDERS에 생성 함수를 하나 추가하면 된다.
각 생성 함수는 DB(llm_models.options)의 추가 인자를 받아, 아래 기본값 위에 덮어써 넘긴다.
"""
import os
from typing import Any, Callable, Optional

from langchain_core.language_models import BaseChatModel

# SDK 자체 재시도는 끈다. 재시도·폴백은 llm_utils.resilient_ainvoke가 로그와 함께 일괄 처리한다.
_SDK_MAX_RETRIES = 0


def _cloud_timeout() -> float:
    """클라우드 API 요청 타임아웃(초). 초과 시 일시 오류로 분류되어 재시도·폴백된다."""
    return float(os.getenv("LLM_REQUEST_TIMEOUT", "300"))

# 환경 변수는 모델 생성 시점에 읽는다 (import 순서와 무관하게 .env 값이 반영되도록).

Builder = Callable[[str, Optional[float], dict[str, Any]], BaseChatModel]


def _build_gemini(name: str, temperature: Optional[float], options: dict[str, Any]) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI
    # ChatGoogleGenerativeAI는 temperature 기본값이 0.7이라, 미지정이면 API 기본값(1.0)을 명시한다.
    return ChatGoogleGenerativeAI(**{
        "model": name,
        "api_key": os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
        "temperature": 1.0 if temperature is None else temperature,
        "max_retries": _SDK_MAX_RETRIES,
        "timeout": _cloud_timeout(),
        **options,
    })


def _build_openai(name: str, temperature: Optional[float], options: dict[str, Any]) -> BaseChatModel:
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(**{"model": name, "temperature": temperature, "max_retries": _SDK_MAX_RETRIES,
                         "timeout": _cloud_timeout(), **options})


def _build_anthropic(name: str, temperature: Optional[float], options: dict[str, Any]) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(**{"model": name, "temperature": temperature, "max_retries": _SDK_MAX_RETRIES,
                            "timeout": _cloud_timeout(), **options})


def _build_ollama(name: str, temperature: Optional[float], options: dict[str, Any]) -> BaseChatModel:
    from langchain_ollama import ChatOllama
    # OLLAMA_API_KEY는 Ollama Cloud 등 인증이 필요한 원격 서버를 쓸 때만 의미가 있다.
    api_key = os.getenv("OLLAMA_API_KEY")
    return ChatOllama(**{
        "model": name,
        "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        "temperature": temperature,
        # 로컬 모델 기본 컨텍스트(수천 토큰)는 법령 도구 결과를 담기에 부족하므로 늘려서 로드한다.
        # 모델별로 다르게 하려면 DB options에 num_ctx를 넣는다.
        "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "32768")),
        "client_kwargs": {"headers": {"Authorization": f"Bearer {api_key}"}} if api_key else {},
        **options,
    })


def _openai_compatible(url_env: str, default_url: str, key_envs: tuple[str, ...],
                       default_key: str) -> Builder:
    def build(name: str, temperature: Optional[float], options: dict[str, Any]) -> BaseChatModel:
        from langchain_openai import ChatOpenAI
        api_key = next((os.getenv(k) for k in key_envs if os.getenv(k)), default_key)
        return ChatOpenAI(**{"model": name, "base_url": os.getenv(url_env, default_url),
                             "api_key": api_key, "temperature": temperature,
                             "max_retries": _SDK_MAX_RETRIES, **options})
    return build


_BUILDERS: dict[str, Builder] = {
    "gemini": _build_gemini,
    "openai": _build_openai,
    "anthropic": _build_anthropic,
    "ollama": _build_ollama,
    "ollama_chat": _build_ollama,  # 'ollama_chat/' 표기도 허용 (ollama/와 동일)
    # 로컬 서버는 키를 검사하지 않지만 OpenAI SDK가 값을 요구하므로 더미 키를 넣는다.
    "lmstudio": _openai_compatible("LMSTUDIO_BASE_URL", "http://localhost:1234/v1",
                                   ("LMSTUDIO_API_KEY",), "lm-studio"),
    "vllm": _openai_compatible("VLLM_BASE_URL", "http://localhost:8000/v1",
                               ("VLLM_API_KEY",), "EMPTY"),
    "huggingface": _openai_compatible("HF_BASE_URL", "https://router.huggingface.co/v1",
                                      ("HF_TOKEN", "HUGGINGFACEHUB_API_TOKEN"), ""),
}


def split_model_id(model_id: str) -> tuple[str, str]:
    """'anthropic/claude-sonnet-5' → ('anthropic', 'claude-sonnet-5'). 모델명 안의 '/'는 보존한다."""
    provider, sep, name = (model_id or "").partition("/")
    if not sep or not name:
        raise ValueError(f"모델 ID는 '접두사/모델명' 형식이어야 합니다: {model_id!r}")
    if provider not in _BUILDERS:
        raise ValueError(f"지원하지 않는 접두사 '{provider}/' (지원: {', '.join(sorted(_BUILDERS))})")
    return provider, name


def build_provider_model(model_id: str, temperature: Optional[float],
                         options: Optional[dict[str, Any]] = None) -> BaseChatModel:
    provider, name = split_model_id(model_id)
    return _BUILDERS[provider](name, temperature, dict(options or {}))
