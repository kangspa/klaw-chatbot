"""
대시보드용 기록: 턴마다 모델 사용량·비용, 조회된 법령, 세션 요약을 Postgres에 남긴다.

  - llm_usage   : CostTrackingCallback이 모은 호출별 토큰·비용을 모델 단위로 합쳐 저장
  - law_lookups : 이번 턴에 법령 도구가 실제로 찾아온 법령명·조문 (LawLookupCallback)
  - chat_sessions.summary : 세션 요약 (답변 저장 후 백그라운드에서 기본 모델로 생성)

기록 실패는 답변에 영향을 주지 않도록 로그만 남긴다.
"""
import asyncio
import json
import re
import threading
from typing import Any, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from api.db import get_pool, is_ready
from api.llm_gateway import CostTrackingCallback
from api.llm_gateway.chat_model import content_to_text
from api.load_variable import MODEL_MANAGER, MODEL_REGISTRY

# 응답이 이 표식으로 시작하면 데이터를 얻지 못한 것 (agent.MCP_FAILURE_LABELS와 같은 기준)
_FAILURE_PREFIXES = ("[NOT_FOUND]", "[LAW_NOT_FOUND]", "[UPSTREAM_NO_DATA]", "[ANNEX_BODY_UNAVAILABLE]",
                     "[INVALID_PARAMETER]", "[EXTERNAL_API_ERROR]")
_LAW_NAME_RE = re.compile(r"^\s*법령명\s*[:：]\s*(.+?)\s*$", re.MULTILINE)
# search_law 응답의 '정확매칭' 첫 항목 ("1. 민법 [현행]")
_EXACT_MATCH_RE = re.compile(r"정확매칭[^\n]*\n\s*1\.\s*(.+?)\s*(?:\[[^\]]*\])?\s*$", re.MULTILINE)
SUMMARY_MESSAGES = 16      # 세션 요약에 넣을 최근 메시지 수
SUMMARY_MSG_CHARS = 1200   # 메시지 1건당 최대 글자 수 (법령 원문이 길게 섞인 답변 대비)


def normalize_article(jo: Any) -> Optional[str]:
    """'60' / '006000' / '제60조' → '제60조', '001002' → '제10조의2'."""
    if jo is None:
        return None
    s = str(jo).strip().replace(" ", "")
    if not s:
        return None
    if re.fullmatch(r"\d{6}", s):
        main, sub = int(s[:4]), int(s[4:])
        return f"제{main}조" + (f"의{sub}" if sub else "")
    if re.fullmatch(r"\d+", s):
        return f"제{int(s)}조"
    m = re.match(r"(제?\d+조(?:의\d+)?)", s)
    if m:
        return m.group(1) if m.group(1).startswith("제") else "제" + m.group(1)
    return s[:30]


def law_from_tool(tool: str, args: dict, output: str) -> Optional[tuple[str, Optional[str]]]:
    """도구 호출 하나에서 (법령명, 조문) 추출. 법령을 특정할 수 없으면 None."""
    if not output or output.lstrip().startswith(_FAILURE_PREFIXES):
        return None
    if tool == "get_law_text":
        m = _LAW_NAME_RE.search(output)
        return (m.group(1), normalize_article(args.get("jo"))) if m else None
    if tool == "get_annexes":
        m = _LAW_NAME_RE.search(output)
        name = m.group(1) if m else args.get("lawName")
        return (name, None) if name else None
    if tool == "search_law":
        # 부분 매칭만 있는 검색은 질문과 무관한 법령이 섞이므로 정확 매칭만 센다
        m = _EXACT_MATCH_RE.search(output)
        return (m.group(1), None) if m else None
    if tool == "legal_analysis" and args.get("lawName"):
        return (str(args["lawName"]), normalize_article(args.get("jo")))
    return None


class LawLookupCallback(BaseCallbackHandler):
    """그래프 실행 중 법령 도구 호출과 결과를 모은다 (도구 래퍼 안쪽의 중첩 호출은 제외)."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._runs: dict[UUID, tuple[str, dict]] = {}
        self.found: list[tuple[str, Optional[str], str]] = []   # (법령명, 조문, 도구)

    def on_tool_start(self, serialized: Any, input_str: str, *, run_id: UUID,
                      parent_run_id: Optional[UUID] = None, inputs: Optional[dict] = None,
                      **kwargs: Any) -> None:
        with self._lock:
            if parent_run_id in self._runs:   # OCR 래퍼가 원본 도구를 다시 부르는 중첩 호출
                return
            name = (serialized or {}).get("name") or kwargs.get("name") or ""
            self._runs[run_id] = (name, inputs if isinstance(inputs, dict) else {})

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        with self._lock:
            info = self._runs.get(run_id)
        if not info:
            return
        text = content_to_text(getattr(output, "content", output))
        law = law_from_tool(info[0], info[1], text)
        if law:
            with self._lock:
                self.found.append((law[0], law[1], info[0]))

    def lookups(self) -> list[tuple[str, Optional[str], str]]:
        """한 턴 안의 중복 제거: 같은 (법령, 조문)은 1건, 조문까지 본 법령은 조문 없는 검색 기록을 뺀다."""
        with_article = {name for name, art, _ in self.found if art}
        seen, out = set(), []
        for name, art, tool in self.found:
            if not art and name in with_article:
                continue
            if (name, art) not in seen:
                seen.add((name, art))
                out.append((name, art, tool))
        return out


async def record_turn(*, user_id: str, session_id: str, turn_id: str, cost_cb: CostTrackingCallback,
                      law_cb: Optional[LawLookupCallback], state: Optional[dict]) -> None:
    """
    턴 1회의 기록을 저장한다. 답변이 실패해도(state=None) 이미 발생한 비용은 기록한다.
    같은 turn_id로 다시 생성하면 조회 법령은 새 결과로 교체하고, 비용은 누적한다(실제로 다시 발생했으므로).
    세션 요약은 답변이 Postgres에 저장된 뒤 schedule_session_summary로 따로 갱신한다.
    """
    if not is_ready():
        return
    usage: dict[str, dict[str, float]] = {}
    for c in cost_cb.per_call:
        u = usage.setdefault(c["model"], {"calls": 0, "input": 0, "cache": 0, "output": 0, "cost": 0.0})
        u["calls"] += 1
        u["input"] += c.get("input_tokens") or 0
        u["cache"] += c.get("cache_read_tokens") or 0
        u["output"] += c.get("output_tokens") or 0
        u["cost"] += c.get("cost_usd") or 0.0
    try:
        async with get_pool().acquire() as conn, conn.transaction():
            for model, u in usage.items():
                await conn.execute(
                    """INSERT INTO llm_usage (user_id, session_id, turn_id, model, calls,
                           input_tokens, cache_read_tokens, output_tokens, cost_usd)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                    user_id, session_id, turn_id, model, int(u["calls"]),
                    int(u["input"]), int(u["cache"]), int(u["output"]), u["cost"],
                )
            if state is None:
                return
            if law_cb is not None:
                await conn.execute("DELETE FROM law_lookups WHERE session_id = $1 AND turn_id = $2",
                                   session_id, turn_id)
                for name, article, tool in law_cb.lookups():
                    await conn.execute(
                        """INSERT INTO law_lookups (user_id, session_id, turn_id, law_name, article, tool)
                           VALUES ($1, $2, $3, $4, $5, $6)""",
                        user_id, session_id, turn_id, name[:200], article, tool,
                    )
    except Exception as e:
        print(f"[insights] 대시보드 기록 실패(무시): {type(e).__name__}: {e}")


# ────────────────────────── 세션 요약 ──────────────────────────
class SessionSummary(BaseModel):
    overall: str = Field(description="세션 전체 요약 2~3문장: 사용자의 상황·질문과 안내된 핵심 결론")
    points: list[str] = Field(description="핵심 쟁점·답변 요지 최대 4개 (각 60자 이내, 관련 법령·조문 포함)")


_SUMMARY_PROMPT = """당신은 법률 상담 기록 정리 담당자입니다. 아래 상담 세션을 대시보드 카드에 넣을 요약으로 정리하세요.

[규칙]
- overall: 사용자의 상황과 질문, 안내된 핵심 결론을 2~3문장 평서문으로.
- points: 쟁점별 답변 요지를 최대 4개. 언급된 법령·조문명은 그대로 살린다.
- 대화에 없는 내용을 지어내지 않는다.

[이전 요약]
{previous}

[상담 대화 (최근 순서대로)]
{transcript}"""

_running: dict[str, asyncio.Task] = {}
_dirty: set[str] = set()


def schedule_session_summary(*, user_id: str, session_id: str, turn_id: Optional[str]) -> None:
    """
    답변이 저장된 뒤 호출. 세션 요약을 백그라운드에서 갱신한다 (답변 응답 시간에 영향 없음).
    같은 세션 요약이 이미 도는 중이면 끝난 뒤 한 번 더 돌도록 표시만 한다.
    """
    if not is_ready():
        return
    if session_id in _running:
        _dirty.add(session_id)
        return

    async def loop() -> None:
        try:
            while True:
                _dirty.discard(session_id)
                await refresh_session_summary(user_id=user_id, session_id=session_id, turn_id=turn_id)
                if session_id not in _dirty:
                    break
        finally:
            _running.pop(session_id, None)

    _running[session_id] = asyncio.create_task(loop())


async def refresh_session_summary(*, user_id: str, session_id: str, turn_id: Optional[str]) -> None:
    try:
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow("SELECT summary FROM chat_sessions WHERE id = $1", session_id)
            if row is None:
                return
            msgs = await conn.fetch(
                """SELECT role, content FROM (
                       SELECT role, content, seq FROM chat_messages WHERE session_id = $1
                        ORDER BY seq DESC LIMIT $2) t ORDER BY seq""",
                session_id, SUMMARY_MESSAGES,
            )
        if not msgs:
            return
        previous = row["summary"]
        if isinstance(previous, str):
            previous = json.loads(previous)
        transcript = "\n\n".join(
            f"[{'사용자' if m['role'] == 'user' else '상담 AI'}] {m['content'][:SUMMARY_MSG_CHARS]}" for m in msgs)
        prompt = _SUMMARY_PROMPT.format(previous=(previous or {}).get("overall") or "없음", transcript=transcript)

        # 사용자가 고른 모델(고가·로컬일 수 있음) 대신 기본 모델로 요약한다. 비용은 사용량에 함께 기록.
        cost_cb = CostTrackingCallback(MODEL_REGISTRY)
        llm = await MODEL_MANAGER.get_model(None)
        result: SessionSummary = await llm.with_structured_output(SessionSummary).ainvoke(
            [SystemMessage(content=prompt), HumanMessage(content="위 세션을 요약하세요.")],
            {"callbacks": [cost_cb]},
        )
        summary = {"overall": result.overall.strip(), "points": [p.strip() for p in result.points if p.strip()][:4]}
        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE chat_sessions SET summary = $2::jsonb, summary_updated_at = now() WHERE id = $1",
                session_id, json.dumps(summary, ensure_ascii=False),
            )
        await record_turn(user_id=user_id, session_id=session_id, turn_id=turn_id,
                          cost_cb=cost_cb, law_cb=None, state=None)
    except Exception as e:
        print(f"[insights] 세션 요약 실패(무시): {type(e).__name__}: {e}")
