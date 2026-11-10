"""
Qdrant 대화 기록 컬렉션 조작 유틸.

임베딩 방식(local·ollama·gemini)마다 컬렉션이 따로 있다 (api/llm_gateway/embeddings.py의 PROVIDERS[...].collection).
저장·검색은 지금 고른 방식의 컬렉션(active_collection)만 쓰고, 삭제·브랜치 복제는 남아 있는 모든 컬렉션에 적용한다
— 나중에 다른 방식으로 돌아가도 지운 대화가 되살아나거나 브랜치 기록이 빠지지 않게.

컬렉션의 point 1개 = 대화 1턴(사용자 질문 + AI 답변)이며
point id는 DB chat_messages.turn_id 와 동일하다(= UUID).

이 대응 덕분에:
  - 메시지 삭제   → 해당 turn_id의 point 삭제
  - 메시지 재생성 → 같은 turn_id로 upsert (덮어쓰기, save_conversation이 수행)
  - 브랜치 생성   → 원본 point들을 새 id + 새 thread_id로 복제
  - 임베딩 방식 변경 → DB 원문으로 새 컬렉션을 다시 채움 (embedding_index.py)

가 DB와 완전히 동일한 범위로 적용된다.
"""

from typing import Iterable

from qdrant_client.http import models as rest
from qdrant_client.models import Distance, PointStruct, VectorParams

from api.llm_gateway.embeddings import PROVIDERS
from api.load_variable import EMBEDDINGS, QDRANT_DB

VECTOR_NAMES = ("combined", "query", "response")
PAYLOAD_INDEXES = [
    ("user_id",     rest.PayloadSchemaType.KEYWORD),
    ("thread_id",   rest.PayloadSchemaType.KEYWORD),
    ("entity_keys", rest.PayloadSchemaType.KEYWORD),
    ("timestamp",   rest.PayloadSchemaType.FLOAT),
]
_ensured: set[str] = set()


def active_collection() -> str:
    """지금 고른 임베딩 방식의 컬렉션 이름."""
    return EMBEDDINGS.collection()


def all_collection_names() -> list[str]:
    return [p.collection for p in PROVIDERS.values()]


async def existing_collections() -> list[str]:
    """임베딩용 컬렉션 중 실제로 있는 것."""
    try:
        names = {c.name for c in (await QDRANT_DB.get_collections()).collections}
    except Exception as e:
        print(f"[Qdrant] 컬렉션 목록 조회 실패: {type(e).__name__}: {e}")
        return []
    return [n for n in all_collection_names() if n in names]


async def ensure_collection(provider: str | None = None) -> str:
    """
    해당 방식(없으면 지금 고른 방식)의 컬렉션과 payload 인덱스를 보장하고 이름을 돌려준다.
    차원은 방식별 고정값(PROVIDERS[...].dims)을 쓴다 — 시작할 때 모델을 부르지 않아도 되게(받는 중·오프라인이어도 기동).
    """
    info = EMBEDDINGS.info(provider)
    name = info.collection
    if name in _ensured:
        return name
    if not await QDRANT_DB.collection_exists(name):
        await QDRANT_DB.create_collection(
            collection_name=name,
            vectors_config={v: VectorParams(size=info.dims, distance=Distance.COSINE) for v in VECTOR_NAMES},
        )
        print(f"[Qdrant] 컬렉션 '{name}' 생성 (dim={info.dims}, {info.model})")
    # 같은 (field_name, schema)로 다시 호출해도 Qdrant는 에러 없이 무시한다
    for field_name, schema in PAYLOAD_INDEXES:
        try:
            await QDRANT_DB.create_payload_index(collection_name=name, field_name=field_name, field_schema=schema)
        except Exception as e:
            print(f"[Qdrant] payload index '{field_name}' 등록 skip: {e}")
    _ensured.add(name)
    return name


def forget_collection(name: str) -> None:
    """컬렉션을 지운 뒤 '보장됨' 표시를 지운다 (다음 저장 때 다시 만든다)."""
    _ensured.discard(name)


async def delete_points(point_ids: Iterable[str]) -> int:
    """turn_id 목록에 해당하는 point들을 (모든 임베딩 컬렉션에서) 삭제. 삭제 시도한 개수를 반환."""
    ids = [pid for pid in point_ids if pid]
    if not ids:
        return 0
    for name in await existing_collections():
        try:
            await QDRANT_DB.delete(
                collection_name=name,
                points_selector=rest.PointIdsList(points=ids),
                wait=True,
            )
            print(f"[Qdrant] '{name}' point {len(ids)}개 삭제: {ids}")
        except Exception as e:
            # 대화 원문(DB)이 이미 지워졌다면 벡터가 남아도 치명적이지 않으므로 best-effort
            print(f"[Qdrant] '{name}' point 삭제 실패(무시): {type(e).__name__}: {e}")
    return len(ids)


async def delete_thread(thread_id: str) -> None:
    """세션 자체를 삭제할 때: 해당 thread_id의 모든 대화 벡터를 (모든 임베딩 컬렉션에서) 제거."""
    for name in await existing_collections():
        try:
            await QDRANT_DB.delete(
                collection_name=name,
                points_selector=rest.FilterSelector(
                    filter=rest.Filter(
                        must=[rest.FieldCondition(key="thread_id", match=rest.MatchValue(value=thread_id))]
                    )
                ),
                wait=True,
            )
            print(f"[Qdrant] '{name}': thread '{thread_id}'의 대화 벡터 전체 삭제")
        except Exception as e:
            print(f"[Qdrant] '{name}' thread 삭제 실패(무시): {type(e).__name__}: {e}")


async def copy_turns(
    turn_id_map: dict[str, str],
    dst_thread_id: str,
    dst_user_id: str | None = None,
) -> int:
    """
    브랜치 생성용 복제.

    turn_id_map: {원본 turn_id: 새 turn_id}
    원본 point의 벡터(combined/query/response)와 payload를 그대로 가져와,
    id와 payload.thread_id 만 새 세션 것으로 바꿔 upsert 한다.
    (임베딩을 다시 호출하지 않으므로 브랜치 생성 비용이 0에 가깝다)
    남아 있는 모든 임베딩 컬렉션에 각각 복제한다.

    반환값: 지금 쓰는 컬렉션에 실제로 복제된 point 개수.
    """
    src_ids = [k for k in turn_id_map.keys() if k]
    if not src_ids:
        return 0
    copied = 0
    active = active_collection()
    for name in await existing_collections():
        n = await _copy_in(name, src_ids, turn_id_map, dst_thread_id, dst_user_id)
        if name == active:
            copied = n
    return copied


async def _copy_in(
    name: str,
    src_ids: list[str],
    turn_id_map: dict[str, str],
    dst_thread_id: str,
    dst_user_id: str | None,
) -> int:
    try:
        records = await QDRANT_DB.retrieve(
            collection_name=name,
            ids=src_ids,
            with_payload=True,
            with_vectors=True,
        )
    except Exception as e:
        print(f"[Qdrant] '{name}' 브랜치용 point 조회 실패: {type(e).__name__}: {e}")
        return 0

    points = []
    for rec in records:
        new_id = turn_id_map.get(str(rec.id))
        if not new_id or rec.vector is None:
            continue
        payload = dict(rec.payload or {})
        payload["thread_id"] = dst_thread_id
        if dst_user_id:
            payload["user_id"] = dst_user_id
        points.append(PointStruct(id=new_id, vector=rec.vector, payload=payload))

    if not points:
        print(f"[Qdrant] '{name}': 복제할 point가 없습니다 (원본 벡터 미존재).")
        return 0

    try:
        await QDRANT_DB.upsert(collection_name=name, points=points, wait=True)
        print(f"[Qdrant] '{name}' 브랜치 복제 완료: {len(points)}개 point → thread '{dst_thread_id}'")
        return len(points)
    except Exception as e:
        print(f"[Qdrant] '{name}' 브랜치 복제 실패: {type(e).__name__}: {e}")
        return 0
