"""
KLaw 데스크톱 실행기 — exe의 진입점. 개발 중에도 `uv run python launcher.py`로 같은 흐름을 시험할 수 있다.

설치·Docker 없이 exe 하나로 돈다:
  1. 콘솔 창 없이 실행한다 (로그는 <HOME>/logs/klaw.log). 하위 프로그램(node·ollama·Edge)도 창을 띄우지 않는다
  2. 이미 실행 중이면 새로 띄우지 않고 브라우저만 연다
  3. 시스템 트레이 아이콘: 열기(아이콘 클릭)·데이터 폴더 열기·종료
  4. 백그라운드 준비: 웹 포트 정하기 → 앱 import(법령 MCP 자식 프로세스 시작, Ollama 엔진이 있으면 시작)
     → 웹 서버 시작 → 브라우저 열기
  5. 종료: 웹 서버와 자식 프로세스를 모두 내린다. 자식은 Job Object에 묶여 있어 exe가 강제 종료돼도 함께 끝난다
     (api/services.py) → 종료 뒤 백그라운드에 남는 프로세스가 없다.

데이터는 %LOCALAPPDATA%\\KLaw 한 곳에만 둔다 (api/stack.py). 이 폴더를 지우면 PC에 남는 것이 없다.
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from api import stack  # 가벼운 모듈 (앱 전체를 import하지 않는다)

APP_NAME = "KLaw"
APP_VERSION = "1.1.0"


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
          f"(home={stack.HOME}, frozen={stack.FROZEN}) =====", flush=True)
    return log_path


def hide_child_windows() -> None:
    """
    창 없는 exe에서 콘솔 프로그램(node·ollama·powershell 등)을 실행하면 매번 검은 창이 떴다 사라진다.
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


# ────────────────────────── 지난 실행의 임시 폴더 정리 ──────────────────────────
def cleanup_stale_extractions() -> None:
    """
    exe 하나 빌드는 실행할 때마다 %TEMP%\\_MEIxxxx에 풀었다가 정상 종료 때 지운다. 작업 관리자로 강제 종료되면
    그 폴더(1GB 이상)가 남으므로, 다음 실행 때 지운다. KLaw가 푼 폴더(표식 파일)만, 지금 쓰는 폴더는 빼고.
    한 번만 실행(뮤텍스)을 잡은 뒤 부르므로 다른 KLaw가 그 폴더를 쓰고 있을 수 없다.
    """
    current = getattr(sys, "_MEIPASS", None)
    if not current:
        return
    import shutil
    import tempfile
    for d in Path(tempfile.gettempdir()).glob("_MEI*"):
        try:
            if d.is_dir() and str(d.resolve()) != str(Path(current).resolve()) \
                    and (d / "assets" / "klaw.ico").exists() and (d / "frontend" / "index.html").exists():
                shutil.rmtree(d, ignore_errors=True)
                print(f"[launcher] 지난 실행의 임시 폴더 정리: {d}", flush=True)
        except OSError:
            pass


# ────────────────────────── 다시 시작 ──────────────────────────
AFTER_PID_FLAG = "--after-pid"


def wait_for_pid(pid: int, timeout: float = 90) -> None:
    """재시작으로 띄워진 경우, 이전 프로세스가 완전히 끝날 때까지 기다린다 (포트·뮤텍스·Qdrant 폴더 잠금이 풀리도록)."""
    if os.name != "nt":
        return
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
    if handle:
        k32.WaitForSingleObject(handle, int(timeout * 1000))
        k32.CloseHandle(handle)


def spawn_restart() -> None:
    """새 인스턴스를 띄운다. 새 인스턴스는 지금 프로세스가 끝난 뒤에 시작한다."""
    cmd = [sys.executable] + ([] if stack.FROZEN else [str(Path(__file__).resolve())])
    cmd += [AFTER_PID_FLAG, str(os.getpid())]
    # PYINSTALLER_RESET_ENVIRONMENT: exe 하나 빌드는 자식이 부모의 압축 해제 폴더를 같이 쓰는데, 재시작한 앱은
    # 부모가 끝난 뒤에도 살아야 하므로 독립 실행으로 띄운다 (부모가 지우는 폴더를 쓰면 안 된다)
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1", "KLAW_NO_BROWSER": "1"}
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, env=env, cwd=str(stack.HOME), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        webbrowser.open(f"http://localhost:{port}/")
    except Exception:
        pass


class DesktopApp:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.status = "시작하는 중…"
        self.ready = False
        self.port = None
        self.server = None
        self.server_thread = None
        self.icon = None

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

    def fail(self, text: str) -> None:
        self.set_status(f"오류: {text}")
        self.notify(text)

    @property
    def url(self) -> str:
        # 화면 주소는 localhost — Google 로그인 버튼(GIS)은 127.0.0.1 출처를 등록해 둬도 거절한다.
        # 서버는 127.0.0.1에만 열리고, localhost가 ::1로 먼저 풀려도 브라우저가 127.0.0.1로 이어 붙는다
        # (다른 프로그램이 ::1의 같은 포트를 쓰면 stack.port_free가 그 포트를 건너뛴다).
        return f"http://localhost:{self.port}/"

    # 트레이 메뉴
    def open_browser(self, *_):
        if self.ready:
            webbrowser.open(self.url)
        else:
            self.notify(self.status)

    def open_home(self, *_):
        os.startfile(str(stack.HOME)) if os.name == "nt" else webbrowser.open(stack.HOME.as_uri())

    def quit(self, *_):
        threading.Thread(target=self._shutdown, daemon=True).start()

    def restart(self) -> None:
        """앱 전체 재시작 (exe에서 LiteLLM 버전을 바꿀 때). 새 인스턴스가 이 프로세스가 끝나길 기다렸다가 뜬다."""
        self.log("[launcher] 다시 시작합니다")
        spawn_restart()
        self._shutdown()

    def _shutdown(self):
        self.ready = False
        self.set_status("종료하는 중…")
        if self.server:
            self.server.should_exit = True
            if self.server_thread:
                self.server_thread.join(timeout=20)
        try:   # 웹 서버 종료(lifespan)에서도 내리지만, 서버가 뜨기 전에 종료한 경우까지 확실히
            from api import services
            services.stop_all()
        except Exception:
            self.log(traceback.format_exc())
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
            pystray.MenuItem("열기", self.open_browser, default=True),
            pystray.MenuItem("데이터 폴더 열기", self.open_home),
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
            port = stack.ensure_web_port(self.log)
            self._start_server(port)
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
            from api import litellm_overlay
            if litellm_overlay.trial_pending():
                # 새로 받은 LiteLLM으로 뜨지 못했다 → 원래 버전으로 다시 띄운다
                litellm_overlay.fail_trial(f"새 버전으로 시작하지 못해 이전 버전으로 되돌렸습니다 ({type(e).__name__}: {e})", self.log)
                self.restart()
                return
            self.fail(f"{type(e).__name__}: {e}")

    def _start_server(self, port: int):
        self.set_status("준비하는 중…")
        os.chdir(stack.HOME)   # 상대 경로로 파일을 만드는 라이브러리가 있어도 앱 폴더 안에만 생기도록
        import asyncio
        import uvicorn
        from api import litellm_overlay
        litellm_overlay.activate(self.log)   # 바꿔 둔 LiteLLM 버전이 있으면 import 경로 맨 앞에 (앱 import 전에)
        import main   # 무거운 import (LLM·MCP·OCR 등). 법령 MCP 자식 프로세스도 여기서 뜬다
        self.set_status("웹 서버를 시작하는 중…")
        from api import runtime_control
        config = uvicorn.Config(main.app, host="localhost", port=port, reload=False, log_config=None)
        sockets = stack.web_sockets(port)   # localhost = 127.0.0.1 + ::1
        self.server = uvicorn.Server(config)
        runtime_control.register(self.server, [])
        runtime_control.register_restart(self.restart)
        runtime_control.register_exit(self._shutdown)
        self.port = port
        self.server_thread = threading.Thread(target=lambda: asyncio.run(self.server.serve(sockets=sockets)), daemon=True)
        self.server_thread.start()
        deadline = time.time() + 180
        while not self.server.started and time.time() < deadline:
            if not self.server_thread.is_alive():
                raise RuntimeError("웹 서버가 시작하지 못했습니다 (로그 확인).")
            time.sleep(0.3)
        if not self.server.started:
            raise RuntimeError("웹 서버가 3분 안에 시작하지 않았습니다.")
        litellm_overlay.confirm_boot(self.log)   # 시험 중이던 LiteLLM 버전으로 웹 서버까지 떴으면 확정


def main() -> int:
    from api import litellm_overlay, uninstall
    if uninstall.FLAG in sys.argv:   # 앱 데이터 삭제 도우미 (앱이 끝나길 기다렸다가 지운다 — 로그 파일도 만들지 않는다)
        return uninstall.run_helper(sys.argv[sys.argv.index(uninstall.FLAG) + 1:])
    if litellm_overlay.CHECK_FLAG in sys.argv:   # LiteLLM 새 버전 시험 모드 (앱을 띄우지 않는다)
        return litellm_overlay.run_check_mode(sys.argv[sys.argv.index(litellm_overlay.CHECK_FLAG) + 1:])
    log_path = setup_logging()
    if AFTER_PID_FLAG in sys.argv:
        wait_for_pid(int(sys.argv[sys.argv.index(AFTER_PID_FLAG) + 1]))
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
    threading.Thread(target=cleanup_stale_extractions, daemon=True).start()
    app = DesktopApp(log_path)
    app.build_icon().run(setup=app.start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
