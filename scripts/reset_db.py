"""
저장소 완전 초기화 스크립트.

Google 로그인 도입으로 chat_sessions.owner_id의 의미가 바뀌었기 때문에
(이전: 이메일 문자열 / 이후: users.id UUID) 기존 데이터를 그대로 둘 수 없다.
이 스크립트는 세 저장소를 모두 비운다.

  - Postgres : users / chat_sessions / chat_messages 테이블 DROP 후 새 스키마로 재생성
               (llm_models / llm_model_prices는 설정 데이터라 지우지 않는다 — scripts/sync_llm_models.py 참고)
  - Qdrant   : conversation_history 컬렉션 삭제 (서버 startup에서 자동 재생성됨)
  - LangGraph: MemorySaver라 프로세스 메모리에만 있으므로 서버 재시작으로 비워짐

사용법:
    uv run python scripts/reset_db.py          # 확인 프롬프트 후 실행
    uv run python scripts/reset_db.py --yes    # 확인 없이 실행
"""

import asyncio
import importlib.util
import os
import sys

# psycopg/asyncpg가 Windows에서 SelectorEventLoop를 필요로 하므로 다른 import보다 먼저 정책 지정
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import asyncpg  # noqa: E402
import dotenv  # noqa: E402

dotenv.load_dotenv()


def load_schema_sql() -> str:
    """
    api/db.py의 SCHEMA_SQL을 그대로 재사용한다 (스키마 정의가 두 곳으로 갈라지지 않도록).

    `from api.db import SCHEMA_SQL`을 쓰지 않는 이유: 그러면 api/__init__.py가 실행되면서
    MCP 클라이언트·LLM 초기화까지 끌려 들어온다. 초기화 스크립트에는 불필요한 비용이라
    파일만 단독으로 로드한다.
    """
    path = os.path.join(PROJECT_ROOT, "api", "db.py")
    spec = importlib.util.spec_from_file_location("_klaw_db_schema", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"api/db.py를 불러오지 못했습니다: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SCHEMA_SQL

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "raglaw")
DB_USER = os.getenv("DB_USER", "raglaw")
DB_PASSWORD = os.getenv("DB_PASSWORD", "raglaw_dev_pw")

QDRANT_COLLECTION = "conversation_history"

DROP_SQL = """
DROP TABLE IF EXISTS chat_messages CASCADE;
DROP TABLE IF EXISTS chat_sessions CASCADE;
DROP TABLE IF EXISTS users CASCADE;
"""


async def reset_postgres() -> None:
    conn = await asyncpg.connect(
        host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )
    try:
        before = await conn.fetch(
            """
            SELECT tablename FROM pg_tables
             WHERE schemaname = 'public'
               AND tablename IN ('users', 'chat_sessions', 'chat_messages')
             ORDER BY tablename
            """
        )
        print(f"  기존 테이블: {[r['tablename'] for r in before] or '없음'}")

        await conn.execute(DROP_SQL)
        print("  테이블 DROP 완료")

        await conn.execute(load_schema_sql())
        print("  새 스키마 생성 완료 (users / chat_sessions / chat_messages)")

        for table in ("users", "chat_sessions", "chat_messages"):
            count = await conn.fetchval(f"SELECT count(*) FROM {table}")
            print(f"    - {table}: {count}행")
    finally:
        await conn.close()


async def reset_qdrant() -> None:
    from qdrant_client import AsyncQdrantClient

    client = AsyncQdrantClient(host="localhost", grpc_port=6334, prefer_grpc=True)
    try:
        if await client.collection_exists(QDRANT_COLLECTION):
            info = await client.get_collection(QDRANT_COLLECTION)
            print(f"  기존 point 수: {info.points_count}")
            await client.delete_collection(QDRANT_COLLECTION)
            print(f"  컬렉션 '{QDRANT_COLLECTION}' 삭제 완료 (서버 startup에서 자동 재생성)")
        else:
            print(f"  컬렉션 '{QDRANT_COLLECTION}' 없음 — 건너뜀")
    finally:
        await client.close()


async def main() -> None:
    print("=" * 60)
    print("저장소 완전 초기화")
    print("=" * 60)
    print(f"  Postgres : {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    print("  Qdrant   : localhost:6334 / conversation_history")
    print()

    if "--yes" not in sys.argv:
        answer = input("모든 회원과 대화 기록이 삭제됩니다. 계속하려면 'yes' 입력: ").strip()
        if answer != "yes":
            print("취소했습니다.")
            return

    print("\n[Postgres]")
    await reset_postgres()

    print("\n[Qdrant]")
    await reset_qdrant()

    print("\n초기화 완료. 서버를 재시작하면 LangGraph 대화 상태(MemorySaver)도 함께 비워집니다.")


if __name__ == "__main__":
    asyncio.run(main())
