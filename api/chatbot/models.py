"""
모델 목록·사용자별 사용 설정·로컬 모델 다운로드 End-point (/chatbot/models/...).

  GET  /chatbot/models             등록된(관리자 활성) 모델 + 이 사용자의 사용 여부 + 로컬 설치 상태
  PUT  /chatbot/models/preference  모델 사용/사용 안 함 (마이 페이지 토글)
  POST /chatbot/models/pull        로컬(Ollama) 모델 다운로드 시작 — 백그라운드로 진행
  GET  /chatbot/models/pulls       진행 중·최근 다운로드 상태 (프론트가 주기적으로 조회)
  GET  /chatbot/models/storage     Ollama에 받아진 모델 전체와 용량 (목록에서 빠지고 파일만 남은 모델 포함)
  POST /chatbot/models/delete      로컬 모델 파일 삭제 (디스크 정리)

사용 여부는 프론트의 선택 목록에만 반영한다. 백엔드는 꺼진 모델의 호출을 막지 않으며,
로컬 모델이 아직 없으면 채팅 호출 시 안내 오류(400)만 돌려준다 (chat_runner.resolve_model).
"""
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth.deps import get_current_user
from api.chatbot.functions import model_prefs
from api.db import is_ready as db_is_ready
from api.llm_gateway.local_models import (
    PULL_MANAGER,
    delete_ollama_model,
    installed_ollama_models,
    installed_size,
    is_installed,
    ollama_inventory,
    ollama_name,
)
from api.llm_gateway.providers import LOCAL_PROVIDERS, split_model_id
from api.load_variable import MODEL_REGISTRY

router = APIRouter(prefix="/models")

LocalStatus = Literal["installed", "missing", "pulling", "unreachable", "unmanaged"]


class ModelOption(BaseModel):
    id: str = Field(..., description="요청 시 model 값으로 보낼 모델 ID ('접두사/모델명')")
    label: str = Field(..., description="선택 목록에 보일 이름")
    group: str = Field(..., description="선택 목록 그룹")
    isDefault: bool = Field(..., description="기본 모델 여부")
    enabled: bool = Field(..., description="이 사용자가 마이 페이지에서 켜 둔 모델인지 (기본 켜짐)")
    local: bool = Field(..., description="로컬 런타임(Ollama 등)에서 도는 모델인지")
    localStatus: Optional[LocalStatus] = Field(
        default=None,
        description="로컬 모델 상태. installed/missing/pulling, Ollama 연결 불가는 unreachable, "
                    "설치 상태를 알 수 없는 런타임(lmstudio·vllm)은 unmanaged. 클라우드 모델은 null",
    )
    pull: Optional[dict[str, Any]] = Field(default=None, description="진행 중·최근 다운로드 상태")
    sizeBytes: Optional[int] = Field(default=None, description="설치된 로컬 모델의 디스크 크기")


class PreferenceRequest(BaseModel):
    modelId: str
    enabled: bool


class PullRequest(BaseModel):
    modelId: str


class DeleteRequest(BaseModel):
    modelId: str = Field(..., description="'ollama/이름'. 목록에 없는(파일만 남은) 모델도 storage 응답의 id로 지울 수 있다")


class StoredModel(BaseModel):
    id: str = Field(..., description="삭제 요청에 쓸 ID ('ollama/이름')")
    name: str = Field(..., description="Ollama 모델 이름")
    sizeBytes: int
    registered: bool = Field(..., description="모델 목록(llm_models)에 등록된 모델인지. false면 파일만 남은 모델")
    label: Optional[str] = None


class StorageResponse(BaseModel):
    reachable: bool = Field(..., description="Ollama에 연결되었는지")
    totalBytes: int
    models: list[StoredModel]


async def _registered(model_id: str):
    spec = next((s for s in await MODEL_REGISTRY.list_enabled() if s.id == model_id), None)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"등록되지 않은 모델입니다: {model_id}")
    return spec


@router.get("", response_model=list[ModelOption])
async def list_models(current_user: dict[str, Any] = Depends(get_current_user)):
    """선택 가능한 모델 목록 (DB llm_models 중 활성 모델, sort_order 순)."""
    specs = await MODEL_REGISTRY.list_enabled()
    disabled = await model_prefs.disabled_models(current_user["id"]) if db_is_ready() else set()
    # Ollama 모델이 하나라도 있을 때만 설치 목록을 조회한다 (클라우드만 쓰는 환경에서 지연 없음)
    installed = await installed_ollama_models() if any(ollama_name(s.id) for s in specs) else None
    pulls = PULL_MANAGER.snapshot()

    out = []
    for s in specs:
        provider, _ = split_model_id(s.id)
        local = provider in LOCAL_PROVIDERS
        name = ollama_name(s.id)
        pull = pulls.get(s.id)
        status: Optional[str] = None
        if name is not None:
            if pull and pull["status"] == "pulling":
                status = "pulling"
            elif installed is None:
                status = "unreachable"
            else:
                status = "installed" if is_installed(name, installed) else "missing"
        elif local:
            status = "unmanaged"
        size = installed_size(name, installed) if (name is not None and installed and status == "installed") else None
        out.append(ModelOption(
            id=s.id, label=s.label, group=s.group, isDefault=s.is_default,
            enabled=s.id not in disabled, local=local, localStatus=status, pull=pull, sizeBytes=size,
        ))
    return out


@router.put("/preference", status_code=204)
async def set_preference(data: PreferenceRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """모델 사용/사용 안 함 저장. 선택 목록 표시용이며 호출 제한은 하지 않는다."""
    if not db_is_ready():
        raise HTTPException(status_code=503, detail="Postgres에 연결되어 있지 않아 설정을 저장할 수 없습니다.")
    await _registered(data.modelId)
    await model_prefs.set_model_enabled(current_user["id"], data.modelId, data.enabled)


@router.post("/pull")
async def start_pull(data: PullRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """
    로컬 모델 다운로드를 시작한다 (이미 받는 중이면 그 상태를 돌려준다).
    Ollama는 서버 전체가 공유하므로 받은 모델은 모든 사용자에게 설치된 것으로 보인다.
    """
    await _registered(data.modelId)
    if ollama_name(data.modelId) is None:
        raise HTTPException(status_code=400, detail="이 모델은 서버에서 다운로드할 수 없습니다 (Ollama 모델만 지원).")
    if await installed_ollama_models() is None:
        raise HTTPException(status_code=503, detail="로컬 모델 서버(Ollama)에 연결할 수 없습니다. `docker compose up -d ollama`를 확인하세요.")
    print(f"[local_models] 다운로드 시작: {data.modelId} (요청: {current_user.get('email')})")
    return {"modelId": data.modelId, **PULL_MANAGER.start(data.modelId).to_dict()}


@router.get("/pulls")
async def list_pulls(current_user: dict[str, Any] = Depends(get_current_user)):
    """모델 ID별 다운로드 상태 {modelId: {status, percent, completed, total, message, ...}}"""
    return PULL_MANAGER.snapshot()


def _registered_ollama_names(specs) -> dict[str, Any]:
    """등록된 Ollama 모델 이름 → 사양. ':latest' 유무가 달라도 같은 모델로 본다."""
    names: dict[str, Any] = {}
    for s in specs:
        name = ollama_name(s.id)
        if name is not None:
            names[name] = s
            names[name if ":" in name else f"{name}:latest"] = s
    return names


@router.get("/storage", response_model=StorageResponse)
async def get_storage(current_user: dict[str, Any] = Depends(get_current_user)):
    """
    Ollama에 받아진 모델 전체와 디스크 크기.
    모델 목록(llm_models)에서 빠졌는데 파일만 남은 모델도 보여줘야 용량을 정리할 수 있다.
    (관리자 비활성 모델도 등록 모델로 본다 — list_all 기준)
    """
    inventory = await ollama_inventory()
    if inventory is None:
        return StorageResponse(reachable=False, totalBytes=0, models=[])
    registered = _registered_ollama_names(await MODEL_REGISTRY.list_all())
    models = []
    for m in sorted(inventory, key=lambda x: -x["size"]):
        spec = registered.get(m["name"])
        models.append(StoredModel(
            id=spec.id if spec else f"ollama/{m['name']}", name=m["name"], sizeBytes=m["size"],
            registered=spec is not None, label=spec.label if spec else None,
        ))
    return StorageResponse(reachable=True, totalBytes=sum(m["size"] for m in inventory), models=models)


@router.post("/delete")
async def delete_local_model(data: DeleteRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """
    로컬 모델 파일을 서버(Ollama)에서 지운다. Ollama는 서버 전체가 공유하므로 모든 사용자에게서 지워진다.
    지울 수 있는 것은 Ollama에 실제로 받아져 있는 모델뿐이다 (등록 여부와 무관 — 파일만 남은 모델 정리용).
    모델 목록(llm_models) 항목은 그대로 남으므로 다시 쓰려면 마이 페이지에서 다시 다운로드하면 된다.
    """
    name = ollama_name(data.modelId)
    if name is None:
        raise HTTPException(status_code=400, detail="서버에 저장되는 로컬(Ollama) 모델만 삭제할 수 있습니다.")
    pull = PULL_MANAGER.get(data.modelId)
    if pull is not None and pull.status == "pulling":
        raise HTTPException(status_code=409, detail="다운로드 중인 모델은 삭제할 수 없습니다. 완료된 뒤 다시 시도하세요.")
    installed = await installed_ollama_models()
    if installed is None:
        raise HTTPException(status_code=503, detail="로컬 모델 서버(Ollama)에 연결할 수 없습니다.")
    if not is_installed(name, installed):
        raise HTTPException(status_code=404, detail=f"서버에 받아져 있지 않은 모델입니다: {name}")
    size = installed_size(name, installed) or 0
    try:
        await delete_ollama_model(name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"모델 삭제에 실패했습니다: {type(e).__name__}: {e}")
    PULL_MANAGER.forget(data.modelId)
    print(f"[local_models] 삭제: {name} ({size / 1e9:.1f} GB, 요청: {current_user.get('email')})")
    return {"modelId": data.modelId, "deleted": True, "sizeBytes": size}
