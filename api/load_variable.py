import os, dotenv
from cachetools import TTLCache
from pydantic import BaseModel
from typing import Annotated, TypedDict, List, Dict, Optional, Any

import litellm
from langchain_litellm import ChatLiteLLM
from langchain_core.embeddings import Embeddings
from langchain_community.utilities import SearxSearchWrapper
from qdrant_client import AsyncQdrantClient

dotenv.load_dotenv()

# DB 환경변수 / Postgres 체크포인터·스토어 / 풀 초기화 코드 전체 제거.
# 현재 단계 목표: 엔드포인트 + LLM + MCP 도구 연결 확인 (상태는 in-memory만).
# 체크포인터는 main.py의 lifespan에서 MemorySaver()로 직접 생성한다.

SEARX_WRAPPER = SearxSearchWrapper(searx_host="http://localhost:8888")


class LiteLLMEmbeddings(Embeddings):
    """
    litellm.embedding / aembedding 을 LangChain Embeddings 인터페이스로 래핑.
    기존 호출부(aembed_documents / aembed_query)를 그대로 사용 가능.

    model 예:
      - 'gemini/gemini-embedding-001'  (3072차원, 기존 Qdrant 인덱스와 호환)
      - 'openai/text-embedding-3-small' (1536차원, 재인덱싱 필요)
      - 'huggingface/sentence-transformers/all-MiniLM-L6-v2' (Inference API)
    """
    def __init__(self, model: str):
        self.model = model

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        resp = litellm.embedding(model=self.model, input=texts)
        return [d["embedding"] for d in resp["data"]]

    def embed_query(self, text: str) -> List[float]:
        resp = litellm.embedding(model=self.model, input=[text])
        return resp["data"][0]["embedding"]

    async def aembed_documents(self, texts: List[str]) -> List[List[float]]:
        resp = await litellm.aembedding(model=self.model, input=texts)
        return [d["embedding"] for d in resp["data"]]

    async def aembed_query(self, text: str) -> List[float]:
        resp = await litellm.aembedding(model=self.model, input=[text])
        return resp["data"][0]["embedding"]


# 임베딩 모델은 환경변수로 오버라이드 가능.
# 기본값을 'gemini/gemini-embedding-001' 으로 두면 기존 Qdrant 인덱스(3072차원)와 호환되어 재인덱싱 불필요.
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "gemini/gemini-embedding-001")
GEMINI_EMBEDDINGS = LiteLLMEmbeddings(model=EMBEDDING_MODEL)

QDRANT_DB = AsyncQdrantClient(host="localhost", grpc_port=6334, prefer_grpc=True)


class DynamicModelManager:
    """
    LiteLLM 기반 ChatLiteLLM 인스턴스 캐시.

    model_name은 LiteLLM 표준 prefix 그대로 사용:
      - 'gemini/gemini-2.5-flash-lite'
      - 'openai/gpt-4o-mini'
      - 'anthropic/claude-sonnet-4-5'
      - 'ollama/llama3.1' 또는 'ollama_chat/llama3.1'
      - 'huggingface/meta-llama/Meta-Llama-3-8B-Instruct'

    API 키는 .env에 등록된 표준 환경 변수명을 LiteLLM이 자동 인식:
      OPENAI_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY /
      OLLAMA_API_KEY / HUGGINGFACEHUB_API_TOKEN
    """
    def __init__(self, ttl: int = 3600, maxsize: int = 10):
        # ttl: 미사용 시 유지 시간(초), maxsize: 최대 저장 모델 개수
        self.cache = TTLCache(maxsize=maxsize, ttl=ttl)

    def get_model(self, model_name: str, temperature: float = 0.2) -> ChatLiteLLM:
        cache_key = f"{model_name}_{temperature}"

        if cache_key not in self.cache:
            print(f"--- [Memory] New model instance: {model_name} ---")
            self.cache[cache_key] = ChatLiteLLM(
                model=model_name,
                temperature=temperature,
            )
        return self.cache[cache_key]


# 글로벌 매니저 인스턴스
MODEL_MANAGER = DynamicModelManager(ttl=3600) # 1시간 후 자동 해제


# === Korean Law MCP 클라이언트 ===
# docker compose로 띄운 korean-law-mcp 서버에 연결, 16개 도구를 LangChain Tool로 자동 변환
import asyncio
from langchain_mcp_adapters.client import MultiServerMCPClient

MCP_CLIENT = MultiServerMCPClient({
    "korean_law": {
        # 환경변수로 오버라이드 가능 (예: 다른 호스트/포트로 띄울 때)
        "url": os.getenv("MCP_KOREAN_LAW_URL", "http://localhost:3000/mcp"),
        "transport": "streamable_http",
    }
})

try:
    # 모듈 임포트 시 1회 블로킹 로드 (~1-2초)
    # 이후 모든 요청에서 동일한 LAW_TOOLS 인스턴스를 재사용
    LAW_TOOLS = asyncio.run(MCP_CLIENT.get_tools())
    print(f"[MCP] Korean Law 도구 로드 완료: {len(LAW_TOOLS)}개")
    for _t in LAW_TOOLS:
        print(f"  - {_t.name}")
except Exception as _mcp_err:
    print(f"[MCP] Korean Law 도구 로드 실패: {_mcp_err}")
    print("[MCP] docker가 실행 중인지, http://localhost:3000/health 가 응답하는지 확인하세요.")
    LAW_TOOLS = []

# 에이전트 상태 정의
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

def merge_dict(existing: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """기존 딕셔너리에 새로운 딕셔너리 값을 병합 (부분 업데이트)"""
    if existing is None:
        return new or {}
    if new is None:
        return existing
    
    # 기존 데이터를 복사한 뒤 새로운 데이터로 업데이트
    return {**existing, **new}

class AgentState(TypedDict):
    # 대화 내역 저장 및 관리하는 부분
    messages: Annotated[List[BaseMessage], add_messages]
    conversation_turns: List[List[int]]      # 사용자 메세지와 AI 메세지의 인덱스 값 저장하는 부분 (ToolMessage 걸러보기 위함)
    current_turn: int                        # 현재 대화의 턴 (entity_memory 관리를 위함)
    tool_counts: Dict[str, Dict[str, int]]   # 노드별 툴 사용 횟수 체크
    
    # 전체적인 대화 메모리 관리를 위한 상태 값
    turn_memory: List[str]         # 하나의 대화쌍(turn)을 요약
    check_turn: int                # turn_memory를 몇번 업데이트 했는지 확인 (7번이 되면 short_memory 업데이트 및 초기화)
    short_memory: List[str]        # 10개의 대화쌍(turn)을 7번의 주기마다 요약
    check_short: int               # short_memory를 몇번 업데이트 했는지 확인 (4번이 되면 long_memory 업데이트 및 초기화)
    long_memory: Optional[str]     # 전체 대화 메세지에 대해 short_memory가 4개 사라질 때마다 요약
    entity_memory: Dict[str,Any]   # 매 턴마다 대화 내역에서 파악 가능한 객체에 대해 간단히 설명을 작성
    last_turn_entities: List[str]  # 직전 턴에서 update_entity가 추출/갱신한 엔티티 Key 목록 (save_conversation의 entity_keys payload에 사용)
    
    # 판단 근거
    data_reason: str