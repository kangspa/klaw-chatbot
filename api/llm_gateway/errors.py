"""
제공자 SDK마다 다른 예외 타입을 '일시 오류(재시도할 가치가 있음)'로 분류한다.

제공자별 예외 클래스를 나열하는 대신 HTTP 상태 코드와 네트워크 예외로 판정한다.
LangChain 통합이 원본 예외를 감싸 다시 던지는 경우가 있어 __cause__ 사슬까지 확인한다.
"""
import asyncio

import httpx

# 408 타임아웃 / 429 한도 / 5xx 서버 오류 / 529 Anthropic 과부하
_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504, 529}


def _status_of(e: BaseException):
    for attr in ("status_code", "code", "status"):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(e, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def is_transient_error(e: BaseException) -> bool:
    seen = set()
    cur = e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError,
                            asyncio.TimeoutError, ConnectionError)):
            return True
        # OpenAI/Anthropic SDK의 연결·타임아웃 예외는 상태 코드가 없다
        if type(cur).__name__ in ("APIConnectionError", "APITimeoutError"):
            return True
        if _status_of(cur) in _TRANSIENT_STATUS:
            return True
        cur = cur.__cause__ or cur.__context__
    return False
