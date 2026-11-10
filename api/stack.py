"""
앱 위치·포트 — 개발(저장소)과 패키징(exe) 공통. Docker 없이 한 프로세스 안(과 자식 프로세스)에서 모두 돈다.

  BUNDLE   읽기 전용 자원 (프론트 빌드·seed·법령 MCP·node.exe 등)
           개발: 저장소 루트 / exe: PyInstaller가 풀어 둔 임시 폴더(sys._MEIPASS)
  HOME     쓰기 가능한 앱 폴더 (.env, data/, logs/, .runtime/)
           개발: 저장소 루트 / exe: %LOCALAPPDATA%\\KLaw  (KLAW_HOME으로 바꿀 수 있다)
  DATA_DIR 대화 DB(SQLite)·벡터(Qdrant 로컬 모드)·Ollama 엔진과 모델
           → 이 폴더(HOME)만 지우면 PC에 남는 것이 없다.

포트: 열리는 것은 모두 127.0.0.1뿐이다.
  - 웹 UI: KLAW_PORT_WEB(기본 8008). 사용 중이면 빈 포트를 골라 .env에 기억한다 (Google 연결 주소가 바뀌지 않도록).
  - 법령 MCP·Ollama: 자식 프로세스라 실행할 때마다 빈 포트를 고른다 (기본 포트 우선). 다른 PC의 프로그램과 겹치지 않는다.
"""
import os
import socket
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = Path(getattr(sys, "_MEIPASS", REPO_ROOT))


def _default_home() -> Path:
    if FROZEN:
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "KLaw"
    return REPO_ROOT


HOME = Path(os.getenv("KLAW_HOME") or _default_home())
DATA_DIR = HOME / "data"
RUNTIME_DIR = HOME / ".runtime"
LOG_DIR = HOME / "logs"
QDRANT_PATH = DATA_DIR / "qdrant"
OLLAMA_DIR = DATA_DIR / "ollama"            # engine/<버전>/ollama.exe, models/, home/

WEB_PORT_ENV, WEB_PORT_DEFAULT = "KLAW_PORT_WEB", 8008
# 자식 프로세스 → (포트 환경 변수, 기본 포트). 기본 포트가 사용 중이면 그때그때 빈 포트를 쓴다.
CHILD_PORTS = {"law_mcp": ("KLAW_PORT_LAW_MCP", 39300), "ollama": ("KLAW_PORT_OLLAMA", 39301)}

for _d in (HOME, DATA_DIR, RUNTIME_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ────────────────────────── 포트 ──────────────────────────
def port_free(p: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", p))
            return True
        except OSError:
            return False


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def web_port() -> int:
    return int(os.getenv(WEB_PORT_ENV) or WEB_PORT_DEFAULT)


def ensure_web_port(log=print) -> int:
    """웹 UI 포트. 사용 중이면 빈 포트를 골라 .env에 기억한다 (다음 실행부터 같은 주소)."""
    p = web_port()
    if port_free(p):
        return p
    from api.env_store import write_env_values
    new = free_port()
    log(f"[stack] 웹 UI 포트 {p} 사용 중 → {new}")
    write_env_values({WEB_PORT_ENV: str(new)}, comment="웹 UI 포트 — 기본 포트가 사용 중이라 KLaw가 고른 값 (지워도 다시 고른다)")
    os.environ[WEB_PORT_ENV] = str(new)
    return new


def apply_env() -> None:
    """
    자식 프로세스 포트를 정하고 앱이 쓰는 접속 주소를 환경 변수로 만든다 (앱 모듈을 import하기 전에 한 번).
    KLAW_PORT_LAW_MCP·KLAW_PORT_OLLAMA를 직접 주면 그 포트를 쓴다.
    """
    if os.environ.get("_KLAW_ENV_APPLIED") == "1":
        return
    taken: set[int] = {web_port()}
    for name, (env, default) in CHILD_PORTS.items():
        if os.getenv(env):
            p = int(os.environ[env])
        else:
            p = default if default not in taken and port_free(default) else free_port()
            while p in taken:
                p = free_port()
            os.environ[env] = str(p)
        taken.add(p)
    law, ollama = int(os.environ["KLAW_PORT_LAW_MCP"]), int(os.environ["KLAW_PORT_OLLAMA"])
    os.environ["MCP_KOREAN_LAW_URL"] = f"http://127.0.0.1:{law}/mcp"
    os.environ["LAW_MCP_HEALTH_URL"] = f"http://127.0.0.1:{law}/health"
    os.environ["OLLAMA_BASE_URL"] = f"http://127.0.0.1:{ollama}"
    os.environ["_KLAW_ENV_APPLIED"] = "1"
