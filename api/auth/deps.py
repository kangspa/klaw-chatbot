"""FastAPI 인증 의존성."""

from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.auth import user_store
from api.auth.security import TokenError, decode_access_token
from api.db import is_ready as db_is_ready

# auto_error=False: 헤더가 없을 때 FastAPI 기본 403 대신 우리 메시지로 401을 주기 위함
_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any]:
    """
    Authorization: Bearer <token> 헤더를 검증하고 회원 정보를 반환한다.
    토큰이 유효해도 회원이 삭제되었으면 거부한다(탈퇴 후 남은 토큰 차단).
    """
    if not db_is_ready():
        raise HTTPException(
            status_code=503,
            detail="Postgres에 연결되어 있지 않습니다. `docker compose up -d postgres` 후 서버를 다시 시작하세요.",
        )
    if credentials is None or not credentials.credentials:
        raise _unauthorized("로그인이 필요합니다.")

    try:
        payload = decode_access_token(credentials.credentials)
    except TokenError as e:
        raise _unauthorized(str(e))

    user = await user_store.get_user(payload["sub"])
    if not user:
        raise _unauthorized("계정을 찾을 수 없습니다. 다시 로그인해주세요.")
    return user
