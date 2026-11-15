# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 빌드 설정 — scripts/build_exe.py가 부른다 (직접: uv run pyinstaller klaw.spec).

  KLAW_BUILD_MODE     onefile(기본: exe 하나) | onedir(폴더 — 시작이 빠르고 디버깅이 쉽다)
  KLAW_BUILD_CONSOLE  1이면 콘솔 창을 띄운다 (빌드 확인용). 기본은 창 없음
  KLAW_NODE_BUNDLE    node.exe가 든 폴더 (build_exe.py가 nodejs.org 배포본에서 꺼내 둔다)
  KLAW_LAW_MCP_BUNDLE 빌드된 korean-law-mcp (build/·운영용 node_modules·package.json)

exe에 들어가는 것: 실행기(launcher.py) + 백엔드(main·api) + 파이썬 의존성 + 빌드된 화면(frontend) + node.exe
+ 법령 MCP 서버 + 아이콘. 설치·Docker가 필요 없다: DB는 SQLite, 벡터는 Qdrant 로컬 모드, 웹 검색은 ddgs,
법령 MCP는 자식 프로세스(node.exe). Ollama 엔진(1.6GB)은 넣지 않고 로컬 모델을 쓸 때 마이 페이지에서 받는다.
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPECPATH)
MODE = os.getenv("KLAW_BUILD_MODE", "onefile")
CONSOLE = os.getenv("KLAW_BUILD_CONSOLE") == "1"
NODE = Path(os.getenv("KLAW_NODE_BUNDLE", ROOT / "build" / "node"))
LAW_MCP = Path(os.getenv("KLAW_LAW_MCP_BUNDLE", ROOT / "build" / "law_mcp"))

datas = [
    (str(ROOT / "vite-project" / "dist"), "frontend"),
    (str(NODE), "node"),
    (str(LAW_MCP), "law_mcp"),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "api" / "llm_gateway" / "seed_models.json"), "api/llm_gateway"),
    # exe에서 LiteLLM 새 버전을 시험할 때 exe 자신이 실행하는 점검 스크립트 (api/litellm_overlay.py)
    (str(ROOT / "scripts" / "check_litellm_compat.py"), "scripts"),
]
# 내장 공개 설정값 (GOOGLE_CLIENT_ID 등) — api/stack.py app_default()
datas.append((str(ROOT / "api" / "app_defaults.json"), "api"))
# LiteLLM 새 버전이 요구하는 패키지 버전을 exe 안에서 확인할 수 있도록 의존성 메타데이터를 함께 넣는다
for dist in ("litellm", "langchain-litellm", "mcp"):
    datas += copy_metadata(dist, recursive=True)
binaries = []
hiddenimports = ["main", "packaging.requirements", "packaging.specifiers", "packaging.markers"] \
    + collect_submodules("api") + collect_submodules("uvicorn")
# 표준 라이브러리는 통째로 넣는다 — 나중에 받는 LiteLLM 새 버전이 지금 코드가 안 쓰는 표준 모듈(예: graphlib)을
# import해도 exe 안에 있도록 (api/litellm_overlay.py). 몇 MB 늘어날 뿐이다.
_STDLIB_SKIP = {"test", "idlelib", "turtledemo", "turtle", "lib2to3", "ensurepip", "venv", "pydoc_data", "this", "antigravity"}
hiddenimports += [m for m in sys.stdlib_module_names if not m.startswith("_") and m not in _STDLIB_SKIP]

# 데이터 파일·동적 import가 많은 패키지는 통째로 담는다 (가격표·토크나이저·OCR 설정·브라우저 드라이버 등)
for pkg in [
    "litellm", "tiktoken", "tiktoken_ext", "tokenizers",
    "langchain_litellm", "langchain_core", "langchain_community", "langgraph", "langchain_mcp_adapters", "mcp",
    "qdrant_client", "grpc", "portalocker", "aiosqlite", "ddgs", "primp", "lxml",
    "crawl4ai", "playwright", "patchright", "fake_useragent",
    "paddle", "paddleocr", "paddlex", "cv2", "pymupdf", "fitz",
    "pystray", "PIL",
    # 내장 임베딩(대화 기록 검색, api/llm_gateway/embeddings.py) — 모델 파일은 exe에 넣지 않고 처음 쓸 때 data/embeddings로 받는다
    "fastembed", "onnxruntime", "py_rust_stemmers", "mmh3", "huggingface_hub", "hf_xet",
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
