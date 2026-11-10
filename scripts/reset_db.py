"""
저장소 완전 초기화 스크립트.

Google 로그인 도입으로 chat_sessions.owner_id의 의미가 바뀌었기 때문에
(이전: 이메일 문자열 / 이후: users.id UUID) 기존 데이터를 그대로 둘 수 없다.
이 스크립트는 세 저장소를 모두 비운다.

  - SQLite   : 사용자·대화·대시보드 기록 테이블 DROP 후 새 스키마로 재생성
               (users / chat_sessions / chat_messages / user_disabled_models / llm_usage / law_lookups)
               (llm_models는 설정 데이터라 지우지 않는다 — scripts/sync_llm_models.py 참고)
  - Qdrant   : 로컬 모드 폴더(data/qdrant)의 대화 기록 컬렉션 전부 삭제 — 임베딩 방식마다 하나씩
               (conversation_history = gemini, conversation_history__local, conversation_history__ollama_qwen3)
               서버 startup에서 지금 고른 방식의 컬렉션이 자동 재생성된다. 임베딩 모델 파일(data/embeddings)·선택(.env)은 남긴다.
  - LangGraph: MemorySaver라 프로세스 메모리에만 있으므로 서버 재시작으로 비워짐

반드시 앱(서버)을 끈 상태에서 실행한다. Qdrant 로컬 모드 폴더는 한 프로세스만 열 수 있다.

사용법:
    uv run python scripts/reset_db.py          # 확인 프롬프트 후 실행
    uv run python scripts/reset_db.py --yes    # 확인 없이 실행
"""

import asyncio
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# api/__init__.py는 .env와 서비스 주소만 읽는 가벼운 초기화라 그대로 import한다 (라우터·LLM은 끌려오지 않는다)
from api import stack  # noqa: E402
from api.db import SCHEMA_SQL, connect, db_path  # noqa: E402

# 임베딩 방식별 컬렉션이 모두 이 이름으로 시작한다 (api/llm_gateway/embeddings.py PROVIDERS)
QDRANT_COLLECTION_PREFIX = "conversation_history"

# 지울 테이블 (자식 → 부모 순서). llm_models·llm_models_removed는 설정이라 남긴다.
RESET_TABLES = ("user_disabled_models", "law_lookups", "llm_usage", "chat_messages", "chat_sessions", "users")


async def reset_sqlite() -> None:
    pool = await connect()
    try:
        async with pool.acquire() as conn:
            before = await conn.fetch(
                f"""SELECT name FROM sqlite_master WHERE type = 'table'
                     AND name IN ({", ".join("'" + t + "'" for t in RESET_TABLES)}) ORDER BY name"""
            )
            print(f"  기존 테이블: {[r['name'] for r in before] or '없음'}")

            # 부모(users)를 지울 때 FK 동작(CASCADE·SET NULL)이 남은 테이블에 번지지 않게 잠시 끈다
            await conn.execute("PRAGMA foreign_keys=OFF")
            try:
                await conn.executescript("".join(f"DROP TABLE IF EXISTS {t};\n" for t in RESET_TABLES)
                                         + "UPDATE llm_models SET created_by = NULL;\n")
            finally:
                await conn.execute("PRAGMA foreign_keys=ON")
            print("  테이블 DROP 완료")

            await conn.executescript(SCHEMA_SQL)
            print("  새 스키마 생성 완료")

            for table in ("users", "chat_sessions", "chat_messages"):
                count = await conn.fetchval(f"SELECT count(*) FROM {table}")
                print(f"    - {table}: {count}행")
    finally:
        await pool.close()


async def reset_qdrant() -> None:
    from qdrant_client import AsyncQdrantClient

    try:
        client = AsyncQdrantClient(path=str(stack.QDRANT_PATH))
    except Exception as e:   # 다른 프로세스(실행 중인 앱)가 폴더를 쥐고 있으면 열리지 않는다
        raise SystemExit(f"  Qdrant 로컬 폴더를 열 수 없습니다 — 앱을 먼저 종료하세요: {type(e).__name__}: {e}")
    try:
        names = sorted(c.name for c in (await client.get_collections()).collections
                       if c.name.startswith(QDRANT_COLLECTION_PREFIX))
        if not names:
            print("  대화 기록 컬렉션 없음 — 건너뜀")
        for name in names:
            info = await client.get_collection(name)
            await client.delete_collection(name)
            print(f"  컬렉션 '{name}' 삭제 완료 (point {info.points_count}개)")
        if names:
            print("  (서버 startup에서 지금 고른 임베딩 방식의 컬렉션이 자동 재생성)")
    finally:
        await client.close()


async def main() -> None:
    print("=" * 60)
    print("저장소 완전 초기화")
    print("=" * 60)
    print(f"  SQLite   : {db_path()}")
    print(f"  Qdrant   : {stack.QDRANT_PATH} / {QDRANT_COLLECTION_PREFIX}* (임베딩 방식별 컬렉션 전부)")
    print("  ※ 앱(서버)이 실행 중이면 먼저 종료하세요 (Qdrant 로컬 폴더는 한 프로세스만 열 수 있다).")
    print()

    if "--yes" not in sys.argv:
        answer = input("모든 회원과 대화 기록이 삭제됩니다. 계속하려면 'yes' 입력: ").strip()
        if answer != "yes":
            print("취소했습니다.")
            return

    print("\n[SQLite]")
    await reset_sqlite()

    print("\n[Qdrant]")
    await reset_qdrant()

    print("\n초기화 완료. 서버를 재시작하면 LangGraph 대화 상태(MemorySaver)도 함께 비워집니다.")


if __name__ == "__main__":
    asyncio.run(main())
