"""Gemini 임베딩 API를 LangChain Embeddings 인터페이스로 감싼 클래스."""
import os
from typing import List

from langchain_core.embeddings import Embeddings

# Gemini 임베딩 API의 요청당 최대 입력 수
_BATCH = 100


class GeminiEmbeddings(Embeddings):
    """
    task_type을 지정하지 않는다. 기존 Qdrant 인덱스가 task_type 없이(LiteLLM 경유) 만들어졌으므로,
    문서/질의용 task_type을 따로 주면 벡터 공간이 어긋나 기존 대화 검색 결과가 달라진다.
    """

    def __init__(self, model: str):
        from google import genai
        self.model = model
        self._client = genai.Client(api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for i in range(0, len(texts), _BATCH):
            resp = self._client.models.embed_content(model=self.model, contents=texts[i:i + _BATCH])
            out.extend(e.values for e in resp.embeddings)
        return out

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]

    async def aembed_documents(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for i in range(0, len(texts), _BATCH):
            resp = await self._client.aio.models.embed_content(model=self.model, contents=texts[i:i + _BATCH])
            out.extend(e.values for e in resp.embeddings)
        return out

    async def aembed_query(self, text: str) -> List[float]:
        return (await self.aembed_documents([text]))[0]
