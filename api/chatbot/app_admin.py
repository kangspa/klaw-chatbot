"""
앱 데이터 관리 End-point (/chatbot/app/...) — 마이 페이지 '앱 데이터 모두 삭제'.

  GET  /chatbot/app/storage     지울 대상과 크기 (앱 폴더 항목별·임시 폴더·라이브러리 공용 캐시)
  POST /chatbot/app/uninstall   {confirm: "삭제", includeShared, deleteExe} 삭제 도우미를 띄우고 앱을 종료한다

개인 PC용 앱이라 별도 권한 확인 없이 실행한다 (확인 문구만 받는다).
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import runtime_control, uninstall
from api.auth.deps import get_current_user

router = APIRouter(prefix="/app", dependencies=[Depends(get_current_user)])
CONFIRM_WORD = "삭제"


class UninstallRequest(BaseModel):
    confirm: str
    includeShared: bool = False
    deleteExe: bool = False


@router.get("/storage")
async def get_storage():
    return await asyncio.to_thread(uninstall.plan)


@router.post("/uninstall", status_code=202)
async def start_uninstall(data: UninstallRequest):
    if data.confirm.strip() != CONFIRM_WORD:
        raise HTTPException(status_code=400, detail=f"확인 문구로 '{CONFIRM_WORD}'를 입력하세요.")
    from api.llm_gateway.ollama_upgrade import UPGRADER
    from api.llm_gateway.runtime_update import RUNTIME_UPDATER
    if UPGRADER.running or RUNTIME_UPDATER.running:
        raise HTTPException(status_code=409, detail="버전을 바꾸는 중에는 삭제할 수 없습니다. 끝난 뒤 다시 시도하세요.")
    targets = await asyncio.to_thread(uninstall.schedule, data.includeShared, data.deleteExe)
    print(f"[uninstall] 삭제 도우미 시작 — 대상 {len(targets)}개, 앱을 종료합니다")
    runtime_control.request_exit()
    return {"targets": targets}
