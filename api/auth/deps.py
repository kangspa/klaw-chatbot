"""FastAPI 사용자 의존성."""

from typing import Any

from fastapi import HTTPException

from api.auth import user_store
from api.db import is_ready as db_is_ready


async def get_current_user() -> dict[str, Any]:
    """
    현재 사용자 = 이 PC의 로컬 사용자.

    개인 PC에서 띄워 쓰는 서비스라 로그인을 요구하지 않는다 (토큰도 받지 않는다).
    Google 계정은 마이 페이지에서 선택적으로 연결하며, 연결해도 사용자는 그대로다 (api/auth/user_store.py).
    """
    if not db_is_ready():
        raise HTTPException(
            status_code=503,
            detail="DB(SQLite)를 열지 못했습니다. 로그(logs/klaw.log)의 [DB] 항목을 확인하고 앱을 다시 시작하세요.",
        )
    return await user_store.ensure_local_user()
