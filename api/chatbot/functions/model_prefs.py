"""
사용자별 모델 사용 설정 (마이 페이지 > 모델 설정).

user_disabled_models에는 사용자가 끈 모델만 저장한다. 행이 없으면 켜진 것이다.
이 설정은 프론트의 모델 선택 목록에만 쓰고, 채팅 호출을 막는 데는 쓰지 않는다.
"""
from api.db import get_pool


async def disabled_models(user_id: str) -> set[str]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT model_id FROM user_disabled_models WHERE user_id = $1", user_id)
    return {r["model_id"] for r in rows}


async def set_model_enabled(user_id: str, model_id: str, enabled: bool) -> None:
    async with get_pool().acquire() as conn:
        if enabled:
            await conn.execute(
                "DELETE FROM user_disabled_models WHERE user_id = $1 AND model_id = $2", user_id, model_id,
            )
        else:
            await conn.execute(
                """INSERT INTO user_disabled_models (user_id, model_id) VALUES ($1, $2)
                   ON CONFLICT DO NOTHING""",
                user_id, model_id,
            )
