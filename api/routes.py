# api/routes.py — 앱 전체 라우터 (import하면 LLM·MCP·OCR 등이 초기화된다)
from fastapi import APIRouter

from .auth import auth_router
from .chatbot import chatbot_router

# 통합 라우터 생성 및 프리픽스 부여
api_router = APIRouter()

# 각 파일에서 가져온 라우터들을 포함시킵니다.
api_router.include_router(auth_router)
api_router.include_router(chatbot_router, tags=["chatbot"])
from .chatbot.embeddings import router as embeddings_router  # noqa: E402  /chatbot/embeddings (임베딩 방식 선택)
api_router.include_router(embeddings_router, tags=["embeddings"])
