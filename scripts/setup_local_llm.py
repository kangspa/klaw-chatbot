"""
로컬 LLM(Docker Ollama) 가속 자동 구성 · 모델 받기 · 가속 검증.

호스트를 감지해 Ollama 컨테이너에 넘길 가속 장치를 정하고 docker-compose.override.yml에 적는다.
(docker compose는 이 파일을 자동으로 병합한다. 호스트마다 다르므로 git에서 제외한다.)

  감지 결과                          컨테이너 설정                        Ollama 백엔드
  NVIDIA GPU                         GPU 예약 (nvidia container toolkit)  CUDA
  Windows Docker Desktop(WSL2) + GPU /dev/dxg + /usr/lib/wsl 마운트       Vulkan (Mesa Dozen, D3D12 위)
  Linux + /dev/dri (Intel/AMD)       /dev/dri 전달                        Vulkan (Mesa ANV/RADV)
  가속 장치 없음 / macOS             없음                                 CPU
내장 GPU(iGPU)는 Ollama가 기본으로 제외하므로 NVIDIA가 아닌 경로에서는 OLLAMA_IGPU_ENABLE=1을 켠다.
NVIDIA가 있으면 그쪽을 쓴다 (공유 메모리를 쓰는 iGPU가 외장 GPU 대신 선택되는 것을 막기 위함).

사용법:
    uv run python scripts/setup_local_llm.py                   # 감지 결과·현재 상태만 출력
    uv run python scripts/setup_local_llm.py --apply           # override 생성 + 컨테이너 빌드·재생성 + 가속 확인
    uv run python scripts/setup_local_llm.py --apply --pull    # + DB(또는 seed JSON)의 활성 로컬 모델 받기
    uv run python scripts/setup_local_llm.py --verify          # 가장 가벼운 로컬 모델로 생성해 GPU 사용률 확인
    uv run python scripts/setup_local_llm.py --apply --accel cpu   # 감지 대신 강제 지정 (nvidia|dxg|dri|cpu)
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import types
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
# api/__init__.py(라우터·MCP 초기화)를 거치지 않고 하위 모듈만 쓰기 위해 빈 패키지로 등록
for _name, _path in (("api", "api"), ("api.llm_gateway", "api/llm_gateway")):
    _pkg = types.ModuleType(_name)
    _pkg.__path__ = [str(PROJECT_ROOT / _path)]
    sys.modules[_name] = _pkg

import dotenv  # noqa: E402

from api.llm_gateway import local_runtime as lr  # noqa: E402

dotenv.load_dotenv(PROJECT_ROOT / ".env")
SYSTEM = platform.system()
OVERRIDE = PROJECT_ROOT / "docker-compose.override.yml"

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
def write_override(accel: str) -> None:
    if accel == "cpu":
        if OVERRIDE.exists():
            OVERRIDE.unlink()
        print("  ✓ GPU 설정 없음 (docker-compose.override.yml 제거)")
    else:
        OVERRIDE.write_text(OVERRIDES[accel], encoding="utf-8")
        print(f"  ✓ {OVERRIDE.name} 생성 ({ACCEL_LABELS[accel]})")


def recreate_container() -> bool:
    print("  컨테이너 빌드·재생성 중… (처음이면 이미지 빌드에 수 분 걸림)", flush=True)
    out = run(["docker", "compose", "up", "-d", "--build", "--force-recreate", "ollama"],
              timeout=1800, cwd=PROJECT_ROOT)
    if out.returncode != 0:
        print("  ✗ docker compose 실패:\n" + out.stderr[-1500:])
        return False
    deadline = time.time() + 60
    while time.time() < deadline:
        log = lr.ollama_log_text()
        if lr.ollama_version() and log and "inference compute" in log:
            return True
        time.sleep(2)
    return bool(lr.ollama_version())


# ────────────────────────── 모델 받기 / 검증 ──────────────────────────
def local_model_specs() -> list[dict]:
    """DB의 활성 ollama 모델 (sort_order 순 = 가벼운 모델 먼저). DB에 못 붙으면 seed JSON."""
    try:
        import asyncio
        import asyncpg
        from api.db import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER

        async def fetch():
            conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME,
                                         user=DB_USER, password=DB_PASSWORD, timeout=5)
            try:
                rows = await conn.fetch("SELECT id, options FROM llm_models "
                                        "WHERE enabled AND id LIKE 'ollama/%' ORDER BY sort_order")
                return [{"id": r["id"], "options": json.loads(r["options"]) if isinstance(r["options"], str)
                         else (r["options"] or {})} for r in rows]
            finally:
                await conn.close()
        if SYSTEM == "Windows":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        return asyncio.run(fetch())
    except Exception as e:
        print(f"  (DB 조회 실패 → seed JSON 사용: {type(e).__name__})")
        seed = json.loads((PROJECT_ROOT / "api" / "llm_gateway" / "seed_models.json").read_text(encoding="utf-8"))
        return [{"id": m["id"], "options": m.get("options") or {}} for m in seed["models"]
                if m["id"].startswith("ollama/") and m.get("enabled", True)]


def _post(path: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(f"{lr.ollama_base_url()}{path}", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def pull(name: str) -> None:
    print(f"  ↓ {name} 받는 중… (모델 크기에 따라 수 분)", flush=True)
    r = _post("/api/pull", {"model": name, "stream": False}, timeout=7200)
    print(f"    {r.get('status', r)}")


def verify(spec: dict) -> None:
    name = spec["id"].split("/", 1)[1]
    num_ctx = spec["options"].get("num_ctx", 8192)
    t = time.time()
    r = _post("/api/chat", {"model": name, "stream": False, "options": {"num_ctx": num_ctx},
                            "messages": [{"role": "user", "content": "한 문장으로 자기소개 해줘."}]}, timeout=900)
    with urllib.request.urlopen(f"{lr.ollama_base_url()}/api/ps", timeout=10) as resp:
        ps = json.load(resp)
    loaded = next((m for m in ps.get("models", []) if m["name"].split(":")[0] == name.split(":")[0]), None)
    gpu = f"GPU {100 * loaded['size_vram'] / loaded['size']:.0f}%" if loaded and loaded["size"] else "알 수 없음"
    speed = r["eval_count"] / (r["eval_duration"] / 1e9) if r.get("eval_duration") else 0
    print(f"  ✓ {name}: {gpu}, 생성 {speed:.1f} tok/s, 총 {time.time() - t:.1f}s (컨텍스트 {num_ctx})")


# ────────────────────────── main ──────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="로컬 LLM(Docker Ollama) 가속 자동 구성")
    parser.add_argument("--apply", action="store_true", help="override 생성 후 컨테이너 빌드·재생성")
    parser.add_argument("--pull", action="store_true", help="활성 로컬 모델 중 없는 것을 받기")
    parser.add_argument("--verify", action="store_true", help="가장 가벼운 로컬 모델로 생성해 GPU 사용률 확인")
    parser.add_argument("--accel", choices=["auto", "nvidia", "dxg", "dri", "cpu"], default="auto",
                        help="가속 경로 강제 지정 (기본: 자동 감지)")
    args = parser.parse_args()

    print("[1] 호스트 감지")
    gpus = detect_host_gpus()
    for g in gpus:
        print(f"  - GPU {g['vendor']}: {g['name']}")
    if not gpus:
        print("  - GPU 없음")
    dplat = docker_platform()
    print(f"  - Docker: {dplat}")
    if dplat == "none":
        print("  ✗ Docker를 사용할 수 없습니다. Docker를 실행한 뒤 다시 시도하세요.")
        return 1
    accel = choose_accel(gpus, dplat) if args.accel == "auto" else args.accel
    print(f"  → 가속 경로: {ACCEL_LABELS[accel]}")

    print("\n[2] Ollama 컨테이너")
    if args.apply:
        write_override(accel)
        if not recreate_container():
            return 1
    version = lr.ollama_version()
    if not version:
        print("  ✗ Ollama 컨테이너가 응답하지 않습니다 → --apply 로 실행하세요.")
        return 1
    print(f"  ✓ Ollama {version} ({lr.ollama_base_url()})")
    discovery = lr.read_discovery(lr.ollama_log_text())
    print(f"  가속: {lr.describe_acceleration(discovery)}")
    if accel != "cpu" and not lr.is_accelerated(discovery):
        print(f"  ⚠ {ACCEL_LABELS[accel]} 경로를 설정했지만 Ollama가 GPU를 쓰지 않습니다 (CPU로 동작). "
              "드라이버·Docker GPU 설정을 확인하거나 --accel cpu 로 고정하세요.")

    specs = local_model_specs()
    installed = lr.ollama_models()
    missing = [s for s in specs if s["id"].split("/", 1)[1] not in installed]
    print(f"\n[3] 로컬 모델 (활성 {len(specs)}개, 없음 {len(missing)}개)")
    for s in specs:
        print(f"  {'✗' if s in missing else '✓'} {s['id']}")
    if missing and args.pull:
        for s in missing:
            pull(s["id"].split("/", 1)[1])

    if args.verify:
        print("\n[4] 가속 검증")
        ready = [s for s in specs if s["id"].split("/", 1)[1] in lr.ollama_models()]
        if ready:
            verify(ready[0])
        else:
            print("  받은 로컬 모델이 없습니다 (--pull).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
