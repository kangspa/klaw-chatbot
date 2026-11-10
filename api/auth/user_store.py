"""
사용자 저장소 (SQLite, api/db.py).

개인 PC에서 띄워 쓰는 서비스라 로그인 없이 '로컬 사용자' 하나로 동작한다 (users.is_local).
Google 계정 연결은 선택 사항이며, 연결해도 사용자가 바뀌지 않고 같은 행에 Google 정보만 채운다.
그래서 연결 전에 나눈 대화·사용량·모델 설정이 연결 후에도 그대로 이어진다.
"""

import uuid
from typing import Any, Optional

from api.db import NOW, get_pool

LOCAL_NAME = "로컬 사용자"


def _user_row(row) -> dict[str, Any]:
    """API 응답용 변환. google_sub은 내부 식별자이므로 밖으로 내보내지 않는다."""
    return {
        "id": row["id"],
        "email": row["email"],
        "emailVerified": row["email_verified"],
        "name": row["name"],
        "picture": row["picture"],
        "createdAt": row["created_at"].isoformat(),
        "lastLoginAt": row["last_login_at"].isoformat() if row["last_login_at"] else None,
        "googleLinked": row["google_sub"] is not None,
    }


async def ensure_local_user() -> dict[str, Any]:
    """
    로컬 사용자를 돌려준다. 없으면 만든다.
    예전(로그인 필수 시절)에 만든 Google 사용자만 있으면, 가장 최근에 쓴 사용자를 로컬 사용자로 삼아 그 데이터를 잇는다.
    """
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE is_local")
        if row:
            return _user_row(row)
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE users SET is_local = TRUE
                    WHERE id = (SELECT id FROM users ORDER BY last_login_at DESC NULLS LAST, created_at DESC LIMIT 1)
                   RETURNING *"""
            )
            if row is None:
                # 동시에 두 요청이 만들려 해도 부분 유니크 인덱스(is_local)가 하나만 남긴다
                row = await conn.fetchrow(
                    """INSERT INTO users (id, email_verified, name, is_local) VALUES ($1, FALSE, $2, TRUE)
                       ON CONFLICT DO NOTHING RETURNING *""",
                    str(uuid.uuid4()), LOCAL_NAME,
                )
        if row is None:
            row = await conn.fetchrow("SELECT * FROM users WHERE is_local")
    return _user_row(row)


async def link_google(user_id: str, claims: dict[str, Any]) -> dict[str, Any]:
    """
    Google ID 토큰의 claims로 로컬 사용자에 Google 계정을 연결한다 (이름·이메일·사진은 Google 값으로).
    같은 Google 계정이 다른(예전) 사용자 행에 남아 있으면 그쪽 연결을 풀어 유니크 충돌을 피한다.
    """
    google_sub = claims["sub"]
    email = claims["email"]
    name = (claims.get("name") or email.split("@")[0]).strip()
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE users SET google_sub = NULL WHERE google_sub = $1 AND id <> $2", google_sub, user_id)
            row = await conn.fetchrow(
                f"""UPDATE users
                      SET google_sub = $2, email = $3, email_verified = $4, name = $5, picture = $6,
                          updated_at = {NOW}, last_login_at = {NOW}
                    WHERE id = $1
                RETURNING *""",
                user_id, google_sub, email, bool(claims.get("email_verified", False)), name, claims.get("picture"),
            )
    return _user_row(row)


async def unlink_google(user_id: str) -> dict[str, Any]:
    """Google 계정 연결을 푼다. 대화·설정 등 데이터는 그대로 남는다."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            f"""UPDATE users
                  SET google_sub = NULL, email = NULL, email_verified = FALSE, name = $2, picture = NULL,
                      updated_at = {NOW}
                WHERE id = $1
            RETURNING *""",
            user_id, LOCAL_NAME,
        )
    return _user_row(row)


async def get_user(user_id: str) -> Optional[dict[str, Any]]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    return _user_row(row) if row else None
