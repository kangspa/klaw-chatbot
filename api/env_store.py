"""
루트 .env 읽기·쓰기 (개인 PC에서 띄워 쓰는 서비스라 설정을 이 파일 하나에 둔다).

쓸 때는 해당 키의 줄만 바꾸고(없으면 끝에 추가) 나머지 줄·주석·순서는 그대로 둔다.
바꾼 값은 이 프로세스의 os.environ에도 반영해 재시작 없이 쓰이게 한다 (언제 읽히는지는 호출부 책임).
사용처: Ollama 버전(ollama_upgrade), API 키 관리(api/chatbot/keys.py)
"""
import os
import re
import threading
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_lock = threading.Lock()


def read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            m = _LINE.match(line)
            if m and not line.lstrip().startswith("#"):
                values[m.group(1)] = m.group(2).strip('"').strip("'")
    return values


def write_env_values(updates: dict[str, str], comment: str = "") -> None:
    """키 값을 바꾸거나 추가한다. 새로 추가하는 키는 comment(있으면)를 위에 달아 끝에 붙인다."""
    for k, v in updates.items():
        if "\n" in v or "\r" in v:
            raise ValueError(f"{k} 값에 줄바꿈을 넣을 수 없습니다.")
    with _lock:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
        pending = dict(updates)
        for i, line in enumerate(lines):
            m = _LINE.match(line)
            if m and not line.lstrip().startswith("#") and m.group(1) in pending:
                lines[i] = f"{m.group(1)}={pending.pop(m.group(1))}"
        if pending:
            if lines and lines[-1].strip():
                lines.append("")
            if comment:
                lines.append(f"# {comment}")
            lines.extend(f"{k}={v}" for k, v in pending.items())
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.update(updates)


def remove_env_keys(keys: list[str]) -> None:
    """키 줄을 지운다 (바로 위의 주석은 남긴다). os.environ에서도 뺀다."""
    with _lock:
        if ENV_FILE.exists():
            kept = []
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                m = _LINE.match(line)
                if m and not line.lstrip().startswith("#") and m.group(1) in keys:
                    continue
                kept.append(line)
            ENV_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
    for k in keys:
        os.environ.pop(k, None)
