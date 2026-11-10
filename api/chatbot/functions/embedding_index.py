"""
임베딩 재인덱싱 — DB(SQLite chat_messages)의 대화 원문으로 고른 임베딩 방식의 Qdrant 컬렉션을 채운다.

방식을 바꾸면 차원·벡터 공간이 달라 이전 컬렉션을 쓸 수 없다. 원문은 DB에 다 있으므로(진실 원본)
턴(user+assistant 한 쌍, 같은 turn_id)마다 combined/query/response 3개 벡터를 다시 만들어 새 컬렉션에 넣는다.

  - 백그라운드 작업 1개. 진행률은 REINDEXER.snapshot()으로 본다 (GET /chatbot/embeddings).
  - 먼저 모델을 준비한다 (내장 모델 받기·Ollama 모델 pull·키 확인). 준비가 안 되면 작업은 '실패'로 끝나고 이유를 남긴다.
  - 그동안 채팅은 그대로 된다. 새 턴은 save_conversation이 새 컬렉션에 바로 저장한다 (작업은 이미 있는 point를 건너뛴다).
  - 이미 있는 point는 건너뛴다. 수동·방식 변경 시에는 원문이 달라진 턴(다른 방식을 쓰는 동안 '다시 실행'으로 바뀐 답변)도
    다시 임베딩한다. full=True면 전부 다시 만든다. DB에 없는 point(지운 대화)는 지운다.
  - entity_keys·created_at은 DB에 없으므로 다른 컬렉션에 같은 turn_id의 point가 있으면 그 payload에서 가져온다 (없으면 []·DB 시각).
  - 다른 방식의 컬렉션은 지우지 않는다 (설정 화면의 '안 쓰는 벡터 삭제'로 지운다).
"""
import asyncio
import time
from dataclasses import asdict, dataclass, field
from datetime import timezone
from typing import Optional

from qdrant_client.http import models as rest
from qdrant_client.models import PointStruct

from api.llm_gateway.embeddings import EmbeddingNotReady
from api.load_variable import EMBEDDINGS, QDRANT_DB
from api.chatbot.functions import qdrant_ops

_BATCH_TURNS = 16      # 한 번에 임베딩할 턴 수 (= 텍스트 48개)


@dataclass
class ReindexJob:
    provider: str
    collection: str
    trigger: str                    # startup | select | manual
    status: str = "preparing"       # preparing | indexing | done | error | cancelled
    total: int = 0                  # 임베딩할 턴 수
    done: int = 0                   # 임베딩을 마친 턴 수
    skipped: int = 0                # 이미 들어 있어 건너뛴 턴 수
    removed: int = 0                # DB에 없어 지운 point 수
    turns: int = 0                  # DB의 전체 턴 수
    message: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["percent"] = 100.0 if self.status == "done" else (round(100 * self.done / self.total, 1) if self.total else None)
        d["startedAt"], d["finishedAt"] = d.pop("started_at"), d.pop("finished_at")
        return d


def _iso_local(dt) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().isoformat()


async def load_turns() -> list[dict]:
    """DB의 완성된 턴(user+assistant 모두 있는 turn_id) 목록. 세션·순서대로."""
    from api.db import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT m.turn_id, m.role, m.content, m.created_at, m.session_id, s.owner_id
                 FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id
                WHERE m.turn_id IS NOT NULL
                ORDER BY m.session_id, m.seq"""
        )
    turns: dict[str, dict] = {}
    for r in rows:
        t = turns.setdefault(r["turn_id"], {"id": r["turn_id"], "thread_id": r["session_id"], "user_id": r["owner_id"],
                                            "user": None, "ai": None, "created_at": None})
        if r["role"] == "user":
            t["user"], t["created_at"] = r["content"], r["created_at"]
        else:
            t["ai"] = r["content"]
            t["created_at"] = t["created_at"] or r["created_at"]
    return [t for t in turns.values() if t["user"] is not None and t["ai"] is not None]


async def count_turns() -> Optional[int]:
    try:
        from api.db import get_pool
        async with get_pool().acquire() as conn:
            return await conn.fetchval(
                """SELECT count(*) FROM (SELECT turn_id FROM chat_messages WHERE turn_id IS NOT NULL
                    GROUP BY turn_id HAVING count(DISTINCT role) = 2)""")
    except Exception:
        return None


async def _existing_turn_ids(ids: list[str]) -> set[str]:
    from api.db import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT turn_id FROM chat_messages WHERE turn_id = ANY($1)", ids)
    return {r["turn_id"] for r in rows}


async def _scroll_payloads(collection: str) -> dict[str, tuple]:
    """컬렉션의 point id → (user_query, ai_response)."""
    out: dict[str, tuple] = {}
    offset = None
    while True:
        points, offset = await QDRANT_DB.scroll(collection_name=collection, limit=512, offset=offset,
                                                with_payload=["user_query", "ai_response"], with_vectors=False)
        for p in points:
            pl = p.payload or {}
            out[str(p.id)] = (pl.get("user_query"), pl.get("ai_response"))
        if offset is None:
            return out


async def _payload_hints(ids: list[str], prefer: str) -> dict[str, dict]:
    """다른(또는 같은) 컬렉션에 남아 있는 같은 턴의 payload — entity_keys·created_at을 이어받는다."""
    hints: dict[str, dict] = {}
    names = await qdrant_ops.existing_collections()
    names.sort(key=lambda n: n != prefer)
    for name in names:
        missing = [i for i in ids if i not in hints]
        if not missing:
            break
        try:
            for rec in await QDRANT_DB.retrieve(collection_name=name, ids=missing, with_payload=True, with_vectors=False):
                hints[str(rec.id)] = rec.payload or {}
        except Exception as e:
            print(f"[reindex] '{name}' payload 조회 실패(무시): {type(e).__name__}: {e}")
    return hints


class Reindexer:
    def __init__(self) -> None:
        self.job: Optional[ReindexJob] = None
        self._task: Optional[asyncio.Task] = None

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> Optional[dict]:
        return self.job.to_dict() if self.job else None

    def start(self, provider: str, trigger: str = "manual", full: bool = False) -> ReindexJob:
        """작업을 시작한다. 다른 방식으로 돌던 작업은 멈춘다 (같은 방식으로 이미 돌고 있으면 그대로 둔다)."""
        if self.running() and self.job is not None:
            if self.job.provider == provider and not full:
                return self.job
            self._task.cancel()
        job = ReindexJob(provider=provider, collection=EMBEDDINGS.collection(provider), trigger=trigger)
        self.job = job
        self._task = asyncio.get_running_loop().create_task(self._run(job, full))
        return job

    async def _run(self, job: ReindexJob, full: bool) -> None:
        p = job.provider
        try:
            job.message = "임베딩 모델 준비 중"
            await EMBEDDINGS.provider(p).wait_ready()
            collection = await qdrant_ops.ensure_collection(p)

            job.message = "대화 기록 확인 중"
            turns = await load_turns()
            job.turns = len(turns)
            existing = await _scroll_payloads(collection)

            db_ids = {t["id"] for t in turns}
            orphans = [i for i in existing if i not in db_ids]
            if orphans:
                await QDRANT_DB.delete(collection_name=collection, points_selector=rest.PointIdsList(points=orphans), wait=True)
                job.removed = len(orphans)

            def needs(t: dict) -> bool:
                if full or t["id"] not in existing:
                    return True
                # 시작 시 동기화는 빠진 턴만 (유료 API로 매번 다시 임베딩하지 않게)
                return job.trigger != "startup" and existing[t["id"]] != (t["user"], t["ai"])

            todo = [t for t in turns if needs(t)]
            job.total, job.skipped = len(todo), len(turns) - len(todo)
            job.status = "indexing"
            job.message = f"{len(todo)}개 턴 임베딩 중" if todo else "새로 임베딩할 턴이 없습니다"

            for i in range(0, len(todo), _BATCH_TURNS):
                batch = todo[i:i + _BATCH_TURNS]
                texts: list[str] = []
                for t in batch:
                    texts += [f"User: {t['user']}\nAI: {t['ai']}", t["user"], t["ai"]]
                vecs = await EMBEDDINGS.aembed_documents(texts, provider=p)
                ids = [t["id"] for t in batch]
                alive = await _existing_turn_ids(ids)   # 임베딩하는 동안 지워진 턴은 되살리지 않는다
                hints = await _payload_hints(ids, prefer=collection)
                points = []
                for k, t in enumerate(batch):
                    if t["id"] not in alive:
                        continue
                    h = hints.get(t["id"], {})
                    created = t["created_at"]
                    points.append(PointStruct(
                        id=t["id"],
                        vector={"combined": vecs[3 * k], "query": vecs[3 * k + 1], "response": vecs[3 * k + 2]},
                        payload={
                            "user_id": t["user_id"],
                            "thread_id": t["thread_id"],
                            "created_at": h.get("created_at") or _iso_local(created),
                            "timestamp": h.get("timestamp") or (created.timestamp() if created else time.time()),
                            "user_query": t["user"],
                            "ai_response": t["ai"],
                            "entity_keys": h.get("entity_keys") or [],
                        },
                    ))
                if points:
                    await QDRANT_DB.upsert(collection_name=collection, points=points, wait=True)
                job.done += len(batch)

            job.status = "done"
            job.message = (f"완료 — {job.done}개 턴 임베딩, {job.skipped}개는 이미 있음"
                           + (f", 지운 대화 벡터 {job.removed}개 정리" if job.removed else ""))
            print(f"[reindex] {p} ({collection}): {job.message}")
        except asyncio.CancelledError:
            job.status, job.message = "cancelled", "다른 임베딩 방식으로 바꿔 멈췄습니다."
            raise
        except EmbeddingNotReady as e:
            job.status, job.message = "error", str(e)
            print(f"[reindex] {p}: 준비 안 됨 — {e}")
        except Exception as e:
            job.status, job.message = "error", f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            print(f"[reindex] {p} 실패: {job.message}")
        finally:
            job.finished_at = time.time()


REINDEXER = Reindexer()
