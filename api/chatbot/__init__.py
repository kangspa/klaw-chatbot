# api/chatbot/__init__.py
from fastapi import APIRouter
from .endpoint import router

# 통합 라우터 생성 및 프리픽스 부여
chatbot_router = APIRouter(prefix="/chatbot")

# 각 파일에서 가져온 라우터들을 포함시킵니다.
chatbot_router.include_router(router)