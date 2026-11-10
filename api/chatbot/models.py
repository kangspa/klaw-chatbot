"""
모델 목록·사용자별 사용 설정·로컬 모델 다운로드 End-point (/chatbot/models/...).

  GET  /chatbot/models             등록된(관리자 활성) 모델 + 이 사용자의 사용 여부 + 로컬 설치 상태
  PUT  /chatbot/models/preference  모델 사용/사용 안 함 (마이 페이지 토글)
  POST /chatbot/models/pull        로컬(Ollama) 모델 다운로드 시작 — 백그라운드로 진행
  GET  /chatbot/models/pulls       진행 중·최근 다운로드 상태 (프론트가 주기적으로 조회)
  GET  /chatbot/models/storage     Ollama에 받아진 모델 전체와 용량 (목록에서 빠지고 파일만 남은 모델 포함)
  POST /chatbot/models/delete      로컬 모델 파일 삭제 (디스크 정리)

  커스텀 모델 (사용자가 Ollama 모델명을 입력해 추가 — llm_models에 is_custom으로 등록되어 다른 모델과 똑같이 쓰인다)
  POST  /chatbot/models/custom         {name, label?} 모델명이 Ollama에 있는지 다운로드를 시작해 확인한 뒤 등록
  PATCH /chatbot/models/custom         {modelId, label} 표시 이름 변경
  POST  /chatbot/models/custom/delete  {modelId} 다운로드 취소·파일 삭제 후 등록 해제

사용 여부는 프론트의 선택 목록에만 반영한다. 백엔드는 꺼진 모델의 호출을 막지 않으며,
로컬 모델이 아직 없으면 채팅 호출 시 안내 오류(400)만 돌려준다 (chat_runner.resolve_model).
"""
import asyncio
from importlib import metadata as importlib_metadata
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth.deps import get_current_user
from api.chatbot.functions import model_prefs
from api.db import get_pool, is_ready as db_is_ready
from api.llm_gateway import catalog, store
from api.llm_gateway.local_models import (
    PULL_MANAGER,
    delete_ollama_model,
    friendly_pull_error,
    installed_ollama_models,
    installed_size,
    is_installed,
    normalize_ollama_name,
    ollama_capabilities,
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
    custom: bool = Field(default=False, description="마이 페이지에서 사용자가 추가한 커스텀 모델인지")
    ollamaName: Optional[str] = Field(default=None, description="Ollama 모델명 (Ollama 모델만)")
    supportsTools: Optional[bool] = Field(
        default=None,
        description="도구 호출 지원 여부 (설치된 커스텀 모델만 확인). false면 법률 검색 도구를 쓸 수 없어 답변이 실패한다",
    )


class PreferenceRequest(BaseModel):
    modelId: str
    enabled: bool


class PullRequest(BaseModel):
    modelId: str


class CustomAddRequest(BaseModel):
    name: str = Field(..., description="Ollama 모델명 (예: lfm3-omni:2b). 'ollama run …'·ollama.com 주소를 붙여 넣어도 된다")
    label: Optional[str] = Field(default=None, description="표시 이름. 비우면 모델명")


class CustomRenameRequest(BaseModel):
    modelId: str
    label: str


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


def _ensure_not_upgrading() -> None:
    """Ollama 버전을 바꾸는 동안에는 엔진이 내려갔다 올라오므로 다운로드·삭제를 받지 않는다."""
    from api.llm_gateway.ollama_upgrade import UPGRADER
    if UPGRADER.running:
        raise HTTPException(status_code=409, detail="로컬 모델 서버(Ollama) 버전을 바꾸는 중입니다. 완료된 뒤 다시 시도하세요.")


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
        supports_tools = None
        if s.is_custom and status == "installed":
            # 커스텀 모델은 처음 보는 모델이라 도구 호출 지원을 확인해 경고한다 (등록 모델은 검증을 거쳐 들어왔다)
            caps = await ollama_capabilities(name)
            supports_tools = None if caps is None else "tools" in caps
        out.append(ModelOption(
            id=s.id, label=s.label, group=s.group, isDefault=s.is_default,
            enabled=s.id not in disabled, local=local, localStatus=status, pull=pull, sizeBytes=size,
            custom=s.is_custom, ollamaName=name, supportsTools=supports_tools,
        ))
    return out


@router.put("/preference", status_code=204)
async def set_preference(data: PreferenceRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """모델 사용/사용 안 함 저장. 선택 목록 표시용이며 호출 제한은 하지 않는다."""
    if not db_is_ready():
        raise HTTPException(status_code=503, detail="DB에 연결되어 있지 않아 설정을 저장할 수 없습니다.")
    await _registered(data.modelId)
    await model_prefs.set_model_enabled(current_user["id"], data.modelId, data.enabled)


@router.post("/pull")
async def start_pull(data: PullRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """
    로컬 모델 다운로드를 시작한다 (이미 받는 중이면 그 상태를 돌려준다).
    Ollama는 서버 전체가 공유하므로 받은 모델은 모든 사용자에게 설치된 것으로 보인다.
    """
    _ensure_not_upgrading()
    await _registered(data.modelId)
    if ollama_name(data.modelId) is None:
        raise HTTPException(status_code=400, detail="이 모델은 서버에서 다운로드할 수 없습니다 (Ollama 모델만 지원).")
    if await installed_ollama_models() is None:
        from api import services
        if not await asyncio.to_thread(services.ensure_ollama):
            raise HTTPException(status_code=503, detail=services.ollama_unavailable_message())
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
    _ensure_not_upgrading()
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


# ────────────────────────── 커스텀 모델 ──────────────────────────
def _require_db() -> None:
    if not db_is_ready():
        raise HTTPException(status_code=503, detail="DB에 연결되어 있지 않아 커스텀 모델을 관리할 수 없습니다.")


async def _custom_spec(model_id: str):
    spec = next((s for s in await MODEL_REGISTRY.list_all() if s.id == model_id), None)
    if spec is None or not spec.is_custom:
        raise HTTPException(status_code=404, detail=f"커스텀 모델이 아닙니다: {model_id}")
    return spec


def _clean_label(label: Optional[str], fallback: str) -> str:
    label = " ".join((label or "").split()) or fallback
    if len(label) > 60:
        raise HTTPException(status_code=400, detail="표시 이름은 60자 이하로 입력하세요.")
    return label


@router.post("/custom", status_code=201)
async def add_custom(data: CustomAddRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """
    Ollama 모델명으로 커스텀 모델을 추가한다.

    Ollama에는 '이 모델이 있는가'만 묻는 API가 없어서, 다운로드를 바로 시작하고 첫 응답을 기다린다.
    없는 모델이면 Ollama가 몇 초 안에 오류를 보내므로 등록하지 않고 400을 돌려준다 (오타로 빈 항목이 남지 않게).
    받기 시작했으면 등록하고, 나머지 다운로드는 백그라운드로 계속된다.
    """
    _require_db()
    _ensure_not_upgrading()
    try:
        name = normalize_ollama_name(data.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    model_id = f"ollama/{name}"
    label = _clean_label(data.label, name)

    if any(s.id == model_id for s in await MODEL_REGISTRY.list_all()):
        raise HTTPException(status_code=409, detail=f"이미 목록에 있는 모델입니다: {name}")
    if await installed_ollama_models() is None:
        from api import services
        if not await asyncio.to_thread(services.ensure_ollama):
            raise HTTPException(status_code=503, detail=services.ollama_unavailable_message())

    PULL_MANAGER.start(model_id)
    state = await PULL_MANAGER.wait_started(model_id)
    if state is not None and state.status == "error":
        PULL_MANAGER.forget(model_id)
        raise HTTPException(status_code=400, detail=friendly_pull_error(state.message, name))

    try:
        async with get_pool().acquire() as conn:
            await store.add_custom_model(conn, model_id, label, current_user["id"])
    except Exception as e:
        PULL_MANAGER.cancel(model_id)
        raise HTTPException(status_code=500, detail=f"커스텀 모델을 등록하지 못했습니다: {type(e).__name__}: {e}")
    await MODEL_REGISTRY.refresh(force=True)
    print(f"[local_models] 커스텀 모델 추가: {name} (요청: {current_user.get('email')})")
    return {"modelId": model_id, "name": name, "label": label,
            "pull": PULL_MANAGER.get(model_id).to_dict() if PULL_MANAGER.get(model_id) else None}


@router.patch("/custom", status_code=204)
async def rename_custom(data: CustomRenameRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """커스텀 모델의 표시 이름을 바꾼다 (채팅 화면 선택 목록에도 이 이름으로 보인다)."""
    _require_db()
    spec = await _custom_spec(data.modelId)
    label = _clean_label(data.label, spec.label)
    async with get_pool().acquire() as conn:
        await store.rename_custom_model(conn, data.modelId, label)
    await MODEL_REGISTRY.refresh(force=True)


@router.post("/custom/delete")
async def delete_custom(data: DeleteRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """
    커스텀 모델을 지운다: 받는 중이면 취소하고, 받아져 있으면 서버에서 파일을 지운 뒤 목록에서 뺀다.
    파일 삭제는 로컬 모델 삭제(POST /chatbot/models/delete)와 같다 — 서버 전체에서 지워진다.
    """
    _require_db()
    _ensure_not_upgrading()
    spec = await _custom_spec(data.modelId)
    name = ollama_name(spec.id)
    freed = 0
    if PULL_MANAGER.cancel(spec.id):
        await asyncio.sleep(0.2)   # 취소가 Ollama 연결을 닫을 틈을 준다
    installed = await installed_ollama_models()
    if installed is not None and name and is_installed(name, installed):
        freed = installed_size(name, installed) or 0
        try:
            await delete_ollama_model(name)
        except FileNotFoundError:
            pass   # 그새 지워졌으면 등록만 해제한다
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"모델 파일 삭제에 실패했습니다: {type(e).__name__}: {e}")
    async with get_pool().acquire() as conn:
        await store.delete_custom_model(conn, spec.id)
    PULL_MANAGER.forget(spec.id)
    await MODEL_REGISTRY.refresh(force=True)
    print(f"[local_models] 커스텀 모델 삭제: {name} ({freed / 1e9:.1f} GB, 요청: {current_user.get('email')})")
    return {"modelId": spec.id, "deleted": True, "sizeBytes": freed}


# ────────────────────────── API 모델 (Google·OpenAI·Anthropic 탭) ──────────────────────────
class ApiModelRequest(BaseModel):
    modelId: str = Field(..., description="'gemini/…', 'openai/…', 'anthropic/…'")


class RestoreRequest(BaseModel):
    provider: str = Field(..., description="gemini | openai | anthropic")


def _api_prefix(model_id: str) -> str:
    prefix = model_id.split("/", 1)[0]
    if prefix not in catalog.API_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"API 모델이 아닙니다 (gemini/·openai/·anthropic/만): {model_id}")
    return prefix


@router.get("/catalog")
async def get_catalog(provider: str):
    """
    제공사별로 추가할 수 있는 모델 목록 — 지금 설치된 LiteLLM이 아는 모델 기준 (api/llm_gateway/catalog.py).
    이미 목록에 있는 모델은 added로 표시하고, 삭제한 기본 모델은 removedDefaults로 알려 복원할 수 있게 한다.
    """
    if provider not in catalog.API_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 제공사입니다: {provider}")
    registered = {s.id for s in await MODEL_REGISTRY.list_all()}
    models = [{**m, "added": m["id"] in registered} for m in catalog.catalog(provider)]
    removed: list[dict] = []
    if db_is_ready():
        async with get_pool().acquire() as conn:
            removed_ids = await store.removed_model_ids(conn)
        seed = {m["id"]: m for m in store.read_seed()}
        removed = [{"id": i, "label": seed[i]["label"]} for i in sorted(removed_ids)
                   if i in seed and i.startswith(f"{provider}/") and i not in registered]
    return {
        "provider": provider,
        "group": catalog.API_PROVIDERS[provider][1],
        "litellmVersion": importlib_metadata.version("litellm"),
        "apiKeyConfigured": catalog.api_key_configured(provider),
        "apiKeyEnv": catalog.API_PROVIDERS[provider][2][0],
        "models": models,
        "removedDefaults": removed,
    }


@router.post("/api", status_code=201)
async def add_api_model(data: ApiModelRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """카탈로그에서 고른 API 모델을 추가한다. 채팅 선택 목록의 해당 제공사 그룹에 바로 보인다."""
    _require_db()
    prefix = _api_prefix(data.modelId)
    item = catalog.find(data.modelId)
    if item is None:
        raise HTTPException(status_code=404, detail=f"설치된 LiteLLM이 지원하지 않는 모델입니다: {data.modelId}")
    if any(s.id == data.modelId for s in await MODEL_REGISTRY.list_all()):
        raise HTTPException(status_code=409, detail=f"이미 목록에 있는 모델입니다: {data.modelId}")
    seed = {m["id"]: m for m in store.read_seed()}
    async with get_pool().acquire() as conn:
        if data.modelId in seed:
            # 삭제했던 기본 모델을 카탈로그에서 다시 고르면 기본 모델 설정 그대로 되살린다
            await store.restore_defaults(conn, [data.modelId])
            label = seed[data.modelId]["label"]
        else:
            label = catalog.pretty_label(item["name"])
            await store.add_api_model(conn, data.modelId, label, catalog.API_PROVIDERS[prefix][1], current_user["id"])
    await MODEL_REGISTRY.refresh(force=True)
    print(f"[models] API 모델 추가: {data.modelId} (요청: {current_user.get('email')})")
    return {"modelId": data.modelId, "label": label}


@router.post("/api/delete")
async def delete_api_model(data: ApiModelRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """
    API 모델을 목록에서 지운다. 기본 제공 모델도 지울 수 있으며(복원 가능), 기본 모델(is_default)만은 지울 수 없다.
    """
    _require_db()
    _api_prefix(data.modelId)
    spec = next((s for s in await MODEL_REGISTRY.list_all() if s.id == data.modelId), None)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"목록에 없는 모델입니다: {data.modelId}")
    if spec.is_default:
        raise HTTPException(status_code=400, detail="기본 모델은 삭제할 수 없습니다. 요청에 모델이 없을 때 쓰이는 모델입니다.")
    async with get_pool().acquire() as conn:
        kind = await store.remove_model(conn, data.modelId)
    await MODEL_REGISTRY.refresh(force=True)
    print(f"[models] API 모델 삭제({kind}): {data.modelId} (요청: {current_user.get('email')})")
    return {"modelId": data.modelId, "deleted": True, "wasDefault": kind == "default"}


@router.post("/api/restore")
async def restore_api_defaults(data: RestoreRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """해당 제공사에서 삭제했던 기본 모델을 모두 되살린다."""
    _require_db()
    if data.provider not in catalog.API_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 제공사입니다: {data.provider}")
    async with get_pool().acquire() as conn:
        ids = [i for i in await store.removed_model_ids(conn) if i.startswith(f"{data.provider}/")]
        restored = await store.restore_defaults(conn, ids)
    await MODEL_REGISTRY.refresh(force=True)
    return {"provider": data.provider, "restored": restored}

