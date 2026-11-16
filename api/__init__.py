# api/__init__.py
# 다른 모듈이 접속 주소(DB·Qdrant·검색·법령 MCP·Ollama)를 읽기 전에, 스택 위치의 .env를 읽고 포트에서 주소를 만든다.
# (개발: 저장소 루트 / exe: %LOCALAPPDATA%\KLaw — api/stack.py)
#
# 여기서는 가벼운 일만 한다. 라우터(→ LLM·MCP·OCR 초기화)는 api/routes.py에 두어, 데스크톱 실행기가
# `from api import stack`만으로 앱 전체를 끌어오지 않게 한다 (서비스를 띄우기 전에 MCP에 붙으려 하면 안 된다).
import os as _os

import dotenv as _dotenv

from api import stack as _stack
from api import backup_restore as _backup_restore

# LiteLLM은 'DEV' 모드에서 import될 때 실행 위치의 .env를 스스로 읽는다 → 앱 폴더(HOME)의 .env만 쓰도록 끈다
_os.environ.setdefault("LITELLM_MODE", "PRODUCTION")

# Google Drive 백업에서 고른 복원이 있으면, 아무것도(.env·DB) 읽기 전에 파일을 바꿔 넣는다
_backup_restore.apply_pending()
_dotenv.load_dotenv(_stack.HOME / ".env")


# 이후 라이브러리가 경로 없이 load_dotenv()를 부르면(crawl4ai 등) 자기 위치에서 위로 올라가며 아무 .env나 읽는다
# (개발: 저장소 .env의 실제 키, exe: 사용자 폴더 근처의 다른 프로그램 .env). 앱 폴더 .env만 쓰도록 막는다.
def _no_implicit_dotenv(dotenv_path=None, *args, **kwargs):
    if dotenv_path is None and not args and kwargs.get("stream") is None:
        return False
    return _orig_load_dotenv(dotenv_path, *args, **kwargs)


_orig_load_dotenv = _dotenv.main.load_dotenv
_dotenv.load_dotenv = _dotenv.main.load_dotenv = _no_implicit_dotenv
_stack.apply_env()
