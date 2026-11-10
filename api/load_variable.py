import os, dotenv
from pydantic import BaseModel
from typing import Annotated, TypedDict, List, Dict, Optional, Any

from qdrant_client import AsyncQdrantClient

dotenv.load_dotenv()

from api.llm_gateway import LiteLLMEmbeddings, ModelManager, ModelRegistry

# DB 환경변수 / DB 체크포인터·스토어 / 풀 초기화 코드 전체 제거.
# 현재 단계 목표: 엔드포인트 + LLM + MCP 도구 연결 확인 (상태는 in-memory만).
# 체크포인터는 main.py의 lifespan에서 MemorySaver()로 직접 생성한다.

# 임베딩 모델은 의도적으로 코드에 고정한다 (환경변수로 바꾸지 않는다).
#
# 이 값은 단독으로 바꿀 수 있는 설정이 아니라 Qdrant 인덱스와 한 몸이다:
# conversation_history 컬렉션은 최초 생성 시 이 모델의 출력 차원(3072)으로 고정되고,
# 이미 존재하는 컬렉션은 차원을 다시 감지하지 않는다(summary.py의 ensure_conversation_history_collection).
# 따라서 차원이 다른 모델로 바꾸면(예: openai/text-embedding-3-small = 1536) 저장이 조용히 실패한다.
# 같은 차원의 모델로 바꾸더라도 벡터 공간이 달라 기존 대화 검색 결과가 어긋난다.
#
# 교체하려면 코드에서 이 값을 바꾸고 scripts/reset_db.py로 컬렉션을 지워 재생성해야 한다.
# 즉 '설정 변경'이 아니라 '재인덱싱을 동반한 마이그레이션'이므로 .env에 노출하지 않는다.
EMBEDDING_MODEL = "gemini/gemini-embedding-001"   # 3072차원 (LiteLLM 표기)
GEMINI_EMBEDDINGS = LiteLLMEmbeddings(model=EMBEDDING_MODEL)

# Qdrant 로컬 모드: 서버 없이 앱 데이터 폴더(data/qdrant)에 바로 저장한다 (한 프로세스만 열 수 있다)
from api.stack import QDRANT_PATH
QDRANT_DB = AsyncQdrantClient(path=str(QDRANT_PATH))


# 채팅 모델은 api/llm_gateway가 '접두사/모델명'(예: 'anthropic/claude-sonnet-5')으로 생성한다.
# 호출 가능한 모델은 DB(llm_models)에서 읽어 LLM_REGISTRY_TTL초(기본 30)마다
# 갱신하므로, DB를 고치면 서버 재시작 없이 반영된다. 모델 인스턴스는 1시간 미사용 시 해제된다.
async def _load_models_from_db():
    from api.db import get_pool
    from api.llm_gateway.store import load_models
    async with get_pool().acquire() as conn:
        return await load_models(conn)

MODEL_REGISTRY = ModelRegistry(_load_models_from_db, ttl=float(os.getenv("LLM_REGISTRY_TTL", "30")))
MODEL_MANAGER = ModelManager(MODEL_REGISTRY, ttl=3600)


# === Korean Law MCP 클라이언트 ===
# api/services.py가 자식 프로세스로 띄운 korean-law-mcp 서버에 연결, 도구를 LangChain Tool로 자동 변환
import asyncio
from langchain_mcp_adapters.client import MultiServerMCPClient

MCP_CLIENT = MultiServerMCPClient({
    "korean_law": {
        # 환경변수로 오버라이드 가능 (예: 다른 호스트/포트로 띄울 때)
        "url": os.getenv("MCP_KOREAN_LAW_URL", "http://127.0.0.1:3000/mcp"),
        "transport": "streamable_http",
        # 127.0.0.1에만 열리지만 같은 PC의 다른 프로그램이 쓰지 못하도록 토큰을 건다 (services가 같은 값을 넘긴다)
        "headers": {"Authorization": f"Bearer {os.getenv('MCP_AUTH_TOKEN', '')}"},
    }
})

async def _load_law_mcp_instructions() -> str:
    """
    서버가 initialize 응답에 담아 보내는 사용 안내(instructions)를 읽는다.

    korean-law-mcp v4.15.12부터 '도구 A로 ID를 얻은 뒤 도구 B' 같은 도구 사이 안내가
    도구 설명에서 빠지고 이 안내문으로만 나간다(Claude 커넥터 디렉터리 정책).
    MultiServerMCPClient는 initialize 결과를 버리므로 직접 받아 프롬프트에 넣어야 모델이 본다.
    """
    async with MCP_CLIENT.session("korean_law", auto_initialize=False) as session:
        result = await session.initialize()
    return (result.instructions or "").strip()


try:
    # 모듈 임포트 시 1회 블로킹 로드 (~1-2초)
    # 이후 모든 요청에서 동일한 LAW_TOOLS 인스턴스를 재사용
    LAW_TOOLS = asyncio.run(MCP_CLIENT.get_tools())
    print(f"[MCP] Korean Law 도구 로드 완료: {len(LAW_TOOLS)}개")
    for _t in LAW_TOOLS:
        print(f"  - {_t.name}")

    # 이미지 기반 별표(PDF) 응답을 PaddleOCR 텍스트로 보강하는 후처리 래핑
    # (OCR 엔진은 첫 사용 시 lazy 초기화 — 서버 기동 시간에는 영향 없음)
    from api.chatbot.functions.tool_annex_ocr import wrap_tools_with_annex_ocr
    LAW_TOOLS = wrap_tools_with_annex_ocr(LAW_TOOLS)
except Exception as _mcp_err:
    print(f"[MCP] Korean Law 도구 로드 실패: {_mcp_err}")
    print(f"[MCP] 법령 MCP 서버 로그를 확인하세요: logs/law-mcp.log ({os.getenv('LAW_MCP_HEALTH_URL')})")
    LAW_TOOLS = []

# 안내문은 없어도 검색은 동작하므로, 실패해도 도구 로드와 별개로 빈 값으로 둔다.
LAW_MCP_INSTRUCTIONS = ""
if LAW_TOOLS:
    try:
        LAW_MCP_INSTRUCTIONS = asyncio.run(_load_law_mcp_instructions())
        print(f"[MCP] Korean Law 서버 안내문 로드: {len(LAW_MCP_INSTRUCTIONS)}자")
    except Exception as _inst_err:
        print(f"[MCP] Korean Law 서버 안내문 로드 실패 (도구 설명만 사용): {_inst_err}")

# 에이전트 상태 정의
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

def merge_dict(existing: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """기존 딕셔너리에 새로운 딕셔너리 값을 병합 (부분 업데이트 및 딥 머지)"""
    if existing is None:
        return new or {}
    if new is None:
        return existing
    
    res = dict(existing)
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(res.get(k), dict):
            res[k] = {**res[k], **v}
        else:
            res[k] = v
    return res

class AgentState(TypedDict):
    # 대화 내역 저장 및 관리하는 부분
    messages: Annotated[List[BaseMessage], add_messages]
    conversation_turns: List[List[int]]      # 사용자 메세지와 AI 메세지의 인덱스 값 저장하는 부분 (ToolMessage 걸러보기 위함)
    current_turn: int                        # 현재 대화의 턴 (entity_memory 관리를 위함)
    tool_counts: Annotated[Dict[str, Dict[str, int]], merge_dict]   # 노드별 툴 사용 횟수 체크
    
    # 전체적인 대화 메모리 관리를 위한 상태 값
    turn_memory: List[str]         # 하나의 대화쌍(turn)을 요약
    check_turn: int                # turn_memory를 몇번 업데이트 했는지 확인 (7번이 되면 short_memory 업데이트 및 초기화)
    short_memory: List[str]        # 10개의 대화쌍(turn)을 7번의 주기마다 요약
    check_short: int               # short_memory를 몇번 업데이트 했는지 확인 (4번이 되면 long_memory 업데이트 및 초기화)
    long_memory: Optional[str]     # 전체 대화 메세지에 대해 short_memory가 4개 사라질 때마다 요약
    entity_memory: Annotated[Dict[str, Any], merge_dict]   # 매 턴마다 대화 내역에서 파악 가능한 객체에 대해 간단히 설명을 작성
    last_turn_entities: List[str]  # 직전 턴에서 update_entity가 추출/갱신한 엔티티 Key 목록 (save_conversation의 entity_keys payload에 사용)
    
    # 노드 분리 관련 상태 값
    is_law_related: Optional[bool]  # 법률 관련 메시지 여부 (True/False)
    law_search_count: Optional[int] # 법률 검색 노드 실행 횟수 (최대 5회)

    