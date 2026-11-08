"""
로컬 모델 서버(Ollama) 버전 관리 End-point (/chatbot/ollama/...).

  GET  /chatbot/ollama          실행 중인 버전·설정 버전·가속 장치·선택 가능한 릴리스·진행 중인 변경 작업
  POST /chatbot/ollama/upgrade  {version} 버전 변경 시작 (이미지 빌드 → 컨테이너 재시작 → 확인, 실패 시 되돌림)
  GET  /chatbot/ollama/upgrade  변경 작업 진행 상태·로그 (프론트가 주기적으로 조회)

개인 PC에서 띄워 쓰는 서비스라 별도 권한 확인 없이 실행한다 (api/llm_gateway/ollama_upgrade.py 참고).
"""
import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth.deps import get_current_user
from api.llm_gateway import local_runtime as lr
from api.llm_gateway.local_models import PULL_MANAGER
from api.llm_gateway.ollama_upgrade import (
    UPGRADER,
    VERSION_RE,
    UpgradeError,
    configured_version,
    list_releases,
    version_key,
)

router = APIRouter(prefix="/ollama", dependencies=[Depends(get_current_user)])


class UpgradeRequest(BaseModel):
    version: str = Field(..., description="바꿀 Ollama 버전 (예: 0.40.15)")


@router.get("")
async def get_ollama_status():
    running = await asyncio.to_thread(lr.ollama_version)
    discovery = await asyncio.to_thread(lambda: lr.read_discovery(lr.ollama_log_text()))
    releases: list[dict] = []
    releases_error: Optional[str] = None
    try:
        releases = await asyncio.to_thread(list_releases)
    except Exception as e:
        releases_error = f"GitHub에서 릴리스 목록을 받지 못했습니다: {type(e).__name__}"
    latest = releases[0]["version"] if releases else None
    return {
        "runningVersion": running,                 # None이면 Ollama에 연결할 수 없음
        "configuredVersion": configured_version(),
        "latestVersion": latest,
        "updateAvailable": bool(running and latest and version_key(latest) > version_key(running)),
        "acceleration": lr.describe_acceleration(discovery) if running else None,
        "accelerated": lr.is_accelerated(discovery) if running else False,
        "releases": releases,
        "releasesError": releases_error,
        "job": UPGRADER.job.to_dict() if UPGRADER.job else None,
    }


@router.post("/upgrade", status_code=202)
async def start_upgrade(data: UpgradeRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    version = data.version.strip().lstrip("v")
    if not VERSION_RE.match(version):
        raise HTTPException(status_code=400, detail=f"버전 형식이 아닙니다: {data.version} (예: 0.40.15)")
    if UPGRADER.running:
        raise HTTPException(status_code=409, detail="이미 Ollama 버전을 변경하는 중입니다.")
    pulling = [mid for mid, p in PULL_MANAGER.snapshot().items() if p["status"] == "pulling"]
    if pulling:
        raise HTTPException(status_code=409, detail="모델을 다운로드하는 중에는 버전을 바꿀 수 없습니다. 완료되거나 취소한 뒤 다시 시도하세요.")
    try:
        releases = await asyncio.to_thread(list_releases, 100)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub에서 릴리스 목록을 받지 못했습니다: {type(e).__name__}")
    if not any(r["version"] == version for r in releases):
        raise HTTPException(status_code=404, detail=f"설치할 수 있는 Ollama 릴리스가 아닙니다: {version}")
    running = await asyncio.to_thread(lr.ollama_version)
    if running == version and configured_version() == version:
        raise HTTPException(status_code=400, detail=f"이미 Ollama {version}을 쓰고 있습니다.")
    try:
        job = UPGRADER.start(version)
    except UpgradeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    print(f"[ollama-upgrade] 시작: {running} → {version} (요청: {current_user.get('email')})")
    return job.to_dict()


@router.get("/upgrade")
async def get_upgrade_job():
    return UPGRADER.job.to_dict() if UPGRADER.job else None
