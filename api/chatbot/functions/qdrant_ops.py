"""
Qdrant conversation_history 컬렉션 조작 유틸.

conversation_history의 point 1개 = 대화 1턴(사용자 질문 + AI 답변)이며
point id는 Postgres chat_messages.turn_id 와 동일하다(= UUID).

이 대응 덕분에:
  - 메시지 삭제   → 해당 turn_id의 point 삭제
  - 메시지 재생성 → 같은 turn_id로 upsert (덮어쓰기, save_conversation이 수행)
  - 브랜치 생성   → 원본 point들을 새 id + 새 thread_id로 복제

가 Postgres와 완전히 동일한 범위로 적용된다.
"""

from typing import Iterable

from qdrant_client.http import models as rest
from qdrant_client.models import PointStruct

from api.load_variable import QDRANT_DB

COLLECTION_NAME = "conversation_history"


async def delete_points(point_ids: Iterable[str]) -> int:
    """turn_id 목록에 해당하는 point들을 삭제. 삭제 시도한 개수를 반환."""
    ids = [pid for pid in point_ids if pid]
    if not ids:
        return 0
    try:
        await QDRANT_DB.delete(
            collection_name=COLLECTION_NAME,
            points_selector=rest.PointIdsList(points=ids),
            wait=True,
        )
        print(f"[Qdrant] point {len(ids)}개 삭제: {ids}")
    except Exception as e:
        # 대화 원문(Postgres)이 이미 지워졌다면 벡터가 남아도 치명적이지 않으므로 best-effort
        print(f"[Qdrant] point 삭제 실패(무시): {type(e).__name__}: {e}")
    return len(ids)


async def delete_thread(thread_id: str) -> None:
    """세션 자체를 삭제할 때: 해당 thread_id의 모든 대화 벡터를 제거."""
    try:
        await QDRANT_DB.delete(
            collection_name=COLLECTION_NAME,
            points_selector=rest.FilterSelector(
                filter=rest.Filter(
                    must=[rest.FieldCondition(key="thread_id", match=rest.MatchValue(value=thread_id))]
                )
            ),
            wait=True,
        )
        print(f"[Qdrant] thread '{thread_id}'의 대화 벡터 전체 삭제")
    except Exception as e:
        print(f"[Qdrant] thread 삭제 실패(무시): {type(e).__name__}: {e}")


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

    반환값: 실제 복제된 point 개수.
    """
    src_ids = [k for k in turn_id_map.keys() if k]
    if not src_ids:
        return 0

    try:
        records = await QDRANT_DB.retrieve(
            collection_name=COLLECTION_NAME,
            ids=src_ids,
            with_payload=True,
            with_vectors=True,
        )
    except Exception as e:
        print(f"[Qdrant] 브랜치용 point 조회 실패: {type(e).__name__}: {e}")
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
        print("[Qdrant] 복제할 point가 없습니다 (원본 벡터 미존재).")
        return 0

    try:
        await QDRANT_DB.upsert(collection_name=COLLECTION_NAME, points=points, wait=True)
        print(f"[Qdrant] 브랜치 복제 완료: {len(points)}개 point → thread '{dst_thread_id}'")
        return len(points)
    except Exception as e:
        print(f"[Qdrant] 브랜치 복제 실패: {type(e).__name__}: {e}")
        return 0
