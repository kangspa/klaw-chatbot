"""
KLaw 데스크톱 exe 빌드.

    uv run python scripts/build_exe.py                 # dist/KLaw.exe (exe 하나, 콘솔 창 없음)
    uv run python scripts/build_exe.py --onedir        # dist/KLaw/KLaw.exe (폴더 — 시작이 빠르다)
    uv run python scripts/build_exe.py --console       # 콘솔 창을 띄우는 확인용 빌드

순서: 화면 빌드(npm run build) → 아이콘(없으면 scripts/make_icons.py) → node.exe(build/node, 받아 둔 것 재사용)
     → 법령 MCP 빌드(build/law_mcp: 컴파일된 JS + 운영용 node_modules) → PyInstaller(klaw.spec)

실행하는 PC에는 아무것도 설치할 필요가 없다 (Docker·Node·Python 불필요). 데이터·설정은 %LOCALAPPDATA%\\KLaw 한 곳.
"""
import argparse
import hashlib
import io
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

ROOT = Path(__file__).resolve().parent.parent
NODE_DIR = ROOT / "build" / "node"
LAW_MCP_DIR = ROOT / "build" / "law_mcp"
NODE_MAJOR = "22"   # korean-law-mcp Dockerfile이 쓰던 Node 22 LTS 계열의 최신 버전을 받는다
LAW_MCP_SOURCES = ["package.json", "package-lock.json", "tsconfig.json", "src", "scripts"]


def step(msg: str) -> None:
    print(f"\n── {msg}", flush=True)


def run(cmd: list[str], **kw) -> None:
    print("$ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def fetch_node(_dir: Path) -> None:
    """nodejs.org 배포본에서 node.exe만 꺼낸다 (SHA-256 확인). 이미 있으면 그대로 쓴다."""
    exe = NODE_DIR / "node.exe"
    if exe.exists():
        print(f"재사용: {exe}")
        return
    index = json.load(urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=30))
    version = next(r["version"] for r in index if r["version"].startswith(f"v{NODE_MAJOR}.") and r.get("lts"))
    name = f"node-{version}-win-x64.zip"
    sums = urllib.request.urlopen(f"https://nodejs.org/dist/{version}/SHASUMS256.txt", timeout=30).read().decode()
    expected = next(line.split()[0] for line in sums.splitlines() if line.endswith(" " + name))
    print(f"받는 중: {name}")
    data = urllib.request.urlopen(f"https://nodejs.org/dist/{version}/{name}", timeout=300).read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError(f"{name}의 SHA-256이 맞지 않습니다")
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        exe.write_bytes(z.read(f"node-{version}-win-x64/node.exe"))
    (NODE_DIR / "VERSION").write_text(version, encoding="utf-8")
    print(f"node.exe {version} ({exe.stat().st_size / 1e6:.0f} MB)")


def build_law_mcp(npm: str) -> None:
    """korean-law-mcp를 build/law_mcp에서 컴파일하고 운영용 의존성만 남긴다 (서브모듈 폴더는 건드리지 않는다)."""
    src = ROOT / "korean-law-mcp"
    if not (src / "package.json").exists():
        raise RuntimeError("korean-law-mcp 서브모듈이 비어 있습니다: git submodule update --init korean-law-mcp")
    shutil.rmtree(LAW_MCP_DIR, ignore_errors=True)
    LAW_MCP_DIR.mkdir(parents=True)
    for name in LAW_MCP_SOURCES:
        (shutil.copytree if (src / name).is_dir() else shutil.copy2)(src / name, LAW_MCP_DIR / name)
    run([npm, "ci", "--omit=optional", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=LAW_MCP_DIR)
    run([npm, "run", "build"], cwd=LAW_MCP_DIR)
    run([npm, "prune", "--omit=dev", "--omit=optional", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=LAW_MCP_DIR)
    for name in ("src", "scripts", "tsconfig.json", "package-lock.json"):
        p = LAW_MCP_DIR / name
        shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
    size = sum(f.stat().st_size for f in LAW_MCP_DIR.rglob("*") if f.is_file()) / 1e6
    print(f"법령 MCP: {LAW_MCP_DIR} ({size:.0f} MB)")


VERSION_INFO = ROOT / "build" / "version_info.txt"


def app_version() -> str:
    """api/version.py의 APP_VERSION (앱 전체를 import하지 않고 읽는다)."""
    m = re.search(r'^APP_VERSION = "([^"]+)"', (ROOT / "api" / "version.py").read_text(encoding="utf-8"), re.M)
    if not m:
        raise RuntimeError("api/version.py에서 APP_VERSION을 찾을 수 없습니다")
    return m.group(1)


def write_version_info(version: str) -> Path:
    """exe 파일 속성(자세히 탭)에 보일 버전 정보 — PyInstaller 버전 파일 형식."""
    nums = (tuple(int(x) for x in re.findall(r"\d+", version)[:4]) + (0, 0, 0, 0))[:4]
    VERSION_INFO.parent.mkdir(parents=True, exist_ok=True)
    VERSION_INFO.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041204B0', [
      StringStruct('ProductName', 'KLaw'),
      StringStruct('FileDescription', 'KLaw - 국내법 상담 챗봇'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('ProductVersion', '{version}'),
      StringStruct('InternalName', 'KLaw'),
      StringStruct('OriginalFilename', 'KLaw.exe')])]),
    VarFileInfo([VarStruct('Translation', [0x0412, 1200])])
  ]
)
""", encoding="utf-8")
    return VERSION_INFO


DEFAULTS_FILE = ROOT / "api" / "app_defaults.json"   # 내장 공개 설정값 (exe에 그대로 들어간다)
CLIENT_ID_RE = re.compile(r"^\d+-[a-z0-9]+\.apps\.googleusercontent\.com$")


def check_app_defaults() -> None:
    """내장 공개 설정값 확인. GOOGLE_CLIENT_ID가 없거나 형식이 틀리면 Google 연결만 꺼진 exe가 되므로 알린다."""
    cid = str(json.loads(DEFAULTS_FILE.read_text(encoding="utf-8")).get("GOOGLE_CLIENT_ID") or "")
    if cid and not CLIENT_ID_RE.match(cid):
        raise RuntimeError(f"{DEFAULTS_FILE.name}의 GOOGLE_CLIENT_ID 형식이 아닙니다 (<숫자>-<문자>.apps.googleusercontent.com).")
    print(f"GOOGLE_CLIENT_ID 내장 (프로젝트 번호 {cid.split('-')[0]})" if cid else "GOOGLE_CLIENT_ID 없음 — Google 연결이 꺼진 exe가 됩니다")


def main() -> int:
    parser = argparse.ArgumentParser(description="KLaw 데스크톱 exe 빌드")
    parser.add_argument("--onedir", action="store_true", help="exe 하나 대신 폴더로 빌드")
    parser.add_argument("--console", action="store_true", help="콘솔 창을 띄우는 확인용 빌드")
    parser.add_argument("--skip-frontend", action="store_true", help="화면 빌드를 건너뛴다 (이미 빌드됨)")
    parser.add_argument("--distpath", default=str(ROOT / "dist"),
                        help="exe를 둘 폴더 (기본 dist — 실행 중인 KLaw.exe가 있어 덮어쓸 수 없을 때 다른 폴더로)")
    args = parser.parse_args()
    t0 = time.time()

    if not args.skip_frontend:
        step("화면 빌드 (vite-project/dist)")
        npm_ = shutil.which("npm") or shutil.which("npm.cmd")
        if npm_ is None:
            print("npm을 찾을 수 없습니다 (Node.js 필요 — 빌드하는 PC에만).")
            return 1
        run([npm_, "run", "build"], cwd=ROOT / "vite-project")

    if not (ROOT / "assets" / "klaw.ico").exists():
        step("아이콘 만들기")
        run([sys.executable, str(ROOT / "scripts" / "make_icons.py")])

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        print("npm을 찾을 수 없습니다 (Node.js 필요 — 빌드하는 PC에만).")
        return 1
    step("node.exe (exe에 넣을 Node 런타임)")
    fetch_node(NODE_DIR)
    step("법령 MCP 빌드 → build/law_mcp")
    build_law_mcp(npm)

    step("내장 설정값 (api/app_defaults.json)")
    check_app_defaults()

    version = app_version()
    step(f"PyInstaller (버전 {version})")
    env = {"KLAW_VERSION_FILE": str(write_version_info(version)), **os.environ, "KLAW_BUILD_MODE": "onedir" if args.onedir else "onefile",
           "KLAW_BUILD_CONSOLE": "1" if args.console else "0",
           "KLAW_NODE_BUNDLE": str(NODE_DIR), "KLAW_LAW_MCP_BUNDLE": str(LAW_MCP_DIR)}
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", args.distpath, str(ROOT / "klaw.spec")],
        cwd=ROOT, env=env)

    out = Path(args.distpath) / ("KLaw" if args.onedir else "KLaw.exe")
    size = (sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) if out.is_dir() else out.stat().st_size) / 1e6
    print(f"\n완료: {out} ({size:,.0f} MB, {time.time() - t0:.0f}초)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
