"""
korean-law-mcp 도구 호환성 점검 스크립트.

서브모듈을 새 버전으로 올린 뒤(컨테이너 재빌드 후) 실행하면, 저장해 둔 도구 스냅샷과
지금 서버가 노출하는 도구를 비교해 바뀐 점과 앱이 의존하는 부분이 깨졌는지 알려준다.

앱이 의존하는 것 (바뀌면 api/chatbot/functions 코드 수정 필요):
  - 도구 이름·인자 : search_law(query) — 재검색 강제(agent.force_next_search), 인증 canary(law_auth)
                     legal_research — 호출 한도(agent.MCP_TOOL_LIMIT_OVERRIDES)
  - 응답 표식      : [NOT_FOUND] 등 실패 표식(agent.MCP_FAILURE_LABELS), '재시도 제안:'(agent.extract_retry_suggestions),
                     '이미지 기반' 별표 안내(tool_annex_ocr)
도구 설명 변경은 bind_tools로 모델에 그대로 전달되므로 대개 코드 수정이 필요 없다.

사용법:
    uv run python scripts/check_mcp_tools.py            # 스냅샷과 비교 (호환성 문제 있으면 종료 코드 1)
    uv run python scripts/check_mcp_tools.py --update   # 현재 도구로 스냅샷 갱신 (확인 후 커밋)
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import dotenv  # noqa: E402
from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = Path(__file__).with_name("mcp_tools_snapshot.json")
SUBMODULE = PROJECT_ROOT / "korean-law-mcp"

# 앱 코드가 이름으로 참조하는 도구와, 반드시 있어야 하는 인자
REQUIRED_TOOLS = {"search_law": {"query"}, "legal_research": {"query"}}
# 앱 코드가 문자열로 감지하는 MCP 응답 표식 (서브모듈 소스에 남아 있어야 함)
REQUIRED_MARKERS = ["[NOT_FOUND]", "재시도 제안", "이미지 기반 PDF"]


async def fetch_tools() -> dict:
    dotenv.load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("MCP_AUTH_TOKEN", "")
    client = MultiServerMCPClient({"korean_law": {
        "url": os.getenv("MCP_KOREAN_LAW_URL", "http://localhost:3000/mcp"),
        "transport": "streamable_http",
        "headers": {"Authorization": f"Bearer {token}"},
    }})
    tools = await client.get_tools()
    out = {}
    for t in tools:
        schema = t.args_schema if isinstance(t.args_schema, dict) else t.args_schema.model_json_schema()
        out[t.name] = {
            "description": t.description,
            "properties": schema.get("properties", {}),
            "required": sorted(schema.get("required", [])),
        }
    return dict(sorted(out.items()))


def submodule_version() -> str:
    try:
        return subprocess.run(["git", "-C", str(SUBMODULE), "describe", "--tags", "--always"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def marker_in_source(marker: str) -> bool:
    for path in (SUBMODULE / "src").rglob("*.ts"):
        if path.name.endswith(".test.ts"):
            continue
        if marker in path.read_text(encoding="utf-8", errors="ignore"):
            return True
    return False


def compare(old: dict, new: dict) -> tuple[list[str], list[str]]:
    """(호환성 문제, 참고용 변경) 목록을 만든다."""
    problems, notes = [], []
    for name in sorted(set(old) - set(new)):
        (problems if name in REQUIRED_TOOLS else notes).append(f"도구 삭제: {name}")
    for name in sorted(set(new) - set(old)):
        notes.append(f"도구 추가: {name}")
    for name in sorted(set(old) & set(new)):
        o, n = old[name], new[name]
        for p in sorted(set(o["properties"]) - set(n["properties"])):
            notes.append(f"{name}: 인자 삭제 '{p}'")
        for p in sorted(set(n["properties"]) - set(o["properties"])):
            notes.append(f"{name}: 인자 추가 '{p}'" + (" (필수)" if p in n["required"] else ""))
        newly_required = sorted(set(n["required"]) - set(o["required"]))
        if newly_required:
            notes.append(f"{name}: 새 필수 인자 {newly_required}")
        for p in sorted(set(o["properties"]) & set(n["properties"])):
            if json.dumps(o["properties"][p], sort_keys=True) != json.dumps(n["properties"][p], sort_keys=True):
                notes.append(f"{name}: 인자 '{p}' 스키마·설명 변경")
        if o["description"] != n["description"]:
            notes.append(f"{name}: 설명 변경 ({len(o['description'])}자 → {len(n['description'])}자)")

    for name, params in REQUIRED_TOOLS.items():
        if name not in new:
            problems.append(f"앱이 참조하는 도구 없음: {name}")
        elif not params <= set(new[name]["properties"]):
            problems.append(f"{name}: 앱이 쓰는 인자 {sorted(params)} 중 누락 → {sorted(new[name]['properties'])}")
    for marker in REQUIRED_MARKERS:
        if not marker_in_source(marker):
            problems.append(f"응답 표식 '{marker}'이(가) 서브모듈 소스에서 사라짐 — 감지 코드 확인 필요")
    return problems, notes


async def main() -> int:
    parser = argparse.ArgumentParser(description="korean-law-mcp 도구 호환성 점검")
    parser.add_argument("--update", action="store_true", help="현재 도구로 스냅샷 갱신")
    args = parser.parse_args()

    try:
        tools = await fetch_tools()
    except Exception as e:
        print(f"MCP 서버에 연결하지 못했습니다: {type(e).__name__}: {e}")
        print("docker compose up -d --build korean-law-mcp 후 다시 실행하세요.")
        return 2

    version = submodule_version()
    print(f"korean-law-mcp {version} — 도구 {len(tools)}개: {', '.join(tools)}")

    if SNAPSHOT.exists():
        snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        print(f"스냅샷 기준 버전: {snap.get('version')}\n")
        problems, notes = compare(snap["tools"], tools)
        for n in notes:
            print(f"  · {n}")
        if not notes:
            print("  · 도구 변경 없음")
        for p in problems:
            print(f"  ✗ {p}")
        print("\n호환성 문제 없음" if not problems else f"\n호환성 문제 {len(problems)}건 — 앱 코드 수정 필요")
    else:
        problems = []
        print("스냅샷이 없어 비교를 건너뜁니다 (--update로 생성).")

    if args.update:
        SNAPSHOT.write_text(json.dumps({"version": version, "tools": tools}, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        print(f"스냅샷 갱신: {SNAPSHOT.relative_to(PROJECT_ROOT)} ({version})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
