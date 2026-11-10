"""
대시보드 API — 로그인한 사용자 본인의 데이터만 집계한다.

  GET /chatbot/dashboard/usage     기간별 모델 사용량·예상 비용 (일별 × 모델)
  GET /chatbot/dashboard/laws      기간 내 자주 조회한 법령과 많이 본 조문
  GET /chatbot/dashboard/sessions  기간 내 활동한 세션의 요약 (최근 활동순)

기간은 사용자의 로컬 날짜(start~end, 양 끝 포함)로 받고, tz(IANA 이름)로 하루의 경계를 정한다.
"""
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth.deps import get_current_user
from api.chatbot.functions.insights import schedule_session_summary
from api.db import get_pool, is_ready
from api.load_variable import MODEL_REGISTRY

router = APIRouter(prefix="/dashboard", dependencies=[Depends(get_current_user)])

MAX_RANGE_DAYS = 366


def _checked_range(start: date, end: date, tz: str) -> tuple[ZoneInfo, datetime, datetime]:
    """
    기간 검증 후 (시간대, UTC 시작 시각, UTC 끝 시각)을 돌려준다.
    사용자 로컬 날짜 [start, end] → UTC 시각 범위 [lo, hi). SQLite에는 시간대 기능이 없어 Python에서 계산한다.
    """
    if end < start:
        raise HTTPException(status_code=400, detail="종료일이 시작일보다 앞설 수 없습니다.")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"조회 기간은 최대 {MAX_RANGE_DAYS}일입니다.")
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=400, detail=f"알 수 없는 시간대입니다: {tz}")
    lo = datetime.combine(start, time.min, zone).astimezone(timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    return zone, lo, hi


def _require_db() -> None:
    if not is_ready():
        raise HTTPException(status_code=503, detail="데이터베이스에 연결되지 않았습니다.")


# 기간 조건: created_at(UTC 문자열)이 [$2, $3) 안 — 파라미터 datetime은 api/db.py가 같은 형식의 문자열로 바꾼다
_RANGE_SQL = "created_at >= $2 AND created_at < $3"


@router.get("/usage")
async def usage(
    start: date = Query(..., description="시작일 (YYYY-MM-DD, 포함)"),
    end: date = Query(..., description="종료일 (YYYY-MM-DD, 포함)"),
    tz: str = Query("Asia/Seoul", description="날짜 경계를 정할 시간대 (IANA 이름)"),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """일별·모델별 토큰과 예상 비용. 모델은 전체 모델 목록 순서(order)로 색을 고정할 수 있게 함께 내려준다."""
    _require_db()
    zone, lo, hi = _checked_range(start, end, tz)
    async with get_pool().acquire() as conn:
        raw = await conn.fetch(
            f"""SELECT created_at, model, calls, input_tokens, cache_read_tokens, output_tokens, cost_usd
                  FROM llm_usage WHERE user_id = $1 AND {_RANGE_SQL}""",
            current_user["id"], lo, hi,
        )
        turns = await conn.fetchval(
            f"SELECT count(DISTINCT turn_id) FROM llm_usage WHERE user_id = $1 AND {_RANGE_SQL}",
            current_user["id"], lo, hi,
        )

    # 사용자 시간대의 날짜 × 모델로 합친다 (DST가 있는 시간대도 행마다 변환하므로 정확하다)
    agg: dict[tuple[date, str], dict[str, Any]] = {}
    for r in raw:
        day = r["created_at"].astimezone(zone).date()
        a = agg.setdefault((day, r["model"]), {"day": day, "model": r["model"], "calls": 0, "input_tokens": 0,
                                               "cache_read_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        a["calls"] += r["calls"] or 0
        a["input_tokens"] += r["input_tokens"] or 0
        a["cache_read_tokens"] += r["cache_read_tokens"] or 0
        a["output_tokens"] += r["output_tokens"] or 0
        a["cost_usd"] += float(r["cost_usd"] or 0)
    rows = [agg[k] for k in sorted(agg)]

    specs = {s.id: (i, s) for i, s in enumerate(await MODEL_REGISTRY.list_all())}
    used = sorted({r["model"] for r in rows}, key=lambda m: (specs.get(m, (10_000, None))[0], m))
    models = []
    for m in used:
        order, spec = specs.get(m, (None, None))
        models.append({"id": m, "label": spec.label if spec else m, "group": spec.group if spec else "",
                       "order": order})
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "tz": tz,
                  "days": [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]},
        "models": models,
        "turns": turns or 0,
        "daily": [{
            "date": r["day"].isoformat(), "model": r["model"], "calls": r["calls"],
            "inputTokens": r["input_tokens"], "cacheReadTokens": r["cache_read_tokens"],
            "outputTokens": r["output_tokens"], "costUsd": r["cost_usd"],
        } for r in rows],
    }


@router.get("/laws")
async def laws(
    start: date = Query(...), end: date = Query(...), tz: str = Query("Asia/Seoul"),
    limit: int = Query(10, ge=1, le=50),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """기간 내 질문에서 조회된 법령 순위. count = 그 법령을 조회한 질문(턴) 수."""
    _require_db()
    _, lo, hi = _checked_range(start, end, tz)
    async with get_pool().acquire() as conn:
        # 턴 = (session_id, turn_id) 쌍. SQLite에는 행 값 DISTINCT가 없어 문자열로 이어 붙여 센다
        rows = await conn.fetch(
            f"""SELECT law_name,
                       count(DISTINCT coalesce(session_id, '') || char(31) || coalesce(turn_id, '')) AS turns,
                       count(DISTINCT session_id) AS sessions, max(created_at) AS last_at
                  FROM law_lookups WHERE user_id = $1 AND {_RANGE_SQL}
                 GROUP BY law_name ORDER BY turns DESC, last_at DESC LIMIT $4""",
            current_user["id"], lo, hi, limit,
        )
        names = [r["law_name"] for r in rows]
        art_rows = await conn.fetch(
            f"""SELECT law_name, article, count(*) AS cnt
                  FROM law_lookups
                 WHERE user_id = $1 AND {_RANGE_SQL} AND article IS NOT NULL AND law_name = ANY($4)
                 GROUP BY law_name, article ORDER BY cnt DESC, article""",
            current_user["id"], lo, hi, names,
        )
    articles: dict[str, list[dict]] = {}
    for a in art_rows:
        lst = articles.setdefault(a["law_name"], [])
        if len(lst) < 4:
            lst.append({"article": a["article"], "count": a["cnt"]})
    return [{
        "lawName": r["law_name"], "turns": r["turns"], "sessions": r["sessions"],
        "lastViewedAt": r["last_at"].isoformat(), "articles": articles.get(r["law_name"], []),
    } for r in rows]


@router.get("/sessions")
async def sessions(
    start: date = Query(...), end: date = Query(...), tz: str = Query("Asia/Seoul"),
    limit: int = Query(12, ge=1, le=50),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    """
    기간 내 마지막 활동이 있는 세션의 요약 (대시보드 기간 필터와 같은 범위).
    요약이 아직 없는 세션(기능 도입 전 대화 등)은 첫 질문을 함께 내려주고, 백그라운드에서 요약을 만든다
    (기본 모델로 세션당 1회, 비용은 사용량에 기록 — 다음 조회부터 요약이 보인다).
    """
    _require_db()
    _, lo, hi = _checked_range(start, end, tz)
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT s.id, s.title, s.created_at, s.updated_at, s.summary, s.summary_updated_at,
                      s.parent_session_id,
                      (SELECT count(*) FROM chat_messages m WHERE m.session_id = s.id) AS message_count,
                      (SELECT m.content FROM chat_messages m WHERE m.session_id = s.id AND m.role = 'user'
                        ORDER BY m.seq LIMIT 1) AS first_question
                 FROM chat_sessions s
                WHERE s.owner_id = $1
                  AND s.updated_at >= $2 AND s.updated_at < $3
                ORDER BY s.updated_at DESC LIMIT $4""",
            current_user["id"], lo, hi, limit,
        )
    out = []
    for r in rows:
        if r["summary"] is None and r["message_count"] > 0:
            schedule_session_summary(user_id=current_user["id"], session_id=r["id"], turn_id=None)
        summary: Optional[Any] = r["summary"]
        if isinstance(summary, str):   # (api/db.py가 JSON 컬럼을 dict로 돌려주지만, 혹시 문자열이면 직접 푼다)
            summary = json.loads(summary)
        out.append({
            "id": r["id"], "title": r["title"], "createdAt": r["created_at"].isoformat(),
            "updatedAt": r["updated_at"].isoformat(), "messageCount": r["message_count"],
            "isBranch": r["parent_session_id"] is not None,
            "firstQuestion": (r["first_question"] or "")[:300],
            "summary": summary,
            "summaryUpdatedAt": r["summary_updated_at"].isoformat() if r["summary_updated_at"] else None,
        })
    return out
