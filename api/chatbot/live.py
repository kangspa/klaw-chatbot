"""
스트리밍 채팅 End-point (/chatbot/chat/stream, /chatbot/chat/live/...) — 답변을 토큰 단위로 흘려 보여 준다.

답변은 요청과 분리된 '진행 중 답변(TurnJob)'으로 서버에서 돈다:
  - 화면이 다른 페이지로 가도(연결이 끊겨도) 답변은 끝까지 만들어져 DB에 저장된다
  - 다시 그 세션에 들어오면 GET /chat/live/{세션}로 진행 중인 질문·지금까지의 답변·단계를 받고,
    GET /chat/live/{세션}/events로 남은 부분을 이어서 받는다

SSE 이벤트 (data는 JSON):
  snapshot {userMessage, text, step}   구독을 시작하면 먼저 한 번 (지금까지 만든 답변 전체)
  status   {step}                       단계 안내 (질문 분석 → 법령 검색 → 답변 작성 → 기억 정리)
  token    {text}                       답변 조각
  reset    {}                           답변 단계가 다시 시작됨 (도구를 쓰고 돌아온 경우 — 앞 조각을 지운다)
  done     {response, turnId, userMessage, assistantMessage, session}
  error    {detail, kind}               kind: auth(API 키 문제) · quota(사용 한도) · other

세션마다 동시에 하나만 돈다 (이미 돌고 있으면 409).
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage

from api.auth.deps import get_current_user
from api.chatbot.functions import session_store
from api.chatbot.functions.chat_runner import describe_exception, friendly_error, resolve_model
from api.chatbot.functions.insights import LawLookupCallback, record_turn, schedule_session_summary
from api.db import is_ready as db_is_ready
from api.llm_gateway import CostTrackingCallback, ModelNotAvailableError
from api.load_variable import MODEL_REGISTRY

router = APIRouter(prefix="/chat", dependencies=[Depends(get_current_user)])

ANSWER_NODES = {"general_chat", "law_response_agent"}
NODE_STEPS = {
    "classify_intent": "질문을 살펴보는 중",
    "general_chat": "답변을 작성하는 중",
    "general_tool_node": "웹에서 자료를 찾는 중",
    "law_search_agent": "법령·판례를 검색하는 중",
    "law_tool_node": "법령 자료를 확인하는 중",
    "law_response_agent": "답변을 작성하는 중",
    "law_response_tool_node": "추가 자료를 확인하는 중",
    "law_key_required": "답변을 작성하는 중",
    "memory_manager": "대화 내용을 정리하는 중",
}
KEEP_FINISHED = 120   # 끝난 작업을 잠시 남겨 둔다 (끝나는 순간 다시 들어온 화면이 결과를 받도록)


@dataclass
class TurnJob:
    session_id: str
    user_id: str
    user_message: str
    user_message_id: str
    turn_id: str
    model: str
    status: str = "running"            # running | done | error
    step: str = "질문을 살펴보는 중"
    text: str = ""
    result: Optional[dict] = None
    error: Optional[dict] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    subscribers: list = field(default_factory=list)

    def snapshot(self) -> dict:
        return {"sessionId": self.session_id, "status": self.status, "step": self.step, "text": self.text,
                "userMessage": {"id": self.user_message_id, "content": self.user_message},
                "model": self.model, "startedAt": self.started_at, "result": self.result, "error": self.error}

    def emit(self, event: str, data: dict) -> None:
        for q in list(self.subscribers):
            q.put_nowait((event, data))


JOBS: dict[str, TurnJob] = {}


def _active(session_id: str) -> Optional[TurnJob]:
    job = JOBS.get(session_id)
    if job and job.finished_at and time.time() - job.finished_at > KEEP_FINISHED:
        JOBS.pop(session_id, None)
        return None
    return job


async def _run(agent, job: TurnJob, isotime: Optional[str]) -> None:
    from api.chatbot.functions.graph_ops import prune_thread

    assistant_message_id = session_store.new_id()
    config = {"configurable": {"user_id": job.user_id, "thread_id": job.session_id, "isotime": isotime,
                               "model": job.model, "turn_id": job.turn_id}}
    cb, law_cb = CostTrackingCallback(MODEL_REGISTRY), LawLookupCallback()
    final_state = None
    try:
        events = agent.astream_events(
            {"messages": [HumanMessage(content=job.user_message, id=job.user_message_id)]},
            config={**config, "callbacks": [cb, law_cb]}, version="v2")
        async for ev in events:
            kind, name = ev["event"], ev.get("name")
            node = (ev.get("metadata") or {}).get("langgraph_node")
            if kind == "on_chain_start" and name in NODE_STEPS and node == name:
                if name in ANSWER_NODES and job.text:
                    job.text = ""            # 도구를 쓰고 돌아와 답변을 다시 시작한다
                    job.emit("reset", {})
                job.step = NODE_STEPS[name]
                job.emit("status", {"step": job.step})
            elif kind == "on_chat_model_stream" and node in ANSWER_NODES:
                chunk = ev["data"].get("chunk")
                text = getattr(chunk, "content", "") if chunk is not None else ""
                if isinstance(text, str) and text and not getattr(chunk, "tool_call_chunks", None):
                    job.text += text
                    job.emit("token", {"text": text})
            elif kind == "on_chain_end" and ev.get("parent_ids") == []:
                final_state = (ev.get("data") or {}).get("output")
    except Exception as e:
        kind, text = friendly_error(e, job.model)
        describe_exception(e)   # 자세한 내용은 로그로
        job.status, job.error = "error", {"detail": text, "kind": kind}
        job.finished_at = time.time()
        job.emit("error", job.error)
        return
    finally:
        await record_turn(user_id=job.user_id, session_id=job.session_id, turn_id=job.turn_id,
                          cost_cb=cb, law_cb=law_cb, state=final_state)
        await prune_thread(agent, job.session_id)

    if not isinstance(final_state, dict):
        try:
            final_state = (await agent.aget_state({"configurable": {"thread_id": job.session_id}})).values
        except Exception:
            final_state = {}
    answer, ai_graph_id = job.text, None
    for msg in reversed(final_state.get("messages", []) or []):
        if isinstance(msg, AIMessage) and msg.content:
            answer, ai_graph_id = msg.content, getattr(msg, "id", None)
            break

    saved: dict[str, Any] = {}
    if db_is_ready():
        try:
            saved = await session_store.append_turn(
                session_id=job.session_id, turn_id=job.turn_id,
                user_message_id=job.user_message_id, user_content=job.user_message, user_graph_id=job.user_message_id,
                assistant_message_id=assistant_message_id, assistant_content=answer, assistant_graph_id=ai_graph_id,
                model=job.model)
            schedule_session_summary(user_id=job.user_id, session_id=job.session_id, turn_id=job.turn_id)
        except Exception as e:
            print(f"[DB] 대화 저장 실패(무시): {type(e).__name__}: {e}")
    job.text = answer
    job.result = {"response": answer, "turnId": job.turn_id, "userMessage": saved.get("userMessage"),
                  "assistantMessage": saved.get("assistantMessage"), "session": saved.get("session")}
    job.status, job.finished_at = "done", time.time()
    job.emit("done", job.result)


def _sse(job: TurnJob) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue()
    job.subscribers.append(queue)

    async def gen():
        try:
            yield _event("snapshot", {"userMessage": job.snapshot()["userMessage"], "text": job.text, "step": job.step})
            if job.status == "done":
                yield _event("done", job.result or {})
                return
            if job.status == "error":
                yield _event("error", job.error or {})
                return
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"   # 프록시·브라우저가 연결을 끊지 않도록
                    continue
                yield _event(event, data)
                if event in ("done", "error"):
                    return
        finally:
            if queue in job.subscribers:
                job.subscribers.remove(queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/stream")
async def chat_stream(request: Request, current_user: dict[str, Any] = Depends(get_current_user)):
    from api.chatbot.endpoint import PlanChatRequest
    data = PlanChatRequest(**await request.json())
    user_id = current_user["id"]
    existing = _active(data.sessionId)
    if existing and existing.status == "running":
        raise HTTPException(status_code=409, detail="이 대화에서 답변을 만드는 중입니다. 끝난 뒤 다시 보내세요.")
    try:
        model = await resolve_model(data.model)
    except ModelNotAvailableError as e:
        raise HTTPException(status_code=400, detail=str(e))
    session = await session_store.get_session(data.sessionId)
    if session is None:
        await session_store.ensure_session(data.sessionId, user_id)
    elif session["ownerId"] != user_id:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    job = TurnJob(session_id=data.sessionId, user_id=user_id, user_message=data.message,
                  user_message_id=session_store.new_id(), turn_id=session_store.new_id(), model=model)
    JOBS[data.sessionId] = job
    response = _sse(job)   # 먼저 구독해 두어 첫 조각부터 받는다
    asyncio.create_task(_run(request.app.state.chatbot_agent, job, data.isotime))   # 화면 연결과 무관하게 끝까지
    return response


@router.get("/live/{sessionId}")
async def live_snapshot(sessionId: str, current_user: dict[str, Any] = Depends(get_current_user)):
    job = _active(sessionId)
    if job is None or job.user_id != current_user["id"]:
        return {"active": False}
    return {"active": job.status == "running", **job.snapshot()}


@router.get("/live/{sessionId}/events")
async def live_events(sessionId: str, current_user: dict[str, Any] = Depends(get_current_user)):
    job = _active(sessionId)
    if job is None or job.user_id != current_user["id"]:
        raise HTTPException(status_code=404, detail="진행 중인 답변이 없습니다.")
    return _sse(job)
