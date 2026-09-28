# ─── Windows asyncio 이벤트 루프 정책 ───────────────────────────
# 반드시 다른 import보다 먼저! 이유:
#   - Windows의 Python 3.8+ 기본 정책은 ProactorEventLoop인데
#     psycopg(async)는 SelectorEventLoop에서만 동작 (Proactor에선 연결 불가)
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

import dotenv, os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn
from langgraph.checkpoint.memory import MemorySaver
from api import api_router
from api.db import close_pool, init_pool
from api.llm_gateway.store import seed_if_empty as seed_llm_models_if_empty
from api.load_variable import MODEL_REGISTRY
from api.chatbot.functions.agent import build_chatbot_agent
from api.chatbot.functions.law_auth import preflight_law_auth
from api.chatbot.functions.summary import ensure_conversation_history_collection

# .env 파일에서 환경 변수 로드
dotenv.load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ───── startup ─────
    # 저장소 3종 준비
    #  - Postgres : 사용자에게 보여줄 세션/메시지 원문 (진실 원본)
    #  - Qdrant   : AI가 과거 대화를 의미 검색하기 위한 벡터 (1턴 = 1 point)
    #  - MemorySaver : LangGraph 실행 상태 (같은 thread_id 내 multi-turn 유지, 서버 재시작 시 휘발)
    pool = await init_pool()
    # LLM 모델 목록: 테이블이 비어 있을 때만 seed JSON으로 채우고, 레지스트리를 미리 읽어둔다
    if pool is not None:
        try:
            seeded = await seed_llm_models_if_empty(pool)
            if seeded:
                print(f"[llm_gateway] llm_models 초기 데이터 {seeded}개 등록 (seed_models.json)")
        except Exception as e:
            print(f"[llm_gateway] 모델 초기 데이터 등록 실패: {type(e).__name__}: {e}")
        await MODEL_REGISTRY.refresh(force=True)
    await ensure_conversation_history_collection()
    # 법제처 Open API 인증 canary 확인 (실패해도 기동은 계속 — 로그로 조기 경보만)
    await preflight_law_auth()
    checkpointer = MemorySaver()
    app.state.chatbot_agent = build_chatbot_agent(checkpointer)

    yield   # ──── 앱 동작 ────

    # ───── shutdown ─────
    # MemorySaver는 GC에 맡기고, Postgres 풀만 명시적으로 닫는다.
    await close_pool()

# --- FastAPI 앱 초기화 ---
app = FastAPI(
    title="Video-Demo",
    description="Provides AI Service End-Point.",
    version="1.0.0",
    lifespan=lifespan
)

# --- 라우터 포함 ---
app.include_router(api_router)

os.makedirs("templates", exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs("static", exist_ok=True)

# --- 정적 파일 마운트 ---
app.mount("/templates", StaticFiles(directory="templates"), name="templates")
app.mount("/data", StaticFiles(directory="data"), name="data")
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- CORS 미들웨어 추가 ---
# 모든 출처, 메소드, 헤더에 대해 요청을 허용합니다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def read_index():
    return FileResponse('templates/index.html')

if __name__ == "__main__":
    # uvicorn.run() 우회 이유:
    #   uvicorn 0.46+의 asyncio_loop_factory()는 Windows에서 정책과 무관하게
    #   ProactorEventLoop을 강제 반환한다 (uvicorn/loops/asyncio.py 참조).
    #   loop="asyncio" 옵션도 동일 factory를 거쳐 무력화됨.
    #   → uvicorn.Config + Server.serve()를 직접 asyncio.run()에 넘겨,
    #     모듈 최상단의 WindowsSelectorEventLoopPolicy가 살아있는 루프를 보장한다.
    config = uvicorn.Config(
        "main:app",
        host="0.0.0.0",
        port=8008,
        reload=False,
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())