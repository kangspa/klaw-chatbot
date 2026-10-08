"""
Postgres 연결 풀 및 스키마 관리.

역할 분담:
  - Qdrant  : AI가 과거 대화를 '의미 검색'하기 위한 벡터 저장소 (turn 단위 1 point)
  - Postgres: 사용자에게 그대로 보여줄 세션/메시지 원문 저장소 (진실 원본)
  - LangGraph checkpointer: 현재 진행 중인 대화의 실행 상태 (MemorySaver, 휘발성)

세 저장소는 chat_messages.turn_id(= Qdrant point id) 와
chat_messages.graph_message_id(= LangGraph state 상의 message id)로 서로 연결된다.
이 두 컬럼 덕분에 삭제 / 브랜치 / 재생성이 세 저장소에서 동일하게 반영될 수 있다.
"""

import os
import asyncio
import dotenv
import asyncpg

dotenv.load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "raglaw")
DB_USER = os.getenv("DB_USER", "raglaw")
DB_PASSWORD = os.getenv("DB_PASSWORD", "raglaw_dev_pw")

# 전역 풀 (main.py lifespan에서 init_pool로 생성)
_POOL: asyncpg.Pool | None = None


SCHEMA_SQL = """
-- 회원 정보. 로그인은 Google 계정 연동만 지원한다.
-- 이메일은 변경될 수 있으므로 Google이 발급하는 불변 식별자(sub)를 계정의 진짜 키로 삼고,
-- 애플리케이션 내부에서는 별도 UUID(id)를 사용해 외부 식별자와 분리한다.
CREATE TABLE IF NOT EXISTS users (
    id             TEXT PRIMARY KEY,          -- 내부 사용자 UUID (chat_sessions.owner_id가 참조)
    google_sub     TEXT UNIQUE NOT NULL,      -- Google 계정 고유 식별자 (변하지 않음)
    email          TEXT NOT NULL,
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    name           TEXT NOT NULL,
    picture        TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id                      TEXT PRIMARY KEY,               -- = LangGraph thread_id = Qdrant payload.thread_id
    owner_id                TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title                   TEXT NOT NULL DEFAULT '새 대화',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    parent_session_id       TEXT,                           -- 브랜치로 만들어졌다면 원본 세션
    branched_from_message_id TEXT                           -- 브랜치 기준이 된 원본 메시지
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_owner
    ON chat_sessions (owner_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    seq              BIGINT NOT NULL,                       -- 세션 내 표시 순서
    role             TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content          TEXT NOT NULL,
    model            TEXT,
    turn_id          TEXT,                                  -- Qdrant point id (user/assistant 한 쌍이 공유)
    graph_message_id TEXT,                                  -- LangGraph state의 message id
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_messages_session_seq
    ON chat_messages (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session
    ON chat_messages (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_chat_messages_turn
    ON chat_messages (session_id, turn_id);

-- 세션 요약 (대시보드 표시용). LangGraph 상태의 요약(turn_memory·long_memory)은 서버 재시작 시 사라지므로
-- 턴이 끝날 때마다 여기에 복사해 둔다. {"overall": 전체 요약 | null, "turns": [최근 턴 요약, ...]}
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS summary JSONB;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS summary_updated_at TIMESTAMPTZ;

-- 턴별 모델 사용량·비용 (대시보드 집계용). 한 턴 안에서 같은 모델 호출은 1행으로 합친다.
-- 세션을 지워도 이미 발생한 비용 기록은 남기므로 session_id에는 FK를 걸지 않는다 (회원 탈퇴 시에는 삭제).
CREATE TABLE IF NOT EXISTS llm_usage (
    id                BIGSERIAL PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id        TEXT,
    turn_id           TEXT,
    model             TEXT NOT NULL,                     -- llm_models.id ('접두사/모델명')
    calls             INT NOT NULL,                      -- 이 턴에서 해당 모델을 호출한 횟수 (요약·검색 노드 포함)
    input_tokens      BIGINT NOT NULL,
    cache_read_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens     BIGINT NOT NULL,
    cost_usd          NUMERIC(14, 8) NOT NULL,           -- 호출 시점 DB 단가로 계산한 예상 비용
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_user_time ON llm_usage (user_id, created_at);

-- 답변을 위해 실제로 조회된 법령 (대시보드 '자주 조회한 법령'용). 법령 도구 호출이 성공한 것만 기록한다.
-- 대화에서 파생된 정보라 세션을 지우면 함께 지운다.
CREATE TABLE IF NOT EXISTS law_lookups (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES chat_sessions(id) ON DELETE CASCADE,
    turn_id    TEXT,
    law_name   TEXT NOT NULL,                            -- 예: 근로기준법
    article    TEXT,                                     -- 조문 단위 조회일 때 (예: 제60조)
    tool       TEXT NOT NULL,                            -- 조회에 쓰인 korean-law-mcp 도구
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_law_lookups_user_time ON law_lookups (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_law_lookups_turn ON law_lookups (session_id, turn_id);

-- 호출 가능한 LLM 모델 목록 (api/llm_gateway가 읽음). 서버 재빌드 없이 모델을 추가·변경하기 위해 DB로 관리한다.
-- 테이블이 비어 있으면 서버 startup에서 api/llm_gateway/seed_models.json으로 채운다.
-- JSON을 고친 뒤 DB에 반영하려면: uv run python scripts/sync_llm_models.py
CREATE TABLE IF NOT EXISTS llm_models (
    id                TEXT PRIMARY KEY,                 -- '접두사/모델명' (API로 전달되는 값, 예: anthropic/claude-sonnet-5)
    label             TEXT NOT NULL,                    -- 선택 목록에 보일 이름
    group_name        TEXT NOT NULL,                    -- 선택 목록 그룹 (Google / OpenAI / Anthropic / 로컬 ...)
    sort_order        INT NOT NULL DEFAULT 0,           -- 그룹 순서와 그룹 내 순서 모두 이 값 기준
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,    -- FALSE면 목록에서 숨기고 호출도 거부
    is_default        BOOLEAN NOT NULL DEFAULT FALSE,   -- 요청에 모델이 없을 때 사용할 기본 모델 (1개)
    fixed_temperature BOOLEAN NOT NULL DEFAULT FALSE,   -- TRUE면 temperature를 보내지 않음 (Gemini 3+ / GPT-5+ / Claude 5+ 등)
    options           JSONB NOT NULL DEFAULT '{}'::jsonb, -- 제공자 모델 생성자에 그대로 넘길 추가 인자 (예: {"num_ctx": 32768})
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_models_default
    ON llm_models (is_default) WHERE is_default;

-- 토큰 단가는 LiteLLM 가격표로 계산한다 (api/llm_gateway/pricing.py). 예전 수동 단가 테이블은 정리한다.
DROP TABLE IF EXISTS llm_model_prices;
"""


async def init_pool(retries: int = 5, delay: float = 2.0) -> asyncpg.Pool | None:
    """
    서버 startup에서 1회 호출. 풀 생성 후 스키마를 보장한다.
    Postgres가 아직 뜨지 않았을 수 있으므로 몇 번 재시도한다.
    실패해도 서버는 뜨지만, 세션 관련 엔드포인트는 503을 반환한다.
    """
    global _POOL
    if _POOL is not None:
        return _POOL

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            _POOL = await asyncpg.create_pool(
                host=DB_HOST,
                port=DB_PORT,
                database=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                min_size=1,
                max_size=10,
                command_timeout=30,
            )
            async with _POOL.acquire() as conn:
                await conn.execute(SCHEMA_SQL)
            print(f"[Postgres] 연결 및 스키마 준비 완료 ({DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME})")
            return _POOL
        except Exception as e:
            last_err = e
            print(f"[Postgres] 연결 실패 ({attempt}/{retries}): {type(e).__name__}: {e}")
            if attempt < retries:
                await asyncio.sleep(delay)

    print(
        "[Postgres] 연결에 최종 실패했습니다. `docker compose up -d postgres` 및 .env의 "
        f"DB_* 값을 확인하세요. (마지막 오류: {last_err!r})"
    )
    _POOL = None
    return None


async def close_pool() -> None:
    """서버 shutdown에서 호출."""
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None
        print("[Postgres] 연결 풀 종료")


def get_pool() -> asyncpg.Pool:
    """풀을 반환. 초기화 실패 상태면 RuntimeError를 던진다."""
    if _POOL is None:
        raise RuntimeError(
            "Postgres 연결 풀이 초기화되지 않았습니다. 서버 로그의 [Postgres] 항목을 확인하세요."
        )
    return _POOL


def is_ready() -> bool:
    return _POOL is not None
