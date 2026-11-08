"""
서비스 스택(도커 컨테이너) 위치·포트·compose 명령 — 개발(저장소)과 패키징(exe) 공통.

  BUNDLE  읽기 전용 자원 (compose 파일·Dockerfile·korean-law-mcp 소스·프론트 빌드·seed 등)
          개발: 저장소 루트 / exe: PyInstaller가 풀어 둔 임시 폴더(sys._MEIPASS)
  HOME    쓰기 가능한 스택 루트 (.env, docker-compose.yml 사본, 데이터 폴더, .runtime, logs)
          개발: 저장소 루트 / exe: %LOCALAPPDATA%\\KLaw  (KLAW_HOME으로 바꿀 수 있다)
  PROJECT docker compose 프로젝트 이름 — 개발 'klaw-chatbot' / exe 'klaw' (KLAW_PROJECT)
          컨테이너 이름을 고정하지 않으므로 같은 PC에서 두 스택이 함께 떠도 이름이 겹치지 않는다.

포트: 각 서비스의 호스트 포트를 .env의 KLAW_PORT_*로 정한다 (없으면 기본값). exe는 시작할 때 ensure_ports()로
기본 포트가 다른 프로그램에 쓰이고 있으면 빈 포트를 골라 .env에 기억한다 → 다른 PC에서도 포트 충돌이 없다.
앱은 apply_env()로 그 포트에서 DB·Qdrant·검색·법령 MCP·Ollama 주소를 만든다.
"""
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

FROZEN = bool(getattr(sys, "frozen", False))
REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = Path(getattr(sys, "_MEIPASS", REPO_ROOT))


def _default_home() -> Path:
    if FROZEN:
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "KLaw"
    return REPO_ROOT


HOME = Path(os.getenv("KLAW_HOME") or _default_home())
PROJECT = os.getenv("KLAW_PROJECT") or ("klaw" if FROZEN else "klaw-chatbot")
COMPOSE_FILE = HOME / "docker-compose.yml"
OVERRIDE_FILE = HOME / "docker-compose.override.yml"
RUNTIME_DIR = HOME / ".runtime"
LOG_DIR = HOME / "logs"

# 서비스 → (포트 환경 변수, 컨테이너 포트, 기본 호스트 포트)
SERVICE_PORTS: dict[str, list[tuple[str, int, int]]] = {
    "postgres": [("KLAW_PORT_POSTGRES", 5432, 5432)],
    "qdrant": [("KLAW_PORT_QDRANT", 6333, 6333), ("KLAW_PORT_QDRANT_GRPC", 6334, 6334)],
    "searxng": [("KLAW_PORT_SEARXNG", 8080, 8888)],
    "korean-law-mcp": [("KLAW_PORT_LAW_MCP", 3000, 3000)],
    "ollama": [("KLAW_PORT_OLLAMA", 11434, 11434)],
}
WEB_PORT_ENV, WEB_PORT_DEFAULT = "KLAW_PORT_WEB", 8008


def port(env: str) -> int:
    """KLAW_PORT_* 값 (없으면 기본값)."""
    for specs in SERVICE_PORTS.values():
        for name, _, default in specs:
            if name == env:
                return int(os.getenv(env) or default)
    if env == WEB_PORT_ENV:
        return int(os.getenv(env) or WEB_PORT_DEFAULT)
    raise KeyError(env)


def apply_env() -> None:
    """
    서비스 포트로 앱이 쓰는 접속 주소를 만든다 (.env를 읽은 뒤, 앱 모듈을 import하기 전에 부른다).
    DB_PORT 등 예전 변수는 KLAW_PORT_*가 있으면 그 값을 따른다 — 포트를 바꾸면 주소도 함께 바뀌도록.
    """
    os.environ["DB_HOST"] = os.getenv("DB_HOST") or "127.0.0.1"
    os.environ["DB_PORT"] = str(port("KLAW_PORT_POSTGRES"))
    os.environ["QDRANT_HOST"] = "127.0.0.1"
    os.environ["QDRANT_PORT"] = str(port("KLAW_PORT_QDRANT"))
    os.environ["QDRANT_GRPC_PORT"] = str(port("KLAW_PORT_QDRANT_GRPC"))
    os.environ["SEARXNG_URL"] = f"http://127.0.0.1:{port('KLAW_PORT_SEARXNG')}"
    os.environ["MCP_KOREAN_LAW_URL"] = f"http://127.0.0.1:{port('KLAW_PORT_LAW_MCP')}/mcp"
    os.environ["LAW_MCP_HEALTH_URL"] = f"http://127.0.0.1:{port('KLAW_PORT_LAW_MCP')}/health"
    os.environ["OLLAMA_BASE_URL"] = f"http://127.0.0.1:{port('KLAW_PORT_OLLAMA')}"


# ────────────────────────── 포트 ──────────────────────────
def port_free(p: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE if hasattr(socket, "SO_EXCLUSIVEADDRUSE")
                     else socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", p))
            return True
        except OSError:
            return False


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def published_ports() -> dict[str, set[int]]:
    """이 프로젝트의 실행 중인 컨테이너가 쓰고 있는 호스트 포트 {서비스: {포트}} — 우리 포트는 '사용 중'으로 보면 안 된다."""
    out = run(["docker", "ps", "--filter", f"label=com.docker.compose.project={PROJECT}",
               "--format", '{{.Label "com.docker.compose.service"}}|{{.Ports}}'], timeout=20)
    used: dict[str, set[int]] = {}
    for line in out.stdout.splitlines():
        service, _, ports = line.partition("|")
        for chunk in ports.split(","):
            # '127.0.0.1:5432->5432/tcp'
            left = chunk.strip().split("->", 1)[0]
            if ":" in left and left.rsplit(":", 1)[1].isdigit():
                used.setdefault(service, set()).add(int(left.rsplit(":", 1)[1]))
    return used


def ensure_ports(log=print) -> dict[str, int]:
    """
    각 서비스의 호스트 포트를 정한다. 지금 값이 다른 프로그램에 쓰이고 있으면 빈 포트를 골라 .env에 기억한다
    (우리 컨테이너가 이미 쓰고 있는 포트는 그대로 둔다). 웹 UI 포트도 같은 방식이다.
    """
    from api.env_store import write_env_values
    ours = published_ports()
    chosen: dict[str, str] = {}
    taken: set[int] = set()
    for service, specs in SERVICE_PORTS.items():
        for env, _, _ in specs:
            p = port(env)
            if p in ours.get(service, set()) or (p not in taken and port_free(p)):
                taken.add(p)
                continue
            new = free_port()
            while new in taken:
                new = free_port()
            log(f"[stack] {service} 포트 {p} 사용 중 → {new}")
            chosen[env] = str(new)
            taken.add(new)
    web = port(WEB_PORT_ENV)
    if web in taken or not port_free(web):
        new = free_port()
        log(f"[stack] 웹 UI 포트 {web} 사용 중 → {new}")
        chosen[WEB_PORT_ENV] = str(new)
    if chosen:
        write_env_values(chosen, comment="서비스 포트 — 기본 포트가 사용 중이라 KLaw가 고른 값 (지워도 다시 고른다)")
    return {env: port(env) for env in [e for s in SERVICE_PORTS.values() for e, _, _ in s] + [WEB_PORT_ENV]}


# ────────────────────────── 명령 ──────────────────────────
def run(cmd: list[str], timeout: int = 60, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, **kw)


def compose_cmd(*args: str) -> list[str]:
    """이 스택의 docker compose 명령 (프로젝트·파일을 명시 — 어느 폴더에서 불러도 같은 스택을 가리킨다)."""
    cmd = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE)]
    if OVERRIDE_FILE.exists():   # -f를 주면 override가 자동으로 붙지 않는다
        cmd += ["-f", str(OVERRIDE_FILE)]
    return cmd + list(args)


def service_container(service: str) -> Optional[str]:
    """서비스의 컨테이너 ID (실행 중이 아니어도 찾는다). 컨테이너 이름을 고정하지 않으므로 라벨로 찾는다."""
    try:
        out = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}",
                   "--filter", f"label=com.docker.compose.service={service}"], timeout=15)
    except Exception:
        return None
    ids = out.stdout.split()
    return ids[0] if out.returncode == 0 and ids else None
