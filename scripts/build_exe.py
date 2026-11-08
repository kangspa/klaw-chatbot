"""
KLaw 데스크톱 exe 빌드.

    uv run python scripts/build_exe.py                 # dist/KLaw.exe (exe 하나, 콘솔 창 없음)
    uv run python scripts/build_exe.py --onedir        # dist/KLaw/KLaw.exe (폴더 — 시작이 빠르다)
    uv run python scripts/build_exe.py --console       # 콘솔 창을 띄우는 확인용 빌드

순서: 화면 빌드(npm run build) → 아이콘(없으면 scripts/make_icons.py) → 서비스 정의 묶음(build/stack_bundle)
     → PyInstaller(klaw.spec)

실행하는 PC에는 Docker Desktop이 있어야 한다 (서비스는 컨테이너로 돈다). 데이터·설정은 %LOCALAPPDATA%\\KLaw.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "build" / "stack_bundle"
SKIP = shutil.ignore_patterns("node_modules", ".git", "dist", "*.log", "settings.yml.new")


def step(msg: str) -> None:
    print(f"\n── {msg}", flush=True)


def run(cmd: list[str], **kw) -> None:
    print("$ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def main() -> int:
    parser = argparse.ArgumentParser(description="KLaw 데스크톱 exe 빌드")
    parser.add_argument("--onedir", action="store_true", help="exe 하나 대신 폴더로 빌드")
    parser.add_argument("--console", action="store_true", help="콘솔 창을 띄우는 확인용 빌드")
    parser.add_argument("--skip-frontend", action="store_true", help="화면 빌드를 건너뛴다 (이미 빌드됨)")
    args = parser.parse_args()
    t0 = time.time()

    if not args.skip_frontend:
        step("화면 빌드 (vite-project/dist)")
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if npm is None:
            print("npm을 찾을 수 없습니다 (Node.js 필요).")
            return 1
        run([npm, "run", "build"], cwd=ROOT / "vite-project")

    if not (ROOT / "assets" / "klaw.ico").exists():
        step("아이콘 만들기")
        run([sys.executable, str(ROOT / "scripts" / "make_icons.py")])

    step(f"서비스 정의 묶기 → {BUNDLE.relative_to(ROOT)}")
    shutil.rmtree(BUNDLE, ignore_errors=True)
    BUNDLE.mkdir(parents=True)
    shutil.copy2(ROOT / "docker-compose.yml", BUNDLE / "docker-compose.yml")
    for name in ("docker", "korean-law-mcp", "config"):
        shutil.copytree(ROOT / name, BUNDLE / name, ignore=SKIP)
    if not (BUNDLE / "korean-law-mcp" / "Dockerfile").exists():
        print("korean-law-mcp 서브모듈이 비어 있습니다: git submodule update --init korean-law-mcp")
        return 1

    step("PyInstaller")
    env = {**os.environ, "KLAW_BUILD_MODE": "onedir" if args.onedir else "onefile",
           "KLAW_BUILD_CONSOLE": "1" if args.console else "0", "KLAW_STACK_BUNDLE": str(BUNDLE)}
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(ROOT / "klaw.spec")], cwd=ROOT, env=env)

    out = ROOT / "dist" / ("KLaw" if args.onedir else "KLaw.exe")
    size = (sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) if out.is_dir() else out.stat().st_size) / 1e6
    print(f"\n완료: {out} ({size:,.0f} MB, {time.time() - t0:.0f}초)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
