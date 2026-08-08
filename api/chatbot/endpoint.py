from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional, Any
from langchain_core.messages import HumanMessage

# CHATBOT_AGENT는 main.py의 lifespan startup에서 compile되어
# request.app.state.chatbot_agent에 저장된다 (체크포인터가 이벤트 루프 필요).
from api.auth.deps import get_current_user
from api.chatbot.functions import session_store
from api.chatbot.functions.chat_runner import (
    LiteLLMCostCallback,  # noqa: F401  (기존 import 경로 호환용 재노출)
    describe_exception,
    run_agent_turn,
)
from api.db import is_ready as db_is_ready

router = APIRouter()


class PlanChatRequest(BaseModel):
    sessionId: str = Field(..., description="세션 ID (thread_id로 사용됨)")
    isotime: str = Field(..., description="클라이언트가 보낸 오프셋(Offset)이 포함된 ISO 8601 형식의 시간 문자열 (형식:YYYY-MM-DDTHH:mm:ss+HH:mm) (예시:2026-04-07T23:16:04+09:00)")
    message: str = Field(..., description="사용자 입력 메시지")
    model: Optional[str] = Field(
        default="gemini/gemini-2.5-flash-lite",
        description=(
            "사용할 AI 모델 명. LiteLLM 표준 prefix 형식을 사용합니다. "
            "예: 'gemini/gemini-2.5-flash-lite', 'openai/gpt-4o-mini', "
            "'anthropic/claude-sonnet-4-5', 'ollama/llama3.1', "
            "'huggingface/meta-llama/Meta-Llama-3-8B-Instruct'"
        ),
    )


class PlanChatResponse(BaseModel):
    response: str = Field(..., description="AI 응답 메시지")
    turnId: Optional[str] = Field(default=None, description="이번 턴의 turn_id (= Qdrant point id)")
    userMessage: Optional[dict[str, Any]] = Field(default=None, description="Postgres에 저장된 사용자 메시지 행")
    assistantMessage: Optional[dict[str, Any]] = Field(default=None, description="Postgres에 저장된 AI 메시지 행")
    session: Optional[dict[str, Any]] = Field(
        default=None,
        description="갱신된 세션 행. 첫 메시지로 제목이 자동 생성되므로 프론트가 이 값으로 목록을 동기화한다.",
    )


@router.post("/chat", response_model=PlanChatResponse)
async def post_plan_chat(
    data: PlanChatRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """
    사용자 메시지를 받아 에이전트를 1턴 실행하고, 결과를 세 저장소에 함께 기록하는 End-point

      - Postgres : 화면에 보여줄 대화 원문 (user/assistant 메시지 2행, 같은 turn_id)
      - Qdrant   : 의미 검색용 벡터 (turn_id를 point id로 사용, 1턴 = 1 point)
      - LangGraph: 실행 상태(체크포인트)

    사용자 id는 요청 본문이 아니라 Bearer 토큰에서 가져온다(남의 세션에 쓰는 것을 막기 위함).
    """
    # lifespan에서 compile된 에이전트를 app.state에서 꺼내 사용
    chatbot_agent = request.app.state.chatbot_agent
    user_id = current_user["id"]

    # 이번 턴의 식별자들을 미리 만들어둔다.
    #  - turn_id: Qdrant point id 이자 Postgres의 turn 키
    #  - *_message_id: Postgres PK 이자 LangGraph state의 message id
    turn_id = session_store.new_id()
    user_message_id = session_store.new_id()
    assistant_message_id = session_store.new_id()

    # 세션 보장 + 소유권 확인 (프론트가 만들기 전에 /chat이 먼저 도달해도 동작하도록)
    session = await session_store.get_session(data.sessionId)
    if session is None:
        await session_store.ensure_session(data.sessionId, user_id)
    elif session["ownerId"] != user_id:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    try:
        answer, ai_graph_id = await run_agent_turn(
            chatbot_agent,
            user_id=user_id,
            session_id=data.sessionId,
            isotime=data.isotime,
            message=data.message,
            model=data.model,
            turn_id=turn_id,
            user_graph_message_id=user_message_id,
        )
    except Exception as e:
        detail = describe_exception(e)
        raise HTTPException(status_code=500, detail=f"에이전트 호출 중 오류가 발생했습니다: {detail}")

    # Postgres 기록 (실패해도 이미 생성된 답변은 그대로 반환한다)
    saved: dict[str, Any] = {}
    if db_is_ready():
        try:
            saved = await session_store.append_turn(
                session_id=data.sessionId,
                turn_id=turn_id,
                user_message_id=user_message_id,
                user_content=data.message,
                user_graph_id=user_message_id,
                assistant_message_id=assistant_message_id,
                assistant_content=answer,
                assistant_graph_id=ai_graph_id,
                model=data.model,
            )
        except Exception as e:
            print(f"[Postgres] 대화 저장 실패(무시): {type(e).__name__}: {e}")

    return PlanChatResponse(
        response=answer,
        turnId=turn_id,
        userMessage=saved.get("userMessage"),
        assistantMessage=saved.get("assistantMessage"),
        session=saved.get("session"),
    )


@router.get("/{sessionId}/history")
async def get_draft_history(
    sessionId: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """
    특정 세션의 대화 기록을 조회하는 End-point.

    Postgres가 준비되어 있으면 그쪽(사용자에게 보여줄 원문)을 우선 사용하고,
    없을 때만 LangGraph 체크포인트에서 읽는다.
    """
    session = await session_store.get_session(sessionId)
    if session is None or session["ownerId"] != current_user["id"]:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    if db_is_ready():
        try:
            messages = await session_store.list_messages(sessionId)
            if messages:
                return {"messages": [{"role": m["role"], "content": m["content"]} for m in messages]}
        except Exception as e:
            print(f"[Postgres] 히스토리 조회 실패(체크포인터로 폴백): {type(e).__name__}: {e}")

    chatbot_agent = request.app.state.chatbot_agent
    config = {"configurable": {"thread_id": sessionId}}

    try:
        state = await chatbot_agent.aget_state(config)
        if not state or not state.values:
            return {"messages": []}

        messages = []
        for msg in state.values.get("messages", []):
            role = "user" if isinstance(msg, HumanMessage) else "assistant"
            messages.append({"role": role, "content": msg.content})

        return {"messages": messages}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
