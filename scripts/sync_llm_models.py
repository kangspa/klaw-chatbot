"""
seed_models.json → Postgres(llm_models) 동기화 스크립트.

서버는 테이블이 비어 있을 때만 JSON으로 채운다. 이후 JSON을 고쳤다면 이 스크립트로 DB에 반영한다.
실행 중인 서버는 LLM_REGISTRY_TTL초(기본 30) 안에 바뀐 내용을 자동으로 다시 읽는다 (재시작 불필요).

  - JSON에 있는 모델: 추가하거나 갱신한다.
  - JSON에 없는 모델: 기본은 그대로 둔다. --prune을 주면 삭제한다.

사용법:
    uv run python scripts/sync_llm_models.py            # 변경 내용 확인 후 실행
    uv run python scripts/sync_llm_models.py --yes      # 확인 없이 실행
    uv run python scripts/sync_llm_models.py --prune    # JSON에 없는 모델까지 삭제
    uv run python scripts/sync_llm_models.py --file 다른경로.json
"""

import argparse
import asyncio
import os
import sys
import types
from pathlib import Path

# asyncpg가 Windows에서 SelectorEventLoop를 필요로 하므로 다른 import보다 먼저 정책 지정
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# api/__init__.py를 실행하지 않고 하위 모듈만 쓰기 위해 빈 패키지로 등록한다.
# (그대로 import하면 라우터·MCP 클라이언트·LLM 초기화까지 끌려 들어온다 — reset_db.py와 같은 이유)
_api_pkg = types.ModuleType("api")
_api_pkg.__path__ = [str(PROJECT_ROOT / "api")]
sys.modules["api"] = _api_pkg

import asyncpg  # noqa: E402

from api.db import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER, SCHEMA_SQL  # noqa: E402
from api.llm_gateway.store import SEED_PATH, read_seed, upsert_models  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description="seed_models.json을 llm_models 테이블에 반영")
    parser.add_argument("--file", type=Path, default=SEED_PATH, help="반영할 JSON 경로")
    parser.add_argument("--prune", action="store_true", help="JSON에 없는 모델을 DB에서 삭제")
    parser.add_argument("--yes", action="store_true", help="확인 없이 실행")
    args = parser.parse_args()

    models = read_seed(args.file)
    json_ids = [m["id"] for m in models]

    conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME,
                                 user=DB_USER, password=DB_PASSWORD)
    try:
        await conn.execute(SCHEMA_SQL)   # 테이블이 없으면 생성
        db_ids = {r["id"] for r in await conn.fetch("SELECT id FROM llm_models")}
        added = [i for i in json_ids if i not in db_ids]
        updated = [i for i in json_ids if i in db_ids]
        missing = sorted(db_ids - set(json_ids))

        print(f"대상 DB : {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
        print(f"JSON    : {args.file}")
        print(f"  추가 {len(added)}개: {added or '-'}")
        print(f"  갱신 {len(updated)}개")
        print(f"  JSON에 없는 모델 {len(missing)}개: {missing or '-'} → {'삭제' if args.prune else '유지'}")

        if not args.yes and input("반영하려면 'yes' 입력: ").strip() != "yes":
            print("취소했습니다.")
            return

        async with conn.transaction():
            await upsert_models(conn, models)
            if args.prune and missing:
                await conn.execute("DELETE FROM llm_models WHERE id = ANY($1::text[])", missing)

        rows = await conn.fetch(
            "SELECT id, enabled, is_default FROM llm_models ORDER BY sort_order, id"
        )
        print("\n반영 완료. 현재 DB 모델:")
        for r in rows:
            flags = ("기본 " if r["is_default"] else "") + ("" if r["enabled"] else "비활성")
            print(f"  - {r['id']}  {flags}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
