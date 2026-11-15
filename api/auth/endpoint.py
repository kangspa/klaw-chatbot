"""
사용자·Google 계정 연결 End-point (/auth/...).

개인 PC에서 띄워 쓰는 서비스라 로그인 없이 모든 기능을 쓴다 (현재 사용자 = 로컬 사용자, api/auth/deps.py).
Google 계정 연결은 선택 사항이다 — 개인 Google 드라이브에 대화·설정을 저장하는 용도로 쓸 계정을 정해 둔다.

흐름:
  1. 마이 페이지의 Google 버튼(Google Identity Services)이 ID 토큰(credential)을 받는다
  2. POST /auth/google 로 그 토큰을 보내면 서버가 Google 공개키로 검증하고 로컬 사용자에 계정 정보를 채운다
  3. POST /auth/google/unlink 로 연결을 푼다 (데이터는 그대로)
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth import google, user_store
from api.auth.deps import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


class GoogleLinkRequest(BaseModel):
    credential: str = Field(..., description="Google Identity Services가 발급한 ID 토큰(JWT)")


class AuthConfigResponse(BaseModel):
    googleClientId: Optional[str] = Field(
        default=None, description="프론트가 Google 버튼을 렌더링할 때 사용할 클라이언트 ID"
    )
    configured: bool = Field(..., description="서버에 Google 계정 연결 설정(GOOGLE_CLIENT_ID)이 있는지")
    allowedOrigins: list[str] = Field(
        default_factory=list,
        description="Google '승인된 JavaScript 원본'에 등록해 두는 주소들 (웹 포트 후보 + Vite 개발 서버). "
                    "지금 주소가 여기 없으면 다른 프로그램이 후보 포트를 모두 써서 임시 포트로 열린 것이다",
    )


@router.get("/config", response_model=AuthConfigResponse)
async def get_auth_config():
    """
    프론트가 Google 클라이언트 ID를 서버에서 받아갈 수 있도록 노출한다.
    (클라이언트 ID는 비밀값이 아니며 브라우저에 노출되는 것이 정상이다)
    """
    from api.stack import WEB_PORT_CANDIDATES
    # 앱 화면은 항상 http://localhost:<포트> (127.0.0.1로 열면 화면이 localhost로 옮긴다)
    origins = ["http://localhost"] + [f"http://localhost:{p}" for p in WEB_PORT_CANDIDATES] + ["http://localhost:5173"]
    return AuthConfigResponse(googleClientId=google.GOOGLE_CLIENT_ID or None, configured=google.is_configured(),
                              allowedOrigins=origins)


@router.get("/me")
async def get_me(current_user: dict[str, Any] = Depends(get_current_user)):
    """현재(로컬) 사용자와 Google 연결 여부 (앱 부팅 시 호출)."""
    return {"user": current_user}


@router.post("/google")
async def link_google_account(data: GoogleLinkRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    """Google ID 토큰을 검증해 로컬 사용자에 Google 계정을 연결한다 (선택 사항)."""
    try:
        claims = await google.verify_id_token(data.credential)
    except google.GoogleAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = await user_store.link_google(current_user["id"], claims)
    print(f"[auth] Google 계정 연결: {user['email']}")
    return {"user": user}


@router.post("/google/unlink")
async def unlink_google_account(current_user: dict[str, Any] = Depends(get_current_user)):
    """Google 계정 연결을 푼다. 대화·설정 등 데이터는 그대로 남는다."""
    user = await user_store.unlink_google(current_user["id"])
    print("[auth] Google 계정 연결 해제")
    return {"user": user}
