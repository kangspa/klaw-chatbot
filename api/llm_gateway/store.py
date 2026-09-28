"""
llm_models / llm_model_prices 테이블 입출력과 seed JSON 반영.

테이블 스키마는 api/db.py의 SCHEMA_SQL에 있다.
연결(asyncpg Pool/Connection)은 호출부가 넘겨준다 — 게이트웨이가 풀 수명주기를 알 필요가 없도록.
"""
import json
from pathlib import Path
from typing import Any, Optional

from api.llm_gateway.pricing import PriceRow
from api.llm_gateway.providers import split_model_id
from api.llm_gateway.registry import ModelSpec

SEED_PATH = Path(__file__).with_name("seed_models.json")


def _num(v: Any) -> Optional[float]:
    return None if v is None else float(v)


async def load_models(conn) -> list[ModelSpec]:
    """DB의 모든 모델(비활성 포함)과 단가를 읽는다."""
    model_rows = await conn.fetch(
        """SELECT id, label, group_name, sort_order, enabled, is_default, fixed_temperature, options
             FROM llm_models ORDER BY sort_order, id"""
    )
    price_rows = await conn.fetch(
        """SELECT model_id, above_input_tokens, input_per_mtok, output_per_mtok,
                  cache_read_per_mtok, cache_write_per_mtok
             FROM llm_model_prices"""
    )
    prices: dict[str, list[PriceRow]] = {}
    for p in price_rows:
        prices.setdefault(p["model_id"], []).append(PriceRow(
            input=float(p["input_per_mtok"]), output=float(p["output_per_mtok"]),
            cache_read=_num(p["cache_read_per_mtok"]), cache_write=_num(p["cache_write_per_mtok"]),
            above_input_tokens=p["above_input_tokens"],
        ))

    specs = []
    for r in model_rows:
        options = r["options"]
        if isinstance(options, str):   # asyncpg는 기본 설정에서 JSONB를 문자열로 돌려준다
            options = json.loads(options)
        specs.append(ModelSpec(
            id=r["id"], label=r["label"], group=r["group_name"], sort_order=r["sort_order"],
            enabled=r["enabled"], is_default=r["is_default"], fixed_temperature=r["fixed_temperature"],
            options=options or {}, prices=tuple(prices.get(r["id"], [])),
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
    JSON의 모델을 DB에 반영한다(있으면 갱신, 없으면 추가). 해당 모델의 단가 행은 통째로 교체한다.
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
        await conn.execute("DELETE FROM llm_model_prices WHERE model_id = $1", m["id"])
        for p in m.get("prices") or []:
            await conn.execute(
                """INSERT INTO llm_model_prices (model_id, above_input_tokens,
                       input_per_mtok, output_per_mtok, cache_read_per_mtok, cache_write_per_mtok)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                m["id"], p.get("above_input_tokens", 0),
                p["input"], p["output"], p.get("cache_read"), p.get("cache_write"),
            )


async def seed_if_empty(pool) -> int:
    """llm_models가 비어 있을 때만 seed JSON으로 채운다. 채운 모델 수를 반환한다."""
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM llm_models)"):
            return 0
        models = read_seed()
        async with conn.transaction():
            await upsert_models(conn, models)
        return len(models)
