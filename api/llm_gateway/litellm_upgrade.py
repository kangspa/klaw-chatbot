"""
LiteLLM 버전 조회·호환성 확인 (마이 페이지 '런타임 버전').

LiteLLM은 백엔드가 직접 쓰는 패키지라 버전을 바꾸려면 서버를 내리고 설치한 뒤 다시 띄워야 한다
(실제 적용은 scripts/apply_litellm_update.py). 그 전에 바꿔도 되는 버전인지 별도 가상환경에서 확인한다:
  uv run --isolated --no-project --with litellm==X --with mcp==<현재> --with langchain-litellm==<현재>
      python scripts/check_litellm_compat.py <등록 모델들>
mcp·langchain-litellm을 지금 설치된 버전으로 묶는 이유: 1.106+처럼 이 프로젝트의 다른 의존성과 맞지 않는
버전(mcp 2.3 필요)을 걸러내기 위해서다.

확인 결과는 .runtime/litellm_compat.json에 캐시한다 (버전마다 결과가 바뀌지 않으므로).
"""
import asyncio
import importlib.metadata as md
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
COMPAT_CACHE = RUNTIME_DIR / "litellm_compat.json"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
PYPI_URL = "https://pypi.org/pypi/litellm/json"


def installed_version() -> str:
    return md.version("litellm")


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


# ────────────────────────── PyPI 릴리스 ──────────────────────────
_release_cache: dict = {"at": 0.0, "data": None}


def list_releases(limit: int = 30) -> list[dict]:
    """정식 릴리스 [{version, publishedAt}] 최신순 (yanked 제외). 10분 캐시."""
    if _release_cache["data"] is None or time.time() - _release_cache["at"] > 600:
        req = urllib.request.Request(PYPI_URL, headers={"User-Agent": "klaw-chatbot"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
        releases = []
        for version, files in data.get("releases", {}).items():
            if not VERSION_RE.match(version) or not files or all(f.get("yanked") for f in files):
                continue
            releases.append({"version": version, "publishedAt": min(f["upload_time"][:10] for f in files)})
        releases.sort(key=lambda r: version_key(r["version"]), reverse=True)
        _release_cache.update(at=time.time(), data=releases)
    return _release_cache["data"][:limit]


# ────────────────────────── 호환성 확인 ──────────────────────────
def _load_cache() -> dict:
    try:
        return json.loads(COMPAT_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)
    COMPAT_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def cached_compat(version: str) -> Optional[dict]:
    """이미 확인한 결과 {ok, reason, checkedAt}. 지금 설치된 버전은 동작 중이므로 호환으로 본다."""
    if version == installed_version():
        return {"ok": True, "reason": "현재 설치된 버전", "checkedAt": None}
    entry = _load_cache().get(version)
    # 함께 묶는 의존성 버전이 바뀌면 결과도 달라질 수 있으므로 같은 조합일 때만 재사용한다
    return entry if entry and entry.get("deps") == _pinned_deps() else None


def _pinned_deps() -> dict:
    return {"mcp": md.version("mcp"), "langchain-litellm": md.version("langchain-litellm")}


def check_compat(version: str, model_ids: list[str], timeout: int = 600) -> dict:
    """
    별도 가상환경에서 해당 LiteLLM 버전을 확인한다 (블로킹, 처음엔 패키지 다운로드로 수십 초).
    반환: {ok, reason, checkedAt, deps}
    """
    if not VERSION_RE.match(version):
        raise ValueError(f"버전 형식이 아닙니다: {version}")
    cached = cached_compat(version)
    if cached is not None:
        return cached
    uv = shutil.which("uv")
    if uv is None:
        return {"ok": False, "reason": "uv 명령을 찾을 수 없어 확인할 수 없습니다.", "checkedAt": time.time()}
    deps = _pinned_deps()
    cmd = [uv, "run", "--isolated", "--no-project", "--python", sys.executable,
           "--with", f"litellm=={version}", "--with", f"mcp=={deps['mcp']}",
           "--with", f"langchain-litellm=={deps['langchain-litellm']}",
           "python", str(PROJECT_ROOT / "scripts" / "check_litellm_compat.py"), *model_ids]
    try:
        out = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"{timeout}초 안에 확인을 끝내지 못했습니다.", "checkedAt": time.time()}
    line = next((l for l in reversed(out.stdout.splitlines()) if l.startswith("COMPAT_RESULT:")), None)
    if line is None:
        # 의존성 해석 실패 등 — uv 오류 끝부분을 이유로 남긴다
        tail = (out.stderr or out.stdout).strip().splitlines()[-3:]
        result = {"ok": False, "reason": "설치할 수 없는 조합입니다: " + " / ".join(tail)[-300:]}
    else:
        data = json.loads(line[len("COMPAT_RESULT:"):])
        errors = data.get("errors") or []
        if data.get("missingPrices"):
            errors.append("가격표에 없는 등록 모델: " + ", ".join(data["missingPrices"]))
        result = {"ok": bool(data.get("ok")), "reason": "호환" if data.get("ok") else "; ".join(errors)[:400]}
    result.update(checkedAt=time.time(), deps=deps)
    cache = _load_cache()
    cache[version] = result
    _save_cache(cache)
    return result


def candidate_versions(releases: list[dict], current: str, max_checks: int = 6) -> list[str]:
    """
    '최신 호환 버전'을 찾을 때 확인할 후보 (현재보다 새 버전, 최신순).
    가장 최신 버전과, 그보다 낮은 각 마이너 라인의 마지막 패치만 본다 (패치 사이에는 호환성이 잘 안 바뀐다).
    """
    newer = [r["version"] for r in releases if version_key(r["version"]) > version_key(current)]
    picked, seen_minor = [], set()
    for v in newer:
        minor = v.rsplit(".", 1)[0]
        if minor in seen_minor:
            continue
        seen_minor.add(minor)
        picked.append(v)
        if len(picked) >= max_checks:
            break
    return picked


class CompatScanner:
    """현재보다 새 버전 중 호환되는 최신 버전을 백그라운드로 찾는다 (마이 페이지를 열 때 한 번)."""

    def __init__(self) -> None:
        self.task: Optional[asyncio.Task] = None
        self.result: Optional[str] = None      # 찾은 최신 호환 버전 (없으면 None)
        self.checked: list[str] = []
        self.finished_at: Optional[float] = None

    @property
    def scanning(self) -> bool:
        return self.task is not None and not self.task.done()

    def ensure_started(self, model_ids: list[str]) -> None:
        # 결과가 1시간 이내면 다시 찾지 않는다 (확인 결과 자체는 파일 캐시라 다시 찾아도 빠르다)
        if self.scanning or (self.finished_at and time.time() - self.finished_at < 3600):
            return
        self.task = asyncio.get_running_loop().create_task(self._scan(model_ids))

    async def _scan(self, model_ids: list[str]) -> None:
        try:
            releases = await asyncio.to_thread(list_releases, 200)
            self.result, self.checked = None, []
            for version in candidate_versions(releases, installed_version()):
                self.checked.append(version)
                res = await asyncio.to_thread(check_compat, version, model_ids)
                if res.get("ok"):
                    # 이 마이너 라인 안에서 더 새 패치가 있으면 그게 최신 호환이다 (후보는 라인별 최신 패치)
                    self.result = version
                    break
        except Exception as e:
            print(f"[litellm-upgrade] 호환 버전 탐색 실패: {type(e).__name__}: {e}")
        finally:
            self.finished_at = time.time()


SCANNER = CompatScanner()
