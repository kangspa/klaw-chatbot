"""
Hugging Face Inference Providers 모델 추가 (마이 페이지 > 모델 설정 > Hugging Face 탭).

사용자가 'org/model'(예: zai-org/GLM-5.3)을 입력하면 Hub API로 확인한 뒤 등록한다:
  - 모델이 없으면(404) 등록하지 않는다
  - 'live' 상태이고 대화(conversational)를 지원하는 추론 제공사(novita·together 등)가 하나도 없으면 등록하지 않는다
  - 도구 호출(features.toolCalling)을 지원하는 제공사를 우선 고른다 — 이 챗봇은 법령·웹 검색을 도구로 한다
고른 제공사는 모델 ID에 박아 둔다: 'huggingface/<제공사>/<org>/<model>' (LiteLLM 표기).
제공사를 고정하지 않으면 라우팅이 도구 호출을 못 하는 제공사로 갈 수 있어서다.

비용: Hugging Face 모델은 LiteLLM 가격표에 없어 비용이 0으로 집계된다 (실제 과금은 HF 계정에서 확인).
"""
import os
import re
from typing import Optional

import httpx

HUB_API = "https://huggingface.co/api/models"
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_PREFIXES = ("https://huggingface.co/", "http://huggingface.co/", "huggingface.co/", "huggingface/")


class HfModelError(ValueError):
    pass


def hf_token() -> Optional[str]:
    return os.getenv("HUGGINGFACEHUB_API_TOKEN") or os.getenv("HF_TOKEN")


def normalize_repo(raw: str) -> str:
    """입력값을 'org/model'로 정리한다 (Hub 주소·'huggingface/' 접두사를 붙여 넣어도 된다)."""
    name = (raw or "").strip().rstrip("/")
    for prefix in _PREFIXES:
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
            break
    name = name.split("?", 1)[0].split("#", 1)[0]
    parts = name.split("/")
    if len(parts) > 2 and parts[2] in ("tree", "blob", "resolve", "discussions"):
        name = "/".join(parts[:2])   # 저장소 하위 페이지 주소
    if not _REPO_RE.match(name):
        raise HfModelError(f"Hugging Face 모델 ID 형식이 아닙니다: {raw!r} (예: zai-org/GLM-5.3)")
    return name


async def inspect(repo: str) -> dict:
    """
    Hub에서 모델과 추론 제공사를 확인한다.
    반환: {repo, providers: [{provider, toolCalling, inputPer1M, outputPer1M, contextTokens}], chosen, supportsTools}
    """
    headers = {"User-Agent": "klaw-chatbot"}
    if hf_token():
        headers["Authorization"] = f"Bearer {hf_token()}"
    async with httpx.AsyncClient(timeout=20) as client:
        # 단일 모델 API로 존재 여부·정식 ID를 확인한다. 다만 이 API의 제공사 정보는 요약본(상태·작업)뿐이라
        # 도구 호출 지원·단가가 없다 → 같은 모델을 목록 API로 다시 찾아 전체 정보를 쓴다.
        resp = await client.get(f"{HUB_API}/{repo}", params={"expand[]": ["inferenceProviderMapping", "pipeline_tag"]},
                                headers=headers)
        if resp.status_code in (401, 404):
            raise HfModelError(f"Hugging Face에서 '{repo}' 모델을 찾을 수 없습니다 (비공개·승인 필요 모델일 수도 있습니다).")
        resp.raise_for_status()
        data = resp.json()
        canonical = data.get("id") or repo
        mapping = data.get("inferenceProviderMapping") or []
        listed = await client.get(HUB_API, params={"search": canonical, "limit": 20,
                                                   "expand[]": "inferenceProviderMapping"}, headers=headers)
        if listed.status_code == 200:
            full = next((m for m in listed.json() if m.get("id") == canonical), None)
            if full and full.get("inferenceProviderMapping"):
                mapping = full["inferenceProviderMapping"]
    if isinstance(mapping, dict):   # 예전 응답 형식 {제공사: {...}}
        mapping = [{"provider": k, **v} for k, v in mapping.items()]
    providers = []
    for m in mapping:
        if m.get("status") != "live" or m.get("task") != "conversational":
            continue
        pricing = (m.get("providerDetails") or {}).get("pricing") or {}
        providers.append({
            "provider": m.get("provider"),
            "toolCalling": bool((m.get("features") or {}).get("toolCalling")),
            "inputPer1M": pricing.get("input"),
            "outputPer1M": pricing.get("output"),
            "contextTokens": (m.get("providerDetails") or {}).get("context_length"),
        })
    if not providers:
        raise HfModelError(f"'{repo}'를 대화용으로 제공하는 추론 제공사가 없습니다 (Inference Providers 미지원 모델).")
    # 도구 호출 지원 → 컨텍스트 긴 순으로 고른다
    providers.sort(key=lambda p: (not p["toolCalling"], -(p["contextTokens"] or 0)))
    chosen = providers[0]
    return {"repo": canonical, "providers": providers, "chosen": chosen,
            "supportsTools": chosen["toolCalling"]}


def model_id_for(repo: str, provider: str) -> str:
    return f"huggingface/{provider}/{repo}"


def split_hf_id(model_id: str) -> tuple[Optional[str], str]:
    """'huggingface/<제공사>/<org>/<model>' → (제공사, 'org/model'). 제공사가 없는 형식이면 (None, 나머지)."""
    rest = model_id.split("/", 1)[1] if "/" in model_id else model_id
    parts = rest.split("/")
    if len(parts) == 3:
        return parts[0], "/".join(parts[1:])
    return None, rest
