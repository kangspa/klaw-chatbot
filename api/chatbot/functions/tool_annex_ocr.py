"""
MCP 도구 응답의 이미지 기반 별표(PDF) OCR 후처리.

korean-law-mcp 도구(get_annexes 등)는 이미지 기반 PDF 별표를 만나면 텍스트 추출을
포기하고 "이미지 기반 PDF입니다 ... 다운로드 링크: <URL>" 같은 안내문만 반환한다
(별표에 담긴 표시·기재 요건이 이런 파일로 존재할 수 있다). 일부 응답에는 원본 HTML
조각(<img src="...">)이 텍스트에 그대로 섞여 나오기도 한다.

이 모듈은 MCP 서버를 건드리지 않고 클라이언트 쪽에서 그런 파일/이미지 URL을 찾아
내려받고(PDF는 페이지를 PNG로 렌더링), PaddleOCR로 텍스트를 추출해 도구 응답 뒤에
덧붙인다. LLM이 이미지 속 조문·표 내용을 실제로 읽고 답변 근거로 쓸 수 있게 된다.

- 실행 디바이스는 런타임에 동적으로 결정한다: 설치된 paddlepaddle이 CUDA 지원 빌드이고
  GPU가 보이면 GPU, 아니면 CPU. (GPU 서버에서는 paddlepaddle-gpu만 설치하면 코드 수정
  없이 GPU로 동작한다)
- OCR 엔진은 첫 사용 시 1회만 생성한다(모델 다운로드 포함 — 이후는 로컬 캐시).
- 다운로드·렌더링·OCR 어느 단계가 실패해도 원본 텍스트를 그대로 반환한다
  (후처리 때문에 도구 자체가 깨지면 안 된다). 도구가 던진 예외는 그대로 전파해
  ToolNode의 에러 처리(재검색 루프)가 기존대로 동작하게 둔다.
- ANNEX_OCR_ENABLED=0 으로 끌 수 있다 (기본 켜짐).
"""

from __future__ import annotations

import asyncio
import base64  # noqa: F401  (향후 멀티모달 첨부 전환 대비)
import os
import re
import tempfile
import threading
from typing import Any, List, Optional
from urllib.parse import urlparse

import requests
from langchain_core.tools import StructuredTool

# 텍스트 안에서 다운로드 가능한 파일 URL(PDF/이미지) 추출.
# get_annexes 가 "다운로드 링크: <URL>" / "파일 링크: <URL>" 형태로 붙여준다.
_FILE_URL_RE = re.compile(r"https?://[^\s\"'<>]+\.(?:pdf|png|jpe?g)", re.IGNORECASE)

# 응답 텍스트에 원본 HTML <img> 조각이 섞여 나오는 경우의 src 추출.
_IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

# "이미지 기반 PDF입니다" 같은 안내가 있을 때만 파일 다운로드를 시도한다
# (모든 응답 속 pdf 링크를 무조건 내려받으면 불필요한 네트워크 호출이 늘어난다).
_IMAGE_HINT_RE = re.compile(r"이미지\s*기반")

DOWNLOAD_TIMEOUT = 20
MAX_FILE_BYTES = 20 * 1024 * 1024   # 20MB — 별표 파일치고 비정상적으로 크면 스킵
MAX_PDF_PAGES = 5                    # 별표 1건이 5페이지를 넘기면 나머지는 버림(토큰 보호)
PDF_RENDER_DPI = 200                 # OCR 인식률 확보 (첨부 표시용보다 높게)
MAX_OCR_CHARS = 4000                 # 응답에 덧붙일 OCR 텍스트 상한(토큰 보호)
_UA = "Mozilla/5.0 (klaw-chatbot annex OCR fetcher)"

# OCR 엔진 lazy 싱글턴 (모델 로드가 무겁고, 프로세스당 1개면 충분)
_ocr_engine: Any = None
_ocr_device: Optional[str] = None
_ocr_init_failed = False
_ocr_lock = threading.Lock()


def is_enabled() -> bool:
    return os.getenv("ANNEX_OCR_ENABLED", "1").strip() not in ("0", "false", "False")


def detect_device() -> str:
    """
    실행 디바이스 동적 결정.
    paddlepaddle이 CUDA 지원 빌드이고 실제 GPU가 보이면 'gpu:0', 아니면 'cpu'.
    """
    try:
        import paddle
        if paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
            return "gpu:0"
    except Exception as e:
        print(f"[annex-ocr] GPU 감지 실패(무시, CPU 사용): {type(e).__name__}: {e}")
    return "cpu"


def _get_ocr():
    """
    PaddleOCR 엔진 lazy 생성 (스레드 안전).
    최초 1회는 모델 다운로드가 있을 수 있어 수 초~수십 초 걸릴 수 있다.
    생성 실패 시 이후 호출은 즉시 None (매번 재시도하며 지연을 만들지 않도록).
    """
    global _ocr_engine, _ocr_device, _ocr_init_failed
    if _ocr_engine is not None or _ocr_init_failed:
        return _ocr_engine

    with _ocr_lock:
        if _ocr_engine is not None or _ocr_init_failed:
            return _ocr_engine
        try:
            from paddleocr import PaddleOCR
            _ocr_device = detect_device()
            print(f"[annex-ocr] PaddleOCR 초기화 (lang=korean, device={_ocr_device}) ...")
            _ocr_engine = PaddleOCR(
                lang="korean",
                device=_ocr_device,
                # 별표는 정방향 스캔 문서라 방향 보정 모듈은 꺼서 초기화·추론 비용 절약
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                # Windows CPU에서 paddle 3.3.x의 oneDNN(PIR) 변환 버그로 추론이
                # NotImplementedError(ConvertPirAttribute2RuntimeAttribute)로 죽는다.
                # CPU 경로에서는 MKLDNN을 꺼서 회피한다 (GPU 경로에는 영향 없음).
                enable_mkldnn=False,
            )
            print(f"[annex-ocr] PaddleOCR 준비 완료 (device={_ocr_device})")
        except Exception as e:
            _ocr_init_failed = True
            print(f"[annex-ocr] PaddleOCR 초기화 실패 — OCR 후처리 비활성화: {type(e).__name__}: {e}")
    return _ocr_engine


def _extract_image_urls(text: str) -> List[str]:
    urls: List[str] = []
    if _IMAGE_HINT_RE.search(text):
        urls.extend(_FILE_URL_RE.findall(text))
    urls.extend(m for m in _IMG_TAG_RE.findall(text) if m.startswith("http"))
    # 순서 유지 dedup
    seen: set[str] = set()
    deduped: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def _download(url: str, dst_path: str) -> bool:
    try:
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT,
                            headers={"User-Agent": _UA}, stream=True)
        resp.raise_for_status()
        size = 0
        with open(dst_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    return False
                f.write(chunk)
        return size > 0
    except requests.RequestException:
        return False


def _pdf_to_png_files(pdf_path: str, out_dir: str) -> List[str]:
    import fitz  # pymupdf

    paths: List[str] = []
    with fitz.open(pdf_path) as doc:
        for i, page in enumerate(doc[:MAX_PDF_PAGES]):
            pix = page.get_pixmap(dpi=PDF_RENDER_DPI)
            p = os.path.join(out_dir, f"page{i}.png")
            pix.save(p)
            paths.append(p)
    return paths


def _ocr_image_files(paths: List[str]) -> List[str]:
    """이미지 파일들을 OCR해 페이지별 텍스트 목록을 반환. 엔진이 없으면 빈 리스트."""
    ocr = _get_ocr()
    if ocr is None:
        return []
    texts: List[str] = []
    for p in paths:
        try:
            results = ocr.predict(input=p)
            lines: List[str] = []
            for r in results or []:
                lines.extend(t for t in (r.get("rec_texts") or []) if t)
            if lines:
                texts.append("\n".join(lines))
        except Exception as e:
            print(f"[annex-ocr] OCR 실패(스킵): {os.path.basename(p)}: {type(e).__name__}: {e}")
    return texts


def _fetch_and_ocr(url: str) -> List[str]:
    """URL 하나를 내려받아 OCR 텍스트 목록으로. 실패는 빈 리스트 (조용히)."""
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext not in (".pdf", ".png", ".jpg", ".jpeg"):
        return []  # HWP/HWPX 등은 렌더링 방법이 없어 스킵

    with tempfile.TemporaryDirectory(prefix="klaw_annex_") as tmp_dir:
        src = os.path.join(tmp_dir, f"annex{ext}")
        if not _download(url, src):
            return []
        try:
            if ext == ".pdf":
                pngs = _pdf_to_png_files(src, tmp_dir)
            else:
                pngs = [src]
            return _ocr_image_files(pngs)
        except Exception:
            return []


def _append_ocr_text_sync(text: str) -> str:
    """(블로킹) 텍스트에서 이미지 URL을 찾아 OCR 결과를 덧붙인다. 없으면 원본 그대로."""
    urls = _extract_image_urls(text)
    if not urls:
        return text

    chunks: List[str] = []
    for url in urls[:3]:  # 응답 하나에 링크가 여럿이어도 3건까지만 (지연·토큰 보호)
        page_texts = _fetch_and_ocr(url)
        if page_texts:
            chunks.append(f"◾ {url}\n" + "\n---\n".join(page_texts))

    if not chunks:
        return text

    ocr_block = "\n\n".join(chunks)
    if len(ocr_block) > MAX_OCR_CHARS:
        ocr_block = ocr_block[:MAX_OCR_CHARS] + "\n…(이하 생략)"

    device = _ocr_device or "cpu"
    return (
        f"{text}\n\n"
        f"[별표 이미지 OCR 추출 텍스트 — PaddleOCR({device}), 스캔 문서 특성상 오탈자 가능]\n"
        f"{ocr_block}"
    )


async def _append_ocr_text(text: str) -> str:
    """다운로드·렌더링·OCR은 전부 블로킹이므로 스레드에서 수행 (이벤트 루프 보호)."""
    # URL이 없으면 스레드 진입 비용조차 아낀다
    if not _extract_image_urls(text):
        return text
    try:
        return await asyncio.to_thread(_append_ocr_text_sync, text)
    except Exception as e:
        print(f"[annex-ocr] 후처리 실패(원본 유지): {type(e).__name__}: {e}")
        return text


def wrap_tool_with_annex_ocr(tool: StructuredTool) -> StructuredTool:
    """MCP 도구를 감싸, 응답 속 이미지/PDF 링크를 OCR 텍스트로 보강해 반환한다."""

    async def _run(**kwargs):
        result = await tool.ainvoke(kwargs)   # 도구 예외는 그대로 전파 (재검색 루프 유지)
        if isinstance(result, str):
            return await _append_ocr_text(result)
        return result

    return StructuredTool.from_function(
        coroutine=_run,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


def wrap_tools_with_annex_ocr(tools: List[StructuredTool]) -> List[StructuredTool]:
    """비활성화(ANNEX_OCR_ENABLED=0) 시 원본 그대로 반환."""
    if not is_enabled():
        print("[annex-ocr] ANNEX_OCR_ENABLED=0 — OCR 후처리 비활성화")
        return tools
    return [wrap_tool_with_annex_ocr(t) for t in tools]
