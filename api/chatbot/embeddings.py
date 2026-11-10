"""
대화 기록 검색용 임베딩 방식 관리 End-point (/chatbot/embeddings/...).

  GET  /chatbot/embeddings               방식 3개(local·ollama·gemini)의 모델·차원·크기·준비 상태·요구 사항 문제,
                                         지금 고른 방식, 컬렉션별 벡터 수·디스크 크기, 재인덱싱 진행률
  POST /chatbot/embeddings/select        {provider} 방식 변경 → 새 컬렉션을 DB 원문으로 채우는 재인덱싱을 백그라운드로 시작
  POST /chatbot/embeddings/reindex       {full?} 지금 방식으로 다시 맞춘다 (기본: 빠지거나 바뀐 턴만 / full: 전부)
  POST /chatbot/embeddings/prepare       {provider} 모델을 미리 받는다 (local: 내장 모델 / ollama: Qwen3 Embedding pull)
  POST /chatbot/embeddings/delete-unused 지금 방식이 아닌 컬렉션(다른 방식으로 만든 벡터)을 지운다

개인 PC에서 띄워 쓰는 서비스라 별도 권한 확인 없이 실행한다.
"""
import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth.deps import get_current_user
from api.chatbot.functions import qdrant_ops
from api.chatbot.functions.embedding_index import REINDEXER, count_turns
from api.llm_gateway.embeddings import PROVIDERS
from api.load_variable import EMBEDDINGS, QDRANT_DB
from api.stack import QDRANT_PATH

router = APIRouter(prefix="/chatbot/embeddings", dependencies=[Depends(get_current_user)])

ProviderId = Literal["local", "ollama", "gemini"]


class SelectRequest(BaseModel):
    provider: ProviderId


class ReindexRequest(BaseModel):
    full: bool = False


class PrepareRequest(BaseModel):
    provider: ProviderId


def _dir_size(path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


async def _collection_stats(name: str, existing: set[str]) -> dict:
    if name not in existing:
        return {"name": name, "exists": False, "points": 0, "bytes": 0}
    try:
        points = (await QDRANT_DB.get_collection(name)).points_count or 0
    except Exception:
        points = 0
    size = await asyncio.to_thread(_dir_size, QDRANT_PATH / "collection" / name)
    return {"name": name, "exists": True, "points": points, "bytes": size}


async def _status() -> dict:
    active = EMBEDDINGS.active
    existing = set(await qdrant_ops.existing_collections())
    providers = []
    for pid, info in PROVIDERS.items():
        prov = EMBEDDINGS.provider(pid)
        providers.append({
            "id": pid, "label": info.label, "model": info.model, "dims": info.dims,
            "approxBytes": info.approx_bytes, "device": info.device, "privacy": info.privacy,
            "local": pid != "gemini",
            "problem": prov.problem(),
            **(await prov.state()),
            "collection": await _collection_stats(info.collection, existing),
        })
    return {"active": active, "providers": providers, "turns": await count_turns(), "reindex": REINDEXER.snapshot()}


@router.get("")
async def get_status():
    return await _status()


@router.post("/select")
async def select_provider(data: SelectRequest):
    prov = EMBEDDINGS.provider(data.provider)
    if problem := prov.problem():
        raise HTTPException(status_code=400, detail=problem)
    if data.provider != EMBEDDINGS.active:
        EMBEDDINGS.set_active(data.provider)
        print(f"[embeddings] 임베딩 방식 변경 → {data.provider} ({PROVIDERS[data.provider].model})")
    await qdrant_ops.ensure_collection(data.provider)     # 새 턴이 바로 저장될 수 있게
    REINDEXER.start(data.provider, trigger="select")       # 모델 준비 → 기존 대화 채우기
    return await _status()


@router.post("/reindex")
async def reindex(data: ReindexRequest):
    active = EMBEDDINGS.active
    if problem := EMBEDDINGS.provider(active).problem():
        raise HTTPException(status_code=400, detail=problem)
    REINDEXER.start(active, trigger="manual", full=data.full)
    return await _status()


@router.post("/prepare")
async def prepare(data: PrepareRequest):
    prov = EMBEDDINGS.provider(data.provider)
    if problem := prov.problem():
        raise HTTPException(status_code=400, detail=problem)
    prov.prepare()
    return await _status()


@router.post("/delete-unused")
async def delete_unused():
    active_name = qdrant_ops.active_collection()
    deleted, freed = [], 0
    existing = set(await qdrant_ops.existing_collections())
    for name in sorted(existing - {active_name}):
        stats = await _collection_stats(name, existing)
        try:
            await QDRANT_DB.delete_collection(name)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"'{name}' 컬렉션을 지우지 못했습니다: {type(e).__name__}: {e}")
        qdrant_ops.forget_collection(name)
        deleted.append(name)
        freed += stats["bytes"]
        print(f"[embeddings] 안 쓰는 벡터 컬렉션 삭제: {name} ({stats['points']}개)")
    return {"deleted": deleted, "freedBytes": freed, **(await _status())}
