"""LiteLLM 임베딩을 LangChain Embeddings 인터페이스로 감싼 클래스."""
from typing import List

import litellm
from langchain_core.embeddings import Embeddings

# 요청당 최대 입력 수 (Gemini 임베딩 API 한도)
_BATCH = 100


class LiteLLMEmbeddings(Embeddings):
    """
    model 예: 'gemini/gemini-embedding-001' (3072차원).
    task_type 같은 추가 인자는 넘기지 않는다. 기존 Qdrant 인덱스가 이 방식으로 만들어졌으므로,
    문서/질의용 task_type을 따로 주면 벡터 공간이 어긋나 기존 대화 검색 결과가 달라진다.
    """

    def __init__(self, model: str):
        self.model = model

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for i in range(0, len(texts), _BATCH):
            resp = litellm.embedding(model=self.model, input=texts[i:i + _BATCH])
            out.extend(d["embedding"] for d in resp["data"])
        return out

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]

    async def aembed_documents(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for i in range(0, len(texts), _BATCH):
            resp = await litellm.aembedding(model=self.model, input=texts[i:i + _BATCH])
            out.extend(d["embedding"] for d in resp["data"])
        return out

    async def aembed_query(self, text: str) -> List[float]:
        return (await self.aembed_documents([text]))[0]
