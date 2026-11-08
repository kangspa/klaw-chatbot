"""
KLaw 데스크톱 실행기 — exe의 진입점. 개발 중에도 `uv run python launcher.py`로 같은 흐름을 시험할 수 있다.

  1. 콘솔 창 없이 실행한다 (로그는 <HOME>/logs/klaw.log). exe에서 실행하는 docker 등 하위 프로그램도 창을 띄우지 않는다
  2. 이미 실행 중이면 새로 띄우지 않고 브라우저만 연다
  3. 시스템 트레이 아이콘: 열기(아이콘 클릭)·로그 폴더 열기·종료
  4. 백그라운드 준비: Docker Desktop 확인(꺼져 있으면 켠다) → 스택 파일·.env 준비 → GPU 설정(첫 실행)
     → 포트 정하기(사용 중이면 빈 포트) → docker compose up → 서비스 준비 대기 → 웹 서버 시작 → 브라우저 열기
  5. 종료: 웹 서버를 내리고 컨테이너를 멈춘다 (데이터는 남는다. KLAW_STOP_ON_EXIT=0이면 컨테이너는 그대로 둔다)

위치·포트·compose 명령은 api/stack.py (개발: 저장소 / exe: %LOCALAPPDATA%\\KLaw, 프로젝트 이름 klaw).
"""
import ctypes
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

from api import stack  # 가벼운 모듈 (앱 전체를 import하지 않는다)

APP_NAME = "KLaw"
APP_VERSION = "1.0.0"
DOCKER_DESKTOP_URL = "https://www.docker.com/products/docker-desktop/"


# ────────────────────────── 콘솔 없이 실행 ──────────────────────────
def setup_logging() -> Path:
    """창 없는 exe는 stdout/stderr가 없으므로 로그 파일로 보낸다 (5MB가 넘으면 하나 보관하고 새로 쓴다)."""
    stack.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = stack.LOG_DIR / "klaw.log"
    if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
        log_path.replace(log_path.with_suffix(".log.1"))
    if stack.FROZEN or sys.stdout is None:
        f = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
    print(f"\n===== {APP_NAME} {APP_VERSION} 시작 {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"(home={stack.HOME}, project={stack.PROJECT}, frozen={stack.FROZEN}) =====", flush=True)
    return log_path


def hide_child_windows() -> None:
    """
    창 없는 exe에서 docker·powershell 같은 콘솔 프로그램을 실행하면 매번 검은 창이 떴다 사라진다.
    subprocess로 띄우는 모든 프로그램에 CREATE_NO_WINDOW를 기본으로 붙인다 (Playwright·asyncio 하위 프로세스 포함).
    """
    if os.name != "nt" or not stack.FROZEN:
        return
    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched


# ────────────────────────── 한 번만 실행 ──────────────────────────
INSTANCE_FILE = stack.RUNTIME_DIR / "instance.json"


def acquire_single_instance() -> bool:
    """이미 실행 중이면 False. Windows 이름 있는 뮤텍스로 확인한다 (프로세스가 죽으면 자동으로 풀린다)."""
    if os.name != "nt":
        return True
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\KLawDesktop")
    acquire_single_instance.handle = handle   # 프로세스가 끝날 때까지 잡고 있는다
    return ctypes.windll.kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS


def open_existing_instance() -> None:
    try:
        port = json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))["port"]
        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception:
        pass


# ────────────────────────── 스택 준비 ──────────────────────────
STACK_ITEMS = ["docker-compose.yml", "docker", "korean-law-mcp"]   # exe에 묶어 온 서비스 정의 (버전마다 갱신)
STACK_KEEP = ["config"]                                            # 사용자가 고칠 수 있는 설정 (없을 때만 복사)


def prepare_home(log) -> None:
    """
    서비스 정의를 HOME에 풀고, .env가 없으면 만든다.
    exe: 묶어 온 자원(BUNDLE/stack)에서 / 개발 중 HOME을 저장소 밖으로 지정(KLAW_HOME)했을 때: 저장소에서.
    개발 중 HOME=저장소이면 아무것도 하지 않는다 (저장소 파일을 그대로 쓴다).
    """
    stack.HOME.mkdir(parents=True, exist_ok=True)
    stack.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if not stack.FROZEN and stack.HOME.resolve() == stack.REPO_ROOT.resolve():
        return
    src = stack.BUNDLE / "stack" if stack.FROZEN else stack.REPO_ROOT
    skip = shutil.ignore_patterns("node_modules", ".git", "dist", "*.log")
    marker = stack.RUNTIME_DIR / "stack_version"
    if not marker.exists() or marker.read_text(encoding="utf-8").strip() != APP_VERSION:
        log(f"[stack] 서비스 정의 풀기 → {stack.HOME}")
        for name in STACK_ITEMS:
            target = stack.HOME / name
            if (src / name).is_dir():
                shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(src / name, target, ignore=skip)
            elif (src / name).exists():
                shutil.copy2(src / name, target)
        marker.write_text(APP_VERSION, encoding="utf-8")
    for name in STACK_KEEP:
        if (src / name).exists() and not (stack.HOME / name).exists():
            shutil.copytree(src / name, stack.HOME / name, ignore=skip)
    env_file = stack.HOME / ".env"
    if not env_file.exists():
        log("[stack] .env 만들기 (API 키는 화면의 'API 키'에서 입력)")
        env_file.write_text(
            "# KLaw 설정 — API 키는 앱 화면의 'API 키'에서 입력·관리한다\n"
            "DB_USER=klaw\n"
            f"DB_PASSWORD={secrets.token_urlsafe(18)}\n"
            "DB_NAME=klaw\n"
            f"MCP_AUTH_TOKEN={secrets.token_urlsafe(32)}\n"
            "LAW_OC=\n",
            encoding="utf-8",
        )


def docker_ready() -> bool:
    try:
        return stack.run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20).returncode == 0
    except Exception:
        return False


def ensure_docker(app) -> None:
    """Docker Desktop이 꺼져 있으면 켜고 기다린다. 설치되어 있지 않으면 안내 후 중단."""
    if shutil.which("docker") is None:
        app.fail("Docker Desktop이 설치되어 있지 않습니다. 설치한 뒤 다시 실행하세요.", show_install=True)
        raise SystemExit
    if docker_ready():
        return
    app.set_status("Docker Desktop을 시작하는 중…")
    for base in (os.getenv("ProgramFiles", r"C:\Program Files"), os.getenv("LOCALAPPDATA", "")):
        exe = Path(base) / "Docker" / "Docker" / "Docker Desktop.exe"
        if exe.exists():
            subprocess.Popen([str(exe)])
            break
    deadline = time.time() + 300
    while time.time() < deadline:
        if docker_ready():
            return
        time.sleep(3)
    app.fail("Docker Desktop이 5분 안에 준비되지 않았습니다. Docker Desktop을 직접 켠 뒤 다시 실행하세요.")
    raise SystemExit


def setup_gpu_once(log) -> None:
    """첫 실행에 한 번 GPU를 감지해 Ollama 가속 설정(docker-compose.override.yml)을 만든다."""
    marker = stack.RUNTIME_DIR / "gpu_setup.json"
    if marker.exists():
        return
    from api.llm_gateway import gpu_setup
    gpus = gpu_setup.detect_host_gpus()
    accel = gpu_setup.choose_accel(gpus, gpu_setup.docker_platform())
    gpu_setup.write_override(accel, log=log)
    marker.write_text(json.dumps({"accel": accel, "gpus": gpus}, ensure_ascii=False), encoding="utf-8")
    log(f"[stack] GPU 설정: {gpu_setup.ACCEL_LABELS[accel]} ({', '.join(g['name'] for g in gpus) or 'GPU 없음'})")


def compose_up(app, log) -> None:
    first = stack.service_container("korean-law-mcp") is None
    app.set_status("처음 실행: 서비스를 준비하는 중 (몇 분 걸립니다)…" if first else "서비스를 시작하는 중…")
    if first:
        app.notify("처음 실행이라 서비스 이미지를 받고 만드는 중입니다. 몇 분 걸릴 수 있습니다.")
    out = stack.run(stack.compose_cmd("up", "-d"), timeout=3600, cwd=stack.HOME)
    log(out.stdout[-3000:] + out.stderr[-3000:])
    if out.returncode != 0:
        raise RuntimeError("docker compose up 실패: " + (out.stderr.strip().splitlines() or ["알 수 없음"])[-1])


def wait_services(app, log, timeout: int = 240) -> None:
    """Postgres가 healthy가 되고 법령 MCP가 응답할 때까지 기다린다 (앱이 시작하면서 바로 붙는다)."""
    app.set_status("서비스가 준비되기를 기다리는 중…")
    deadline = time.time() + timeout
    pg_ok = mcp_ok = False
    while time.time() < deadline and not (pg_ok and mcp_ok):
        if not pg_ok:
            cid = stack.service_container("postgres")
            if cid:
                st = stack.run(["docker", "inspect", "-f", "{{.State.Health.Status}}", cid], timeout=15).stdout.strip()
                pg_ok = st == "healthy"
        if not mcp_ok:
            try:
                with urllib.request.urlopen(os.environ["LAW_MCP_HEALTH_URL"], timeout=3) as r:
                    mcp_ok = r.status == 200
            except Exception:
                pass
        if not (pg_ok and mcp_ok):
            time.sleep(2)
    log(f"[stack] 준비 상태: postgres={pg_ok} 법령MCP={mcp_ok}")
    if not pg_ok:
        raise RuntimeError("Postgres가 준비되지 않았습니다.")


# ────────────────────────── 앱 ──────────────────────────
class DesktopApp:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.status = "시작하는 중…"
        self.ready = False
        self.port = None
        self.server = None
        self.server_thread = None
        self.icon = None
        self.show_install = False

    # 상태 표시
    def log(self, msg: str) -> None:
        print(msg, flush=True)

    def set_status(self, text: str) -> None:
        self.status = text
        self.log(f"[상태] {text}")
        if self.icon:
            self.icon.title = f"{APP_NAME} — {text}"[:120]
            self.icon.update_menu()

    def notify(self, text: str) -> None:
        try:
            if self.icon:
                self.icon.notify(text, APP_NAME)
        except Exception:
            pass

    def fail(self, text: str, show_install: bool = False) -> None:
        self.show_install = show_install
        self.set_status(f"오류: {text}")
        self.notify(text)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    # 트레이 메뉴
    def open_browser(self, *_):
        if self.ready:
            webbrowser.open(self.url)
        elif self.show_install:
            webbrowser.open(DOCKER_DESKTOP_URL)
        else:
            self.notify(self.status)

    def open_logs(self, *_):
        os.startfile(str(stack.LOG_DIR)) if os.name == "nt" else webbrowser.open(stack.LOG_DIR.as_uri())

    def quit(self, *_):
        threading.Thread(target=self._shutdown, daemon=True).start()

    def _shutdown(self):
        self.ready = False
        self.set_status("종료하는 중…")
        if self.server:
            self.server.should_exit = True
            if self.server_thread:
                self.server_thread.join(timeout=20)
        if os.getenv("KLAW_STOP_ON_EXIT", "1") != "0" and docker_ready():
            out = stack.run(stack.compose_cmd("stop"), timeout=180, cwd=stack.HOME)
            self.log(f"[stack] 컨테이너 정지 (종료 코드 {out.returncode})")
        INSTANCE_FILE.unlink(missing_ok=True)
        self.log(f"===== {APP_NAME} 종료 =====")
        if self.icon:
            self.icon.stop()

    def build_icon(self):
        import pystray
        from PIL import Image
        image = Image.open(stack.BUNDLE / "assets" / "klaw.png")
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: self.status if len(self.status) < 60 else self.status[:57] + "…", None, enabled=False),
            pystray.MenuItem(lambda _: f"주소: {self.url}" if self.ready else "주소: 준비 중", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: "Docker Desktop 설치 페이지" if self.show_install else "열기",
                             self.open_browser, default=True),
            pystray.MenuItem("로그 폴더 열기", self.open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", self.quit),
        )
        self.icon = pystray.Icon(APP_NAME, image, f"{APP_NAME} — {self.status}", menu)
        return self.icon

    # 백그라운드 준비
    def start(self, icon):
        icon.visible = True
        threading.Thread(target=self._boot, daemon=True).start()

    def _boot(self):
        try:
            prepare_home(self.log)
            import dotenv
            dotenv.load_dotenv(stack.HOME / ".env", override=True)
            ensure_docker(self)
            setup_gpu_once(self.log)
            ports = stack.ensure_ports(self.log)
            dotenv.load_dotenv(stack.HOME / ".env", override=True)   # 새로 고른 포트 반영
            stack.apply_env()
            self.log(f"[stack] 포트: {ports}")
            compose_up(self, self.log)
            wait_services(self, self.log)
            self._start_server(ports[stack.WEB_PORT_ENV])
            self.ready = True
            INSTANCE_FILE.write_text(json.dumps({"pid": os.getpid(), "port": self.port}), encoding="utf-8")
            self.set_status("실행 중")
            self.notify(f"KLaw가 실행 중입니다. 트레이 아이콘을 눌러 열 수 있습니다.\n{self.url}")
            if os.getenv("KLAW_NO_BROWSER") != "1":
                webbrowser.open(self.url)
        except SystemExit:
            pass
        except Exception as e:
            self.log(traceback.format_exc())
            self.fail(f"{type(e).__name__}: {e}")

    def _start_server(self, port: int):
        self.set_status("웹 서버를 시작하는 중…")
        os.chdir(stack.HOME)   # 앱이 상대 경로로 만드는 templates/·data/·static/ 폴더 위치
        import asyncio
        import uvicorn
        import main   # 무거운 import (LLM·MCP·OCR 등) — 서비스가 뜬 뒤에 한다
        from api import runtime_control
        config = uvicorn.Config(main.app, host="127.0.0.1", port=port, reload=False, log_config=None)
        self.server = uvicorn.Server(config)
        runtime_control.register(self.server, [])
        self.port = port
        self.server_thread = threading.Thread(target=lambda: asyncio.run(self.server.serve()), daemon=True)
        self.server_thread.start()
        deadline = time.time() + 180
        while not self.server.started and time.time() < deadline:
            if not self.server_thread.is_alive():
                raise RuntimeError("웹 서버가 시작하지 못했습니다 (로그 확인).")
            time.sleep(0.3)
        if not self.server.started:
            raise RuntimeError("웹 서버가 3분 안에 시작하지 않았습니다.")


def main() -> int:
    log_path = setup_logging()
    hide_child_windows()
    if not acquire_single_instance():
        print("[launcher] 이미 실행 중 — 브라우저만 엽니다", flush=True)
        open_existing_instance()
        return 0
    if "_PYI_SPLASH_IPC" in os.environ:   # exe 하나 빌드의 시작 화면(압축 해제 중 보이던 이미지)을 닫는다
        try:
            import pyi_splash  # type: ignore
            pyi_splash.close()
        except Exception:
            pass
    app = DesktopApp(log_path)
    app.build_icon().run(setup=app.start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
