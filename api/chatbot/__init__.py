# api/chatbot/__init__.py
from fastapi import APIRouter
from .endpoint import router
from .sessions import router as sessions_router

# 통합 라우터 생성 및 프리픽스 부여
chatbot_router = APIRouter(prefix="/chatbot")

# 각 파일에서 가져온 라우터들을 포함시킵니다.
# sessions_router를 먼저 등록해 /chatbot/sessions/* 가 /chatbot/{sessionId}/* 패턴보다 우선 매칭되게 한다.
chatbot_router.include_router(sessions_router)
chatbot_router.include_router(router)
