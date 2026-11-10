# ─── Windows asyncio 이벤트 루프 정책 ───────────────────────────
# 반드시 다른 import보다 먼저! 이유:
#   - 아래 from api.load_variable import 가 평가되면 그 안에서
#     asyncio.run(MCP_CLIENT.get_tools())이 호출되면서 임시 루프를 만든다.
#     정책 변경은 그 호출보다 앞서야 한다.
import sys
import asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Windows 콘솔(cp949)에서 em-dash 등 비-cp949 문자가 섞인 로그(print)가
# UnicodeEncodeError로 죽는 것을 방지 — 에러 로깅이 에러를 내면 원인 추적이 불가능해진다.
# (외부 API 에러 메시지에는 em-dash/이모지가 흔히 포함된다)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
# ──────────────────────────────────────────────────────────────────

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn
import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

# 자식 프로세스(법령 MCP·Ollama 엔진)를 먼저 띄운다 — 아래 api.routes → load_variable이 import 시점에
# 법령 MCP 도구 목록을 읽기 때문이다. `import api`가 앱 폴더의 .env를 읽고 포트를 정한다 (api/__init__.py).
from api import runtime_control, services, stack
services.start_core()

from api.routes import api_router
from api.db import close_pool, init_pool
from api.llm_gateway.local_runtime import report_local_runtime
from api.llm_gateway.store import seed_if_empty as seed_llm_models_if_empty
from api.load_variable import MODEL_REGISTRY
from api.chatbot.functions.agent import build_chatbot_agent
from api.chatbot.functions.law_auth import preflight_law_auth
from api.chatbot.functions.summary import ensure_conversation_history_collection
from api.auth.user_store import ensure_local_user

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ───── startup ─────
    # 마이 페이지의 LiteLLM 버전 변경이 다른 스레드에서 서버 종료를 요청할 수 있도록 루프를 등록한다
    runtime_control.bind_loop(asyncio.get_running_loop())
    # 저장소 3종 준비
    #  - SQLite   : 사용자에게 보여줄 세션/메시지 원문 (진실 원본, data/klaw.db)
    #  - Qdrant   : AI가 과거 대화를 의미 검색하기 위한 벡터 (1턴 = 1 point, 로컬 모드 data/qdrant)
    #  - SQLite 체크포인터 : LangGraph 실행 상태 (최근 메시지·턴/단기/장기 요약·엔티티 기억, data/checkpoints.db)
    #                       앱을 껐다 켜도 이어진다. 대화마다 최근 체크포인트 몇 개만 남긴다 (graph_ops.prune_thread)
    pool = await init_pool()
    # 로그인 없이 쓰는 개인 PC용 서비스 — 로컬 사용자를 미리 만들어 둔다 (Google 계정 연결은 선택, api/auth/user_store.py)
    if pool is not None:
        local_user = await ensure_local_user()
        print(f"[auth] 로컬 사용자: {local_user['name']}"
              + (f" (Google 연결: {local_user['email']})" if local_user["googleLinked"] else " (Google 미연결)"))
    # LLM 모델 목록: 테이블이 비어 있을 때만 seed JSON으로 채우고, 레지스트리를 미리 읽어둔다
    if pool is not None:
        try:
            seeded = await seed_llm_models_if_empty(pool)
            if seeded:
                print(f"[llm_gateway] llm_models 초기 데이터 {seeded}개 등록 (seed_models.json)")
        except Exception as e:
            print(f"[llm_gateway] 모델 초기 데이터 등록 실패: {type(e).__name__}: {e}")
        await MODEL_REGISTRY.refresh(force=True)
        # 로컬 모델(Ollama 엔진) 읽기 전용 점검: 미기동·모델 누락·CPU 동작 여부를 로그로 알린다
        local_ids = [s.id for s in await MODEL_REGISTRY.list_enabled() if s.id.startswith("ollama/")]
        await asyncio.to_thread(report_local_runtime, local_ids)
    await ensure_conversation_history_collection()
    # 법제처 Open API 인증 canary 확인 (실패해도 기동은 계속 — 로그로 조기 경보만)
    await preflight_law_auth()
    checkpoint_conn = await aiosqlite.connect(str(stack.DATA_DIR / "checkpoints.db"))
    await checkpoint_conn.execute("PRAGMA journal_mode=WAL")
    checkpointer = AsyncSqliteSaver(checkpoint_conn)
    await checkpointer.setup()
    app.state.checkpoint_conn = checkpoint_conn
    app.state.chatbot_agent = build_chatbot_agent(checkpointer)

    yield   # ──── 앱 동작 ────

    # ───── shutdown ─────
    # DB·대화 상태·벡터 저장소를 닫고 자식 프로세스를 내린다.
    await close_pool()
    try:
        await checkpoint_conn.close()
    except Exception:
        pass
    try:
        from api.load_variable import QDRANT_DB
        await QDRANT_DB.close()
    except Exception:
        pass
    await asyncio.to_thread(services.stop_all)

# --- FastAPI 앱 초기화 ---
app = FastAPI(
    title="Video-Demo",
    description="Provides AI Service End-Point.",
    version="1.0.0",
    lifespan=lifespan
)

# --- 라우터 포함 ---
app.include_router(api_router)

# --- CORS 미들웨어 추가 ---
# 모든 출처, 메소드, 헤더에 대해 요청을 허용합니다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 웹 UI (프론트 빌드) ---
# exe·운영에서는 Vite 개발 서버 없이 이 서버가 빌드된 화면을 같은 주소·포트로 내준다 (API와 같은 출처라 프록시·CORS 불필요).
# 개발 중에 vite-project/dist가 없으면 API만 연다 (화면은 Vite 개발 서버 5173에서 따로 뜬다).
FRONTEND_DIST = stack.BUNDLE / ("frontend" if stack.FROZEN else "vite-project/dist")
_API_PREFIXES = ("auth/", "chatbot/", "docs", "redoc", "openapi.json")

if (FRONTEND_DIST / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend(full_path: str):
        """화면 경로(/dashboard, /chat/…, /settings 등)는 index.html — 라우팅은 React가 한다."""
        if full_path.startswith(_API_PREFIXES):
            raise HTTPException(status_code=404)
        target = (FRONTEND_DIST / full_path).resolve()
        if full_path and target.is_file() and FRONTEND_DIST.resolve() in target.parents:
            return FileResponse(target)   # favicon.svg 등 dist 루트 파일
        return FileResponse(FRONTEND_DIST / "index.html")

if __name__ == "__main__":
    # uvicorn.run() 우회 이유:
    #   uvicorn 0.46+의 asyncio_loop_factory()는 Windows에서 정책과 무관하게
    #   ProactorEventLoop을 강제 반환한다 (uvicorn/loops/asyncio.py 참조).
    #   loop="asyncio" 옵션도 동일 factory를 거쳐 무력화됨.
    #   → uvicorn.Config + Server.serve()를 직접 asyncio.run()에 넘겨,
    #     모듈 최상단의 WindowsSelectorEventLoopPolicy가 살아있는 루프를 보장한다.
    # 개인 PC용이라 기본은 이 PC에서만 접속(127.0.0.1). 포트는 .env의 KLAW_PORT_WEB (기본 8008)
    config = uvicorn.Config(
        "main:app",
        host=os.getenv("KLAW_HOST", "127.0.0.1"),
        port=stack.ensure_web_port(),
        reload=False,
    )
    server = uvicorn.Server(config)
    # LiteLLM 버전 변경 시 서버를 내렸다가 같은 방식으로 다시 띄우기 위해 등록한다 (api/runtime_control.py)
    runtime_control.register(server, ["uv", "run", "python", "main.py"])
    asyncio.run(server.serve())