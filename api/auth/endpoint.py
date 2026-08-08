"""
인증 End-point (/auth/...).

로그인 방식은 Google 계정 연동 하나뿐이며, 최초 로그인이 곧 회원가입이다.

흐름:
  1. 프론트가 Google Identity Services 버튼으로 ID 토큰(credential)을 받는다
  2. POST /auth/google 로 그 토큰을 보낸다
  3. 서버가 Google 공개키로 검증하고 users 테이블에 회원을 생성/갱신한다
  4. 서버가 자체 서명한 액세스 토큰을 발급한다 — 이후 모든 API는 이 토큰만 사용
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth import google, user_store
from api.auth.deps import get_current_user
from api.auth.security import ACCESS_TOKEN_TTL_SECONDS, create_access_token
from api.db import is_ready as db_is_ready

router = APIRouter(prefix="/auth", tags=["auth"])


class GoogleLoginRequest(BaseModel):
    credential: str = Field(..., description="Google Identity Services가 발급한 ID 토큰(JWT)")


class LoginResponse(BaseModel):
    accessToken: str = Field(..., description="이후 API 호출에 사용할 Bearer 토큰")
    expiresIn: int = Field(..., description="토큰 만료까지 남은 초")
    user: dict[str, Any] = Field(..., description="로그인한 회원 정보")
    isNewUser: bool = Field(..., description="이번 로그인으로 새로 가입되었는지 여부")


class AuthConfigResponse(BaseModel):
    googleClientId: Optional[str] = Field(
        default=None, description="프론트가 Google 버튼을 렌더링할 때 사용할 클라이언트 ID"
    )
    configured: bool = Field(..., description="서버에 Google 로그인 설정이 완료되었는지")


@router.get("/config", response_model=AuthConfigResponse)
async def get_auth_config():
    """
    프론트가 Google 클라이언트 ID를 서버에서 받아갈 수 있도록 노출한다.
    (클라이언트 ID는 비밀값이 아니며 브라우저에 노출되는 것이 정상이다.
     프론트 빌드에 VITE_GOOGLE_CLIENT_ID를 심지 않아도 되게 하려는 목적)
    """
    return AuthConfigResponse(
        googleClientId=google.GOOGLE_CLIENT_ID or None,
        configured=google.is_configured(),
    )


@router.post("/google", response_model=LoginResponse)
async def login_with_google(data: GoogleLoginRequest):
    """Google ID 토큰을 검증해 로그인시킨다. 처음이면 회원이 자동 생성된다."""
    if not db_is_ready():
        raise HTTPException(
            status_code=503,
            detail="Postgres에 연결되어 있지 않습니다. `docker compose up -d postgres` 후 서버를 다시 시작하세요.",
        )

    try:
        claims = await google.verify_id_token(data.credential)
    except google.GoogleAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    existing = None
    try:
        # 신규 가입 여부 판단용 (응답의 isNewUser에만 사용)
        from api.db import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT id FROM users WHERE google_sub = $1", claims["sub"]
            )
    except Exception as e:
        print(f"[auth] 기존 회원 조회 실패(무시): {type(e).__name__}: {e}")

    user = await user_store.upsert_google_user(claims)
    token, expires_in = create_access_token(user["id"])

    print(f"[auth] 로그인 성공: {user['email']} ({'신규 가입' if not existing else '기존 회원'})")

    return LoginResponse(
        accessToken=token,
        expiresIn=expires_in,
        user=user,
        isNewUser=existing is None,
    )


@router.get("/me")
async def get_me(current_user: dict[str, Any] = Depends(get_current_user)):
    """저장된 토큰이 아직 유효한지 확인하고 최신 회원 정보를 돌려준다 (앱 부팅 시 호출)."""
    return {"user": current_user, "expiresIn": ACCESS_TOKEN_TTL_SECONDS}


@router.delete("/me")
async def delete_me(current_user: dict[str, Any] = Depends(get_current_user)):
    """
    회원 탈퇴. chat_sessions / chat_messages는 FK CASCADE로 함께 삭제된다.
    Qdrant의 대화 벡터도 이 회원의 세션 단위로 모두 지운다.
    """
    from api.chatbot.functions import qdrant_ops, session_store

    sessions = await session_store.list_sessions(current_user["id"])
    for s in sessions:
        await qdrant_ops.delete_thread(s["id"])

    await user_store.delete_user(current_user["id"])
    print(f"[auth] 회원 탈퇴: {current_user['email']} (세션 {len(sessions)}개 삭제)")
    return {"deleted": True, "removedSessions": len(sessions)}
