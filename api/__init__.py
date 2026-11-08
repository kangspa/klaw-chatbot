# api/__init__.py
# 다른 모듈이 접속 주소(DB·Qdrant·검색·법령 MCP·Ollama)를 읽기 전에, 스택 위치의 .env를 읽고 포트에서 주소를 만든다.
# (개발: 저장소 루트 / exe: %LOCALAPPDATA%\KLaw — api/stack.py)
#
# 여기서는 가벼운 일만 한다. 라우터(→ LLM·MCP·OCR 초기화)는 api/routes.py에 두어, 데스크톱 실행기가
# `from api import stack`만으로 앱 전체를 끌어오지 않게 한다 (서비스를 띄우기 전에 MCP에 붙으려 하면 안 된다).
import dotenv as _dotenv

from api import stack as _stack

_dotenv.load_dotenv(_stack.HOME / ".env")
_stack.apply_env()
