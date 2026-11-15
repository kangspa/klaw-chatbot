"""
Google Drive 백업 End-point (/chatbot/backup/...) — 마이 페이지 'Google Drive 백업'.

  GET  /chatbot/backup                    로컬 크기·마지막 백업·받아 둔 복원·마지막 복원
  POST /chatbot/backup/drive/upload       {accessToken, includeKeys} 지금 백업 (오래된 것은 최근 KEEP개만 남김)
  POST /chatbot/backup/drive/list         {accessToken} Drive에 있는 백업 목록
  POST /chatbot/backup/drive/restore      {accessToken, fileId} 받아서 검사 → 다음 시작 때 적용 + 앱 재시작
  POST /chatbot/backup/drive/delete       {accessToken, fileId}
  POST /chatbot/backup/restore/cancel     받아 둔 복원 취소

저장 위치는 Drive의 '앱 전용 숨김 폴더'(appDataFolder, 범위 drive.appdata) — 사용자의 Drive 화면에 보이지 않고
이 앱(OAuth 클라이언트)만 읽고 쓸 수 있다. 사용자의 다른 Drive 파일은 볼 수 없다.

액세스 토큰은 화면이 Google에서 그때그때 받아(1시간 유효) 요청에 실어 보낸다. 서버는 그 요청에만 쓰고 저장하지 않는다.
"""
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import backup_restore as br
from api import runtime_control, stack
from api.auth.deps import get_current_user

router = APIRouter(prefix="/backup", dependencies=[Depends(get_current_user)])

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
KEEP = 5
STATE_FILE = stack.RUNTIME_DIR / "backup_state.json"
APP_VERSION = "1.1.0"


class TokenRequest(BaseModel):
    accessToken: str


class UploadRequest(TokenRequest):
    includeKeys: bool = True


class FileRequest(TokenRequest):
    fileId: str


def _key_envs() -> set[str]:
    from api.chatbot.keys import KEYS
    return {n for k in KEYS for n in (k.env, *k.aliases)}


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _drive_error(resp: httpx.Response) -> HTTPException:
    try:
        err = resp.json().get("error", {})
        reason = (err.get("errors") or [{}])[0].get("reason") or err.get("status") or ""
        msg = err.get("message") or resp.text[:200]
    except Exception:
        reason, msg = "", resp.text[:200]
    if resp.status_code == 401:
        return HTTPException(status_code=401, detail="Google 인증이 만료되었습니다. 다시 시도하세요.")
    if resp.status_code == 403 and reason in ("accessNotConfigured", "SERVICE_DISABLED"):
        return HTTPException(status_code=502, detail="이 앱의 GCP 프로젝트에서 Google Drive API가 꺼져 있습니다.")
    if resp.status_code == 403 and "insufficient" in (reason + msg).lower():
        return HTTPException(status_code=403, detail="Drive 권한(앱 전용 폴더)에 동의하지 않았습니다. 다시 시도해 동의하세요.")
    return HTTPException(status_code=502, detail=f"Google Drive 오류 ({resp.status_code} {reason}): {msg}")


def _client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=120, headers={"Authorization": f"Bearer {token}"})


async def _check_account(c: httpx.AsyncClient, user: dict) -> str:
    """토큰의 Google 계정이 연결해 둔 계정과 같은지 (다른 계정 Drive에 올리는 실수를 막는다)."""
    r = await c.get(f"{DRIVE}/about", params={"fields": "user(emailAddress)"})
    if r.status_code != 200:
        raise _drive_error(r)
    email = (r.json().get("user") or {}).get("emailAddress", "")
    linked = (user.get("email") or "").lower()
    if linked and email.lower() != linked:
        raise HTTPException(status_code=409, detail=f"연결된 Google 계정({linked})과 다른 계정({email})으로 로그인했습니다.")
    return email


async def _list(c: httpx.AsyncClient) -> list[dict]:
    r = await c.get(f"{DRIVE}/files", params={
        "spaces": "appDataFolder", "orderBy": "createdTime desc", "pageSize": 100,
        "q": "appProperties has { key='klaw' and value='backup' } and trashed = false",
        "fields": "files(id,name,size,createdTime,appProperties)"})
    if r.status_code != 200:
        raise _drive_error(r)
    out = []
    for f in r.json().get("files", []):
        props = f.get("appProperties") or {}
        out.append({"id": f["id"], "name": f["name"], "size": int(f.get("size") or 0), "createdTime": f.get("createdTime"),
                    "includesKeys": props.get("includesKeys") == "1", "appVersion": props.get("appVersion", "")})
    return out


@router.get("")
async def backup_status(current_user: dict[str, Any] = Depends(get_current_user)):
    sizes = {name: (p.stat().st_size if p.exists() else 0) for name, p in br.TARGETS.items()}
    return {
        "googleLinked": bool(current_user.get("googleLinked")),
        "email": current_user.get("email"),
        "localBytes": sizes,
        "lastBackup": _read_state().get("lastBackup"),
        "pendingRestore": br.pending(),
        "lastRestore": br.last_restore(),
        "canRestartApp": runtime_control.can_restart_app(),
        "keep": KEEP,
    }


@router.post("/drive/upload")
async def upload_backup(data: UploadRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    async with _client(data.accessToken) as c:
        email = await _check_account(c, current_user)
        bundle = await asyncio.to_thread(br.build_bundle, data.includeKeys, _key_envs(), APP_VERSION)
        try:
            size = bundle.stat().st_size
            stamp = time.strftime("%Y%m%d-%H%M%S")
            meta = {"name": f"klaw-backup-{stamp}.zip", "parents": ["appDataFolder"], "mimeType": "application/zip",
                    "appProperties": {"klaw": "backup", "format": str(br.FORMAT), "appVersion": APP_VERSION,
                                      "includesKeys": "1" if data.includeKeys else "0"}}
            # 재개 가능 업로드: 세션을 연 뒤 본문을 한 번에 보낸다 (크기와 무관하게 같은 방식)
            r = await c.post(UPLOAD, params={"uploadType": "resumable", "fields": "id,name,size,createdTime"},
                             json=meta, headers={"X-Upload-Content-Type": "application/zip",
                                                 "X-Upload-Content-Length": str(size)})
            if r.status_code != 200:
                raise _drive_error(r)
            r = await c.put(r.headers["Location"], content=bundle.read_bytes(), headers={"Content-Type": "application/zip"})
            if r.status_code not in (200, 201):
                raise _drive_error(r)
            created = r.json()
        finally:
            bundle.unlink(missing_ok=True)
        # 최근 KEEP개만 남긴다
        removed = 0
        for old in (await _list(c))[KEEP:]:
            if (await c.delete(f"{DRIVE}/files/{old['id']}")).status_code in (200, 204):
                removed += 1
    last = {"at": time.time(), "fileId": created["id"], "name": created["name"], "size": int(created.get("size") or size),
            "includesKeys": data.includeKeys, "account": email}
    STATE_FILE.write_text(json.dumps({"lastBackup": last}, ensure_ascii=False), encoding="utf-8")
    print(f"[backup] Drive 백업 완료: {created['name']} ({size / 1e6:.1f}MB, 키 포함={data.includeKeys}, 정리 {removed}개)")
    return {"backup": last, "removedOld": removed}


@router.post("/drive/list")
async def list_backups(data: TokenRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    async with _client(data.accessToken) as c:
        email = await _check_account(c, current_user)
        return {"account": email, "backups": await _list(c)}


@router.post("/drive/delete")
async def delete_backup(data: FileRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    async with _client(data.accessToken) as c:
        await _check_account(c, current_user)
        r = await c.delete(f"{DRIVE}/files/{data.fileId}")
        if r.status_code not in (200, 204):
            raise _drive_error(r)
    return {"deleted": data.fileId}


@router.post("/drive/restore")
async def restore_backup(data: FileRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    from api.llm_gateway.runtime_update import RUNTIME_UPDATER
    from api.llm_gateway.ollama_upgrade import UPGRADER
    if RUNTIME_UPDATER.running or UPGRADER.running:
        raise HTTPException(status_code=409, detail="버전을 바꾸는 중에는 복원할 수 없습니다.")
    async with _client(data.accessToken) as c:
        await _check_account(c, current_user)
        r = await c.get(f"{DRIVE}/files/{data.fileId}", params={"alt": "media"})
        if r.status_code != 200:
            raise _drive_error(r)
        tmp = stack.RUNTIME_DIR / "restore_download.zip"
        tmp.write_bytes(r.content)
    try:
        manifest = await asyncio.to_thread(br.stage_restore, tmp)
    except br.BundleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        tmp.unlink(missing_ok=True)
    if runtime_control.can_restart_app():
        runtime_control.request_restart()   # 다시 뜨면서(api/__init__) 파일을 바꿔 넣는다
        restart = "app"
    else:
        restart = "manual"   # 개발 실행: 서버를 다시 띄우면 적용된다
    print(f"[backup] 복원 준비 완료 (백업 시각 {manifest.get('createdAt')}) — 재시작: {restart}")
    return {"restart": restart, "manifest": manifest}


@router.post("/restore/cancel")
async def cancel_restore():
    br.cancel_pending()
    return {"cancelled": True}
