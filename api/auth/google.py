"""
Google ID 토큰 검증.

프론트의 Google Identity Services 버튼이 돌려주는 `credential`은 Google이 서명한 JWT다.
이 값을 그대로 신뢰하면 안 되고, 반드시 아래를 서버에서 확인해야 한다.

  1. 서명   : Google의 공개키(JWKS)로 RS256 서명 검증 — 위조 방지
  2. aud    : 우리 앱의 OAuth 클라이언트 ID와 일치 — 다른 앱용 토큰 재사용(token replay) 방지
  3. iss    : accounts.google.com 발급분인지
  4. exp    : 만료 여부

PyJWKClient가 JWKS를 내려받아 캐시하므로 매 로그인마다 네트워크 요청이 나가지는 않는다.
다만 동기 호출이라 asyncio.to_thread로 감싸 이벤트 루프를 막지 않게 한다.
"""

import asyncio
import os
from typing import Any

import dotenv
import jwt
from jwt import PyJWKClient

dotenv.load_dotenv()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()

JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
VALID_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# 공개키는 자주 바뀌지 않으므로 클라이언트를 모듈 수준에서 재사용한다 (내부 캐시 활용).
_jwk_client = PyJWKClient(JWKS_URI, cache_keys=True, lifespan=3600)


class GoogleAuthError(Exception):
    """Google ID 토큰이 유효하지 않은 경우."""


def is_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID)


def _verify_sync(credential: str) -> dict[str, Any]:
    signing_key = _jwk_client.get_signing_key_from_jwt(credential)
    return jwt.decode(
        credential,
        signing_key.key,
        algorithms=["RS256"],
        audience=GOOGLE_CLIENT_ID,
        options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        leeway=30,  # 서버 시계 오차 허용
    )


async def verify_id_token(credential: str) -> dict[str, Any]:
    """
    검증에 성공하면 Google이 담아 보낸 사용자 정보(claims)를 반환한다.
    주요 키: sub(고유 식별자), email, email_verified, name, picture
    """
    if not GOOGLE_CLIENT_ID:
        raise GoogleAuthError(
            "GOOGLE_CLIENT_ID가 설정되지 않았습니다. .env에 Google OAuth 클라이언트 ID를 지정하세요."
        )
    if not credential:
        raise GoogleAuthError("Google 인증 정보가 비어 있습니다.")

    try:
        claims = await asyncio.to_thread(_verify_sync, credential)
    except jwt.ExpiredSignatureError as e:
        raise GoogleAuthError("Google 인증 정보가 만료되었습니다. 다시 시도해주세요.") from e
    except jwt.InvalidAudienceError as e:
        raise GoogleAuthError(
            "이 앱을 위해 발급된 Google 인증 정보가 아닙니다. "
            "프론트의 VITE_GOOGLE_CLIENT_ID와 백엔드의 GOOGLE_CLIENT_ID가 같은지 확인하세요."
        ) from e
    except jwt.InvalidTokenError as e:
        raise GoogleAuthError(f"Google 인증 정보를 검증하지 못했습니다: {e}") from e
    except Exception as e:  # JWKS 조회 실패(네트워크 등)
        raise GoogleAuthError(f"Google 공개키를 가져오지 못했습니다: {type(e).__name__}: {e}") from e

    if claims.get("iss") not in VALID_ISSUERS:
        raise GoogleAuthError("Google이 발급한 인증 정보가 아닙니다.")

    if not claims.get("sub"):
        raise GoogleAuthError("Google 계정 식별자를 확인할 수 없습니다.")

    if not claims.get("email"):
        raise GoogleAuthError(
            "Google 계정에서 이메일을 받지 못했습니다. 로그인 시 이메일 제공에 동의해주세요."
        )

    return claims
