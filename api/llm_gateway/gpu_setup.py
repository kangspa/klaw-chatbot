"""
로컬 LLM(Ollama 컨테이너) GPU 가속 설정 — 호스트를 감지해 docker-compose.override.yml을 만든다.

scripts/setup_local_llm.py(개발)와 패키징된 exe의 첫 실행(launcher)이 함께 쓴다.
override 파일은 스택 위치(api/stack.py의 HOME)에 쓰며, compose_cmd()가 있으면 함께 붙인다.

  감지 결과                          컨테이너 설정                        Ollama 백엔드
  NVIDIA GPU                         GPU 예약 (nvidia container toolkit)  CUDA
  Windows Docker Desktop(WSL2) + GPU /dev/dxg + /usr/lib/wsl 마운트       Vulkan (Mesa Dozen, D3D12 위)
  Linux + /dev/dri (Intel/AMD)       /dev/dri 전달                        Vulkan (Mesa ANV/RADV)
  가속 장치 없음 / macOS             없음                                 CPU
"""
import platform
import subprocess
from pathlib import Path

from api.stack import OVERRIDE_FILE

SYSTEM = platform.system()

OVERRIDES = {
    "nvidia": """\
# scripts/setup_local_llm.py가 생성 (NVIDIA → CUDA). 호스트별 파일이라 git에서 제외된다.
services:
  ollama:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
""",
    "dxg": """\
# scripts/setup_local_llm.py가 생성 (Windows Docker Desktop/WSL2 → Vulkan on D3D12, Mesa Dozen).
# 호스트별 파일이라 git에서 제외된다.
services:
  ollama:
    devices:
      - /dev/dxg:/dev/dxg                 # WSL2가 노출하는 호스트 GPU (DirectX)
    volumes:
      - /usr/lib/wsl:/usr/lib/wsl:ro      # D3D12 사용자 모드 드라이버 (libd3d12, libdxcore)
    environment:
      - OLLAMA_IGPU_ENABLE=1              # Dozen은 내장 GPU로 보고되며 Ollama는 기본으로 iGPU를 제외한다
""",
    "dri": """\
# scripts/setup_local_llm.py가 생성 (Linux Intel/AMD → Vulkan, Mesa ANV/RADV). 호스트별 파일이라 git에서 제외된다.
services:
  ollama:
    devices:
      - /dev/dri:/dev/dri
    environment:
      - OLLAMA_IGPU_ENABLE=1              # 내장 GPU만 있는 경우를 위해 (외장 GPU는 원래 사용됨)
""",
}
ACCEL_LABELS = {"nvidia": "NVIDIA CUDA", "dxg": "Vulkan (WSL2 D3D12 / Dozen)",
                "dri": "Vulkan (/dev/dri)", "cpu": "CPU"}


def run(cmd: list[str], timeout: int = 60, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore",
                          timeout=timeout, **kw)


# ────────────────────────── 감지 ──────────────────────────
def detect_host_gpus() -> list[dict]:
    """호스트 OS가 보고하는 GPU 목록 [{vendor, name}]."""
    gpus: list[dict] = []
    try:
        out = run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=10)
        gpus += [{"vendor": "NVIDIA", "name": n.strip()} for n in out.stdout.splitlines() if n.strip()]
    except Exception:
        pass
    names: list[str] = []
    try:
        if SYSTEM == "Windows":
            names = run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                        timeout=20).stdout.splitlines()
        elif SYSTEM == "Linux":
            names = [ln.split(": ", 1)[-1] for ln in run(["lspci"], timeout=10).stdout.splitlines()
                     if "VGA" in ln or "3D controller" in ln or "Display" in ln]
    except Exception:
        pass
    for n in (x.strip() for x in names if x.strip()):
        low = n.lower()
        vendor = ("NVIDIA" if "nvidia" in low else "AMD" if ("amd" in low or "radeon" in low)
                  else "Intel" if "intel" in low else "기타")
        if not (vendor == "NVIDIA" and any(g["vendor"] == "NVIDIA" for g in gpus)):
            gpus.append({"vendor": vendor, "name": n})
    return gpus


def docker_platform() -> str:
    """'wsl2'(Windows Docker Desktop) / 'linux' / 'mac' / 'none'."""
    try:
        info = run(["docker", "info", "--format", "{{.OperatingSystem}}|{{.KernelVersion}}"], timeout=20)
    except Exception:
        return "none"
    if info.returncode != 0:
        return "none"
    if "WSL2" in info.stdout or (SYSTEM == "Windows" and "Docker Desktop" in info.stdout):
        return "wsl2"
    return "mac" if SYSTEM == "Darwin" else "linux"


def choose_accel(gpus: list[dict], dplat: str) -> str:
    if any(g["vendor"] == "NVIDIA" for g in gpus) and dplat in ("wsl2", "linux"):
        return "nvidia"
    if dplat == "wsl2" and gpus:
        return "dxg"
    if dplat == "linux" and Path("/dev/dri").exists():
        return "dri"
    return "cpu"   # macOS Docker는 GPU(Metal)를 컨테이너에 넘길 수 없다


# ────────────────────────── 적용 ──────────────────────────
def write_override(accel: str, log=print) -> None:
    if accel == "cpu":
        if OVERRIDE_FILE.exists():
            OVERRIDE_FILE.unlink()
        log("  ✓ GPU 설정 없음 (docker-compose.override.yml 제거)")
    else:
        OVERRIDE_FILE.write_text(OVERRIDES[accel], encoding="utf-8")
        log(f"  ✓ {OVERRIDE_FILE.name} 생성 ({ACCEL_LABELS[accel]})")
