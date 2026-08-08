"""회원 정보 Postgres 저장소."""

import uuid
from typing import Any, Optional

from api.db import get_pool


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
    }


async def upsert_google_user(claims: dict[str, Any]) -> dict[str, Any]:
    """
    Google ID 토큰의 claims로 회원을 생성하거나 갱신한다(로그인 = 최초 가입 겸용).

    google_sub을 충돌 키로 쓰는 이유: 사용자가 Google 계정의 이메일을 바꿔도
    같은 계정으로 인식되어 기존 대화 세션을 그대로 유지할 수 있다.
    이름/사진/이메일은 로그인할 때마다 Google 쪽 최신 값으로 덮어쓴다.
    """
    pool = get_pool()
    google_sub = claims["sub"]
    email = claims["email"]
    name = (claims.get("name") or email.split("@")[0]).strip()
    picture = claims.get("picture")
    email_verified = bool(claims.get("email_verified", False))

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (id, google_sub, email, email_verified, name, picture, last_login_at)
            VALUES ($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (google_sub) DO UPDATE
               SET email          = EXCLUDED.email,
                   email_verified = EXCLUDED.email_verified,
                   name           = EXCLUDED.name,
                   picture        = EXCLUDED.picture,
                   updated_at     = now(),
                   last_login_at  = now()
            RETURNING *
            """,
            str(uuid.uuid4()), google_sub, email, email_verified, name, picture,
        )
    return _user_row(row)


async def get_user(user_id: str) -> Optional[dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    return _user_row(row) if row else None


async def delete_user(user_id: str) -> bool:
    """회원 탈퇴. chat_sessions/chat_messages는 FK CASCADE로 함께 삭제된다."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM users WHERE id = $1", user_id)
    return result.endswith(" 1")
