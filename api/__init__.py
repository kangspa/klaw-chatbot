# api/__init__.py
from fastapi import APIRouter
from .chatbot import chatbot_router

# 통합 라우터 생성 및 프리픽스 부여
api_router = APIRouter()

# 각 파일에서 가져온 라우터들을 포함시킵니다.
api_router.include_router(chatbot_router, tags=["chatbot"])