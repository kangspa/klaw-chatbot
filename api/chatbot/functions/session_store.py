"""
Postgres 기반 세션/메시지 저장소 (사용자에게 보여줄 대화 원문의 진실 원본).

여기서 다루는 단위:
  - session : 프론트의 대화 세션 = LangGraph thread_id = Qdrant payload.thread_id
  - message : 사용자에게 보여지는 한 말풍선 (role = user | assistant)
  - turn    : (user 메시지 + assistant 메시지) 한 쌍. 같은 turn_id를 공유하며
              Qdrant의 point 1개와 1:1로 대응한다.

삭제 / 브랜치 / 재생성이 Qdrant와 어긋나지 않도록, 모든 조작은 turn_id를 기준으로 한다.
"""

import uuid
from typing import Any, Optional

from api.db import get_pool

DEFAULT_TITLE = "새 대화"


def new_id() -> str:
    """Qdrant point id로도 그대로 쓸 수 있도록 UUID 문자열을 사용한다."""
    return str(uuid.uuid4())


def _session_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ownerId": row["owner_id"],
        "title": row["title"],
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
        "parentSessionId": row["parent_session_id"],
        "branchedFromMessageId": row["branched_from_message_id"],
    }


def _message_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "sessionId": row["session_id"],
        "seq": row["seq"],
        "role": row["role"],
        "content": row["content"],
        "model": row["model"],
        "turnId": row["turn_id"],
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


# ────────────────────────── 세션 ──────────────────────────

async def create_session(
    owner_id: str,
    title: Optional[str] = None,
    session_id: Optional[str] = None,
    parent_session_id: Optional[str] = None,
    branched_from_message_id: Optional[str] = None,
) -> dict[str, Any]:
    pool = get_pool()
    sid = session_id or new_id()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_sessions
                (id, owner_id, title, parent_session_id, branched_from_message_id)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            sid, owner_id, (title or DEFAULT_TITLE), parent_session_id, branched_from_message_id,
        )
    return _session_row(row)


async def ensure_session(session_id: str, owner_id: str) -> dict[str, Any]:
    """
    세션이 없으면 만들어서 반환. (/chat이 프론트보다 먼저 도달하는 경우 대비)
    이미 있으면 기존 행을 그대로 반환한다.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_sessions (id, owner_id, title)
            VALUES ($1, $2, $3)
            ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
            RETURNING *
            """,
            session_id, owner_id, DEFAULT_TITLE,
        )
    return _session_row(row)


async def get_session(session_id: str) -> Optional[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM chat_sessions WHERE id = $1", session_id)
    return _session_row(row) if row else None


async def list_sessions(owner_id: str) -> list[dict[str, Any]]:
    """최근 활동순 세션 목록 (메시지는 포함하지 않음)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM chat_sessions WHERE owner_id = $1 ORDER BY updated_at DESC",
            owner_id,
        )
    return [_session_row(r) for r in rows]


async def list_sessions_with_messages(owner_id: str) -> list[dict[str, Any]]:
    """세션 목록 + 각 세션의 메시지 전체 (프론트 사이드바/요약 페이지가 한 번에 쓰는 형태)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        srows = await conn.fetch(
            "SELECT * FROM chat_sessions WHERE owner_id = $1 ORDER BY updated_at DESC",
            owner_id,
        )
        if not srows:
            return []
        ids = [r["id"] for r in srows]
        mrows = await conn.fetch(
            "SELECT * FROM chat_messages WHERE session_id = ANY($1::text[]) ORDER BY session_id, seq",
            ids,
        )

    grouped: dict[str, list[dict[str, Any]]] = {sid: [] for sid in ids}
    for m in mrows:
        grouped[m["session_id"]].append(_message_row(m))

    result = []
    for s in srows:
        session = _session_row(s)
        session["messages"] = grouped.get(s["id"], [])
        result.append(session)
    return result


async def get_session_with_messages(session_id: str) -> Optional[dict[str, Any]]:
    session = await get_session(session_id)
    if not session:
        return None
    session["messages"] = await list_messages(session_id)
    return session


async def rename_session(session_id: str, title: str) -> Optional[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE chat_sessions SET title = $2, updated_at = now() WHERE id = $1 RETURNING *",
            session_id, title,
        )
    return _session_row(row) if row else None


async def delete_session(session_id: str) -> bool:
    """세션 삭제 (chat_messages는 ON DELETE CASCADE로 함께 삭제)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM chat_sessions WHERE id = $1", session_id)
    return result.endswith(" 1")


async def touch_session(session_id: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE chat_sessions SET updated_at = now() WHERE id = $1", session_id)


# ────────────────────────── 메시지 ──────────────────────────

async def list_messages(session_id: str) -> list[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM chat_messages WHERE session_id = $1 ORDER BY seq",
            session_id,
        )
    return [_message_row(r) for r in rows]


async def get_message(session_id: str, message_id: str) -> Optional[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM chat_messages WHERE session_id = $1 AND id = $2",
            session_id, message_id,
        )
    return _message_row(row) if row else None


async def get_turn_messages(session_id: str, turn_id: str) -> list[dict[str, Any]]:
    """같은 turn_id를 가진 (user, assistant) 쌍을 seq 순으로 반환."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM chat_messages WHERE session_id = $1 AND turn_id = $2 ORDER BY seq",
            session_id, turn_id,
        )
    return [_message_row(r) for r in rows]


async def append_turn(
    session_id: str,
    turn_id: str,
    user_message_id: str,
    user_content: str,
    user_graph_id: str,
    assistant_message_id: str,
    assistant_content: str,
    assistant_graph_id: Optional[str],
    model: Optional[str],
) -> dict[str, Any]:
    """
    사용자 메시지 + AI 응답을 한 트랜잭션으로 저장하고, 세션 제목/updated_at을 갱신한다.
    반환값: {"userMessage": {...}, "assistantMessage": {...}, "session": {...}}

    session을 함께 돌려주는 이유: 첫 사용자 메시지로 세션 제목이 자동 생성되므로,
    프론트가 이 값을 받아야 사이드바 제목이 '새 대화'에 머무르지 않는다.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            next_seq = await conn.fetchval(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_messages WHERE session_id = $1",
                session_id,
            )
            urow = await conn.fetchrow(
                """
                INSERT INTO chat_messages
                    (id, session_id, seq, role, content, model, turn_id, graph_message_id)
                VALUES ($1, $2, $3, 'user', $4, $5, $6, $7)
                RETURNING *
                """,
                user_message_id, session_id, next_seq, user_content, model, turn_id, user_graph_id,
            )
            arow = await conn.fetchrow(
                """
                INSERT INTO chat_messages
                    (id, session_id, seq, role, content, model, turn_id, graph_message_id)
                VALUES ($1, $2, $3, 'assistant', $4, $5, $6, $7)
                RETURNING *
                """,
                assistant_message_id, session_id, next_seq + 1, assistant_content, model,
                turn_id, assistant_graph_id,
            )
            # 첫 사용자 메시지면 제목 자동 생성
            srow = await conn.fetchrow(
                """
                UPDATE chat_sessions
                   SET updated_at = now(),
                       title = CASE WHEN title = $2 THEN LEFT($3, 40) ELSE title END
                 WHERE id = $1
                RETURNING *
                """,
                session_id, DEFAULT_TITLE, (user_content.strip() or DEFAULT_TITLE),
            )
    return {
        "userMessage": _message_row(urow),
        "assistantMessage": _message_row(arow),
        "session": _session_row(srow) if srow else None,
    }


async def update_assistant_message(
    session_id: str,
    message_id: str,
    content: str,
    graph_message_id: Optional[str],
    model: Optional[str],
) -> Optional[dict[str, Any]]:
    """재생성 결과로 기존 assistant 행을 '덮어쓴다' (id/seq 유지 → 대화 순서가 흔들리지 않음)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE chat_messages
                   SET content = $3,
                       graph_message_id = COALESCE($4, graph_message_id),
                       model = COALESCE($5, model),
                       updated_at = now()
                 WHERE session_id = $1 AND id = $2
                RETURNING *
                """,
                session_id, message_id, content, graph_message_id, model,
            )
            if row:
                await conn.execute(
                    "UPDATE chat_sessions SET updated_at = now() WHERE id = $1", session_id
                )
    return _message_row(row) if row else None


async def delete_turn(session_id: str, turn_id: str) -> list[dict[str, Any]]:
    """turn_id에 속한 메시지(사용자 질문 + AI 답변)를 모두 삭제하고 삭제된 행을 반환."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "DELETE FROM chat_messages WHERE session_id = $1 AND turn_id = $2 RETURNING *",
                session_id, turn_id,
            )
            if rows:
                await conn.execute(
                    "UPDATE chat_sessions SET updated_at = now() WHERE id = $1", session_id
                )
    return [_message_row(r) for r in rows]


async def delete_message(session_id: str, message_id: str) -> Optional[dict[str, Any]]:
    """turn_id가 없는 (짝이 없는) 단일 메시지 삭제용."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "DELETE FROM chat_messages WHERE session_id = $1 AND id = $2 RETURNING *",
                session_id, message_id,
            )
            if row:
                await conn.execute(
                    "UPDATE chat_sessions SET updated_at = now() WHERE id = $1", session_id
                )
    return _message_row(row) if row else None


async def copy_messages_upto(
    src_session_id: str,
    dst_session_id: str,
    upto_seq: int,
    turn_id_map: dict[str, str],
    message_id_map: dict[str, str],
) -> list[dict[str, Any]]:
    """
    브랜치 생성용: 원본 세션의 seq <= upto_seq 인 메시지를 새 세션으로 복사한다.

    - turn_id는 turn_id_map으로 새 id에 매핑한다 (Qdrant에 복제될 새 point id와 동일하게 유지).
    - message id는 message_id_map으로 새 id에 매핑한다 (세션 간 PK 충돌 방지).
    - graph_message_id는 그대로 유지한다. 브랜치된 LangGraph 스레드에도 동일한 message 객체가
      복사되므로, 새 세션에서도 재생성/삭제 시 state 상의 메시지를 그대로 찾을 수 있다.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            src_rows = await conn.fetch(
                "SELECT * FROM chat_messages WHERE session_id = $1 AND seq <= $2 ORDER BY seq",
                src_session_id, upto_seq,
            )
            inserted = []
            for r in src_rows:
                new_turn = turn_id_map.get(r["turn_id"]) if r["turn_id"] else None
                new_mid = message_id_map[r["id"]]
                row = await conn.fetchrow(
                    """
                    INSERT INTO chat_messages
                        (id, session_id, seq, role, content, model, turn_id, graph_message_id, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING *
                    """,
                    new_mid, dst_session_id, r["seq"], r["role"], r["content"], r["model"],
                    new_turn, r["graph_message_id"], r["created_at"],
                )
                inserted.append(_message_row(row))
            await conn.execute(
                "UPDATE chat_sessions SET updated_at = now() WHERE id = $1", dst_session_id
            )
    return inserted
