"""
Ollama 컨테이너 버전 변경 (마이 페이지에서 실행).

흐름: GitHub 릴리스에서 SHA-256 확인 → 이미지 빌드(klaw-ollama:<버전>) → 컨테이너 재생성 → 버전·가속 확인
      → 성공하면 루트 .env에 OLLAMA_VERSION / OLLAMA_SHA256 저장
      → 실패하면 이전 버전 이미지로 컨테이너를 되돌린다 (이미지를 버전별 태그로 남기므로 빌드 없이 재생성만 한다)

docker-compose.yml이 .env의 두 값을 빌드 인자·이미지 태그로 쓰므로, 저장해 두면 이후 `docker compose up`이나
scripts/setup_local_llm.py로 컨테이너를 다시 만들어도 같은 버전이 유지된다.
받아 둔 모델은 Docker 볼륨(ollama_models)에 있어 버전을 바꿔도 남는다.

이 서비스는 개인 PC에서 띄워 쓰는 것을 전제로 하므로 별도 권한 확인 없이 실행한다.
docker CLI가 있는 호스트에서 백엔드가 돌아야 한다 (백엔드를 컨테이너로 띄우면 Docker 소켓이 필요해 권장하지 않는다).
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from api.env_store import read_env_file, write_env_values
from api.llm_gateway import local_runtime as lr

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# docker-compose.yml·Dockerfile의 기본값과 같아야 한다 (.env에 값이 없을 때 쓰이는 버전)
DEFAULT_VERSION = "0.34.4"
DEFAULT_SHA256 = "c238986e61d40c0cc5f4a9b9e40b9eea104350b77efa34741fc134e105cb9533"
ASSET = "ollama-linux-amd64.tar.zst"
RELEASES_API = "https://api.github.com/repos/ollama/ollama/releases?per_page=40"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

READY_TIMEOUT = 180       # 컨테이너 재시작 후 Ollama가 응답·장치 감지를 마칠 때까지 기다리는 시간(초)
BUILD_TIMEOUT = 1800      # 이미지 빌드 상한 (Ollama 배포본 다운로드 포함)


def configured_version() -> str:
    """docker-compose가 쓸 버전 (.env의 OLLAMA_VERSION, 없으면 기본값)."""
    return _read_env_file().get("OLLAMA_VERSION") or DEFAULT_VERSION


def _read_env_file() -> dict[str, str]:
    return read_env_file()


def _write_env_values(updates: dict[str, str]) -> None:
    write_env_values(updates, comment="로컬 모델 서버(Ollama) 버전 — 마이 페이지에서 변경 (docker-compose.yml이 빌드 인자로 사용)")


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
            continue   # 리눅스 배포본이 아직 올라오지 않은 릴리스는 빌드할 수 없다
        releases.append({"version": version, "publishedAt": (r.get("published_at") or "")[:10]})
    _release_cache.update(at=time.time(), data=releases)
    return releases[:limit]


def fetch_sha256(version: str) -> str:
    """릴리스의 sha256sum.txt에서 리눅스 배포본의 SHA-256을 읽는다 (Dockerfile이 다운로드 파일을 이 값으로 검증)."""
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
    """한 번에 하나의 버전 변경만 실행한다. 진행 상태는 메모리에 있으며 서버를 재시작하면 사라진다."""

    def __init__(self) -> None:
        self.job: Optional[UpgradeJob] = None

    @property
    def running(self) -> bool:
        return self.job is not None and self.job.status == "running"

    def start(self, target: str) -> UpgradeJob:
        if self.running:
            raise UpgradeError("이미 Ollama 버전을 변경하는 중입니다.")
        self.job = UpgradeJob(target=target, started_at=time.time())
        asyncio.get_running_loop().create_task(self._run_async(self.job))
        return self.job

    async def _run_async(self, job: UpgradeJob) -> None:
        # docker CLI 호출이 블로킹이고, Windows에서는 SelectorEventLoop라 asyncio 서브프로세스를 못 쓰므로 스레드에서 돌린다
        await asyncio.to_thread(self._run, job)

    # ── 단계 ──
    def _run(self, job: UpgradeJob) -> None:
        prev_version = lr.ollama_version() or configured_version()
        job.previous = prev_version
        prev_env = _read_env_file()
        prev_sha = prev_env.get("OLLAMA_SHA256") or (DEFAULT_SHA256 if prev_version == DEFAULT_VERSION else "")
        prev_accelerated = lr.is_accelerated(lr.read_discovery(lr.ollama_log_text()))
        changed_container = False
        try:
            if shutil.which("docker") is None:
                raise UpgradeError("docker 명령을 찾을 수 없습니다. 백엔드를 Docker가 설치된 호스트에서 실행하세요.")

            self._step(job, "릴리스 확인")
            sha = fetch_sha256(job.target)
            self._log(job, f"v{job.target} {ASSET} SHA-256: {sha}")

            env = {"OLLAMA_VERSION": job.target, "OLLAMA_SHA256": sha}
            self._step(job, "이미지 빌드")
            self._compose(job, ["build", "ollama"], env, BUILD_TIMEOUT)

            self._step(job, "컨테이너 재시작")
            changed_container = True
            self._compose(job, ["up", "-d", "--no-build", "--force-recreate", "ollama"], env, 300)

            self._step(job, "동작 확인")
            discovery = self._wait_ready(job, job.target)
            accel = lr.describe_acceleration(discovery)
            self._log(job, f"가속: {accel}")
            if prev_accelerated and not lr.is_accelerated(discovery):
                raise UpgradeError(f"Ollama {job.target}에서 GPU를 인식하지 못했습니다 ({accel}). 이전에는 GPU를 쓰고 있었습니다.")

            _write_env_values(env)
            job.status, job.step = "done", "완료"
            job.message = f"Ollama {prev_version} → {job.target} 변경 완료 ({accel})"
            self._log(job, job.message)
        except Exception as e:
            job.message = str(e) or type(e).__name__
            self._log(job, f"[오류] {job.message}")
            if changed_container:
                self._rollback(job, prev_version, prev_sha)
            job.status = "error"
            job.step = "실패" + (" (이전 버전으로 되돌림)" if job.rolled_back else "")
        finally:
            job.finished_at = time.time()
            print(f"[ollama-upgrade] {job.status}: {job.message}")

    def _rollback(self, job: UpgradeJob, version: str, sha: str) -> None:
        self._step(job, f"이전 버전({version})으로 되돌리는 중")
        try:
            env = {"OLLAMA_VERSION": version, "OLLAMA_SHA256": sha or DEFAULT_SHA256}
            # 이전 버전 이미지는 태그로 남아 있으므로 빌드하지 않고 재생성만 한다
            self._compose(job, ["up", "-d", "--no-build", "--force-recreate", "ollama"], env, 300)
            self._wait_ready(job, version)
            job.rolled_back = True
            self._log(job, f"Ollama {version}으로 되돌렸습니다.")
        except Exception as e:
            self._log(job, f"[오류] 되돌리기 실패: {e} — `docker compose up -d ollama`로 직접 다시 띄우세요.")

    # ── 도우미 ──
    def _compose(self, job: UpgradeJob, args: list[str], env_over: dict[str, str], timeout: int) -> None:
        cmd = ["docker", "compose", *args]
        self._log(job, "$ " + " ".join(cmd))
        proc = subprocess.Popen(
            cmd, cwd=PROJECT_ROOT, env={**os.environ, **env_over},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        deadline = time.time() + timeout
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self._log(job, line)
            if time.time() > deadline:
                proc.kill()
                raise UpgradeError(f"docker compose {args[0]}가 {timeout}초 안에 끝나지 않았습니다.")
        if proc.wait() != 0:
            raise UpgradeError(f"docker compose {args[0]} 실패 (종료 코드 {proc.returncode}). 로그를 확인하세요.")

    def _wait_ready(self, job: UpgradeJob, version: str) -> Optional[dict]:
        """
        새 컨테이너의 Ollama가 해당 버전으로 응답하고 추론 장치 감지를 마칠 때까지 기다린다.
        API가 먼저 열리고 장치 감지 로그('inference compute')가 조금 뒤에 찍히므로, 버전이 맞은 뒤에도
        최대 30초 더 기다린다. 그래도 장치 줄이 없으면 그때 상태(=CPU로 판단)를 돌려준다.
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
                    time.sleep(2)
                return discovery
            time.sleep(2)
        raise UpgradeError(f"Ollama {version}이 {READY_TIMEOUT}초 안에 준비되지 않았습니다 (현재 응답 버전: {running}).")

    def _step(self, job: UpgradeJob, step: str) -> None:
        job.step = step
        self._log(job, f"── {step}")

    @staticmethod
    def _log(job: UpgradeJob, line: str) -> None:
        job.log.append(line)


UPGRADER = OllamaUpgrader()
