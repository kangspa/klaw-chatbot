"""
법령 MCP(법제처 Open API) 인증 상태 확인.

문제 배경:
  LAW_OC 인증키가 만료·오설정되면 korean-law-mcp의 모든 검색이 실패하는데,
  그 실패가 '검색 결과 없음'과 구분되지 않아서 재검색 5회를 소진한 뒤
  "다른 검색어로 다시 질문해주세요"라는 엉뚱한 안내가 나간다.
  원인은 시스템(인증)인데 사용자에게는 질문이 잘못된 것처럼 보이는 사고다.

대응 (apr-ai regulatory_affairs의 preflight 패턴을 이식):
  1. 서버 시작 시 canary 조회 — 반드시 존재하는 법령('민법')을 search_law로 검색해
     인증이 실제로 동작하는지 확인하고, 실패하면 로그에 크게 경고를 남긴다.
     (서버 기동 자체는 막지 않는다 — 비법률 대화는 여전히 가능해야 하므로)
  2. 대화 중 도구 응답에서 인증 실패 시그니처를 감지하면, 재검색을 즉시 중단하고
     사용자에게 '시스템 문제'로 안내한다 (질문을 바꾸라는 안내가 아니라).
"""

from datetime import datetime, timezone
from typing import Any, Optional

# 법제처 Open API 인증/키 실패를 나타내는 응답 시그니처.
# (OC 값이 잘못되면 API가 정상 XML 대신 이런 문구가 담긴 안내 페이지를 반환한다)
AUTH_FAIL_SIGNS = (
    "API 키가 필요합니다",
    "사용자 정보 검증에 실패",
    "사용자 검증",          # "OPEN API 호출 시 사용자 검증을 위하여..."
    "인증키",
    "OC 값",
    "OC를 확인",
)

# canary로 조회할, 반드시 존재하는 법령 (결과가 없으면 인증/연결 이상)
CANARY_QUERY = "민법"

# 서버 시작 시 preflight 결과 (런타임 감지와 별개인 '조기 경보'용)
_STATUS: dict[str, Any] = {"ok": None, "detail": "미확인", "checked_at": None}


def looks_like_auth_failure(text: str) -> bool:
    """도구 응답 텍스트에 인증 실패 시그니처가 있는지 판별."""
    if not text:
        return False
    return any(sign in text for sign in AUTH_FAIL_SIGNS)


def auth_status() -> dict[str, Any]:
    return dict(_STATUS)


async def preflight_law_auth() -> bool:
    """
    서버 시작 시 1회 호출. canary 법령을 검색해 인증 동작을 확인한다.

    실패해도 예외를 던지지 않고 False를 반환한다 — 기동은 계속하되,
    실제 요청에서의 최종 방어는 대화 중 시그니처 감지(looks_like_auth_failure)가 담당한다.
    """
    from api.load_variable import LAW_TOOLS

    _STATUS["checked_at"] = datetime.now(timezone.utc).isoformat()

    tool = next((t for t in LAW_TOOLS if getattr(t, "name", "") == "search_law"), None)
    if tool is None:
        _STATUS.update(ok=False, detail="search_law 도구를 찾을 수 없음 (MCP 서버 연결 실패?)")
        _warn(_STATUS["detail"])
        return False

    try:
        res = await tool.ainvoke({"query": CANARY_QUERY})
    except Exception as e:
        # 네트워크/서버 오류도 canary 실패로 기록 (docker가 아직 안 떴을 수 있음)
        _STATUS.update(ok=False, detail=f"canary 조회 호출 실패: {type(e).__name__}: {e}")
        _warn(_STATUS["detail"])
        return False

    text = res if isinstance(res, str) else str(res)
    if looks_like_auth_failure(text):
        _STATUS.update(ok=False, detail=f"인증 실패 시그니처 감지: {text[:200]}")
        _warn(_STATUS["detail"])
        return False
    if "법령ID" not in text and "MST" not in text:
        _STATUS.update(ok=False, detail=f"canary('{CANARY_QUERY}') 정상 결과 없음: {text[:200]}")
        _warn(_STATUS["detail"])
        return False

    _STATUS.update(ok=True, detail="정상")
    print(f"[law-auth] preflight 통과 - 법제처 Open API 인증 정상 (canary: {CANARY_QUERY})")
    return True


def _warn(detail: str) -> None:
    print("=" * 70)
    print("[law-auth] [!] 법령 MCP 인증 preflight 실패 - 법률 검색이 동작하지 않을 수 있습니다.")
    print(f"[law-auth]     {detail}")
    print("[law-auth]     korean-law-mcp/.env의 LAW_OC 인증키와 컨테이너 상태를 확인하세요.")
    print("=" * 70)


def find_auth_failure(attempts: list[dict]) -> Optional[str]:
    """
    이번 턴의 검색 시도 이력(summarize_law_search 결과)에서 인증 실패를 찾는다.
    발견하면 해당 응답 요약을 반환한다 (없으면 None).
    """
    for a in attempts:
        if not a.get("ok") and looks_like_auth_failure(a.get("reason", "")):
            return a["reason"][:200]
    return None
