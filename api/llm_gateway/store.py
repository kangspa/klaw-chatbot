"""
llm_models 테이블 입출력과 seed JSON 반영.

테이블 스키마는 api/db.py의 SCHEMA_SQL에 있다.
연결(asyncpg Pool/Connection)은 호출부가 넘겨준다 — 게이트웨이가 풀 수명주기를 알 필요가 없도록.
"""
import json
from pathlib import Path


from api.llm_gateway.providers import split_model_id
from api.llm_gateway.registry import ModelSpec

SEED_PATH = Path(__file__).with_name("seed_models.json")


async def load_models(conn) -> list[ModelSpec]:
    """DB의 모든 모델(비활성 포함)을 읽는다."""
    model_rows = await conn.fetch(
        """SELECT id, label, group_name, sort_order, enabled, is_default, fixed_temperature, options, is_custom
             FROM llm_models ORDER BY sort_order, id"""
    )
    specs = []
    for r in model_rows:
        options = r["options"]
        if isinstance(options, str):   # asyncpg는 기본 설정에서 JSONB를 문자열로 돌려준다
            options = json.loads(options)
        specs.append(ModelSpec(
            id=r["id"], label=r["label"], group=r["group_name"], sort_order=r["sort_order"],
            enabled=r["enabled"], is_default=r["is_default"], fixed_temperature=r["fixed_temperature"],
            options=options or {}, is_custom=r["is_custom"],
        ))
    return specs


def read_seed(path: Path = SEED_PATH) -> list[dict]:
    """seed JSON을 읽고 기본 검증을 한다 (접두사 지원 여부, 기본 모델 1개)."""
    models = json.loads(path.read_text(encoding="utf-8"))["models"]
    for m in models:
        split_model_id(m["id"])   # 지원하지 않는 접두사면 ValueError
    defaults = [m["id"] for m in models if m.get("is_default")]
    if len(defaults) > 1:
        raise ValueError(f"is_default가 true인 모델은 하나여야 합니다: {defaults}")
    return models


async def upsert_models(conn, models: list[dict]) -> None:
    """
    JSON의 모델을 DB에 반영한다(있으면 갱신, 없으면 추가).
    트랜잭션 안에서 호출해야 한다.
    """
    if any(m.get("is_default") for m in models):
        # 기본 모델은 하나만 허용(부분 유니크 인덱스)하므로 먼저 기존 기본값을 해제한다.
        await conn.execute("UPDATE llm_models SET is_default = FALSE WHERE is_default")
    for m in models:
        await conn.execute(
            """INSERT INTO llm_models (id, label, group_name, sort_order, enabled, is_default,
                                       fixed_temperature, options)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
               ON CONFLICT (id) DO UPDATE SET
                   label = EXCLUDED.label, group_name = EXCLUDED.group_name,
                   sort_order = EXCLUDED.sort_order, enabled = EXCLUDED.enabled,
                   is_default = EXCLUDED.is_default, fixed_temperature = EXCLUDED.fixed_temperature,
                   options = EXCLUDED.options, updated_at = now()""",
            m["id"], m["label"], m["group"], m.get("sort_order", 0), m.get("enabled", True),
            bool(m.get("is_default")), bool(m.get("fixed_temperature")),
            json.dumps(m.get("options") or {}, ensure_ascii=False),
        )


# ────────────── 커스텀 모델 (마이 페이지에서 사용자가 추가한 Ollama 모델) ──────────────
CUSTOM_GROUP = "커스텀"
CUSTOM_SORT_BASE = 1000          # 등록 모델(seed) 뒤에 추가한 순서대로 보인다
# 처음 보는 모델이라 크기를 모른다. 다른 로컬 모델과 같은 컨텍스트로 두되,
# 큰 모델은 VRAM을 넘으면 Ollama가 일부를 CPU로 옮겨 느려진다 (실패하지는 않는다).
CUSTOM_OPTIONS = {"num_ctx": 32768}


async def add_custom_model(conn, model_id: str, label: str, created_by: str) -> None:
    sort_order = await conn.fetchval(
        "SELECT COALESCE(MAX(sort_order), $1) + 1 FROM llm_models WHERE is_custom", CUSTOM_SORT_BASE,
    )
    await conn.execute(
        """INSERT INTO llm_models (id, label, group_name, sort_order, enabled, is_default,
                                   fixed_temperature, options, is_custom, created_by)
           VALUES ($1, $2, $3, $4, TRUE, FALSE, FALSE, $5::jsonb, TRUE, $6)""",
        model_id, label, CUSTOM_GROUP, sort_order, json.dumps(CUSTOM_OPTIONS), created_by,
    )


async def rename_custom_model(conn, model_id: str, label: str) -> bool:
    result = await conn.execute(
        "UPDATE llm_models SET label = $2, updated_at = now() WHERE id = $1 AND is_custom", model_id, label,
    )
    return result.endswith(" 1")


async def delete_custom_model(conn, model_id: str) -> bool:
    """커스텀 모델 등록을 지운다 (사용자별 사용 설정은 FK CASCADE로 함께 삭제)."""
    result = await conn.execute("DELETE FROM llm_models WHERE id = $1 AND is_custom", model_id)
    return result.endswith(" 1")


# ────────────── API 모델 (마이 페이지 Google·OpenAI·Anthropic 탭) ──────────────
async def add_api_model(conn, model_id: str, label: str, group: str, created_by: str) -> None:
    """
    카탈로그(설치된 LiteLLM이 아는 모델)에서 고른 API 모델을 추가한다. 같은 그룹의 맨 뒤에 붙는다.
    최신 모델 대부분이 temperature 지정을 거부하므로(Gemini 3+, GPT-5+, Claude 4.7+) 보내지 않는다.
    """
    sort_order = await conn.fetchval(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM llm_models WHERE group_name = $1 AND NOT (id LIKE 'ollama/%')",
        group,
    )
    await conn.execute(
        """INSERT INTO llm_models (id, label, group_name, sort_order, enabled, is_default,
                                   fixed_temperature, options, is_custom, created_by)
           VALUES ($1, $2, $3, $4, TRUE, FALSE, TRUE, '{}'::jsonb, TRUE, $5)""",
        model_id, label, group, sort_order, created_by,
    )
    await conn.execute("DELETE FROM llm_models_removed WHERE id = $1", model_id)


async def remove_model(conn, model_id: str) -> str:
    """
    모델을 목록에서 지운다. 사용자가 추가한 모델은 그냥 지우고, 기본(seed) 모델은 지운 것을 기억해
    seed 동기화가 되살리지 않게 한다. 반환: 'custom' | 'default' | 'missing'
    """
    row = await conn.fetchrow("SELECT is_custom FROM llm_models WHERE id = $1", model_id)
    if row is None:
        return "missing"
    async with conn.transaction():
        await conn.execute("DELETE FROM llm_models WHERE id = $1", model_id)
        if not row["is_custom"]:
            await conn.execute("INSERT INTO llm_models_removed (id) VALUES ($1) ON CONFLICT DO NOTHING", model_id)
    return "custom" if row["is_custom"] else "default"


async def removed_model_ids(conn) -> set[str]:
    return {r["id"] for r in await conn.fetch("SELECT id FROM llm_models_removed")}


async def restore_defaults(conn, model_ids: list[str]) -> int:
    """삭제했던 기본 모델을 seed JSON 값으로 되살린다. 되살린 개수를 반환한다."""
    seed = {m["id"]: m for m in read_seed()}
    targets = [seed[i] for i in model_ids if i in seed]
    if not targets:
        return 0
    async with conn.transaction():
        # is_default는 건드리지 않는다 (지금 기본 모델을 그대로 둔다)
        await upsert_models(conn, [{**m, "is_default": False} for m in targets])
        await conn.execute("DELETE FROM llm_models_removed WHERE id = ANY($1::text[])", [m["id"] for m in targets])
    return len(targets)


async def seed_if_empty(pool) -> int:
    """llm_models가 비어 있을 때만 seed JSON으로 채운다. 채운 모델 수를 반환한다."""
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM llm_models)"):
            return 0
        models = read_seed()
        async with conn.transaction():
            await upsert_models(conn, models)
        return len(models)
