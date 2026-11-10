"""
자식 프로세스(법령 MCP 서버·Ollama 엔진) 관리.

깔끔한 종료가 목표다: 모든 자식은 Windows Job Object에 넣어 두고 'KILL_ON_JOB_CLOSE'를 건다.
→ 앱이 정상 종료하든, 작업 관리자로 강제 종료되든, 튕기든, 이 프로세스가 사라지는 순간 OS가 자식을 함께 끝낸다.
   백그라운드에 남는 프로세스가 없다.

  law_mcp  korean-law-mcp(Node.js). exe에는 node.exe와 빌드된 서버를 함께 넣는다. 개발에서는 PATH의 node.
  ollama   Ollama Windows 배포본(ollama.exe). 용량이 커서(1.6GB) exe에 넣지 않고, 로컬 모델을 쓰려 할 때
           마이 페이지에서 내려받는다 → data/ollama/engine/<버전>/. 모델·키도 data/ollama 아래에만 둔다.
"""
import atexit
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

from api import stack

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ────────────────────────── Job Object (자식 동반 종료) ──────────────────────────
class _Job:
    """Windows Job Object. 이 프로세스의 핸들이 닫히면(=프로세스가 끝나면) 넣어 둔 자식이 모두 끝난다."""

    def __init__(self) -> None:
        self.handle = None
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        self._k32 = k32
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if k32.SetInformationJobObject(wintypes.HANDLE(handle), 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.handle = handle   # 9 = JobObjectExtendedLimitInformation

    def add(self, pid: int) -> None:
        if not self.handle:
            return
        from ctypes import wintypes
        h = self._k32.OpenProcess(0x0101, False, pid)   # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if h:
            self._k32.AssignProcessToJobObject(wintypes.HANDLE(self.handle), wintypes.HANDLE(h))
            self._k32.CloseHandle(wintypes.HANDLE(h))

    def terminate(self) -> None:
        """넣어 둔 프로세스와 그 자손(예: Ollama의 모델 실행 프로세스)을 한 번에 끝낸다."""
        if self.handle:
            from ctypes import wintypes
            self._k32.TerminateJobObject(wintypes.HANDLE(self.handle), 1)


# ────────────────────────── 관리되는 자식 프로세스 ──────────────────────────
class ManagedProcess:
    def __init__(self, name: str, health_url: str, ready_timeout: float = 60) -> None:
        self.name = name
        self.health_url = health_url
        self.ready_timeout = ready_timeout
        self.proc: Optional[subprocess.Popen] = None
        self.log_path = stack.LOG_DIR / f"{name}.log"
        self._lock = threading.RLock()
        self._job: Optional[_Job] = None

    # 하위 클래스가 정한다
    def command(self) -> Optional[tuple[list[str], dict[str, str], Path]]:
        raise NotImplementedError

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def healthy(self, timeout: float = 2.0) -> bool:
        try:
            with urllib.request.urlopen(self.health_url, timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def start(self, wait: bool = True) -> bool:
        """이미 떠 있으면 그대로. 실행할 수 없으면(엔진 없음 등) False."""
        with self._lock:
            if self.running:
                return True
            spec = self.command()
            if spec is None:
                return False
            cmd, env, cwd = spec
            # 실행할 때마다 로그를 새로 쓴다 (Ollama 장치 감지 줄이 '이번 실행' 것만 남도록)
            log = open(self.log_path, "w", encoding="utf-8", errors="replace")
            try:
                self.proc = subprocess.Popen(cmd, cwd=cwd, env={**os.environ, **env}, stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
            finally:
                log.close()   # 자식이 핸들을 물려받았다
            # 프로세스마다 Job을 따로 둔다 → 재시작할 때 자손까지 정리하고, 앱이 사라지면 OS가 모두 끝낸다
            self._job = _Job()
            self._job.add(self.proc.pid)
            print(f"[services] {self.name} 시작 (pid {self.proc.pid}) — 로그 {self.log_path}")
        if wait:
            return self.wait_ready()
        return True

    def wait_ready(self) -> bool:
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            if self.healthy():
                return True
            if not self.running:
                print(f"[services] {self.name}가 바로 종료됐습니다 — 로그 {self.log_path}")
                return False
            time.sleep(0.3)
        print(f"[services] {self.name}가 {self.ready_timeout:.0f}초 안에 응답하지 않습니다 — 로그 {self.log_path}")
        return False

    def stop(self, timeout: float = 10) -> None:
        with self._lock:
            proc, self.proc = self.proc, None
            job, self._job = self._job, None
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                print(f"[services] {self.name} 종료")
            if job is not None:
                job.terminate()   # 남은 자손 프로세스까지

    def restart(self) -> bool:
        self.stop()
        return self.start()


# ────────────────────────── 법령 MCP (Node.js) ──────────────────────────
def node_exe() -> Optional[str]:
    bundled = stack.BUNDLE / "node" / "node.exe"
    if bundled.exists():
        return str(bundled)
    built = stack.REPO_ROOT / "build" / "node" / "node.exe"   # scripts/build_exe.py가 받아 둔 것
    if built.exists():
        return str(built)
    return shutil.which("node")


def law_mcp_dir() -> Path:
    for d in (stack.BUNDLE / "law_mcp", stack.REPO_ROOT / "korean-law-mcp"):
        if (d / "build" / "index.js").exists():
            return d
    return stack.REPO_ROOT / "korean-law-mcp"


class LawMcp(ManagedProcess):
    def __init__(self) -> None:
        super().__init__("law-mcp", os.environ.get("LAW_MCP_HEALTH_URL", ""), ready_timeout=40)

    def command(self):
        node, root = node_exe(), law_mcp_dir()
        if node is None or not (root / "build" / "index.js").exists():
            print(f"[services] 법령 MCP를 실행할 수 없습니다 (node={node}, 빌드={root / 'build' / 'index.js'}). "
                  "개발 환경이면: cd korean-law-mcp && npm ci --omit=optional --ignore-scripts && npm run build")
            return None
        port = os.environ["KLAW_PORT_LAW_MCP"]
        env = {"NODE_ENV": "production", "MCP_HTTP_HOST": "127.0.0.1", "PORT": port,
               "LAW_OC": os.getenv("LAW_OC", ""), "MCP_AUTH_TOKEN": os.getenv("MCP_AUTH_TOKEN", "")}
        return [node, str(root / "build" / "index.js"), "--mode", "http", "--port", port], env, root


# ────────────────────────── Ollama 엔진 ──────────────────────────
ENGINE_DIR = stack.OLLAMA_DIR / "engine"
MODELS_DIR = stack.OLLAMA_DIR / "models"
OLLAMA_HOME = stack.OLLAMA_DIR / "home"     # Ollama가 사용자 폴더(~/.ollama)에 쓰는 키 등을 여기로


def installed_engine_versions() -> list[str]:
    if not ENGINE_DIR.exists():
        return []
    return sorted((d.name for d in ENGINE_DIR.iterdir() if (d / "ollama.exe").exists()),
                  key=lambda v: tuple(int(x) for x in v.split(".") if x.isdigit()))


def current_engine_version() -> Optional[str]:
    """쓰는 엔진 버전 (data/ollama/engine/current.txt, 없으면 설치된 것 중 최신)."""
    marker = ENGINE_DIR / "current.txt"
    if marker.exists():
        v = marker.read_text(encoding="utf-8").strip()
        if (ENGINE_DIR / v / "ollama.exe").exists():
            return v
    versions = installed_engine_versions()
    return versions[-1] if versions else None


def set_current_engine(version: str) -> None:
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    (ENGINE_DIR / "current.txt").write_text(version, encoding="utf-8")


class Ollama(ManagedProcess):
    def __init__(self) -> None:
        super().__init__("ollama", os.environ.get("OLLAMA_BASE_URL", "") + "/api/version", ready_timeout=60)
        self.version_override: Optional[str] = None   # 버전 변경 중 새 엔진을 시험할 때

    def command(self):
        version = self.version_override or current_engine_version()
        if version is None:
            return None
        exe = ENGINE_DIR / version / "ollama.exe"
        for d in (MODELS_DIR, OLLAMA_HOME):
            d.mkdir(parents=True, exist_ok=True)
        env = {"OLLAMA_HOST": f"127.0.0.1:{os.environ['KLAW_PORT_OLLAMA']}", "OLLAMA_MODELS": str(MODELS_DIR),
               "USERPROFILE": str(OLLAMA_HOME), "HOME": str(OLLAMA_HOME),
               "OLLAMA_NO_CLOUD": "1"}   # 로컬 전용 (클라우드 모델 호출 기능 끔)
        return [str(exe), "serve"], env, exe.parent


LAW_MCP = LawMcp()
OLLAMA = Ollama()
_started = False


def start_core() -> None:
    """앱 모듈(load_variable)이 법령 도구를 읽기 전에 부른다. Ollama는 엔진이 있으면 백그라운드로 띄운다."""
    global _started
    if _started:
        return
    _started = True
    if not os.getenv("MCP_AUTH_TOKEN"):
        # 같은 PC의 다른 프로그램이 법령 MCP를 쓰지 못하게 실행마다 새 토큰 (load_variable이 같은 값을 쓴다)
        import secrets
        os.environ["MCP_AUTH_TOKEN"] = secrets.token_urlsafe(24)
    t0 = time.time()
    ok = LAW_MCP.start()
    print(f"[services] 법령 MCP {'준비' if ok else '실패'} ({time.time() - t0:.1f}s, 포트 {os.environ.get('KLAW_PORT_LAW_MCP')})")
    if current_engine_version():
        threading.Thread(target=OLLAMA.start, name="ollama-start", daemon=True).start()
    else:
        print("[services] Ollama 엔진 없음 — 로컬 모델은 마이 페이지에서 엔진을 받은 뒤 쓸 수 있습니다.")


def stop_all() -> None:
    for p in (OLLAMA, LAW_MCP):
        try:
            p.stop()
        except Exception as e:
            print(f"[services] {p.name} 종료 실패: {e}")


atexit.register(stop_all)


def ensure_ollama() -> bool:
    """엔진이 있는데 떠 있지 않으면(아직 시작 중·비정상 종료) 띄운다. 응답하면 True."""
    if OLLAMA.healthy():
        return True
    if current_engine_version() is None:
        return False
    return OLLAMA.start()


def ollama_unavailable_message() -> str:
    if current_engine_version() is None:
        return "Ollama 엔진이 아직 설치되지 않았습니다. 마이 페이지 > 런타임 버전에서 Ollama 엔진을 설치하세요."
    return f"Ollama 엔진이 응답하지 않습니다. 로그를 확인하세요: {OLLAMA.log_path}"
