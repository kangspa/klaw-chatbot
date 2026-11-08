"""
런타임 버전 관리 End-point (/chatbot/runtime/...) — Ollama(로컬 모델 서버)와 LiteLLM(모델 호출 라이브러리).

  GET  /chatbot/runtime                 두 런타임의 현재·최신·(LiteLLM) 최신 호환 버전, 릴리스 목록, 진행 중·마지막 작업
  POST /chatbot/runtime/update          {ollama?, litellm?} 버전 변경 시작 — 둘 다 주면 한 번에 바꾼다
  GET  /chatbot/runtime/update          진행 상태·로그 (프론트가 주기적으로 조회. LiteLLM 적용 중엔 서버가 잠시 내려간다)
  POST /chatbot/runtime/litellm/check   {version} 해당 LiteLLM 버전의 호환성 확인 (별도 가상환경, 결과 캐시)

개인 PC에서 띄워 쓰는 서비스라 별도 권한 확인 없이 실행한다.
"""
import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import runtime_control
from api.auth.deps import get_current_user
from api.llm_gateway import litellm_upgrade as lu
from api.llm_gateway import local_runtime as lr
from api.llm_gateway import ollama_upgrade as ou
from api.llm_gateway.local_models import PULL_MANAGER
from api.llm_gateway.providers import LOCAL_PROVIDERS, split_model_id
from api.llm_gateway.runtime_update import RUNTIME_UPDATER, RuntimeUpdateError
from api.load_variable import MODEL_REGISTRY

router = APIRouter(prefix="/runtime", dependencies=[Depends(get_current_user)])


class UpdateRequest(BaseModel):
    ollama: Optional[str] = Field(default=None, description="바꿀 Ollama 버전 (예: 0.40.15)")
    litellm: Optional[str] = Field(default=None, description="바꿀 LiteLLM 버전 (예: 1.105.7)")


class CheckRequest(BaseModel):
    version: str


async def _api_model_ids() -> list[str]:
    """LiteLLM 호환성 확인에 쓸 등록 API 모델 (로컬 모델은 단가 계산 대상이 아니다)."""
    # Hugging Face 모델은 LiteLLM 가격표에 원래 없으므로 확인 대상에서 뺀다
    return [s.id for s in await MODEL_REGISTRY.list_all()
            if split_model_id(s.id)[0] not in LOCAL_PROVIDERS and split_model_id(s.id)[0] != "huggingface"]


async def _ollama_status() -> dict:
    running = await asyncio.to_thread(lr.ollama_version)
    discovery = await asyncio.to_thread(lambda: lr.read_discovery(lr.ollama_log_text())) if running else None
    releases, error = [], None
    try:
        releases = await asyncio.to_thread(ou.list_releases)
    except Exception as e:
        error = f"GitHub에서 Ollama 릴리스 목록을 받지 못했습니다: {type(e).__name__}"
    latest = releases[0]["version"] if releases else None
    return {
        "current": running,                                  # None이면 Ollama에 연결할 수 없음
        "configured": ou.configured_version(),
        "latest": latest,
        "updateAvailable": bool(running and latest and ou.version_key(latest) > ou.version_key(running)),
        "acceleration": lr.describe_acceleration(discovery) if running else None,
        "accelerated": lr.is_accelerated(discovery) if running else False,
        "releases": releases,
        "releasesError": error,
    }


async def _litellm_status() -> dict:
    current = lu.installed_version()
    releases, error = [], None
    try:
        releases = await asyncio.to_thread(lu.list_releases, 200)
    except Exception as e:
        error = f"PyPI에서 LiteLLM 릴리스 목록을 받지 못했습니다: {type(e).__name__}"
    # 최신 호환 버전은 백그라운드로 찾는다 (처음엔 버전마다 수십 초)
    if releases:
        lu.SCANNER.ensure_started(await _api_model_ids())
    shown = [r for r in releases if lu.version_key(r["version"]) >= lu.version_key(current)][:15]
    shown += [r for r in releases if lu.version_key(r["version"]) < lu.version_key(current)][:10]
    for r in shown:
        c = lu.cached_compat(r["version"])
        r["compatible"] = None if c is None else c["ok"]
        r["compatReason"] = None if c is None else c.get("reason")
    latest = releases[0]["version"] if releases else None
    compatible = lu.SCANNER.result
    return {
        "current": current,
        "latest": latest,
        "latestCompatible": compatible,
        "scanning": lu.SCANNER.scanning,
        "updateAvailable": bool(compatible and lu.version_key(compatible) > lu.version_key(current)),
        "canRestart": runtime_control.can_restart(),
        "releases": shown,
        "releasesError": error,
    }


@router.get("")
async def get_runtime_status():
    ollama, litellm = await asyncio.gather(_ollama_status(), _litellm_status())
    return {"ollama": ollama, "litellm": litellm, "job": RUNTIME_UPDATER.current()}


@router.post("/update", status_code=202)
async def start_update(data: UpdateRequest):
    ollama = (data.ollama or "").strip().lstrip("v") or None
    litellm = (data.litellm or "").strip().lstrip("v") or None
    if not ollama and not litellm:
        raise HTTPException(status_code=400, detail="바꿀 버전을 하나 이상 고르세요.")
    if any(p["status"] == "pulling" for p in PULL_MANAGER.snapshot().values()):
        raise HTTPException(status_code=409, detail="모델을 다운로드하는 중에는 버전을 바꿀 수 없습니다. 완료되거나 취소한 뒤 다시 시도하세요.")

    if ollama:
        if not ou.VERSION_RE.match(ollama):
            raise HTTPException(status_code=400, detail=f"Ollama 버전 형식이 아닙니다: {ollama}")
        try:
            known = await asyncio.to_thread(ou.list_releases, 100)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"GitHub에서 Ollama 릴리스 목록을 받지 못했습니다: {type(e).__name__}")
        if not any(r["version"] == ollama for r in known):
            raise HTTPException(status_code=404, detail=f"설치할 수 있는 Ollama 릴리스가 아닙니다: {ollama}")
        running = await asyncio.to_thread(lr.ollama_version)
        if running == ollama and ou.configured_version() == ollama:
            ollama = None if litellm else ollama
            if ollama:
                raise HTTPException(status_code=400, detail=f"이미 Ollama {ollama}을 쓰고 있습니다.")
    if litellm:
        if not lu.VERSION_RE.match(litellm):
            raise HTTPException(status_code=400, detail=f"LiteLLM 버전 형식이 아닙니다: {litellm}")
        if litellm == lu.installed_version():
            if not ollama:
                raise HTTPException(status_code=400, detail=f"이미 LiteLLM {litellm}을 쓰고 있습니다.")
            litellm = None
        else:
            try:
                known = await asyncio.to_thread(lu.list_releases, 1000)
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"PyPI에서 LiteLLM 릴리스 목록을 받지 못했습니다: {type(e).__name__}")
            if not any(r["version"] == litellm for r in known):
                raise HTTPException(status_code=404, detail=f"설치할 수 있는 LiteLLM 릴리스가 아닙니다: {litellm}")

    try:
        job = RUNTIME_UPDATER.start(ollama, litellm, await _api_model_ids())
    except RuntimeUpdateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    print(f"[runtime-update] 시작: ollama={ollama} litellm={litellm}")
    return job.to_dict()


@router.get("/update")
async def get_update_job():
    return RUNTIME_UPDATER.current()


@router.post("/litellm/check")
async def check_litellm(data: CheckRequest):
    version = data.version.strip().lstrip("v")
    if not lu.VERSION_RE.match(version):
        raise HTTPException(status_code=400, detail=f"버전 형식이 아닙니다: {data.version}")
    return {"version": version, **await asyncio.to_thread(lu.check_compat, version, await _api_model_ids())}
