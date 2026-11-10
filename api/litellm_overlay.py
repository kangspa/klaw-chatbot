"""
exe에서 LiteLLM 버전 바꾸기 — 다시 빌드하지 않고 '덮어 쓰는 패키지 폴더'로.

exe 안의 파이썬 패키지는 바꿀 수 없지만, import 경로(sys.path) 맨 앞에 폴더를 두면 그 폴더의 패키지가 먼저 쓰인다.
LiteLLM은 PyPI에 Windows용 휠(cp310-abi3-win_amd64)이 있어 받아서 풀기만 하면 exe 안의 파이썬이 그대로 쓴다.

  data/pylib/litellm-<버전>/     받아서 푼 휠 (litellm/ + litellm-<버전>.dist-info/)
  .runtime/litellm_overlay.json  {"active": 쓰는 버전, "trial": 다음 실행에 시험할 버전, "trialBoots": n}

적용 순서 (api/llm_gateway/runtime_update.py):
  1. 휠 받기·SHA-256 확인·풀기 → 2. 요구 패키지 버전이 exe 안의 것과 맞는지 → 3. exe 자신을 시험 모드로 띄워
     실제로 import·도구 호출·LangChain 연동·단가 계산 확인 (scripts/check_litellm_compat.py) → 4. trial로 기록하고 앱 재시작
  5. 새로 뜬 앱이 웹 서버까지 올라오면 active로 확정하고 이전 폴더를 지운다.
     뜨다가 실패하면(또는 다음 실행에서 시험 흔적이 남아 있으면) trial을 버리고 원래 버전으로 다시 뜬다.

exe에 묶인 원래 버전으로 돌아가기 = active를 비우고 재시작.
이 모듈은 앱을 import하기 전(launcher)에 쓰이므로 무거운 import를 하지 않는다.
"""
import hashlib
import importlib.metadata as md
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from api import stack

PYLIB = stack.DATA_DIR / "pylib"
STATE_FILE = stack.RUNTIME_DIR / "litellm_overlay.json"
JOB_FILE = stack.RUNTIME_DIR / "runtime_job.json"
BUNDLED_ENV = "KLAW_LITELLM_BUNDLED"      # exe에 묶인 원래 버전 (activate가 덮어 쓰기 전에 기록)
CHECK_FLAG = "--klaw-litellm-check"


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def version_dir(version: str) -> Path:
    return PYLIB / f"litellm-{version}"


def bundled_version() -> str:
    return os.getenv(BUNDLED_ENV) or md.version("litellm")


# ────────────────────────── 시작할 때 (launcher, 앱 import 전) ──────────────────────────
def activate(log: Callable[[str], None] = print) -> Optional[str]:
    """덮어 쓸 버전이 있으면 sys.path 맨 앞에 넣는다. 쓰는 덮어쓰기 버전 (없으면 None)."""
    os.environ.setdefault(BUNDLED_ENV, md.version("litellm"))
    state = _read_state()
    version = None
    if state.get("trial"):
        if state.get("trialBoots", 0) >= 1:
            # 지난번에 이 버전으로 뜨다가 확정하지 못했다 (튕김·강제 종료) → 버리고 원래대로
            fail_trial("새 버전으로 앱이 시작되지 않아 이전 버전으로 되돌렸습니다.", log)
            state = _read_state()
        else:
            state["trialBoots"] = state.get("trialBoots", 0) + 1
            _write_state(state)
            version = state["trial"]
    version = version or state.get("active")
    if version and (version_dir(version) / "litellm").is_dir():
        sys.path.insert(0, str(version_dir(version)))
        log(f"[litellm] {version} 사용 (exe 기본 {os.environ[BUNDLED_ENV]}) — {version_dir(version)}")
        return version
    if version:
        log(f"[litellm] {version} 폴더가 없어 exe 기본 버전을 씁니다")
    return None


def confirm_boot(log: Callable[[str], None] = print) -> None:
    """웹 서버까지 올라왔으면 시험 중인 버전을 확정하고, 진행 중이던 작업 기록을 '완료'로 바꾼다."""
    state = _read_state()
    trial = state.get("trial")
    if not trial:
        # exe 기본 버전으로 되돌린 재시작이었다면 그 작업도 완료로
        _finish_job("done", f"LiteLLM {md.version('litellm')} (exe 기본 버전) 적용 완료 (앱 재시작)")
        return
    state.update(active=trial, trial=None, trialBoots=0)
    _write_state(state)
    _cleanup_except(trial)
    log(f"[litellm] {trial} 확정")
    _finish_job("done", f"LiteLLM {trial} 적용 완료 (앱 재시작)")


def fail_trial(reason: str, log: Callable[[str], None] = print) -> None:
    state = _read_state()
    trial = state.get("trial")
    if not trial:
        return
    state.update(trial=None, trialBoots=0)
    _write_state(state)
    shutil.rmtree(version_dir(trial), ignore_errors=True)
    log(f"[litellm] {trial} 시험 실패 — {reason}")
    _finish_job("error", f"LiteLLM {trial}: {reason}", rolled_back=True)


def trial_pending() -> bool:
    return bool(_read_state().get("trial"))


def _cleanup_except(keep: Optional[str]) -> None:
    if not PYLIB.exists():
        return
    for d in PYLIB.iterdir():
        if d.is_dir() and d.name != (f"litellm-{keep}" if keep else None):
            shutil.rmtree(d, ignore_errors=True)


def _finish_job(status: str, message: str, rolled_back: bool = False) -> None:
    try:
        job = json.loads(JOB_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if job.get("status") != "restarting":
        return
    item = (job.get("items") or {}).get("litellm") or {}
    item.update(status=status, rolledBack=rolled_back)
    job.update(status=status, step="완료" if status == "done" else "실패", message=message, finishedAt=time.time())
    job.setdefault("log", []).append(("" if status == "done" else "[오류] ") + message)
    JOB_FILE.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")


# ────────────────────────── 받기·확인 ──────────────────────────
def _wheel_info(version: str) -> dict:
    req = urllib.request.Request(f"https://pypi.org/pypi/litellm/{version}/json", headers={"User-Agent": "klaw-chatbot"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        files = json.load(resp)["urls"]
    for pattern in (r"-cp3\d+-abi3-win_amd64\.whl$", r"-py3-none-any\.whl$"):
        for f in files:
            if re.search(pattern, f["filename"]) and not f.get("yanked"):
                return f
    raise RuntimeError(f"LiteLLM {version}에는 Windows에서 쓸 수 있는 설치 파일(휠)이 없습니다.")


def fetch(version: str, log: Callable[[str], None] = print) -> Path:
    """휠을 받아 data/pylib/litellm-<버전>에 푼다 (이미 있으면 그대로)."""
    target = version_dir(version)
    if (target / "litellm").is_dir():
        return target
    info = _wheel_info(version)
    PYLIB.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    wheel = PYLIB / info["filename"]
    try:
        log(f"받는 중: {info['filename']} ({info['size'] / 1e6:.0f}MB)")
        digest = hashlib.sha256()
        req = urllib.request.Request(info["url"], headers={"User-Agent": "klaw-chatbot"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(wheel, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != info["digests"]["sha256"]:
            raise RuntimeError("받은 파일의 SHA-256이 PyPI 값과 다릅니다.")
        with zipfile.ZipFile(wheel) as z:
            z.extractall(tmp)
        shutil.rmtree(target, ignore_errors=True)
        tmp.rename(target)
    finally:
        wheel.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return target


def unmet_requirements(folder: Path) -> list[str]:
    """휠이 요구하는 패키지 중 exe 안의 버전과 맞지 않는 것 (있으면 새 exe가 필요하다)."""
    from packaging.requirements import Requirement
    meta = next(folder.glob("litellm-*.dist-info/METADATA"), None)
    if meta is None:
        return ["METADATA 없음"]
    bad = []
    for line in meta.read_text(encoding="utf-8").splitlines():
        if not line.startswith("Requires-Dist:"):
            continue
        req = Requirement(line.split(":", 1)[1].strip())
        if req.marker and not req.marker.evaluate({"extra": ""}):
            continue   # 선택 기능(extra)용 의존성
        try:
            have = md.version(req.name)
        except md.PackageNotFoundError:
            bad.append(f"{req.name}(exe에 없음)")
            continue
        if req.specifier and not req.specifier.contains(have, prereleases=True):
            bad.append(f"{req.name}{req.specifier} (exe에는 {have})")
    return bad


def self_check(folder: Path, model_ids: list[str], timeout: int = 300) -> dict:
    """
    exe 자신을 시험 모드로 띄워 그 폴더의 LiteLLM을 실제로 불러 확인한다 (지금 앱에 이미 올라온 LiteLLM과 섞이지 않도록 별도 프로세스).
    onefile exe는 같은 앱의 자식 프로세스로 보고 이미 풀어 둔 임시 폴더를 다시 쓰므로 빠르다.
    """
    out = stack.RUNTIME_DIR / f"litellm_check_{os.getpid()}.json"
    cmd = [sys.executable] + ([] if stack.FROZEN else [str(stack.REPO_ROOT / "launcher.py")])
    cmd += [CHECK_FLAG, str(folder), str(out), *model_ids]
    try:
        subprocess.run(cmd, timeout=timeout, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = out.read_text(encoding="utf-8") if out.exists() else ""
    except subprocess.TimeoutExpired:
        return {"ok": False, "errors": [f"{timeout}초 안에 확인을 끝내지 못했습니다."]}
    finally:
        out.unlink(missing_ok=True)
    line = next((l for l in reversed(text.splitlines()) if l.startswith("COMPAT_RESULT:")), None)
    if line is None:
        return {"ok": False, "errors": ["확인 프로세스가 결과를 남기지 못했습니다."]}
    return json.loads(line[len("COMPAT_RESULT:"):])


def run_check_mode(argv: list[str]) -> int:
    """launcher가 CHECK_FLAG로 불렸을 때: <폴더> <결과 파일> <모델들…> — 결과를 파일에 쓴다."""
    folder, out, *models = argv
    sys.path.insert(0, folder)
    import contextlib
    import runpy
    script = stack.BUNDLE / "scripts" / "check_litellm_compat.py"
    with open(out, "w", encoding="utf-8") as f, contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
        sys.argv = [str(script), *models]
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit:
            pass
    return 0


def check(version: str, model_ids: list[str], log: Callable[[str], None] = print) -> dict:
    """받기 → 요구 패키지 확인 → 실제 import 확인. 통과하면 폴더를 남겨 두고(적용할 때 씀), 실패하면 지운다."""
    folder = fetch(version, log)
    unmet = unmet_requirements(folder)
    if unmet:
        result = {"ok": False, "reason": "exe에 든 패키지와 맞지 않습니다 (새 exe 필요): " + ", ".join(unmet)}
    else:
        data = self_check(folder, model_ids)
        errors = list(data.get("errors") or [])
        if data.get("missingPrices"):
            errors.append("가격표에 없는 등록 모델: " + ", ".join(data["missingPrices"]))
        result = {"ok": bool(data.get("ok")), "reason": "호환" if data.get("ok") else "; ".join(errors)[:400]}
    state = _read_state()
    if not result["ok"] and version not in (state.get("active"), state.get("trial")):
        shutil.rmtree(folder, ignore_errors=True)
    return result


def set_trial(version: Optional[str]) -> None:
    """다음 실행에 쓸 버전. None이면 exe 기본 버전으로 돌아간다 (바로 확정)."""
    state = _read_state()
    if version is None or version == bundled_version():
        state.update(active=None, trial=None, trialBoots=0)
        _write_state(state)
        _cleanup_except(None)
        return
    if not (version_dir(version) / "litellm").is_dir():
        raise RuntimeError(f"LiteLLM {version} 폴더가 없습니다 (확인 단계를 먼저 거쳐야 합니다).")
    state.update(trial=version, trialBoots=0)
    _write_state(state)
