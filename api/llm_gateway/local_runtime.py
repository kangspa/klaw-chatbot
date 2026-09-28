"""
로컬 LLM 런타임(Docker의 Ollama 컨테이너) 상태·가속 장치 확인.

가속 백엔드 선택은 Ollama가 서버 시작 시 스스로 한다 (NVIDIA → CUDA, AMD → ROCm, Intel/그 외 → Vulkan,
없으면 CPU). 우리가 정하는 것은 '컨테이너에 어떤 장치를 넘기느냐'이며, 이는 scripts/setup_local_llm.py가
호스트를 감지해 docker-compose.override.yml로 만든다. 이 모듈은 그 결과를 컨테이너 로그로 확인한다.

  - scripts/setup_local_llm.py : 호스트 감지 → override 생성 → 컨테이너 재생성 → 모델 받기 → 가속 검증
  - main.py startup            : 읽기 전용 점검 (컨테이너 미기동·모델 누락·CPU 동작을 경고)
"""
import json
import os
import re
import subprocess
import urllib.request
from typing import Optional

# docker/ollama/Dockerfile에 고정한 Ollama 버전
TESTED_OLLAMA_VERSION = "0.34.4"

_GPU_LIBRARIES = {"CUDA": "NVIDIA CUDA", "ROCm": "AMD ROCm", "Vulkan": "Vulkan", "Metal": "Apple Metal"}


def ollama_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")


def ollama_container() -> str:
    return os.getenv("OLLAMA_CONTAINER", "ollama")


def _get_json(url: str, timeout: float = 3.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)
    except Exception:
        return None


def ollama_version(base_url: Optional[str] = None) -> Optional[str]:
    data = _get_json(f"{base_url or ollama_base_url()}/api/version")
    return data.get("version") if data else None


def ollama_models(base_url: Optional[str] = None) -> set[str]:
    """설치된 모델 이름 (':latest' 태그는 생략한 이름도 함께 넣는다)."""
    data = _get_json(f"{base_url or ollama_base_url()}/api/tags") or {}
    names = set()
    for m in data.get("models", []):
        names.add(m["name"])
        if m["name"].endswith(":latest"):
            names.add(m["name"][: -len(":latest")])
    return names


def ollama_log_text(tail: int = 400) -> Optional[str]:
    """Ollama 컨테이너 로그 끝부분 (docker CLI가 없거나 컨테이너가 없으면 None)."""
    try:
        out = subprocess.run(["docker", "logs", "--tail", str(tail), ollama_container()],
                             capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=10)
    except Exception:
        return None
    text = (out.stdout or "") + (out.stderr or "")   # Ollama는 로그를 stderr로 쓴다
    return text if out.returncode == 0 and text else None


_FIELD_RE = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')


def _log_fields(line: str) -> dict[str, str]:
    """Ollama 로그 한 줄의 key=value / key="value with space" 쌍."""
    return {m.group(1): m.group(2) if m.group(2) is not None else m.group(3) for m in _FIELD_RE.finditer(line)}


def read_discovery(log_text: Optional[str]) -> Optional[dict]:
    """
    마지막 서버 시작 시 Ollama가 감지한 추론 장치를 로그에서 읽는다.
    반환: {"devices": [{"library", "name", "type", "total"}], "dropped_igpu": [이름]} / 로그가 없으면 None
    """
    if not log_text:
        return None
    start = log_text.rfind("discovering available GPUs")
    block = log_text[start:] if start >= 0 else log_text
    devices, dropped = [], []
    for line in block.splitlines():
        fields = _log_fields(line)
        if fields.get("msg") == "inference compute":
            devices.append({"library": fields.get("library", ""),
                            "name": fields.get("description") or fields.get("name", ""),
                            "type": fields.get("type", ""), "total": fields.get("total", "")})
        elif fields.get("msg", "").startswith("dropping integrated GPU"):
            dropped.append(fields.get("description") or "integrated GPU")
    return {"devices": devices, "dropped_igpu": dropped}


def describe_acceleration(discovery: Optional[dict]) -> str:
    if discovery is None:
        return "알 수 없음 (Ollama 컨테이너 로그를 읽지 못함)"
    gpus = [d for d in discovery["devices"] if d["library"] in _GPU_LIBRARIES]
    if gpus:
        return ", ".join(f"{_GPU_LIBRARIES[d['library']]} — {d['name']}"
                         + (f" ({d['type']}, {d['total']})" if d["type"] or d["total"] else "") for d in gpus)
    if discovery["dropped_igpu"]:
        return f"CPU (내장 GPU {', '.join(discovery['dropped_igpu'])} 제외됨 — OLLAMA_IGPU_ENABLE=1 필요)"
    return "CPU (컨테이너에 넘어온 가속 장치 없음)"


def is_accelerated(discovery: Optional[dict]) -> bool:
    return bool(discovery) and any(d["library"] in _GPU_LIBRARIES for d in discovery["devices"])


def report_local_runtime(model_ids: list[str]) -> None:
    """
    서버 시작 시 로컬 모델 런타임을 읽기 전용으로 점검해 로그로 알린다 (설정은 바꾸지 않는다).
    model_ids: DB에 활성화된 'ollama/...' 모델 ID
    """
    if not model_ids:
        return
    version = ollama_version()
    if version is None:
        print(f"[local-llm] Ollama({ollama_base_url()})에 연결할 수 없어 로컬 모델 {len(model_ids)}개를 쓸 수 없습니다. "
              "→ uv run python scripts/setup_local_llm.py --apply")
        return
    if version != TESTED_OLLAMA_VERSION:
        print(f"[local-llm] Ollama {version} (고정 버전 {TESTED_OLLAMA_VERSION}과 다름)")
    installed = ollama_models()
    missing = [m for m in model_ids if m.split("/", 1)[1] not in installed]
    print(f"[local-llm] Ollama {version} / 가속: {describe_acceleration(read_discovery(ollama_log_text()))}")
    if missing:
        print(f"[local-llm] 받지 않은 모델 {len(missing)}개: {missing} → uv run python scripts/setup_local_llm.py --pull")
