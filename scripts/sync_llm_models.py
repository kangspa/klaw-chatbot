"""
seed_models.json → SQLite(llm_models) 동기화 스크립트.

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
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# api/__init__.py는 .env와 서비스 주소만 읽는 가벼운 초기화라 그대로 import한다 (라우터·LLM은 끌려오지 않는다)
from api.db import connect, db_path  # noqa: E402
from api.llm_gateway.store import SEED_PATH, read_seed, upsert_models  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description="seed_models.json을 llm_models 테이블에 반영")
    parser.add_argument("--file", type=Path, default=SEED_PATH, help="반영할 JSON 경로")
    parser.add_argument("--prune", action="store_true", help="JSON에 없는 모델을 DB에서 삭제")
    parser.add_argument("--yes", action="store_true", help="확인 없이 실행")
    args = parser.parse_args()

    models = read_seed(args.file)
    json_ids = [m["id"] for m in models]

    pool = await connect()   # DB 파일·테이블이 없으면 생성
    conn = pool.connection()
    try:
        rows = await conn.fetch("SELECT id, is_custom FROM llm_models")
        db_ids = {r["id"] for r in rows}
        # 마이 페이지에서 삭제한 기본 모델은 되살리지 않는다 (복원은 마이 페이지 '기본 모델 복원')
        removed = {r["id"] for r in await conn.fetch("SELECT id FROM llm_models_removed")}
        if removed & set(json_ids):
            print(f"  마이 페이지에서 삭제한 기본 모델 {len(removed & set(json_ids))}개: 다시 넣지 않음 {sorted(removed & set(json_ids))}")
        models = [m for m in models if m["id"] not in removed]
        json_ids = [m["id"] for m in models]
        # 커스텀 모델은 사용자가 마이 페이지에서 추가한 것이라 JSON에 없는 게 정상이다 → --prune에서도 지키지 않는다
        custom_ids = {r["id"] for r in rows if r["is_custom"]}
        added = [i for i in json_ids if i not in db_ids]
        updated = [i for i in json_ids if i in db_ids]
        missing = sorted(db_ids - set(json_ids) - custom_ids)

        print(f"대상 DB : {db_path()}")
        print(f"JSON    : {args.file}")
        print(f"  추가 {len(added)}개: {added or '-'}")
        print(f"  갱신 {len(updated)}개")
        print(f"  JSON에 없는 모델 {len(missing)}개: {missing or '-'} → {'삭제' if args.prune else '유지'}")
        if custom_ids:
            print(f"  커스텀 모델 {len(custom_ids)}개: 마이 페이지에서 관리 (동기화 대상 아님)")

        if not args.yes and input("반영하려면 'yes' 입력: ").strip() != "yes":
            print("취소했습니다.")
            return

        async with conn.transaction():
            await upsert_models(conn, models)
            if args.prune and missing:
                await conn.execute("DELETE FROM llm_models WHERE id = ANY($1)", missing)

        rows = await conn.fetch(
            "SELECT id, enabled, is_default FROM llm_models ORDER BY sort_order, id"
        )
        print("\n반영 완료. 현재 DB 모델:")
        for r in rows:
            flags = ("기본 " if r["is_default"] else "") + ("" if r["enabled"] else "비활성")
            print(f"  - {r['id']}  {flags}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
