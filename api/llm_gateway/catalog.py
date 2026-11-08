"""
API 모델 카탈로그 — 사용자가 마이 페이지에서 고를 수 있는 Google·OpenAI·Anthropic 모델 목록.

기준은 '지금 설치된 LiteLLM 버전이 아는 모델'이다. 그래서 LiteLLM에 내장된 가격표
(model_prices_and_context_window_backup.json)를 읽는다. 서버가 시작할 때 받아 오는 원격 가격표는
설치된 버전보다 새 모델을 담을 수 있어 쓰지 않는다 — LiteLLM 버전을 올리면 고를 수 있는 모델도 늘어난다.

걸러내는 기준:
  - mode가 chat인 모델만 (이미지·음성·임베딩 등 제외)
  - 종료일(deprecation_date)이 지난 모델 제외, fine-tune(ft:) 제외
  - 도구 호출(supports_function_calling)을 지원하는 모델만 — 이 챗봇은 법령·웹 검색을 도구로 한다
  - 텍스트를 출력하지 않는 모델 제외 (supported_output_modalities에 text가 없으면)
"""
import datetime
import importlib.metadata as md
import importlib.resources
import json
import os
import re
from typing import Optional

# 우리 접두사 → (LiteLLM 가격표의 litellm_provider, 선택 목록 그룹, API 키 환경 변수)
API_PROVIDERS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "gemini": ("gemini", "Google", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    "openai": ("openai", "OpenAI", ("OPENAI_API_KEY",)),
    "anthropic": ("anthropic", "Anthropic", ("ANTHROPIC_API_KEY",)),
}

_cache: dict = {"version": None, "data": None}


def _bundled_price_map() -> dict:
    """설치된 LiteLLM 버전에 내장된 가격표 (버전이 바뀌면 다시 읽는다)."""
    version = md.version("litellm")
    if _cache["version"] != version:
        path = importlib.resources.files("litellm") / "model_prices_and_context_window_backup.json"
        _cache.update(version=version, data=json.loads(path.read_text(encoding="utf-8")))
    return _cache["data"]


def _per_million(v) -> Optional[float]:
    return round(float(v) * 1_000_000, 4) if isinstance(v, (int, float)) else None


def catalog(prefix: str) -> list[dict]:
    """
    한 제공사의 선택 가능한 모델 목록 (이름순).
    반환 항목: {id('접두사/모델명'), name, inputPer1M, outputPer1M, contextTokens, deprecationDate,
               supportsVision, supportsReasoning}
    """
    litellm_provider, _, _ = API_PROVIDERS[prefix]
    today = datetime.date.today().isoformat()
    out: dict[str, dict] = {}
    for key, info in _bundled_price_map().items():
        if not isinstance(info, dict) or info.get("litellm_provider") != litellm_provider:
            continue
        if info.get("mode") != "chat" or not info.get("supports_function_calling"):
            continue
        outputs = info.get("supported_output_modalities")
        if isinstance(outputs, list) and "text" not in outputs:
            continue
        deprecation = info.get("deprecation_date")
        if deprecation and str(deprecation) < today:
            continue
        name = key.split("/", 1)[1] if key.startswith(f"{litellm_provider}/") else key
        if name.startswith("ft:") or "/" in name:
            continue
        model_id = f"{prefix}/{name}"
        out[model_id] = {
            "id": model_id,
            "name": name,
            "inputPer1M": _per_million(info.get("input_cost_per_token")),
            "outputPer1M": _per_million(info.get("output_cost_per_token")),
            "contextTokens": info.get("max_input_tokens"),
            "deprecationDate": str(deprecation) if deprecation else None,
            "supportsVision": bool(info.get("supports_vision")),
            "supportsReasoning": bool(info.get("supports_reasoning")),
        }
    return sorted(out.values(), key=lambda m: m["name"])


def find(model_id: str) -> Optional[dict]:
    prefix = model_id.split("/", 1)[0]
    if prefix not in API_PROVIDERS:
        return None
    return next((m for m in catalog(prefix) if m["id"] == model_id), None)


def api_key_configured(prefix: str) -> bool:
    return any(os.getenv(k) for k in API_PROVIDERS[prefix][2])


_WORD_CASE = {"gpt": "GPT", "o1": "o1", "o3": "o3", "o4": "o4", "ai": "AI"}


def pretty_label(name: str) -> str:
    """
    모델명을 표시 이름으로: 'gpt-6.1-terra' → 'GPT-6.1 Terra', 'claude-sonnet-4-6' → 'Claude Sonnet 4.6',
    'gemini-3.6-flash' → 'Gemini 3.6 Flash'. 날짜 스냅샷(20251001)은 그대로 둔다.
    """
    parts = name.split("-")
    words: list[str] = []
    i = 0
    while i < len(parts):
        p = parts[i]
        if p.lower() == "gpt" and i + 1 < len(parts) and re.match(r"^\d", parts[i + 1]):
            words.append(f"GPT-{parts[i + 1]}")   # GPT-6.1
            i += 2
            continue
        # 날짜 스냅샷 2026-04-23은 그대로 붙여 둔다
        if re.fullmatch(r"\d{4}", p) and i + 2 < len(parts) \
                and re.fullmatch(r"\d{2}", parts[i + 1]) and re.fullmatch(r"\d{2}", parts[i + 2]):
            words.append(f"{p}-{parts[i + 1]}-{parts[i + 2]}")
            i += 3
            continue
        # claude-sonnet-4-6 처럼 숫자 조각이 이어지면 버전(4.6)으로 합친다 (8자리 날짜는 제외)
        if re.fullmatch(r"\d{1,2}", p):
            nums = [p]
            while i + 1 < len(parts) and re.fullmatch(r"\d{1,2}", parts[i + 1]):
                nums.append(parts[i + 1])
                i += 1
            words.append(".".join(nums))
        else:
            words.append(_WORD_CASE.get(p.lower(), p[:1].upper() + p[1:]))
        i += 1
    return " ".join(words)
