"""
API 키 관리 End-point (/chatbot/keys/...) — 사용자가 화면에서 키를 넣고 바꾸고 지우고, 연결을 확인한다.

키는 이 PC의 루트 .env에 저장한다 (개인 PC에서 띄워 쓰는 서비스 — api/env_store.py).
응답에는 키 원문을 절대 담지 않고 앞뒤 몇 글자만 보여준다. 연결 확인은 해당 회사 API에만 키를 보낸다.

키마다 다시 읽히는 시점이 달라서 저장 뒤 처리가 다르다:
  OpenAI·Gemini·Anthropic  LiteLLM이 호출할 때마다 환경 변수를 읽는다 → 바로 적용
  Hugging Face (HF_TOKEN)  모델 '다운로드' 인증용 (huggingface_hub — 예: OCR 모델). 받을 때마다 읽는다 → 바로 적용.
                           모델 호출에는 쓰지 않는다 (HF 클라우드 추론은 지원하지 않음)
  LAW_OC                   법령 MCP 서버(자식 프로세스)가 시작할 때 읽는다 → 서버를 다시 띄우고 인증을 확인한다
Ollama는 로컬 모델만 쓰므로 키가 없다. 대신 Ollama가 Hugging Face의 승인·비공개 저장소(hf.co/…)를 받을 때 쓰는
Ollama 엔진의 SSH 공개키를 보여준다 (HF 계정 > SSH keys에 등록 — HF_TOKEN으로는 인증되지 않는다).
"""
import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth.deps import get_current_user
from api.env_store import read_env_file, remove_env_keys, rename_env_key, write_env_values
from api import services

router = APIRouter(prefix="/keys", dependencies=[Depends(get_current_user)])


@dataclass(frozen=True)
class KeySpec:
    id: str
    label: str
    env: str                       # 저장할 .env 변수
    aliases: tuple[str, ...]       # 같은 키로 인정하는 다른 변수 (예전 이름)
    description: str
    apply: str                     # immediate | restart_law
    deletable: bool = True


KEYS: list[KeySpec] = [
    KeySpec("OPENAI", "OpenAI", "OPENAI_API_KEY", (), "OpenAI 모델(GPT) 호출", "immediate"),
    KeySpec("GEMINI", "Google Gemini", "GEMINI_API_KEY", ("GOOGLE_API_KEY",), "Gemini 모델 호출", "immediate"),
    KeySpec("ANTHROPIC", "Anthropic", "ANTHROPIC_API_KEY", (), "Claude 모델 호출", "immediate"),
    # huggingface_hub가 읽는 표준 이름은 HF_TOKEN. 예전 이름(HUGGINGFACEHUB_API_TOKEN 등)이면 저장할 때 HF_TOKEN으로 옮긴다
    KeySpec("HUGGINGFACE", "Hugging Face", "HF_TOKEN", ("HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN"),
            "모델 다운로드 인증 — 승인(gated)·비공개 저장소에서 받을 때 (예: 법령 별표 OCR 모델). 모델 호출에는 쓰지 않습니다",
            "immediate"),
    # 법령 검색의 필수 값이라 지울 수 없고 바꾸기만 한다
    KeySpec("LAW_OC", "법제처 Open API (LAW_OC)", "LAW_OC", (),
            "법령·판례 검색(korean-law-mcp)의 법제처 인증값. 바꾸면 법령 검색 서버를 다시 시작합니다", "restart_law",
            deletable=False),
]
_BY_ID = {k.id: k for k in KEYS}

# LAW_OC 적용(서버 재시작 + 인증 확인) 진행 상태
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


def _restart_law_mcp() -> None:
    """LAW_OC를 새 값으로 쓰도록 법령 MCP 서버(자식 프로세스)를 다시 띄운 뒤 법제처 인증을 확인한다 (백그라운드)."""
    _law_apply.update(status="running", message="법령 검색 서버를 다시 시작하는 중", at=time.time())

    async def run() -> None:
        try:
            # 자식 프로세스를 다시 띄운다 (LAW_OC는 시작할 때 환경 변수로 넘긴다). 응답할 때까지 기다린다.
            if not await asyncio.to_thread(services.LAW_MCP.restart):
                raise RuntimeError(f"서버가 응답하지 않습니다 (로그: {services.LAW_MCP.log_path})")
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
    if var and var != spec.env:
        rename_env_key(var, spec.env)   # 예전 변수 이름 → 표준 이름 (값은 아래에서 새로 쓴다)
    write_env_values({spec.env: value}, comment="API 키 — 화면(API 키 관리)에서 입력")
    if spec.apply == "restart_law":
        _restart_law_mcp()
    print(f"[keys] {spec.env} 저장")
    return _item(spec)


@router.post("/delete")
async def delete_key(data: KeyIdRequest):
    spec = _spec(data.id)
    if not spec.deletable:
        raise HTTPException(status_code=400, detail=f"{spec.label}는 지울 수 없습니다 (법령 검색 서버 실행에 꼭 필요). 새 값으로 바꾸세요.")
    remove_env_keys([spec.env, *spec.aliases])
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


@router.get("/ollama-ssh")
async def ollama_ssh_key():
    """
    Ollama 엔진의 SSH 공개키 (data/ollama/home/.ollama — 엔진이 처음 실행될 때 만든다). Hugging Face 계정(Settings > SSH and GPG keys)에 등록하면 Ollama가
    승인(gated)·비공개 hf.co/… 저장소의 GGUF를 받을 수 있다. 공개키라 보여줘도 안전하다 (개인키는 앱 데이터 폴더에만 있다).
    """
    key_file = services.OLLAMA_HOME / ".ollama" / "id_ed25519.pub"
    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    if not key.startswith("ssh-"):
        return {"available": False, "publicKey": None,
                "message": "Ollama 엔진을 설치해 한 번 실행하면 키가 만들어집니다."}
    return {"available": True, "publicKey": key, "message": ""}
