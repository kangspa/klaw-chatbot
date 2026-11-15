"""
백업 묶음(zip) 만들기·검사와, 받아 둔 복원 적용 — Google Drive 백업(api/chatbot/backup.py)이 쓴다.

복원은 실행 중에 할 수 없다 (DB·대화 상태 파일이 열려 있고 .env도 이미 읽혔다). 그래서
  1. 화면에서 고른 백업을 받아 검사한 뒤 .runtime/restore_pending/ 에 풀어 둔다
  2. 앱을 다시 시작한다 (exe: 자동 재시작, 개발 실행: 사용자가 다시 띄운다)
  3. 다음 시작 때 앱 모듈이 아무것도 열기 전에(api/__init__) apply_pending()이 파일을 바꿔 넣는다
     — 바꾸기 전 파일은 .runtime/restore_previous/ 에 한 벌 남겨 둔다 (한 번 되돌릴 수 있게)
벡터(Qdrant)는 백업하지 않는다 — 시작할 때 임베딩 동기화가 복원된 대화로 다시 맞춘다.

이 모듈은 앱을 import하기 전에 불리므로 가벼운 표준 라이브러리만 쓴다.
"""
import json
import os
from contextlib import closing
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional

from api import stack

FORMAT = 1
PENDING = stack.RUNTIME_DIR / "restore_pending"
PREVIOUS = stack.RUNTIME_DIR / "restore_previous"
DONE_FILE = stack.RUNTIME_DIR / "restore_done.json"
# 묶음 안 이름 → 이 PC의 위치
TARGETS = {
    "klaw.db": stack.DATA_DIR / "klaw.db",                 # 대화·세션·설정·모델 목록·사용량
    "checkpoints.db": stack.DATA_DIR / "checkpoints.db",   # 대화 상태 (요약·엔티티 기억)
    "env": stack.HOME / ".env",                            # 설정·API 키
}
_HELPER_FLAGS = ("--klaw-litellm-check", "--klaw-uninstall")


# ────────────────────────── 묶기 ──────────────────────────
def _snapshot_sqlite(src: Path, dst: Path) -> bool:
    """열려 있는(WAL) DB도 일관된 시점으로 복사한다 (sqlite 온라인 백업)."""
    if not src.exists():
        return False
    # sqlite3의 with는 연결을 닫지 않는다 — Windows에서 파일이 잠긴 채 남지 않도록 closing으로 닫는다
    with closing(sqlite3.connect(f"file:{src}?mode=ro", uri=True)) as s, closing(sqlite3.connect(dst)) as d:
        s.backup(d)
    return True


def _env_text(include_keys: bool, key_envs: set[str]) -> str:
    env = TARGETS["env"]
    if not env.exists():
        return ""
    lines = env.read_text(encoding="utf-8").splitlines()
    if not include_keys:
        lines = [l for l in lines if l.split("=", 1)[0].strip() not in key_envs]
    return "\n".join(lines) + "\n"


def build_bundle(include_keys: bool, key_envs: set[str], app_version: str = "") -> Path:
    """백업 zip을 임시 파일로 만든다. 호출한 쪽이 지운다."""
    work = Path(tempfile.mkdtemp(prefix="klaw-backup-"))
    try:
        files = {}
        for name in ("klaw.db", "checkpoints.db"):
            if _snapshot_sqlite(TARGETS[name], work / name):
                files[name] = (work / name).stat().st_size
        (work / "env").write_text(_env_text(include_keys, key_envs), encoding="utf-8")
        files["env"] = (work / "env").stat().st_size
        manifest = {"app": "klaw", "format": FORMAT, "createdAt": time.time(), "appVersion": app_version,
                    "includesKeys": include_keys, "files": files}
        fd, name = tempfile.mkstemp(prefix="klaw-backup-", suffix=".zip")
        os.close(fd)   # Windows: 핸들을 닫아야 같은 파일에 zip을 쓸 수 있다
        out = Path(name)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
            for name in files:
                z.write(work / name, name)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ────────────────────────── 복원 준비 ──────────────────────────
class BundleError(ValueError):
    pass


def stage_restore(bundle: Path) -> dict:
    """받은 zip을 검사하고 restore_pending/ 에 풀어 둔다. manifest를 돌려준다."""
    try:
        z = zipfile.ZipFile(bundle)
    except zipfile.BadZipFile:
        raise BundleError("백업 파일이 손상되었습니다.")
    with z:
        try:
            manifest = json.loads(z.read("manifest.json"))
        except KeyError:
            raise BundleError("KLaw 백업 파일이 아닙니다 (manifest 없음).")
        if manifest.get("app") != "klaw":
            raise BundleError("KLaw 백업 파일이 아닙니다.")
        if int(manifest.get("format", 0)) > FORMAT:
            raise BundleError("더 새 버전의 KLaw에서 만든 백업입니다. KLaw를 업데이트한 뒤 복원하세요.")
        names = [n for n in z.namelist() if n in TARGETS]
        if "klaw.db" not in names:
            raise BundleError("백업에 대화 DB가 없습니다.")
        shutil.rmtree(PENDING, ignore_errors=True)
        PENDING.mkdir(parents=True)
        for n in names:
            (PENDING / n).write_bytes(z.read(n))
    # 풀어 둔 DB가 정상인지 열어 본다
    try:
        with closing(sqlite3.connect(PENDING / "klaw.db")) as c:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise BundleError(f"백업 DB 검사 실패: {ok}")
    except sqlite3.DatabaseError as e:
        shutil.rmtree(PENDING, ignore_errors=True)
        raise BundleError(f"백업 DB를 열 수 없습니다: {e}")
    (PENDING / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return manifest


def cancel_pending() -> None:
    shutil.rmtree(PENDING, ignore_errors=True)


def pending() -> Optional[dict]:
    try:
        return json.loads((PENDING / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return None


# ────────────────────────── 시작할 때 적용 ──────────────────────────
def _wait_for_previous_instance() -> None:
    """재시작으로 띄워진 경우 이전 프로세스가 끝나 파일을 놓을 때까지 기다린다."""
    if "--after-pid" not in sys.argv or os.name != "nt":
        return
    try:
        pid = int(sys.argv[sys.argv.index("--after-pid") + 1])
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
        if h:
            k32.WaitForSingleObject(h, 90_000)
            k32.CloseHandle(h)
    except Exception:
        pass


def apply_pending() -> Optional[dict]:
    """받아 둔 복원이 있으면 파일을 바꿔 넣는다 (앱 모듈이 DB를 열기 전, api/__init__에서)."""
    manifest = pending()
    if manifest is None or any(f in sys.argv for f in _HELPER_FLAGS):
        return None
    _wait_for_previous_instance()
    shutil.rmtree(PREVIOUS, ignore_errors=True)
    PREVIOUS.mkdir(parents=True)
    names = [n for n in TARGETS if (PENDING / n).exists()]
    moved: list[tuple[Path, Path]] = []   # (원래 위치, 보관 위치) — 실패하면 되돌린다
    try:
        # 1단계: 지금 파일을 모두 보관 폴더로 (이전 DB의 WAL 조각이 새 DB에 섞이면 안 되므로 -wal·-shm도)
        for name in names:
            for suffix in ("", "-wal", "-shm"):
                old = Path(str(TARGETS[name]) + suffix)
                if old.exists():
                    keep = PREVIOUS / (".env" if name == "env" else old.name)
                    _move_retry(old, keep)
                    moved.append((old, keep))
        # 2단계: 백업 파일을 제자리에
        for name in names:
            TARGETS[name].parent.mkdir(parents=True, exist_ok=True)
            _move_retry(PENDING / name, TARGETS[name])
    except OSError as e:
        # 파일이 잠겨 있으면(다른 프로그램·백신 등) 원래대로 돌려놓고, 복원은 다음 시작 때 다시 시도한다
        for name in names:
            if TARGETS[name].exists() and not (PENDING / name).exists():
                shutil.move(str(TARGETS[name]), str(PENDING / name))
        for orig, keep in reversed(moved):
            if keep.exists() and not orig.exists():
                shutil.move(str(keep), str(orig))
        print(f"[backup] 복원 적용 실패 — 원래대로 두고 다음 시작 때 다시 시도합니다: {e}", flush=True)
        return None
    replaced = names
    shutil.rmtree(PENDING, ignore_errors=True)
    info = {"appliedAt": time.time(), "backupCreatedAt": manifest.get("createdAt"), "replaced": replaced,
            "includesKeys": manifest.get("includesKeys")}
    DONE_FILE.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    print(f"[backup] 복원 적용: {replaced} (이전 파일은 {PREVIOUS})", flush=True)
    return info


def _move_retry(src: Path, dst: Path, tries: int = 10) -> None:
    for i in range(tries):
        try:
            shutil.move(str(src), str(dst))
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.5)   # 이전 프로세스가 막 끝나 파일 잠금이 아직 풀리지 않았을 수 있다


def last_restore() -> Optional[dict]:
    try:
        return json.loads(DONE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
