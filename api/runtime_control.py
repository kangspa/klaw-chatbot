"""
실행 중인 백엔드 서버를 스스로 내리기 위한 연결점.

LiteLLM처럼 백엔드 자신이 쓰는 패키지를 바꾸려면 서버를 내린 뒤 설치하고 다시 띄워야 한다
(scripts/apply_litellm_update.py가 담당). main.py가 uvicorn.Server를 여기에 등록해 두면
API가 정상 종료(lifespan shutdown 포함)를 요청할 수 있다.
`uvicorn main:app`처럼 다른 방식으로 띄우면 등록되지 않아 자동 재시작을 쓸 수 없다.

데스크톱 앱(launcher)은 앱 전체를 다시 띄우는 함수(RESTART_FN)를 등록한다 — exe에서 LiteLLM을 바꿀 때
새 프로세스를 띄우고 지금 프로세스는 정상 종료한다 (api/litellm_overlay.py).
"""
import asyncio
import threading
from typing import Any, Callable, Optional

SERVER: Optional[Any] = None       # uvicorn.Server
LAUNCH_ARGS: list[str] = []        # 다시 띄울 때 쓸 명령 (main.py가 채운다)
_LOOP: Optional[asyncio.AbstractEventLoop] = None
RESTART_FN: Optional[Callable[[], None]] = None   # 데스크톱 앱 전체 재시작 (launcher가 등록)


def register(server: Any, launch_args: list[str]) -> None:
    global SERVER, LAUNCH_ARGS
    SERVER = server
    LAUNCH_ARGS = list(launch_args)


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """서버 이벤트 루프 (다른 스레드에서 종료를 요청할 때 쓴다). lifespan startup에서 등록한다."""
    global _LOOP
    _LOOP = loop


def register_restart(fn: Callable[[], None]) -> None:
    global RESTART_FN
    RESTART_FN = fn


def can_restart() -> bool:
    return SERVER is not None


def can_restart_app() -> bool:
    return RESTART_FN is not None


def request_restart(delay: float = 1.5) -> None:
    """데스크톱 앱 전체를 다시 띄운다 (마지막 응답이 나가도록 잠시 뒤에)."""
    if RESTART_FN is None:
        raise RuntimeError("데스크톱 앱(launcher)으로 실행하지 않아 앱을 다시 띄울 수 없습니다.")
    threading.Timer(delay, RESTART_FN).start()


def request_shutdown(delay: float = 1.5) -> None:
    """
    서버를 정상 종료시킨다. 마지막 응답(진행 상태 조회 등)이 나갈 수 있도록 잠시 뒤에 내린다.
    다른 스레드에서 불러도 된다.
    """
    if SERVER is None or _LOOP is None:
        raise RuntimeError("main.py로 실행한 서버가 아니라 자동으로 재시작할 수 없습니다.")

    def _exit() -> None:
        SERVER.should_exit = True

    _LOOP.call_soon_threadsafe(lambda: _LOOP.call_later(delay, _exit))
