"""
API 키 관리 End-point (/chatbot/keys/...) — 사용자가 화면에서 키를 넣고 바꾸고 지우고, 연결을 확인한다.

키는 이 PC의 루트 .env에 저장한다 (개인 PC에서 띄워 쓰는 서비스 — api/env_store.py).
응답에는 키 원문을 절대 담지 않고 앞뒤 몇 글자만 보여준다. 연결 확인은 해당 회사 API에만 키를 보낸다.

키마다 다시 읽히는 시점이 달라서 저장 뒤 처리가 다르다:
  OpenAI·Gemini·Anthropic  LiteLLM이 호출할 때마다 환경 변수를 읽는다 → 바로 적용
  Ollama·Hugging Face      모델 인스턴스를 만들 때 키를 넘긴다(캐시) → 모델 캐시를 비운다
  LAW_OC                   korean-law-mcp 컨테이너가 시작할 때 읽는다 → 컨테이너를 다시 만들고 인증을 확인한다
"""
import asyncio
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth.deps import get_current_user
from api.env_store import read_env_file, remove_env_keys, write_env_values

router = APIRouter(prefix="/keys", dependencies=[Depends(get_current_user)])
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class KeySpec:
    id: str
    label: str
    env: str                       # 저장할 .env 변수
    aliases: tuple[str, ...]       # 같은 키로 인정하는 다른 변수 (예전 이름)
    description: str
    apply: str                     # immediate | reset_models | restart_law
    deletable: bool = True


KEYS: list[KeySpec] = [
    KeySpec("OPENAI", "OpenAI", "OPENAI_API_KEY", (), "OpenAI 모델(GPT) 호출", "immediate"),
    KeySpec("GEMINI", "Google Gemini", "GEMINI_API_KEY", ("GOOGLE_API_KEY",), "Gemini 모델 호출", "immediate"),
    KeySpec("ANTHROPIC", "Anthropic", "ANTHROPIC_API_KEY", (), "Claude 모델 호출", "immediate"),
    KeySpec("HUGGINGFACE", "Hugging Face", "HUGGINGFACEHUB_API_TOKEN", ("HF_TOKEN",),
            "Hugging Face Inference Providers 모델 호출", "reset_models"),
    KeySpec("OLLAMA", "Ollama", "OLLAMA_API_KEY", (), "Ollama 클라우드(ollama.com) 계정 키 — 로컬 Ollama는 키 없이 동작",
            "reset_models"),
    # docker-compose가 LAW_OC 없이는 korean-law-mcp를 띄우지 않으므로(필수 값) 지울 수 없고 바꾸기만 한다
    KeySpec("LAW_OC", "법제처 Open API (LAW_OC)", "LAW_OC", (),
            "법령·판례 검색(korean-law-mcp)의 법제처 인증값. 바꾸면 법령 검색 서버를 다시 시작합니다", "restart_law",
            deletable=False),
]
_BY_ID = {k.id: k for k in KEYS}

# LAW_OC 적용(컨테이너 재생성 + 인증 확인) 진행 상태
_law_apply: dict = {"status": None, "message": "", "at": None}


def _current(spec: KeySpec) -> tuple[Optional[str], Optional[str]]:
    """(값, 실제 변수 이름). .env를 먼저 보고, 없으면 프로세스 환경 변수를 본다."""
    env_file = read_env_file()
    for name in (spec.env, *spec.aliases):
        if env_file.get(name):
            return env_file[name], name
    for name in (spec.env, *spec.aliases):
        if os.getenv(name):
            return os.getenv(name), name
    return None, None


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def _item(spec: KeySpec) -> dict:
    value, var = _current(spec)
    item = {
        "id": spec.id, "label": spec.label, "env": var or spec.env, "description": spec.description,
        "configured": bool(value), "masked": _mask(value) if value else None,
        "deletable": spec.deletable, "apply": spec.apply,
    }
    if spec.id == "LAW_OC":
        item["applyStatus"] = dict(_law_apply)
    return item


class SetKeyRequest(BaseModel):
    id: str
    value: str = Field(..., min_length=1, max_length=500)


class KeyIdRequest(BaseModel):
    id: str


def _spec(key_id: str) -> KeySpec:
    spec = _BY_ID.get(key_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"관리하지 않는 키입니다: {key_id}")
    return spec


def _reset_model_cache() -> None:
    """Ollama·HF 모델 인스턴스는 만들 때 키를 넘기므로, 키가 바뀌면 캐시를 비워 새로 만들게 한다."""
    from api.load_variable import MODEL_MANAGER
    MODEL_MANAGER.cache.clear()


def _restart_law_mcp() -> None:
    """LAW_OC를 새 값으로 쓰도록 korean-law-mcp 컨테이너를 다시 만든 뒤 법제처 인증을 확인한다 (백그라운드)."""
    _law_apply.update(status="running", message="법령 검색 서버를 다시 시작하는 중", at=time.time())

    async def run() -> None:
        try:
            out = await asyncio.to_thread(
                subprocess.run, ["docker", "compose", "up", "-d", "--force-recreate", "korean-law-mcp"],
                cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
                env={**os.environ},
            )
            if out.returncode != 0:
                raise RuntimeError((out.stderr or out.stdout).strip().splitlines()[-1][:200])
            # 컨테이너가 응답할 때까지 기다린 뒤 canary 검색으로 인증 확인
            async with httpx.AsyncClient(timeout=3) as client:
                for _ in range(40):
                    try:
                        if (await client.get("http://localhost:3000/health")).status_code == 200:
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(1.5)
            from api.chatbot.functions.law_auth import auth_status, preflight_law_auth
            ok = await preflight_law_auth()
            _law_apply.update(status="done" if ok else "error",
                              message="법제처 인증 확인 완료" if ok else f"인증 확인 실패: {auth_status().get('detail', '')}",
                              at=time.time())
        except Exception as e:
            _law_apply.update(status="error", message=f"법령 검색 서버 재시작 실패: {e}", at=time.time())

    asyncio.get_running_loop().create_task(run())


@router.get("")
async def list_keys():
    return [_item(k) for k in KEYS]


@router.put("")
async def set_key(data: SetKeyRequest):
    spec = _spec(data.id)
    value = data.value.strip()
    if not value or any(c.isspace() for c in value):
        raise HTTPException(status_code=400, detail="키에 공백이나 줄바꿈을 넣을 수 없습니다.")
    _, var = _current(spec)
    write_env_values({var or spec.env: value}, comment="API 키 — 화면(API 키 관리)에서 입력")
    if spec.apply == "reset_models":
        _reset_model_cache()
    elif spec.apply == "restart_law":
        _restart_law_mcp()
    print(f"[keys] {spec.env} 저장")
    return _item(spec)


@router.post("/delete")
async def delete_key(data: KeyIdRequest):
    spec = _spec(data.id)
    if not spec.deletable:
        raise HTTPException(status_code=400, detail=f"{spec.label}는 지울 수 없습니다 (법령 검색 서버 실행에 꼭 필요). 새 값으로 바꾸세요.")
    remove_env_keys([spec.env, *spec.aliases])
    if spec.apply == "reset_models":
        _reset_model_cache()
    print(f"[keys] {spec.env} 삭제")
    return _item(spec)


@router.post("/test")
async def test_key(data: KeyIdRequest):
    """
    저장된 키로 해당 서비스에 가벼운 인증 요청을 보내 확인한다 (모델 목록·계정 조회 — 토큰을 쓰지 않는다).
    LAW_OC는 korean-law-mcp로 '민법'을 검색해 본다.
    """
    spec = _spec(data.id)
    value, _ = _current(spec)
    if not value:
        return {"ok": False, "message": "저장된 키가 없습니다."}
    if spec.id == "LAW_OC":
        from api.chatbot.functions.law_auth import auth_status, preflight_law_auth
        ok = await preflight_law_auth()
        return {"ok": ok, "message": "법제처 인증 정상 (민법 검색 성공)" if ok else auth_status().get("detail", "인증 실패")}

    requests = {
        "OPENAI": ("GET", "https://api.openai.com/v1/models", {"Authorization": f"Bearer {value}"}),
        "GEMINI": ("GET", f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=1&key={value}", {}),
        "ANTHROPIC": ("GET", "https://api.anthropic.com/v1/models?limit=1",
                      {"x-api-key": value, "anthropic-version": "2023-06-01"}),
        "HUGGINGFACE": ("GET", "https://huggingface.co/api/whoami-v2", {"Authorization": f"Bearer {value}"}),
        "OLLAMA": ("POST", "https://ollama.com/api/me", {"Authorization": f"Bearer {value}"}),
    }
    method, url, headers = requests[spec.id]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.request(method, url, headers={"User-Agent": "klaw-chatbot", **headers},
                                        json={} if method == "POST" else None)
    except Exception as e:
        return {"ok": False, "message": f"연결 실패: {type(e).__name__}"}
    if resp.status_code == 200:
        return {"ok": True, "message": "인증 정상"}
    if resp.status_code in (400, 401, 403):
        return {"ok": False, "message": f"키가 올바르지 않거나 권한이 없습니다 (HTTP {resp.status_code})"}
    return {"ok": False, "message": f"확인하지 못했습니다 (HTTP {resp.status_code})"}
