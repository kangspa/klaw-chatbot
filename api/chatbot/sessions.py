"""
대화 세션/메시지 관리 End-point (/chatbot/sessions/...).

프론트의 세 가지 버튼이 여기에 대응한다.

  1) 삭제      DELETE /chatbot/sessions/{sessionId}/messages/{messageId}
  2) 브랜치    POST   /chatbot/sessions/{sessionId}/messages/{messageId}/branch
  3) 다시 실행 POST   /chatbot/sessions/{sessionId}/messages/{messageId}/regenerate

세 동작 모두 Postgres(원문) · Qdrant(벡터) · LangGraph 체크포인트(실행 상태)에
같은 범위로 반영된다. 셋을 묶어주는 열쇠는 chat_messages.turn_id (= Qdrant point id)와
chat_messages.graph_message_id (= LangGraph state 상의 message id)다.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.auth.deps import get_current_user
from api.chatbot.functions import graph_ops, qdrant_ops, session_store
from api.chatbot.functions.chat_runner import describe_exception, resolve_model, run_agent_turn
from api.chatbot.functions.insights import schedule_session_summary
from api.llm_gateway import ModelNotAvailableError

# 이 라우터의 모든 경로는 로그인 필수. 사용자 id는 요청 파라미터가 아니라 Bearer 토큰에서 가져온다.
router = APIRouter(prefix="/sessions", dependencies=[Depends(get_current_user)])


async def _load_session(session_id: str, owner_id: str) -> dict[str, Any]:
    """
    세션을 읽고 소유권까지 확인한다.
    남의 세션은 '권한 없음'이 아니라 404로 응답한다 — 세션 id의 존재 여부 자체를 노출하지 않기 위함.
    """
    session = await session_store.get_session(session_id)
    if not session or session["ownerId"] != owner_id:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    return session


async def _load_message(session_id: str, message_id: str) -> dict[str, Any]:
    message = await session_store.get_message(session_id, message_id)
    if not message:
        raise HTTPException(status_code=404, detail="메시지를 찾을 수 없습니다.")
    return message


# ────────────────────────── 세션 CRUD ──────────────────────────

class CreateSessionRequest(BaseModel):
    title: Optional[str] = Field(default=None, description="세션 제목 (미지정 시 '새 대화')")


class RenameSessionRequest(BaseModel):
    title: str = Field(..., description="새 세션 제목")


@router.get("")
async def list_sessions(current_user: dict[str, Any] = Depends(get_current_user)):
    """로그인한 회원의 세션 목록 (메시지 포함, 최근 활동순)."""
    return {"sessions": await session_store.list_sessions_with_messages(current_user["id"])}


@router.post("", status_code=201)
async def create_session(
    data: CreateSessionRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """새 세션 생성. 반환된 id가 그대로 LangGraph thread_id / Qdrant thread_id로 쓰인다."""
    session = await session_store.create_session(current_user["id"], data.title)
    session["messages"] = []
    return session


@router.get("/{sessionId}")
async def get_session(
    sessionId: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    await _load_session(sessionId, current_user["id"])
    session = await session_store.get_session_with_messages(sessionId)
    if not session:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    return session


@router.patch("/{sessionId}")
async def rename_session(
    sessionId: str,
    data: RenameSessionRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    await _load_session(sessionId, current_user["id"])
    title = data.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="제목은 비워둘 수 없습니다.")
    session = await session_store.rename_session(sessionId, title)
    if not session:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    return session


@router.delete("/{sessionId}")
async def delete_session(
    sessionId: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """세션 삭제: Postgres 행 + Qdrant의 해당 thread 벡터 전체 + LangGraph state를 모두 정리."""
    await _load_session(sessionId, current_user["id"])

    await session_store.delete_session(sessionId)
    await qdrant_ops.delete_thread(sessionId)
    await graph_ops.clear_thread(request.app.state.chatbot_agent, sessionId)

    return {"deleted": True, "sessionId": sessionId}


# ────────────────────────── 1) 메시지 삭제 ──────────────────────────

@router.delete("/{sessionId}/messages/{messageId}")
async def delete_message(
    sessionId: str,
    messageId: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """
    챗봇이 출력한 문장(또는 그 짝인 질문)을 삭제한다.

    Qdrant는 (질문 + 답변)을 point 1개로 저장하므로 삭제 단위도 '턴' 단위다.
    따라서 assistant 메시지를 지우면 짝이 되는 user 메시지도 함께 사라진다.
    세 저장소에서 동일하게 제거된다:
      - Postgres : 같은 turn_id를 가진 메시지 행 전부
      - Qdrant   : turn_id에 해당하는 point
      - LangGraph: 해당 턴의 첫 메시지부터 그 이후 메시지까지 (중간 ToolMessage 포함)
    """
    await _load_session(sessionId, current_user["id"])
    message = await _load_message(sessionId, messageId)

    turn_id = message.get("turnId")

    # 삭제 시작 지점(= 턴의 첫 메시지)의 graph id를 먼저 확보해야 state를 정확히 자를 수 있다.
    turn_messages = await session_store.get_turn_messages(sessionId, turn_id) if turn_id else []
    first_of_turn = turn_messages[0]["id"] if turn_messages else messageId
    first_graph_id = await _graph_id_of(sessionId, first_of_turn)

    # 1) LangGraph 실행 상태에서 제거 (다음 턴 프롬프트에 다시 들어가지 않도록)
    await graph_ops.truncate_from(
        request.app.state.chatbot_agent, sessionId, first_graph_id,
    )

    # 2) Postgres에서 제거
    if turn_id:
        removed = await session_store.delete_turn(sessionId, turn_id)
    else:
        row = await session_store.delete_message(sessionId, messageId)
        removed = [row] if row else []

    # 3) Qdrant에서 제거
    if turn_id:
        await qdrant_ops.delete_points([turn_id])

    return {
        "deleted": True,
        "sessionId": sessionId,
        "turnId": turn_id,
        "removedMessageIds": [m["id"] for m in removed if m],
        "messages": await session_store.list_messages(sessionId),
    }


async def _graph_id_of(session_id: str, message_id: str) -> Optional[str]:
    """chat_messages.graph_message_id 조회 (session_store가 응답에 노출하지 않는 내부 컬럼)."""
    from api.db import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT graph_message_id FROM chat_messages WHERE session_id = $1 AND id = $2",
            session_id, message_id,
        )


# ────────────────────────── 2) 브랜치 생성 ──────────────────────────

class BranchRequest(BaseModel):
    title: Optional[str] = Field(default=None, description="새 세션 제목 (미지정 시 '원본제목 (분기)')")


@router.post("/{sessionId}/messages/{messageId}/branch", status_code=201)
async def branch_from_message(
    sessionId: str,
    messageId: str,
    data: BranchRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """
    선택한 메시지까지의 대화를 그대로 물려받는 새 세션(브랜치)을 만든다.

    - Postgres : seq <= 기준 메시지의 seq 인 메시지를 새 session_id로 복사 (새 message id / 새 turn_id)
    - Qdrant   : 복사된 턴들의 point를 벡터·payload 그대로 복제하고 id와 thread_id만 새 값으로 교체
                 (임베딩 재호출 없음 → 비용 0, 검색 결과도 원본과 동일하게 재현)
    - LangGraph: 원본 thread의 state를 기준 메시지까지 잘라 새 thread로 복사

    이후 새 세션에서 이어지는 대화는 원본 세션에 전혀 영향을 주지 않는다.
    """
    owner_id = current_user["id"]
    src_session = await _load_session(sessionId, owner_id)
    message = await _load_message(sessionId, messageId)

    src_messages = await session_store.list_messages(sessionId)
    upto_seq = message["seq"]
    kept = [m for m in src_messages if m["seq"] <= upto_seq]
    if not kept:
        raise HTTPException(status_code=400, detail="브랜치로 복사할 대화가 없습니다.")

    # 새 세션 생성
    title = (data.title or "").strip() or f"{src_session['title']} (분기)"
    new_session = await session_store.create_session(
        owner_id=owner_id,
        title=title,
        parent_session_id=sessionId,
        branched_from_message_id=messageId,
    )
    new_session_id = new_session["id"]

    # id 매핑 준비 (turn_id는 Qdrant point id와 동일해야 하므로 여기서 함께 결정)
    turn_id_map: dict[str, str] = {}
    for m in kept:
        tid = m.get("turnId")
        if tid and tid not in turn_id_map:
            turn_id_map[tid] = session_store.new_id()
    message_id_map = {m["id"]: session_store.new_id() for m in kept}

    try:
        # 1) Postgres 복사
        copied = await session_store.copy_messages_upto(
            src_session_id=sessionId,
            dst_session_id=new_session_id,
            upto_seq=upto_seq,
            turn_id_map=turn_id_map,
            message_id_map=message_id_map,
        )

        # 2) Qdrant 복제 (새 thread_id로)
        copied_points = await qdrant_ops.copy_turns(
            turn_id_map=turn_id_map,
            dst_thread_id=new_session_id,
            dst_user_id=owner_id,
        )

        # 3) LangGraph state 분기
        branch_graph_id = await _graph_id_of(sessionId, messageId)
        await graph_ops.fork_thread(
            request.app.state.chatbot_agent,
            src_thread_id=sessionId,
            dst_thread_id=new_session_id,
            upto_graph_message_id=branch_graph_id,
            user_id=owner_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        # 복사 도중 실패하면 반쪽짜리 세션이 남지 않도록 정리
        await session_store.delete_session(new_session_id)
        await qdrant_ops.delete_thread(new_session_id)
        raise HTTPException(status_code=500, detail=f"브랜치 생성 실패: {describe_exception(e)}")

    new_session["messages"] = copied
    return {
        "session": new_session,
        "copiedMessages": len(copied),
        "copiedVectorPoints": copied_points,
    }


# ────────────────────────── 3) 다시 실행 (재생성) ──────────────────────────

class RegenerateRequest(BaseModel):
    isotime: Optional[str] = Field(default=None, description="클라이언트 로컬 시각 (ISO 8601, 오프셋 포함)")
    model: Optional[str] = Field(default=None, description="이번 재생성에 사용할 모델 (미지정 시 원래 모델 유지)")


@router.post("/{sessionId}/messages/{messageId}/regenerate")
async def regenerate_message(
    sessionId: str,
    messageId: str,
    data: RegenerateRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """
    잘못 나온 답변을 같은 질문으로 다시 생성한다.

    처리 순서:
      1. LangGraph state를 해당 턴의 사용자 메시지 직전까지 되돌린다
         (이전 답변과 그때 붙은 ToolMessage들을 제거, 요약 채널도 직전 체크포인트 값으로 복원)
      2. 같은 질문으로 그래프를 다시 1턴 실행한다. 이때 turn_id를 그대로 넘기므로
         save_conversation이 Qdrant의 같은 point를 새 내용으로 덮어쓴다.
      3. Postgres의 assistant 행을 새 내용으로 UPDATE 한다 (id/seq 유지 → 대화 순서 불변)
    """
    owner_id = current_user["id"]
    await _load_session(sessionId, owner_id)
    message = await _load_message(sessionId, messageId)

    turn_id = message.get("turnId")
    if not turn_id:
        raise HTTPException(status_code=400, detail="이 메시지는 재생성할 수 있는 대화 턴에 속해 있지 않습니다.")

    turn_messages = await session_store.get_turn_messages(sessionId, turn_id)
    user_row = next((m for m in turn_messages if m["role"] == "user"), None)
    assistant_row = next((m for m in turn_messages if m["role"] == "assistant"), None)
    if not user_row or not assistant_row:
        raise HTTPException(status_code=400, detail="질문과 답변이 짝을 이루지 않아 재생성할 수 없습니다.")

    # state를 되돌리기 전에 모델부터 확인한다 (미등록 모델이면 아무것도 바꾸지 않고 400)
    try:
        model = await resolve_model(data.model or assistant_row.get("model"))
    except ModelNotAvailableError as e:
        raise HTTPException(status_code=400, detail=str(e))
    user_graph_id = await _graph_id_of(sessionId, user_row["id"]) or user_row["id"]

    # 1) 이전 실행 결과를 state에서 되돌린다
    await graph_ops.truncate_from(
        request.app.state.chatbot_agent, sessionId, user_graph_id, user_id=owner_id,
    )

    # 2) 같은 질문 + 같은 turn_id로 재실행 → Qdrant point는 덮어쓰기 된다
    try:
        answer, ai_graph_id = await run_agent_turn(
            request.app.state.chatbot_agent,
            user_id=owner_id,
            session_id=sessionId,
            isotime=data.isotime,
            message=user_row["content"],
            model=model,
            turn_id=turn_id,
            user_graph_message_id=user_graph_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"재생성 중 오류가 발생했습니다: {describe_exception(e)}")

    # 3) Postgres의 기존 답변 행을 덮어쓴다
    updated = await session_store.update_assistant_message(
        session_id=sessionId,
        message_id=assistant_row["id"],
        content=answer,
        graph_message_id=ai_graph_id,
        model=model,
    )
    # 대시보드용 세션 요약을 백그라운드에서 갱신 (응답은 기다리지 않는다)
    schedule_session_summary(user_id=owner_id, session_id=sessionId, turn_id=turn_id)

    return {
        "regenerated": True,
        "sessionId": sessionId,
        "turnId": turn_id,
        "assistantMessage": updated,
        "messages": await session_store.list_messages(sessionId),
    }
