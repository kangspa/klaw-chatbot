"""
SQLite 연결 및 스키마 관리.

역할 분담:
  - Qdrant  : AI가 과거 대화를 '의미 검색'하기 위한 벡터 저장소 (turn 단위 1 point)
  - SQLite  : 사용자에게 그대로 보여줄 세션/메시지 원문 저장소 (진실 원본)
  - LangGraph checkpointer: 진행 중인 대화의 실행 상태 (SQLite data/checkpoints.db — 앱을 다시 켜도 남는다)

세 저장소는 chat_messages.turn_id(= Qdrant point id) 와
chat_messages.graph_message_id(= LangGraph state 상의 message id)로 서로 연결된다.
이 두 컬럼 덕분에 삭제 / 브랜치 / 재생성이 세 저장소에서 동일하게 반영될 수 있다.

설치 없이 실행되는 데스크톱 앱이라 DB 서버 대신 파일 하나(SQLite)를 쓴다.
  - 위치: KLAW_DB_PATH 환경변수, 없으면 <앱 폴더>/data/klaw.db (api/stack.py의 DATA_DIR)
  - 호출부는 예전 asyncpg 코드 그대로 쓸 수 있게 asyncpg와 같은 모양의 얇은 래퍼를 제공한다:
      async with get_pool().acquire() as conn:
          await conn.fetch / fetchrow / fetchval / execute / executemany(...)
          async with conn.transaction(): ...
    · 자리표시자는 asyncpg처럼 $1, $2 ... (같은 번호 재사용·순서 무관 가능) → 내부에서 ? 로 바꾼다.
    · `col = ANY($n)` 에 리스트를 넘기면 `col IN (?, ?, ...)` 로 펼친다.
    · 행은 row["col"] / row[0] / dict(row) 를 모두 지원한다.
    · 값 변환: *_at 컬럼(ISO-8601 UTC 문자열) → datetime(UTC), 불리언 컬럼(0/1) → bool,
      JSON 컬럼(options·summary) → dict. 파라미터의 datetime은 같은 형식의 문자열로 저장한다.
  - 연결은 하나를 공유하고, 쓰기가 섞이지 않도록 문장·트랜잭션 단위로 asyncio.Lock을 잡는다.
    transaction() 블록은 끝날 때까지 연결을 독점한다 (같은 태스크 안의 중첩 호출은 SAVEPOINT).
"""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import aiosqlite

from api import stack

# SQL 안에서 '지금'을 나타내는 식. 모든 시각은 이 형식(UTC, 밀리초, Z)의 문자열로 저장해 문자열 비교로 정렬·범위 조회한다.
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def db_path() -> Path:
    """DB 파일 경로. KLAW_DB_PATH가 있으면 그 값, 없으면 <앱 폴더>/data/klaw.db (stack.DATA_DIR)."""
    env = os.getenv("KLAW_DB_PATH")
    return Path(env) if env else stack.DATA_DIR / "klaw.db"


SCHEMA_SQL = f"""
-- 사용자. 개인 PC에서 띄워 쓰는 서비스라 로그인 없이 '로컬 사용자'(is_local) 하나로 동작한다.
-- Google 계정은 선택 사항이며, 연결하면 같은 로컬 사용자 행에 google_sub·이메일·이름·사진을 채운다
-- (로그인 전후로 사용자가 바뀌지 않아 대화·설정이 그대로 이어진다). 내부에서는 UUID(id)만 쓴다.
-- 시각 컬럼(*_at)은 ISO-8601 UTC 문자열, 불리언은 0/1 정수, JSON은 문자열(TEXT)로 저장한다.
CREATE TABLE IF NOT EXISTS users (
    id             TEXT PRIMARY KEY,          -- 내부 사용자 UUID (chat_sessions.owner_id가 참조)
    google_sub     TEXT UNIQUE,               -- Google 계정 고유 식별자 (연결했을 때만)
    email          TEXT,
    email_verified INTEGER NOT NULL DEFAULT 0,
    name           TEXT NOT NULL,
    picture        TEXT,
    is_local       INTEGER NOT NULL DEFAULT 0, -- 로컬 사용자 표시 (하나만 둔다)
    created_at     TEXT NOT NULL DEFAULT ({NOW}),
    updated_at     TEXT NOT NULL DEFAULT ({NOW}),
    last_login_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_local ON users (is_local) WHERE is_local;

CREATE TABLE IF NOT EXISTS chat_sessions (
    id                       TEXT PRIMARY KEY,              -- = LangGraph thread_id = Qdrant payload.thread_id
    owner_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title                    TEXT NOT NULL DEFAULT '새 대화',
    created_at               TEXT NOT NULL DEFAULT ({NOW}),
    updated_at               TEXT NOT NULL DEFAULT ({NOW}),
    parent_session_id        TEXT,                          -- 브랜치로 만들어졌다면 원본 세션
    branched_from_message_id TEXT,                          -- 브랜치 기준이 된 원본 메시지
    -- 세션 요약 (대시보드 표시용). LangGraph 상태의 요약(turn_memory·long_memory)은 서버 재시작 시 사라지므로
    -- 턴이 끝날 때마다 여기에 복사해 둔다. JSON {{"overall": 전체 요약 | null, "points": [...]}}
    summary                  TEXT,
    summary_updated_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_owner
    ON chat_sessions (owner_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL,                      -- 세션 내 표시 순서
    role             TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content          TEXT NOT NULL,
    model            TEXT,
    turn_id          TEXT,                                  -- Qdrant point id (user/assistant 한 쌍이 공유)
    graph_message_id TEXT,                                  -- LangGraph state의 message id
    created_at       TEXT NOT NULL DEFAULT ({NOW}),
    updated_at       TEXT NOT NULL DEFAULT ({NOW})
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_messages_session_seq
    ON chat_messages (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_chat_messages_turn
    ON chat_messages (session_id, turn_id);

-- 턴별 모델 사용량·비용 (대시보드 집계용). 한 턴 안에서 같은 모델 호출은 1행으로 합친다.
-- 세션을 지워도 이미 발생한 비용 기록은 남기므로 session_id에는 FK를 걸지 않는다 (사용자 삭제 시에는 삭제).
CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id        TEXT,
    turn_id           TEXT,
    model             TEXT NOT NULL,                     -- llm_models.id ('접두사/모델명')
    calls             INTEGER NOT NULL,                  -- 이 턴에서 해당 모델을 호출한 횟수 (요약·검색 노드 포함)
    input_tokens      INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL,
    cost_usd          REAL NOT NULL,                     -- 호출 시점 단가로 계산한 예상 비용
    created_at        TEXT NOT NULL DEFAULT ({NOW})
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_user_time ON llm_usage (user_id, created_at);

-- 답변을 위해 실제로 조회된 법령 (대시보드 '자주 조회한 법령'용). 법령 도구 호출이 성공한 것만 기록한다.
-- 대화에서 파생된 정보라 세션을 지우면 함께 지운다.
CREATE TABLE IF NOT EXISTS law_lookups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES chat_sessions(id) ON DELETE CASCADE,
    turn_id    TEXT,
    law_name   TEXT NOT NULL,                            -- 예: 근로기준법
    article    TEXT,                                     -- 조문 단위 조회일 때 (예: 제60조)
    tool       TEXT NOT NULL,                            -- 조회에 쓰인 korean-law-mcp 도구
    created_at TEXT NOT NULL DEFAULT ({NOW})
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
    sort_order        INTEGER NOT NULL DEFAULT 0,       -- 그룹 순서와 그룹 내 순서 모두 이 값 기준
    enabled           INTEGER NOT NULL DEFAULT 1,       -- 0이면 목록에서 숨기고 호출도 거부
    is_default        INTEGER NOT NULL DEFAULT 0,       -- 요청에 모델이 없을 때 사용할 기본 모델 (1개)
    fixed_temperature INTEGER NOT NULL DEFAULT 0,       -- 1이면 temperature를 보내지 않음 (Gemini 3+ / GPT-5+ / Claude 5+ 등)
    options           TEXT NOT NULL DEFAULT '{{}}',     -- JSON. 제공자 모델 생성자에 그대로 넘길 추가 인자 (예: {{"num_ctx": 32768}})
    -- 커스텀 모델: 사용자가 마이 페이지에서 추가한 모델 (Ollama 모델명 입력 또는 API 카탈로그).
    -- seed JSON에 없으므로 sync_llm_models.py --prune 대상에서 제외한다.
    is_custom         INTEGER NOT NULL DEFAULT 0,
    created_by        TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at        TEXT NOT NULL DEFAULT ({NOW}),
    updated_at        TEXT NOT NULL DEFAULT ({NOW})
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_models_default
    ON llm_models (is_default) WHERE is_default;

-- 마이 페이지에서 삭제한 기본(seed) 모델. sync_llm_models.py가 seed JSON으로 되살리지 않도록 기억해 둔다.
-- 마이 페이지의 '기본 모델 복원'이 여기서 지우고 seed JSON 값으로 다시 넣는다.
CREATE TABLE IF NOT EXISTS llm_models_removed (
    id         TEXT PRIMARY KEY,
    removed_at TEXT NOT NULL DEFAULT ({NOW})
);

-- 사용자가 마이 페이지에서 '사용 안 함'으로 둔 모델. 행이 있으면 꺼진 것이고, 없으면 켜진 것이다.
-- (새 모델이 추가되면 별도 작업 없이 모두에게 켜진 상태로 보이게 하려고 '끈 것'만 저장한다)
-- 프론트의 모델 선택 목록에만 반영한다 — 백엔드는 이 설정으로 호출을 막지 않는다.
CREATE TABLE IF NOT EXISTS user_disabled_models (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model_id   TEXT NOT NULL REFERENCES llm_models(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT ({NOW}),
    PRIMARY KEY (user_id, model_id)
);
"""

# 읽을 때 Python 타입으로 되돌릴 컬럼 (SQLite에는 불리언·JSON 타입이 없다)
_BOOL_COLS = frozenset({"email_verified", "is_local", "enabled", "is_default", "fixed_temperature", "is_custom"})
_JSON_COLS = frozenset({"options", "summary"})


# ────────────────────────── 값 변환 ──────────────────────────
def to_db_time(dt: datetime) -> str:
    """datetime → NOW와 같은 형식의 UTC 문자열 (문자열 비교가 시각 비교와 같아지도록)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _to_param(v: Any) -> Any:
    if isinstance(v, datetime):
        return to_db_time(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, UUID):
        return str(v)
    return v


def _from_db(col: str, v: Any) -> Any:
    if v is None:
        return None
    if col in _BOOL_COLS:
        return bool(v)
    if col.endswith("_at") and isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v)
        except ValueError:
            return v
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if col in _JSON_COLS and isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


class Record(dict):
    """asyncpg.Record처럼 row["col"]와 row[0]을 모두 지원하는 행. dict(row)도 그대로 된다."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


# `col = ANY($n)` (선택적 ::type[] 캐스트 포함) 또는 일반 `$n`
_PARAM_RE = re.compile(r"=\s*ANY\s*\(\s*\$(\d+)(?:\s*::\s*\w+\s*\[\])?\s*\)|\$(\d+)", re.IGNORECASE)


def translate(sql: str, args: tuple) -> tuple[str, list]:
    """asyncpg식 $n 자리표시자를 sqlite3의 ? 로 바꾸고 인자를 등장 순서대로 펼친다 (재사용·순서 무관 지원)."""
    params: list = []

    def sub(m: re.Match) -> str:
        if m.group(1) is not None:   # = ANY($n) → IN (?, ?, ...)
            values = args[int(m.group(1)) - 1]
            values = list(values or [])
            params.extend(_to_param(v) for v in values)
            return "IN (" + ", ".join("?" * len(values)) + ")"
        params.append(_to_param(args[int(m.group(2)) - 1]))
        return "?"

    return _PARAM_RE.sub(sub, sql), params


def _status(sql: str, rowcount: int) -> str:
    """asyncpg execute()가 돌려주는 상태 문자열 흉내 ('UPDATE 1', 'DELETE 0', 'INSERT 0 1' ...)."""
    verb = (sql.lstrip().split(None, 1) or [""])[0].upper()
    n = max(rowcount, 0)
    return f"INSERT 0 {n}" if verb == "INSERT" else f"{verb} {n}"


# ────────────────────────── 연결 래퍼 ──────────────────────────
class _Database:
    """공유 SQLite 연결 하나 + 태스크 단위 재진입 잠금."""

    def __init__(self, conn: aiosqlite.Connection, path: Path):
        self.conn = conn
        self.path = path
        self.lock = asyncio.Lock()
        self.owner: Optional[asyncio.Task] = None   # 지금 연결을 쥔 태스크
        self.tx_depth = 0
        self._sp_seq = 0

    @asynccontextmanager
    async def exclusive(self):
        """문장 하나를 실행하는 동안 연결을 잡는다. 이미 이 태스크가 트랜잭션으로 쥐고 있으면 그대로 쓴다."""
        task = asyncio.current_task()
        if self.owner is not None and self.owner is task:
            yield
            return
        async with self.lock:
            self.owner = task
            try:
                yield
            finally:
                self.owner = None

    async def run(self, sql: str, args: tuple) -> tuple[list[Record], int]:
        q, params = translate(sql, args)
        async with self.exclusive():
            cur = await self.conn.execute(q, params)
            try:
                raw = await cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
                rowcount = cur.rowcount
            finally:
                await cur.close()
        rows = [Record((c, _from_db(c, v)) for c, v in zip(cols, r)) for r in raw]
        return rows, rowcount


class _Transaction:
    def __init__(self, db: _Database):
        self.db = db
        self.savepoint: Optional[str] = None
        self.locked = False

    async def __aenter__(self):
        db = self.db
        task = asyncio.current_task()
        if db.owner is task and db.tx_depth > 0:
            # 같은 태스크 안의 중첩 트랜잭션 → SAVEPOINT (asyncpg와 같은 동작)
            db._sp_seq += 1
            self.savepoint = f"sp_{db._sp_seq}"
            await db.conn.execute(f"SAVEPOINT {self.savepoint}")
        else:
            await db.lock.acquire()
            self.locked = True
            db.owner = task
            try:
                await db.conn.execute("BEGIN IMMEDIATE")
            except BaseException:
                db.owner = None
                db.lock.release()
                self.locked = False
                raise
        db.tx_depth += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        db = self.db
        db.tx_depth -= 1
        if self.savepoint:
            if exc_type is not None:
                await db.conn.execute(f"ROLLBACK TO {self.savepoint}")
            await db.conn.execute(f"RELEASE {self.savepoint}")
            return False
        try:
            if exc_type is not None:
                await db.conn.execute("ROLLBACK")
            else:
                try:
                    await db.conn.execute("COMMIT")
                except BaseException:
                    await db.conn.execute("ROLLBACK")
                    raise
        finally:
            db.owner = None
            db.lock.release()
            self.locked = False
        return False


class Connection:
    """asyncpg.Connection과 같은 이름의 메서드를 제공하는 얇은 래퍼 (실제 연결은 공유)."""

    def __init__(self, db: _Database):
        self._db = db

    async def fetch(self, sql: str, *args) -> list[Record]:
        rows, _ = await self._db.run(sql, args)
        return rows

    async def fetchrow(self, sql: str, *args) -> Optional[Record]:
        rows, _ = await self._db.run(sql, args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args, column: int = 0) -> Any:
        rows, _ = await self._db.run(sql, args)
        return rows[0][column] if rows else None

    async def execute(self, sql: str, *args) -> str:
        _, rowcount = await self._db.run(sql, args)
        return _status(sql, rowcount)

    async def executemany(self, sql: str, args_list) -> None:
        async with self.transaction():
            for args in args_list:
                await self._db.run(sql, tuple(args))

    def transaction(self) -> _Transaction:
        return _Transaction(self._db)

    async def executescript(self, script: str) -> None:
        async with self._db.exclusive():
            await self._db.conn.executescript(script)


class Pool:
    """asyncpg.Pool 흉내: acquire()는 공유 연결 위의 Connection 래퍼를 돌려준다."""

    def __init__(self, db: _Database):
        self._db = db

    @property
    def path(self) -> Path:
        return self._db.path

    @asynccontextmanager
    async def acquire(self):
        yield Connection(self._db)

    def connection(self) -> Connection:
        """async with 없이 쓰는 연결 (스크립트용). 실제 연결은 공유라 따로 반납할 필요가 없다."""
        return Connection(self._db)

    async def fetch(self, sql: str, *args):
        return await Connection(self._db).fetch(sql, *args)

    async def fetchrow(self, sql: str, *args):
        return await Connection(self._db).fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args):
        return await Connection(self._db).fetchval(sql, *args)

    async def execute(self, sql: str, *args):
        return await Connection(self._db).execute(sql, *args)

    async def close(self) -> None:
        await self._db.conn.close()


async def connect(path: Optional[Path] = None) -> Pool:
    """DB 파일을 열고(없으면 생성) 스키마를 보장한 Pool을 돌려준다. 스크립트에서도 쓴다."""
    path = Path(path) if path else db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: 자동 커밋. 트랜잭션은 transaction()이 BEGIN/COMMIT으로 직접 관리한다.
    conn = await aiosqlite.connect(str(path), isolation_level=None)
    try:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.executescript(SCHEMA_SQL)
    except BaseException:
        await conn.close()
        raise
    return Pool(_Database(conn, path))


# 전역 풀 (main.py lifespan에서 init_pool로 생성)
_POOL: Optional[Pool] = None


async def init_pool(retries: int = 5, delay: float = 2.0) -> Optional[Pool]:
    """
    서버 startup에서 1회 호출. DB 파일을 열고 스키마를 보장한다.
    파일이 잠겨 있는 등 일시적인 실패에 대비해 몇 번 재시도한다.
    실패해도 서버는 뜨지만, 세션 관련 엔드포인트는 503을 반환한다.
    """
    global _POOL
    if _POOL is not None:
        return _POOL

    path = db_path()
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            _POOL = await connect(path)
            print(f"[DB] SQLite 준비 완료 ({path})")
            return _POOL
        except Exception as e:
            last_err = e
            print(f"[DB] SQLite 열기 실패 ({attempt}/{retries}): {type(e).__name__}: {e}")
            if attempt < retries:
                await asyncio.sleep(delay)

    print(f"[DB] SQLite를 열지 못했습니다. 경로·권한을 확인하세요: {path} (마지막 오류: {last_err!r})")
    _POOL = None
    return None


async def close_pool() -> None:
    """서버 shutdown에서 호출."""
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None
        print("[DB] SQLite 연결 종료")


def get_pool() -> Pool:
    """풀을 반환. 초기화 실패 상태면 RuntimeError를 던진다."""
    if _POOL is None:
        raise RuntimeError(
            "DB 연결이 초기화되지 않았습니다. 서버 로그의 [DB] 항목을 확인하세요."
        )
    return _POOL


def is_ready() -> bool:
    return _POOL is not None
