"""
앱 데이터 완전 삭제 (설치형 프로그램의 '제거'처럼). 마이 페이지에서 실행한다.

무설치 exe지만 실행하면서 어쩔 수 없이 생기는 것들:
  - 앱 폴더 HOME (%LOCALAPPDATA%\\KLaw): 대화 DB·대화 상태·벡터·Ollama 엔진과 모델·임베딩 모델·OCR 모델·LiteLLM 받은 버전·로그·설정(.env)
  - exe 하나 빌드의 압축 해제 폴더 (%TEMP%\\_MEI…): 강제 종료로 남은 지난 실행 분
  - 라이브러리 공용 캐시 (예전 버전이 사용자 폴더에 만든 ~/.paddlex, ~/.crawl4ai, ~/.cache/paddle 등)
    → 지금은 앱 폴더로 돌려 두었다 (api/stack.py). 비어 있으면 함께 지우고, 내용이 있으면 다른 프로그램도 쓸 수 있으므로
      사용자가 고를 때만 지운다.

흐름: 화면에서 확인 → 서버가 '삭제 도우미'(exe 자신, --klaw-uninstall)를 띄우고 앱을 종료 → 도우미가 앱 프로세스가
완전히 끝나길 기다렸다가(파일 잠금 해제) 지운다 → 완료 알림 창. 실행 중인 파일은 지울 수 없어서 도우미 방식을 쓴다.

개발 실행(저장소가 HOME)에서는 저장소를 지우면 안 되므로 data/·logs/·.runtime/만 지운다.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from api import stack

FLAG = "--klaw-uninstall"
_SHARED_CACHES = [".paddlex", ".paddleocr", ".crawl4ai", ".cache/paddle", ".cache/huggingface", ".cache/modelscope"]


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _stale_extractions() -> list[Path]:
    current = Path(getattr(sys, "_MEIPASS", "") or "/nonexistent").resolve()
    out = []
    for d in Path(tempfile.gettempdir()).glob("_MEI*"):
        try:
            if d.is_dir() and d.resolve() != current and (d / "assets" / "klaw.ico").exists() \
                    and (d / "frontend" / "index.html").exists():
                out.append(d)
        except OSError:
            pass
    return out


def app_targets() -> list[Path]:
    if stack.FROZEN or stack.HOME.resolve() != stack.REPO_ROOT.resolve():
        return [stack.HOME]
    return [stack.DATA_DIR, stack.LOG_DIR, stack.RUNTIME_DIR]   # 개발: 저장소는 지우지 않는다


def plan() -> dict:
    """지울 대상과 크기 (화면 표시용)."""
    items = []
    labels = {"ollama": "Ollama 엔진·로컬 모델", "embeddings": "임베딩 모델", "paddlex": "OCR 모델",
              "huggingface": "모델 다운로드 캐시", "pylib": "받은 LiteLLM 버전", "qdrant": "대화 기억 벡터",
              "klaw.db": "대화·설정 DB", "checkpoints.db": "대화 상태", ".crawl4ai": "웹 크롤링 캐시",
              "modelscope": "모델 다운로드 캐시"}
    for base in app_targets():
        if not base.exists():
            continue
        if base == stack.HOME:
            for child in sorted(base.iterdir()):
                if child.name == "data" and child.is_dir():
                    groups: dict[str, dict] = {}
                    for sub in sorted(child.iterdir()):
                        key = sub.name.split(".db")[0] + ".db" if ".db" in sub.name else sub.name   # klaw.db-wal → klaw.db
                        g = groups.setdefault(key, {"kind": "app", "label": labels.get(key, key), "path": str(child / key), "bytes": 0})
                        g["bytes"] += _size(sub)
                    items.extend(groups.values())
                else:
                    items.append({"kind": "app", "label": {"logs": "로그", ".env": "설정·API 키 (.env)",
                                                           ".runtime": "실행 기록"}.get(child.name, child.name),
                                  "path": str(child), "bytes": _size(child)})
        else:
            items.append({"kind": "app", "label": base.name, "path": str(base), "bytes": _size(base)})
    for d in _stale_extractions():
        items.append({"kind": "temp", "label": "지난 실행의 임시 폴더", "path": str(d), "bytes": _size(d)})
    shared = []
    for rel in _SHARED_CACHES:
        p = Path.home() / rel
        if p.exists():
            size = _size(p)
            shared.append({"kind": "shared" if size else "empty", "label": rel, "path": str(p), "bytes": size})
    return {
        "frozen": stack.FROZEN,
        "home": str(stack.HOME),
        "exePath": sys.executable if stack.FROZEN else None,
        "items": items,
        "shared": shared,
        "totalBytes": sum(i["bytes"] for i in items),
    }


def schedule(include_shared: bool, delete_exe: bool) -> list[str]:
    """삭제 도우미를 띄운다 (이 프로세스가 끝나길 기다렸다가 지운다). 지울 경로 목록."""
    p = plan()
    targets = [str(b) for b in app_targets()] + [i["path"] for i in p["items"] if i["kind"] == "temp"]
    targets += [s["path"] for s in p["shared"] if s["kind"] == "empty" or include_shared]
    exe = p["exePath"] if (delete_exe and p["exePath"]) else "-"
    cmd = [sys.executable] + ([] if stack.FROZEN else [str(stack.REPO_ROOT / "launcher.py")])
    cmd += [FLAG, str(os.getpid()), exe, *targets]
    # 도우미는 앱과 별개로 끝까지 살아야 한다 (exe 하나 빌드가 부모의 압축 해제 폴더를 같이 쓰지 않도록)
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    flags = 0x00000008 | 0x00000200 | 0x08000000 if os.name == "nt" else 0   # DETACHED | NEW_GROUP | NO_WINDOW
    subprocess.Popen(cmd, env=env, cwd=tempfile.gettempdir(), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return targets


# ────────────────────────── 도우미 (launcher가 FLAG로 불렸을 때) ──────────────────────────
def _wait_pid(pid: int, timeout: float = 120) -> None:
    if os.name != "nt":
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
    if h:
        k32.WaitForSingleObject(h, int(timeout * 1000))
        k32.CloseHandle(h)


def _rmtree_retry(path: Path, tries: int = 20) -> bool:
    for _ in range(tries):
        if not path.exists():
            return True
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            time.sleep(0.5)   # 자식 프로세스가 막 끝나 파일 잠금이 아직 풀리지 않았을 수 있다
    return not path.exists()


def run_helper(argv: list[str]) -> int:
    pid, exe, *targets = argv
    _wait_pid(int(pid))
    time.sleep(1.5)
    left = [t for t in targets if not _rmtree_retry(Path(t))]
    for d in _stale_extractions():   # 그 사이 정상 종료로 지워지지 않은 압축 해제 폴더
        _rmtree_retry(d, tries=4)
    if exe != "-" and os.name == "nt":
        # 실행 중인 exe(이 도우미 자신)는 지울 수 없으므로, 도우미가 끝난 뒤 지우는 숨은 명령을 남긴다
        subprocess.Popen(f'cmd /c ping -n 4 127.0.0.1 >nul & del /f /q "{exe}"', shell=False,
                         creationflags=0x08000000 | 0x00000008, close_fds=True)
    _notify("KLaw를 제거했습니다." if not left else
            "KLaw를 제거했지만 일부를 지우지 못했습니다:\n" + "\n".join(left[:5]),
            error=bool(left))
    return 0 if not left else 1


def _notify(text: str, error: bool = False) -> None:
    if os.name != "nt" or os.getenv("KLAW_UNINSTALL_SILENT") == "1":
        print(text)
        return
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, "KLaw", 0x10 if error else 0x40)
