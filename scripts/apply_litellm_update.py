"""
LiteLLM 버전 적용 도우미 — 백엔드 서버가 내려간 뒤 실행된다 (마이 페이지 '런타임 버전'이 띄운다).

백엔드가 쓰는 패키지를 바꾸므로 서버가 돌고 있으면 안 된다 (Windows는 사용 중인 파일을 못 바꾸기도 한다).
그래서 서버가 이 도우미를 새 콘솔 창으로 띄우고 스스로 종료하면, 도우미가:
  1. 서버 프로세스가 끝나기를 기다린다
  2. pyproject.toml·uv.lock을 백업하고 litellm 줄을 "litellm==<버전>"으로 바꾼다
  3. uv lock --upgrade-package litellm → uv sync
  4. 설치된 버전과 호환성(scripts/check_litellm_compat.py)을 확인한다
  5. 실패하면 백업을 되돌리고 uv sync
  6. 결과를 .runtime/runtime_job.json에 적고, 서버를 이 콘솔에서 다시 띄운다 (uv run python main.py)

표준 라이브러리만 쓴다 (바꾸는 중인 패키지를 import하지 않도록).

수동 실행 예: python scripts/apply_litellm_update.py --target 1.105.7 --pid 0
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT / ".runtime"
JOB_FILE = RUNTIME_DIR / "runtime_job.json"
BACKUP_DIR = RUNTIME_DIR / "backup"
LITELLM_LINE = re.compile(r'^(\s*)"litellm(?:[=<>!~][^"]*)?",\s*$', re.M)


# ────────────────────────── 진행 기록 ──────────────────────────
def load_job() -> dict:
    try:
        return json.loads(JOB_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "running", "step": "", "log": [], "items": {}}


def save_job(job: dict) -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)
    job["log"] = job.get("log", [])[-400:]
    tmp = JOB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(JOB_FILE)


def log(job: dict, line: str) -> None:
    print(line, flush=True)
    job.setdefault("log", []).append(line)
    save_job(job)


def step(job: dict, name: str) -> None:
    job["step"] = name
    log(job, f"── {name}")


def run(job: dict, cmd: list[str], timeout: int = 1800) -> str:
    log(job, "$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    out = []
    deadline = time.time() + timeout
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        out.append(line)
        if line:
            log(job, line)
        if time.time() > deadline:
            proc.kill()
            raise RuntimeError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''}가 {timeout}초 안에 끝나지 않았습니다.")
    if proc.wait() != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} 실패 (종료 코드 {proc.returncode})")
    return "\n".join(out)


# ────────────────────────── 단계 ──────────────────────────
def wait_for_exit(pid: int, timeout: float = 120) -> bool:
    """서버 프로세스가 끝날 때까지 기다린다."""
    if pid <= 0:
        return True
    deadline = time.time() + timeout
    if sys.platform == "win32":
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return True   # 이미 끝남
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.5)
    return False


def set_litellm_pin(version: str) -> None:
    path = ROOT / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    new, count = LITELLM_LINE.subn(lambda m: f'{m.group(1)}"litellm=={version}",', text, count=1)
    if count != 1:
        raise RuntimeError("pyproject.toml에서 litellm 의존성 줄을 찾지 못했습니다.")
    path.write_text(new, encoding="utf-8")


def installed_version(uv: str) -> str:
    out = subprocess.run([uv, "run", "python", "-c", "import importlib.metadata as m; print(m.version('litellm'))"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stdout.strip().splitlines()[-1] if out.returncode == 0 and out.stdout.strip() else ""


def compat_check(job: dict, uv: str, model_ids: list[str]) -> dict:
    out = run(job, [uv, "run", "python", "scripts/check_litellm_compat.py", *model_ids], timeout=300)
    line = next((l for l in reversed(out.splitlines()) if l.startswith("COMPAT_RESULT:")), None)
    if line is None:
        raise RuntimeError("호환성 점검 결과를 읽지 못했습니다.")
    return json.loads(line[len("COMPAT_RESULT:"):])


def apply(job: dict, target: str, uv: str) -> None:
    item = job.setdefault("items", {}).setdefault("litellm", {})
    previous = item.get("from", "")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "pyproject.toml", BACKUP_DIR / "pyproject.toml")
    shutil.copy2(ROOT / "uv.lock", BACKUP_DIR / "uv.lock")
    try:
        step(job, f"LiteLLM {target} 설치")
        set_litellm_pin(target)
        run(job, [uv, "lock", "--upgrade-package", "litellm"])
        run(job, [uv, "sync"])

        step(job, "설치 확인")
        got = installed_version(uv)
        if got != target:
            raise RuntimeError(f"설치된 litellm 버전이 {got or '알 수 없음'}입니다 (기대 {target}).")
        result = compat_check(job, uv, job.get("modelIds", []))
        if not result.get("ok"):
            raise RuntimeError("설치 후 호환성 점검 실패: " + "; ".join(result.get("errors", [])[:2]))

        item["status"] = "done"
        job["status"] = "done"
        job["message"] = (job.get("message", "") + " " if job.get("message") else "") + \
            f"LiteLLM {previous} → {target} 변경 완료"
        log(job, job["message"])
    except Exception as e:
        log(job, f"[오류] {e}")
        step(job, f"이전 버전({previous})으로 되돌리는 중")
        shutil.copy2(BACKUP_DIR / "pyproject.toml", ROOT / "pyproject.toml")
        shutil.copy2(BACKUP_DIR / "uv.lock", ROOT / "uv.lock")
        try:
            run(job, [uv, "sync"])
            item["rolledBack"] = True
            log(job, f"LiteLLM {previous}으로 되돌렸습니다.")
        except Exception as e2:
            log(job, f"[오류] 되돌리기 실패: {e2} — 터미널에서 `uv sync` 후 서버를 직접 띄우세요.")
        item["status"] = "error"
        job["status"] = "error"
        job["message"] = (job.get("message", "") + " " if job.get("message") else "") + f"LiteLLM 변경 실패: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description="LiteLLM 버전 적용 후 서버 재시작")
    parser.add_argument("--target", required=True)
    parser.add_argument("--pid", type=int, default=0, help="종료를 기다릴 서버 프로세스 ID")
    parser.add_argument("--no-restart", action="store_true", help="적용만 하고 서버는 띄우지 않는다")
    args = parser.parse_args()

    if sys.platform == "win32":
        os.system(f"title KLaw 서버 - LiteLLM {args.target} 적용")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    job = load_job()
    uv = shutil.which("uv")
    try:
        step(job, "백엔드 서버 종료 대기")
        if not wait_for_exit(args.pid):
            raise RuntimeError("서버가 120초 안에 종료되지 않았습니다.")
        if uv is None:
            raise RuntimeError("uv 명령을 찾을 수 없습니다.")
        apply(job, args.target, uv)
    except Exception as e:
        job["status"] = "error"
        job["message"] = f"LiteLLM 변경 실패: {e}"
        log(job, f"[오류] {e}")
    finally:
        job["finishedAt"] = time.time()
        job["step"] = "완료" if job.get("status") == "done" else "실패"
        save_job(job)

    if args.no_restart:
        return 0 if job.get("status") == "done" else 1
    print("\n백엔드 서버를 다시 시작합니다 (이 창을 닫으면 서버도 종료됩니다).\n", flush=True)
    return subprocess.call([uv or "uv", "run", "python", "main.py"], cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
