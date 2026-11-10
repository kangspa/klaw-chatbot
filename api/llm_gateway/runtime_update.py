"""
런타임 버전 변경 — Ollama와 LiteLLM을 한 작업으로 바꾼다 (마이 페이지 '런타임 버전').

순서 (둘 다 바꿀 때):
  1. LiteLLM 호환성 확인 (별도 가상환경)  — 안 맞으면 아무것도 바꾸지 않고 끝낸다
  2. Ollama 변경 (엔진 다운로드 → 다시 시작 → 확인, 실패 시 이전 버전으로 되돌림) — 실패하면 LiteLLM은 건드리지 않는다
  3. LiteLLM 적용
     개발: 서버가 scripts/apply_litellm_update.py를 새 콘솔로 띄우고 스스로 종료한다.
           도우미가 설치·확인(실패 시 되돌림)한 뒤 서버를 다시 띄운다.
     exe : 확인 단계에서 받아 둔 휠 폴더를 '다음 실행에 시험할 버전'으로 기록하고 앱 전체를 다시 띄운다.
           새 앱이 뜨면 확정, 못 뜨면 원래 버전으로 되돌린다 (api/litellm_overlay.py).

진행 상태는 메모리에 두되, 서버가 재시작되는 3단계부터는 .runtime/runtime_job.json에 적어
도우미가 이어서 기록하고, 다시 뜬 서버가 그 파일을 읽어 결과를 보여준다.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from api import runtime_control
from api.llm_gateway import litellm_upgrade as lu
from api.llm_gateway.ollama_upgrade import UPGRADER as OLLAMA_UPGRADER, UpgradeJob

from api.stack import FROZEN, REPO_ROOT as PROJECT_ROOT, RUNTIME_DIR

JOB_FILE = RUNTIME_DIR / "runtime_job.json"
HELPER = PROJECT_ROOT / "scripts" / "apply_litellm_update.py"


class RuntimeUpdateError(RuntimeError):
    pass


@dataclass
class RuntimeJob:
    status: str = "running"              # running | restarting | done | error
    step: str = "준비 중"
    items: dict = field(default_factory=dict)   # {"ollama": {from,to,status,rolledBack}, "litellm": {...}}
    message: str = ""
    started_at: float = 0.0
    finished_at: Optional[float] = None
    model_ids: list = field(default_factory=list)   # LiteLLM 호환성 확인에 쓸 등록 모델
    log: deque = field(default_factory=lambda: deque(maxlen=400))

    def to_dict(self, log_tail: int = 80) -> dict:
        return {
            "status": self.status, "step": self.step, "items": self.items, "message": self.message,
            "startedAt": self.started_at, "finishedAt": self.finished_at,
            "modelIds": self.model_ids, "log": list(self.log)[-log_tail:],
        }


class RuntimeUpdater:
    def __init__(self) -> None:
        self.job: Optional[RuntimeJob] = None

    @property
    def running(self) -> bool:
        return self.job is not None and self.job.status in ("running", "restarting")

    def current(self) -> Optional[dict]:
        """진행 중이거나 마지막 작업. 서버가 재시작됐다면 도우미가 남긴 파일에서 읽는다."""
        if self.job is not None:
            return self.job.to_dict()
        try:
            return json.loads(JOB_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None

    def start(self, ollama: Optional[str], litellm: Optional[str], model_ids: list[str]) -> RuntimeJob:
        if self.running or OLLAMA_UPGRADER.running:
            raise RuntimeUpdateError("이미 런타임 버전을 변경하는 중입니다.")
        if litellm and FROZEN and not runtime_control.can_restart_app():
            raise RuntimeUpdateError("앱을 다시 띄울 수 없는 실행 방식이라 LiteLLM을 바꿀 수 없습니다.")
        if litellm and not FROZEN and not runtime_control.can_restart():
            raise RuntimeUpdateError(
                "LiteLLM을 바꾸려면 서버를 다시 띄워야 하는데, 이 서버는 `uv run python main.py`로 실행되지 않아 "
                "자동 재시작을 할 수 없습니다."
            )
        job = RuntimeJob(started_at=time.time(), model_ids=model_ids)
        if ollama:
            job.items["ollama"] = {"from": None, "to": ollama, "status": "pending", "rolledBack": False}
        if litellm:
            job.items["litellm"] = {"from": lu.installed_version(), "to": litellm, "status": "pending", "rolledBack": False}
        self.job = job
        asyncio.get_running_loop().create_task(asyncio.to_thread(self._run, job))
        return job

    # ── 실행 (스레드) ──
    def _run(self, job: RuntimeJob) -> None:
        ollama = job.items.get("ollama")
        litellm = job.items.get("litellm")
        try:
            if litellm:
                self._step(job, f"LiteLLM {litellm['to']} 호환성 확인")
                res = lu.check_compat(litellm["to"], job.model_ids)
                self._log(job, f"호환성: {'통과' if res.get('ok') else '실패'} — {res.get('reason', '')}")
                if not res.get("ok"):
                    litellm["status"] = "error"
                    raise RuntimeUpdateError(f"LiteLLM {litellm['to']}은 이 프로젝트와 호환되지 않아 바꾸지 않았습니다.")

            if ollama:
                sub = UpgradeJob(target=ollama["to"], started_at=time.time(), log=job.log)
                OLLAMA_UPGRADER.job = sub       # 다운로드·채팅 차단 가드가 Ollama 변경 중임을 알 수 있게
                self._step(job, f"Ollama {ollama['to']} 적용")
                OLLAMA_UPGRADER._run(sub)
                ollama.update(**{"from": sub.previous, "status": sub.status, "rolledBack": sub.rolled_back})
                if sub.status != "done":
                    if litellm:
                        litellm["status"] = "skipped"
                    raise RuntimeUpdateError(sub.message + (" (LiteLLM은 바꾸지 않았습니다)" if litellm else ""))
                job.message = sub.message

            if litellm and FROZEN:
                from api import litellm_overlay
                self._step(job, f"LiteLLM {litellm['to']} 적용 — 앱 재시작")
                litellm_overlay.set_trial(litellm["to"])
                self._log(job, "앱을 다시 시작합니다. 잠시 뒤 이 화면이 다시 연결됩니다 (새 버전으로 뜨지 못하면 원래 버전으로 되돌립니다).")
                litellm["status"] = "running"
                job.status = "restarting"
                self._save(job)
                runtime_control.request_restart()
                return   # 이후 기록은 다시 뜬 앱이 이어서 한다 (litellm_overlay.confirm_boot)

            if litellm:
                self._step(job, f"LiteLLM {litellm['to']} 적용 — 서버 재시작")
                self._log(job, "백엔드 서버를 내리고 새 콘솔 창에서 설치 후 다시 띄웁니다. 잠시 뒤 이 화면이 다시 연결됩니다.")
                litellm["status"] = "running"
                job.status = "restarting"
                self._save(job)
                self._spawn_helper(litellm["to"])
                runtime_control.request_shutdown()
                return   # 이후 기록은 도우미가 이어서 한다

            job.status, job.step = "done", "완료"
        except Exception as e:
            job.status, job.step = "error", "실패"
            job.message = str(e) or type(e).__name__
            self._log(job, f"[오류] {job.message}")
        finally:
            if job.status in ("done", "error"):
                job.finished_at = time.time()
                self._save(job)

    def _spawn_helper(self, target: str) -> None:
        cmd = [sys.executable, str(HELPER), "--target", target, "--pid", str(os.getpid())]
        if sys.platform == "win32":
            # 새 콘솔 창: 도우미의 설치 로그와 다시 뜬 서버 로그가 이 창에 보인다
            subprocess.Popen(cmd, cwd=PROJECT_ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            log = open(RUNTIME_DIR / "server.log", "ab")
            subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def _save(self, job: RuntimeJob) -> None:
        JOB_FILE.parent.mkdir(exist_ok=True)
        data = job.to_dict(log_tail=400)
        tmp = JOB_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(JOB_FILE)

    def _step(self, job: RuntimeJob, step: str) -> None:
        job.step = step
        self._log(job, f"── {step}")

    @staticmethod
    def _log(job: RuntimeJob, line: str) -> None:
        job.log.append(line)


RUNTIME_UPDATER = RuntimeUpdater()
