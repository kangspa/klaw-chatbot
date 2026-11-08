# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 빌드 설정 — scripts/build_exe.py가 부른다 (직접: uv run pyinstaller klaw.spec).

  KLAW_BUILD_MODE     onefile(기본: exe 하나) | onedir(폴더 — 시작이 빠르고 디버깅이 쉽다)
  KLAW_BUILD_CONSOLE  1이면 콘솔 창을 띄운다 (빌드 확인용). 기본은 창 없음
  KLAW_STACK_BUNDLE   서비스 정의 묶음 폴더 (build_exe.py가 만든다: compose·Dockerfile·korean-law-mcp 소스·searxng 설정)

exe에 들어가는 것: 실행기(launcher.py) + 백엔드(main·api) + 파이썬 의존성 + 빌드된 화면(frontend) + 서비스 정의(stack)
+ 아이콘. 서비스(Postgres·Qdrant·SearXNG·법령 MCP·Ollama)는 Docker Desktop에서 컨테이너로 돈다.
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH)
MODE = os.getenv("KLAW_BUILD_MODE", "onefile")
CONSOLE = os.getenv("KLAW_BUILD_CONSOLE") == "1"
STACK = Path(os.getenv("KLAW_STACK_BUNDLE", ROOT / "build" / "stack_bundle"))

datas = [
    (str(ROOT / "vite-project" / "dist"), "frontend"),
    (str(STACK), "stack"),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "api" / "llm_gateway" / "seed_models.json"), "api/llm_gateway"),
]
binaries = []
hiddenimports = ["main"] + collect_submodules("api") + collect_submodules("uvicorn")

# 데이터 파일·동적 import가 많은 패키지는 통째로 담는다 (가격표·토크나이저·OCR 설정·브라우저 드라이버 등)
for pkg in [
    "litellm", "tiktoken", "tiktoken_ext", "tokenizers",
    "langchain_litellm", "langchain_core", "langchain_community", "langgraph", "langchain_mcp_adapters", "mcp",
    "qdrant_client", "grpc", "asyncpg",
    "crawl4ai", "playwright", "patchright", "fake_useragent",
    "paddle", "paddleocr", "paddlex", "cv2", "pymupdf", "fitz",
    "pystray", "PIL",
]:
    try:
        d, b, h = collect_all(pkg)
    except Exception as e:  # 설치되지 않은 선택 패키지
        print(f"[spec] {pkg} 건너뜀: {e}")
        continue
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "IPython", "jupyter", "notebook", "matplotlib.tests", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

splash = Splash(
    str(ROOT / "assets" / "splash.png"),
    binaries=a.binaries,
    datas=a.datas,
    text_pos=None,
    always_on_top=False,
) if MODE == "onefile" else None

common = dict(name="KLaw", debug=False, strip=False, upx=False, console=CONSOLE,
              icon=str(ROOT / "assets" / "klaw.ico"), disable_windowed_traceback=False)

if MODE == "onefile":
    exe = EXE(pyz, splash, splash.binaries, a.scripts, a.binaries, a.datas, [],
              runtime_tmpdir=None, **common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="KLaw")
