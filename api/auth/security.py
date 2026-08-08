"""
앱 자체 세션 토큰(JWT) 발급/검증.

Google ID 토큰은 로그인 순간에만 쓰고 그대로 보관하지 않는다.
검증에 성공하면 우리 서버가 서명한 짧은 수명의 액세스 토큰을 새로 발급하고,
이후 모든 API 요청은 이 토큰만 사용한다(Authorization: Bearer ...).
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import dotenv
import jwt

dotenv.load_dotenv()

ALGORITHM = "HS256"
ISSUER = "klaw-chatbot"

# 토큰 수명 (기본 7일)
ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("APP_JWT_TTL_SECONDS", str(7 * 24 * 3600)))

_SECRET = os.getenv("APP_JWT_SECRET", "").strip()
if not _SECRET:
    # 개발 편의를 위해 임시 키를 생성한다. 서버를 재시작하면 기존 토큰이 모두 무효가 되므로
    # 실제로 쓸 때는 반드시 .env에 APP_JWT_SECRET을 지정해야 한다.
    _SECRET = secrets.token_urlsafe(48)
    print(
        "[auth] APP_JWT_SECRET이 설정되지 않아 임시 키를 생성했습니다. "
        "서버 재시작 시 모든 로그인 세션이 풀립니다 — .env에 APP_JWT_SECRET을 지정하세요."
    )


class TokenError(Exception):
    """토큰이 없거나, 서명/만료 검증에 실패한 경우."""


def create_access_token(user_id: str, extra: Optional[dict[str, Any]] = None) -> tuple[str, int]:
    """(토큰 문자열, 만료까지 남은 초)를 반환."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, _SECRET, algorithm=ALGORITHM), ACCESS_TOKEN_TTL_SECONDS


def decode_access_token(token: str) -> dict[str, Any]:
    """검증 후 payload를 반환. 실패하면 TokenError."""
    try:
        return jwt.decode(
            token,
            _SECRET,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenError("로그인 세션이 만료되었습니다. 다시 로그인해주세요.") from e
    except jwt.InvalidTokenError as e:
        raise TokenError("유효하지 않은 인증 토큰입니다.") from e
