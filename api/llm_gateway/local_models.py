"""
로컬 모델(Ollama) 설치 상태 조회와 다운로드(pull) 관리.

마이 페이지에서 사용자가 로컬 모델을 직접 받고 지울 수 있게 하려는 것이다.
  - 설치 상태 : Ollama /api/tags 로 확인한다 (Ollama에 못 붙으면 '알 수 없음')
  - 다운로드  : Ollama /api/pull 을 서버 백그라운드 작업으로 돌리고, 진행률을 메모리에 들고 있는다.
               프론트는 진행률을 주기적으로 조회하므로 페이지를 떠나도 다운로드는 계속된다.
  - 삭제      : Ollama /api/delete. 디스크 용량을 정리하는 용도다.

받을 수 있는 모델은 llm_models에 등록된 ollama 모델로 한정한다 (임의 이름을 받게 하지 않는다 — 호출부 책임).
진행 상태는 프로세스 메모리에만 있으므로 서버를 재시작하면 사라진다. 받는 중이던 모델은 Ollama가
이어받기를 지원하므로 다시 '다운로드'를 누르면 남은 부분부터 받는다.
"""
import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Optional

import httpx

from api.llm_gateway.local_runtime import ollama_base_url
from api.llm_gateway.providers import split_model_id

# 설치 상태를 확인하고 받을 수 있는 접두사 (lmstudio·vllm은 서버가 모델 파일을 관리하지 않는다)
PULLABLE_PROVIDERS = {"ollama", "ollama_chat"}


def ollama_name(model_id: str) -> Optional[str]:
    """'ollama/이름' → Ollama 모델 이름. Ollama 모델이 아니면 None."""
    provider, name = split_model_id(model_id)
    return name if provider in PULLABLE_PROVIDERS else None


# Ollama 모델명: 'lfm3-omni:2b', 'qwen3.8:27b-q4_K_M', 'hf.co/google/gemma-4-12B-it-qat-q4_0-gguf' 등
_OLLAMA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(/[A-Za-z0-9._\-]+)*(:[A-Za-z0-9._\-]+)?$")
# 사용자가 Ollama 사이트에서 그대로 복사해 붙여 넣는 앞부분
_NAME_PREFIXES = ("ollama run ", "ollama pull ", "https://ollama.com/library/", "https://ollama.com/", "ollama.com/library/")


def normalize_ollama_name(raw: str) -> str:
    """
    입력한 모델명을 Ollama 모델명으로 정리한다. 형식이 맞지 않으면 ValueError.
    'ollama run lfm3-omni:2b'나 ollama.com 주소를 붙여 넣어도 모델명만 남긴다.
    """
    name = (raw or "").strip()
    lowered = name.lower()
    for prefix in _NAME_PREFIXES:
        if lowered.startswith(prefix):
            name = name[len(prefix):].strip()
            break
    if not name:
        raise ValueError("모델명을 입력하세요. 예: lfm3-omni:2b")
    if len(name) > 200 or ".." in name or not _OLLAMA_NAME_RE.match(name):
        raise ValueError(f"Ollama 모델명 형식이 아닙니다: {raw!r} (예: lfm3-omni:2b, qwen3.8:27b-q4_K_M)")
    if name.lower().startswith("hf.co/"):
        # Ollama는 hf.co/로 받으면 승인(gated)·비공개 저장소의 인증 단계에서 'realm host huggingface.co does not match
        # original host hf.co' 오류로 멈춘다. huggingface.co/로 받으면 공개 저장소는 똑같이 받아지고, 승인 저장소는
        # Ollama SSH 키 인증까지 간다 (API 키 화면의 공개키를 HF 계정에 등록해 두면 받아진다).
        name = "huggingface.co/" + name[len("hf.co/"):]
    tag = name.rsplit(":", 1)[1].lower() if ":" in name else ""
    if tag == "cloud" or tag.endswith("-cloud"):
        # Ollama 클라우드 모델은 이 PC가 아니라 ollama.com에서 실행된다 — 로컬 모델만 지원한다
        raise ValueError(f"'{name}'은 Ollama 클라우드 모델이라 지원하지 않습니다. 이 PC에서 실행되는 로컬 모델만 추가할 수 있습니다.")
    return name


async def ollama_capabilities(name: str, timeout: float = 5.0) -> Optional[list[str]]:
    """Ollama /api/show의 capabilities (예: ['completion', 'tools', 'vision']). 확인할 수 없으면 None."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{ollama_base_url()}/api/show", json={"model": name})
            resp.raise_for_status()
            caps = resp.json().get("capabilities")
    except Exception:
        return None
    return list(caps) if isinstance(caps, list) else None


def friendly_pull_error(message: str, name: str) -> str:
    """Ollama 오류 문구를 사용자 안내로 바꾼다."""
    if "requires a newer version of Ollama" in message:
        # 최신 모델은 새 Ollama가 필요하다 (레지스트리가 412로 거절). 컨테이너 Ollama 버전을 올려야 받을 수 있다.
        from api.llm_gateway.local_runtime import ollama_version
        current = ollama_version() or "알 수 없음"
        return (f"'{name}' 모델은 더 새 버전의 Ollama가 필요합니다 (서버 Ollama {current}). "
                "docker/ollama/Dockerfile의 Ollama 버전을 올린 뒤 컨테이너를 다시 빌드하세요.")
    if name.lower().startswith(("huggingface.co/", "hf.co/")) and ("401" in message or "403" in message):
        return (f"'{name}'은 Hugging Face 승인(gated)·비공개 저장소입니다. API 키 화면의 'Ollama SSH 공개키'를 "
                "Hugging Face 계정 > Settings > SSH and GPG keys에 등록하고, 모델 페이지에서 사용 승인을 받은 뒤 다시 추가하세요.")
    if "realm host" in message:
        return f"'{name}'은 인증이 필요한 저장소입니다. 이름을 huggingface.co/로 시작하도록 바꿔 다시 추가하세요."
    if "file does not exist" in message or "not found" in message.lower():
        return f"Ollama에서 '{name}' 모델을 찾을 수 없습니다. 모델명과 태그를 확인하세요 (예: lfm3-omni:2b)."
    return f"'{name}' 모델을 받지 못했습니다: {message}"


async def ollama_inventory(timeout: float = 3.0) -> Optional[list[dict]]:
    """Ollama에 실제로 받아져 있는 모델 [{name, size}] (Ollama 표기 그대로). 연결할 수 없으면 None."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{ollama_base_url()}/api/tags")
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None
    return [{"name": m.get("name") or m.get("model") or "", "size": int(m.get("size") or 0)}
            for m in data.get("models", [])]


async def installed_ollama_models(timeout: float = 3.0) -> Optional[dict[str, int]]:
    """설치된 모델 이름 → 디스크 크기(바이트). ':latest'를 뗀 이름도 함께 넣는다. 연결할 수 없으면 None."""
    inventory = await ollama_inventory(timeout)
    if inventory is None:
        return None
    sizes: dict[str, int] = {}
    for m in inventory:
        sizes[m["name"]] = m["size"]
        if m["name"].endswith(":latest"):
            sizes[m["name"][: -len(":latest")]] = m["size"]
    return sizes


def is_installed(name: str, installed: dict[str, int]) -> bool:
    return name in installed or f"{name}:latest" in installed


def installed_size(name: str, installed: dict[str, int]) -> Optional[int]:
    return installed.get(name, installed.get(f"{name}:latest"))


async def delete_ollama_model(name: str) -> None:
    """
    Ollama에서 모델 파일을 지운다. 메모리에 올라가 있으면 먼저 내린다(keep_alive=0).
    다른 모델과 공유하는 레이어는 Ollama가 남겨 두므로 실제로 풀리는 용량은 모델 크기보다 작을 수 있다.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        try:
            await client.post(f"{ollama_base_url()}/api/generate", json={"model": name, "keep_alive": 0})
        except Exception:
            pass  # 내리기 실패는 무시하고 삭제를 시도한다 (삭제 자체가 실패하면 그때 알린다)
        resp = await client.request("DELETE", f"{ollama_base_url()}/api/delete", json={"model": name})
        if resp.status_code == 404:
            raise FileNotFoundError(f"Ollama에 없는 모델입니다: {name}")
        resp.raise_for_status()


@dataclass
class PullState:
    status: str = "pulling"         # pulling | done | error
    completed: int = 0              # 받은 바이트 (레이어 합계)
    total: int = 0                  # 전체 바이트 (지금까지 알려진 레이어 합계)
    message: str = ""               # Ollama가 보낸 마지막 상태 문구 또는 오류
    started_at: float = 0.0
    finished_at: Optional[float] = None
    # 화면에 보여준 최대 진행률. Ollama는 레이어 크기를 받기 시작할 때에야 알려주므로
    # 큰 레이어가 끝난 뒤 작은 레이어(비전 프로젝터 등)가 나타나면 계산상 진행률이 100% → 97%처럼 줄어든다.
    _peak: float = 0.0

    def percent(self) -> Optional[float]:
        """줄어들지 않는 진행률. 완료 신호 전에는 99%를 넘기지 않는다 (뒤에 레이어가 더 있을 수 있음)."""
        if self.status == "done":
            return 100.0
        if not self.total:
            return None
        self._peak = max(self._peak, min(99.0, 100 * self.completed / self.total))
        return round(self._peak, 1)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_peak", None)
        d["percent"] = self.percent()
        return d


class PullManager:
    """모델별 다운로드 작업 1개씩. 같은 모델을 다시 요청하면 진행 중인 작업을 그대로 돌려준다."""

    def __init__(self) -> None:
        self._states: dict[str, PullState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # 첫 레이어를 받기 시작했거나(=모델이 존재함) 끝났을 때(완료·실패) 켜지는 신호
        self._started: dict[str, asyncio.Event] = {}

    def start(self, model_id: str) -> PullState:
        state = self._states.get(model_id)
        if state is not None and state.status == "pulling":
            return state
        name = ollama_name(model_id)
        if name is None:
            raise ValueError(f"다운로드할 수 있는 로컬 모델이 아닙니다: {model_id}")
        state = PullState(started_at=time.time(), message="다운로드 준비 중")
        self._states[model_id] = state
        self._started[model_id] = asyncio.Event()
        self._tasks[model_id] = asyncio.create_task(self._run(model_id, name, state))
        return state

    async def wait_started(self, model_id: str, timeout: float = 20.0) -> Optional[PullState]:
        """
        다운로드가 실제로 시작될 때까지(또는 실패할 때까지) 기다린다.
        Ollama는 없는 모델이면 'pulling manifest' 직후 오류를 보내므로, 이걸로 모델명이 맞는지 판별한다.
        시간 안에 결론이 안 나면 그때 상태를 그대로 돌려준다 (느린 네트워크 — 받는 중으로 본다).
        """
        event = self._started.get(model_id)
        if event is not None:
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        return self._states.get(model_id)

    def cancel(self, model_id: str) -> bool:
        """받는 중인 다운로드를 멈춘다. 받다 만 파일은 Ollama가 다음 시작 시 정리한다 (OLLAMA_NOPRUNE 기본값)."""
        task = self._tasks.get(model_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def get(self, model_id: str) -> Optional[PullState]:
        return self._states.get(model_id)

    def forget(self, model_id: str) -> None:
        """모델을 지운 뒤 지난 '완료/실패' 기록이 화면에 남지 않도록 지운다 (받는 중이면 그대로 둔다)."""
        state = self._states.get(model_id)
        if state is not None and state.status != "pulling":
            del self._states[model_id]

    def snapshot(self) -> dict[str, dict]:
        return {mid: s.to_dict() for mid, s in self._states.items()}

    async def _run(self, model_id: str, name: str, state: PullState) -> None:
        # 레이어(digest)별로 total/completed가 따로 오므로 합산해 전체 진행률을 만든다
        layers: dict[str, tuple[int, int]] = {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None)) as client:
                async with client.stream("POST", f"{ollama_base_url()}/api/pull",
                                         json={"model": name, "stream": True}) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if event.get("error"):
                            raise RuntimeError(event["error"])
                        state.message = event.get("status", state.message)
                        digest = event.get("digest")
                        if digest and event.get("total"):
                            layers[digest] = (event.get("completed", 0) or 0, event["total"])
                            state.completed = sum(c for c, _ in layers.values())
                            state.total = sum(t for _, t in layers.values())
                            self._signal_started(model_id)
                        if event.get("status") == "success":
                            state.status = "done"
            if state.status != "done":
                raise RuntimeError("Ollama가 완료 신호 없이 연결을 닫았습니다.")
            state.completed = state.total
            print(f"[local_models] 다운로드 완료: {name}")
        except asyncio.CancelledError:
            state.status = "error"
            state.message = "다운로드를 취소했습니다."
            print(f"[local_models] 다운로드 취소: {name}")
        except Exception as e:
            state.status = "error"
            state.message = str(e) if isinstance(e, RuntimeError) and str(e) else (
                f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
            print(f"[local_models] 다운로드 실패: {name} — {state.message}")
        finally:
            state.finished_at = time.time()
            self._tasks.pop(model_id, None)
            self._signal_started(model_id)

    def _signal_started(self, model_id: str) -> None:
        event = self._started.get(model_id)
        if event is not None and not event.is_set():
            event.set()


PULL_MANAGER = PullManager()
