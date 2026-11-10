"""
Ollama 엔진 설치·버전 변경 (마이 페이지에서 실행). Docker 없이 Windows 배포본을 받아 자식 프로세스로 돌린다.

흐름: GitHub 릴리스에서 SHA-256 확인 → ollama-windows-amd64.zip 다운로드·검증 → data/ollama/engine/<버전>/에 풀기
      → 엔진을 새 버전으로 다시 띄워 버전·가속 확인 → 성공하면 그 버전을 '현재'로 기록하고 이전 버전 폴더를 지운다
      → 실패하면 이전 버전으로 다시 띄우고 받은 폴더를 지운다
엔진이 아직 없으면 같은 흐름이 '처음 설치'가 된다. 받아 둔 모델은 data/ollama/models에 있어 버전을 바꿔도 남는다.

이 서비스는 개인 PC에서 띄워 쓰는 것을 전제로 하므로 별도 권한 확인 없이 실행한다.
"""
import asyncio
import hashlib
import json
import re
import shutil
import time
import urllib.request
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from api import services
from api.llm_gateway import local_runtime as lr

DEFAULT_VERSION = lr.TESTED_OLLAMA_VERSION
ASSET = "ollama-windows-amd64.zip"
RELEASES_API = "https://api.github.com/repos/ollama/ollama/releases?per_page=40"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

READY_TIMEOUT = 120       # 엔진을 다시 띄운 뒤 응답·장치 감지를 마칠 때까지 기다리는 시간(초)


def configured_version() -> str:
    """쓰고 있는 엔진 버전 (없으면 처음 받을 기본 버전)."""
    return services.current_engine_version() or DEFAULT_VERSION


# ────────────────────────── GitHub 릴리스 ──────────────────────────
_release_cache: dict = {"at": 0.0, "data": None}


def _http_get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "klaw-chatbot", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def list_releases(limit: int = 20) -> list[dict]:
    """정식 릴리스 [{version, publishedAt}] 최신순. 10분 캐시 (GitHub API 비인증 호출 한도 보호)."""
    if _release_cache["data"] is not None and time.time() - _release_cache["at"] < 600:
        return _release_cache["data"][:limit]
    data = json.loads(_http_get(RELEASES_API))
    releases = []
    for r in data:
        version = (r.get("tag_name") or "").lstrip("v")
        if r.get("draft") or r.get("prerelease") or not VERSION_RE.match(version):
            continue
        if not any(a.get("name") == ASSET for a in r.get("assets", [])):
            continue   # Windows 배포본이 아직 올라오지 않은 릴리스
        releases.append({"version": version, "publishedAt": (r.get("published_at") or "")[:10]})
    _release_cache.update(at=time.time(), data=releases)
    return releases[:limit]


def fetch_sha256(version: str) -> str:
    """릴리스의 sha256sum.txt에서 Windows 배포본의 SHA-256을 읽는다 (받은 파일을 이 값으로 검증)."""
    text = _http_get(f"https://github.com/ollama/ollama/releases/download/v{version}/sha256sum.txt").decode()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("./") == ASSET and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    raise RuntimeError(f"v{version} 릴리스에서 {ASSET}의 SHA-256을 찾지 못했습니다.")


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


# ────────────────────────── 버전 변경 작업 ──────────────────────────
@dataclass
class UpgradeJob:
    status: str = "running"            # running | done | error
    step: str = "준비 중"
    target: str = ""
    previous: str = ""
    rolled_back: bool = False
    message: str = ""
    started_at: float = 0.0
    finished_at: Optional[float] = None
    log: deque = field(default_factory=lambda: deque(maxlen=400))

    def to_dict(self, log_tail: int = 60) -> dict:
        return {
            "status": self.status, "step": self.step, "target": self.target, "previous": self.previous,
            "rolledBack": self.rolled_back, "message": self.message,
            "startedAt": self.started_at, "finishedAt": self.finished_at,
            "log": list(self.log)[-log_tail:],
        }


class UpgradeError(RuntimeError):
    pass


class OllamaUpgrader:
    """한 번에 하나의 설치·버전 변경만 실행한다. 진행 상태는 메모리에 있으며 앱을 다시 켜면 사라진다."""

    def __init__(self) -> None:
        self.job: Optional[UpgradeJob] = None

    @property
    def running(self) -> bool:
        return self.job is not None and self.job.status == "running"

    def start(self, target: str) -> UpgradeJob:
        if self.running:
            raise UpgradeError("이미 Ollama 엔진을 설치·변경하는 중입니다.")
        self.job = UpgradeJob(target=target, started_at=time.time())
        asyncio.get_running_loop().create_task(asyncio.to_thread(self._run, self.job))
        return self.job

    # ── 단계 ──
    def _run(self, job: UpgradeJob) -> None:
        prev_version = services.current_engine_version()
        job.previous = prev_version or ""
        prev_accelerated = bool(prev_version) and lr.is_accelerated(lr.read_discovery(lr.ollama_log_text()))
        target_dir = services.ENGINE_DIR / job.target
        fresh = not (target_dir / "ollama.exe").exists()
        switched = False
        try:
            if fresh:
                self._step(job, "릴리스 확인")
                sha = fetch_sha256(job.target)
                self._log(job, f"v{job.target} {ASSET} SHA-256: {sha}")
                self._step(job, "다운로드")
                archive = self._download(job, sha)
                self._step(job, "압축 풀기")
                self._extract(job, archive, target_dir)

            self._step(job, "엔진 다시 시작" if prev_version else "엔진 시작")
            switched = True
            services.OLLAMA.stop()
            services.OLLAMA.version_override = job.target
            if not services.OLLAMA.start():
                raise UpgradeError(f"Ollama {job.target} 엔진이 시작되지 않았습니다 (logs/ollama.log 확인).")

            self._step(job, "동작 확인")
            discovery = self._wait_ready(job, job.target)
            accel = lr.describe_acceleration(discovery)
            self._log(job, f"가속: {accel}")
            if prev_accelerated and not lr.is_accelerated(discovery):
                raise UpgradeError(f"Ollama {job.target}에서 GPU를 인식하지 못했습니다 ({accel}). 이전에는 GPU를 쓰고 있었습니다.")

            services.set_current_engine(job.target)
            services.OLLAMA.version_override = None
            freed = self._remove_other_versions(job.target)
            job.status, job.step = "done", "완료"
            job.message = (f"Ollama {prev_version} → {job.target} 변경 완료 ({accel})" if prev_version
                           else f"Ollama {job.target} 엔진 설치 완료 ({accel})")
            if freed:
                job.message += f" — 이전 엔진 {freed / 1e9:.1f}GB 정리"
            self._log(job, job.message)
        except Exception as e:
            job.message = str(e) or type(e).__name__
            self._log(job, f"[오류] {job.message}")
            if switched:
                self._rollback(job, prev_version)
            if fresh:
                shutil.rmtree(target_dir, ignore_errors=True)
            job.status = "error"
            job.step = "실패" + (" (이전 버전으로 되돌림)" if job.rolled_back else "")
        finally:
            job.finished_at = time.time()
            print(f"[ollama-upgrade] {job.status}: {job.message}")

    def _download(self, job: UpgradeJob, sha: str) -> Path:
        url = f"https://github.com/ollama/ollama/releases/download/v{job.target}/{ASSET}"
        services.ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        part = services.ENGINE_DIR / f"{job.target}.zip.part"
        digest = hashlib.sha256()
        req = urllib.request.Request(url, headers={"User-Agent": "klaw-chatbot"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                done, next_mark = 0, 0.0
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if total and done / total >= next_mark:
                        job.step = f"다운로드 {done / total:.0%} ({done / 1e9:.2f}/{total / 1e9:.2f}GB)"
                        self._log(job, job.step)
                        next_mark += 0.1
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        if digest.hexdigest() != sha:
            part.unlink(missing_ok=True)
            raise UpgradeError("받은 파일의 SHA-256이 릴리스 값과 다릅니다 (다운로드가 손상됐을 수 있습니다).")
        self._log(job, "SHA-256 확인")
        return part

    def _extract(self, job: UpgradeJob, archive: Path, target_dir: Path) -> None:
        tmp = target_dir.with_name(target_dir.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp)
            if not (tmp / "ollama.exe").exists():
                raise UpgradeError(f"{ASSET} 안에 ollama.exe가 없습니다.")
            shutil.rmtree(target_dir, ignore_errors=True)
            tmp.rename(target_dir)
            self._log(job, f"엔진 설치 위치: {target_dir} ({_dir_size(target_dir) / 1e9:.1f}GB)")
        finally:
            archive.unlink(missing_ok=True)   # 압축 파일은 바로 지운다 (디스크 절약)
            shutil.rmtree(tmp, ignore_errors=True)

    def _remove_other_versions(self, keep: str) -> int:
        freed = 0
        for v in services.installed_engine_versions():
            if v != keep:
                d = services.ENGINE_DIR / v
                freed += _dir_size(d)
                shutil.rmtree(d, ignore_errors=True)
        return freed

    def _rollback(self, job: UpgradeJob, version: Optional[str]) -> None:
        services.OLLAMA.stop()
        services.OLLAMA.version_override = None
        if not version:
            return
        self._step(job, f"이전 버전({version})으로 되돌리는 중")
        try:
            if not services.OLLAMA.start():
                raise UpgradeError("엔진이 시작되지 않았습니다")
            self._wait_ready(job, version)
            job.rolled_back = True
            self._log(job, f"Ollama {version}으로 되돌렸습니다.")
        except Exception as e:
            self._log(job, f"[오류] 되돌리기 실패: {e} — 앱을 다시 켜 보세요.")

    def _wait_ready(self, job: UpgradeJob, version: str) -> Optional[dict]:
        """
        엔진이 해당 버전으로 응답하고 추론 장치 감지를 마칠 때까지 기다린다.
        API가 먼저 열리고 장치 감지 로그('inference compute')가 조금 뒤에 찍히므로 버전이 맞은 뒤 최대 30초 더 기다린다.
        """
        deadline = time.time() + READY_TIMEOUT
        running = None
        while time.time() < deadline:
            running = lr.ollama_version()
            if running == version:
                self._log(job, f"Ollama {running} 응답 확인")
                settle = time.time() + 30
                discovery = None
                while time.time() < settle:
                    discovery = lr.read_discovery(lr.ollama_log_text())
                    if discovery and discovery["devices"]:
                        break
                    time.sleep(1)
                return discovery
            time.sleep(1)
        raise UpgradeError(f"Ollama {version}이 {READY_TIMEOUT}초 안에 준비되지 않았습니다 (현재 응답 버전: {running}).")

    def _step(self, job: UpgradeJob, step: str) -> None:
        job.step = step
        self._log(job, f"── {step}")

    @staticmethod
    def _log(job: UpgradeJob, line: str) -> None:
        job.log.append(line)


def _dir_size(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.exists() else 0


def engine_disk_usage() -> dict:
    return {"engine": _dir_size(services.ENGINE_DIR), "models": _dir_size(services.MODELS_DIR)}


def remove_engine(include_models: bool = False) -> int:
    """엔진(과 선택 시 받은 모델)을 지워 디스크를 비운다. 지운 바이트 수."""
    if UPGRADER.running:
        raise UpgradeError("엔진을 설치·변경하는 중에는 지울 수 없습니다.")
    services.OLLAMA.stop()
    freed = 0
    for d in [services.ENGINE_DIR] + ([services.MODELS_DIR] if include_models else []):
        freed += _dir_size(d)
        shutil.rmtree(d, ignore_errors=True)
    return freed


UPGRADER = OllamaUpgrader()
