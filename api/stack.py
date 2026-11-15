"""
앱 위치·포트 — 개발(저장소)과 패키징(exe) 공통. Docker 없이 한 프로세스 안(과 자식 프로세스)에서 모두 돈다.

  BUNDLE   읽기 전용 자원 (프론트 빌드·seed·법령 MCP·node.exe 등)
           개발: 저장소 루트 / exe: PyInstaller가 풀어 둔 임시 폴더(sys._MEIPASS)
  HOME     쓰기 가능한 앱 폴더 (.env, data/, logs/, .runtime/)
           개발: 저장소 루트 / exe: %LOCALAPPDATA%\\KLaw  (KLAW_HOME으로 바꿀 수 있다)
  DATA_DIR 대화 DB(SQLite)·벡터(Qdrant 로컬 모드)·Ollama 엔진과 모델
           → 이 폴더(HOME)만 지우면 PC에 남는 것이 없다.

포트: 열리는 것은 모두 127.0.0.1뿐이다. 다른 프로그램이 쓰고 있으면 실행할 때마다 자동으로 빈 포트를 고른다.
  - 웹 UI: 후보 8008~8012를 차례로 시도한다 (KLAW_PORT_WEB을 주면 그 포트를 먼저). 후보를 정해 두는 이유는
           Google 계정 연결이 '승인된 JavaScript 원본'에 등록된 주소에서만 되기 때문이다 — 이 다섯 주소를 등록해 두면
           어느 포트가 잡혀도 연결이 된다. 후보가 모두 쓰이고 있으면 아무 빈 포트로 열고, Google 연결만 안내로 바뀐다.
  - 법령 MCP·Ollama: 자식 프로세스라 빈 포트 아무 것이나 쓴다 (기본 39300·39301 우선).
'사용 중' 판단: 127.0.0.1에 묶어 볼 뿐 아니라 127.0.0.1·::1로 접속도 해 본다 — 다른 프로그램이 IPv6(::1)에만
떠 있으면 묶기는 성공해도 브라우저의 localhost가 그쪽으로 가기 때문이다 (예: 다른 Vite 개발 서버).
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
WEB_PORT_CANDIDATES = (8008, 8009, 8010, 8011, 8012)   # Google '승인된 JavaScript 원본'에 등록할 주소들
WEB_PORT_FILE = RUNTIME_DIR / "web_port"               # 지금 쓰는 웹 포트 (Vite 개발 서버 프록시가 읽는다)
# 자식 프로세스 → (포트 환경 변수, 기본 포트). 기본 포트가 사용 중이면 그때그때 빈 포트를 쓴다.
CHILD_PORTS = {"law_mcp": ("KLAW_PORT_LAW_MCP", 39300), "ollama": ("KLAW_PORT_OLLAMA", 39301)}

for _d in (HOME, DATA_DIR, RUNTIME_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 라이브러리가 사용자 폴더(~/.paddlex, ~/.cache/huggingface …)나 임시 폴더에 받아 두는 캐시를 앱 폴더 안으로 돌린다
# → 앱 폴더만 지우면 PC에 남는 것이 없다 (마이 페이지 '앱 데이터 모두 삭제', api/uninstall.py). 사용자가 직접 준 값이 우선.
for _env, _path in {
    "PADDLE_PDX_CACHE_HOME": DATA_DIR / "paddlex",          # PaddleOCR(PaddleX) 모델
    "HF_HOME": DATA_DIR / "huggingface",                    # Hugging Face 다운로드
    "MODELSCOPE_CACHE": DATA_DIR / "modelscope",            # PaddleX가 고를 수 있는 다른 모델 저장소
    "CRAWL4_AI_BASE_DIRECTORY": DATA_DIR,                   # crawl4ai 캐시 DB (.crawl4ai)
}.items():
    os.environ.setdefault(_env, str(_path))


# ────────────────────────── 배포 기본값 ──────────────────────────
def app_default(name: str) -> str:
    """
    exe에 넣어 둔 기본값 (scripts/build_exe.py가 app_defaults.json으로 만든다). 예: GOOGLE_CLIENT_ID —
    공개값이라 exe에 넣어도 되며, 받아서 쓰는 사람이 따로 설정하지 않아도 Google 연결이 된다. .env 값이 항상 우선.
    """
    import json
    try:
        return str(json.loads((BUNDLE / "app_defaults.json").read_text(encoding="utf-8")).get(name) or "")
    except (OSError, ValueError):
        return ""


# ────────────────────────── 포트 ──────────────────────────
def _someone_listening(p: int) -> bool:
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex((host, p)) == 0:
                    return True
        except OSError:
            pass
    return False


def port_free(p: int) -> bool:
    """127.0.0.1에 묶을 수 있고, 127.0.0.1·::1 어느 쪽에서도 다른 프로그램이 받고 있지 않은 포트."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            return False
    return not _someone_listening(p)


def free_port() -> int:
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        if not _someone_listening(p):
            return p
    return p


def web_port() -> int:
    return int(os.getenv(WEB_PORT_ENV) or WEB_PORT_DEFAULT)


def ensure_web_port(log=print) -> int:
    """
    웹 UI 포트를 고른다 (실행할 때마다). KLAW_PORT_WEB → 후보 8008~8012 → 아무 빈 포트 순.
    고른 값은 기억하지 않는다 — 다음 실행에서 8008이 비어 있으면 다시 8008을 쓴다.
    """
    children = {os.getenv(env) for env, _ in CHILD_PORTS.values()}
    preferred = [int(os.environ[WEB_PORT_ENV])] if os.getenv(WEB_PORT_ENV) else []
    for p in preferred + [c for c in WEB_PORT_CANDIDATES if c not in preferred]:
        if str(p) not in children and port_free(p):
            if p != (preferred or [WEB_PORT_DEFAULT])[0]:
                log(f"[stack] 웹 UI 포트 {(preferred or [WEB_PORT_DEFAULT])[0]} 사용 중 → {p}")
            break
    else:
        p = free_port()
        log(f"[stack] 웹 UI 후보 포트({WEB_PORT_CANDIDATES[0]}~{WEB_PORT_CANDIDATES[-1]})가 모두 사용 중 → {p} "
            "(이 주소에서는 Google 계정 연결이 되지 않습니다)")
    os.environ[WEB_PORT_ENV] = str(p)
    try:
        WEB_PORT_FILE.write_text(str(p), encoding="utf-8")
    except OSError:
        pass
    return p


def google_origin_ok(port: int) -> bool:
    return port in WEB_PORT_CANDIDATES


def apply_env() -> None:
    """
    자식 프로세스 포트를 정하고 앱이 쓰는 접속 주소를 환경 변수로 만든다 (앱 모듈을 import하기 전에 한 번).
    KLAW_PORT_LAW_MCP·KLAW_PORT_OLLAMA를 직접 주면 그 포트를 쓴다.
    """
    if os.environ.get("_KLAW_ENV_APPLIED") == "1":
        return
    taken: set[int] = set(WEB_PORT_CANDIDATES) | {web_port()}   # 웹 UI 후보 포트는 비워 둔다
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
