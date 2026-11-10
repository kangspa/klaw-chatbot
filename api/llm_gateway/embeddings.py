"""
대화 기록 의미 검색용 임베딩 — 사용자가 고르는 3가지 방식과, 지금 고른 방식으로 보내는 창구(EmbeddingService).

  local   (기본) 앱 안에서 CPU로 돈다 (fastembed + ONNX). 키·Ollama 없이 동작하고 대화가 PC 밖으로 나가지 않는다.
          intfloat/multilingual-e5-small (int8 양자화 ONNX, 약 135MB, 384차원). 처음 쓸 때 앱 데이터 폴더로 받는다.
  ollama  앱의 Ollama 엔진으로 Qwen3-Embedding-0.6B (Q4_K_M GGUF, 약 396MB, 1024차원). GPU가 있으면 GPU로 돈다.
          런타임 버전 카드에서 Ollama 엔진을 먼저 설치해야 한다. 대화는 PC 밖으로 나가지 않는다.
  gemini  gemini/gemini-embedding-001 (LiteLLM, 3072차원). GEMINI_API_KEY가 필요하고 대화 원문이 Google로 전송된다.

방식마다 차원·벡터 공간이 달라 Qdrant 컬렉션을 따로 쓴다 (ProviderInfo.collection).
gemini는 예전부터 쓰던 'conversation_history'를 그대로 쓴다 (기존 인덱스 호환).

고른 방식은 앱 폴더 .env의 EMBEDDING_PROVIDER에 둔다 — 앱 전체에 하나(개인 PC의 로컬 사용자 1명)이고,
DB 초기화(scripts/reset_db.py)와 상관없이 남아야 하는 '설정'이라 다른 앱 설정(OLLAMA_VERSION·API 키)과 같은 곳에 둔다.

준비되지 않은 방식(모델 미다운로드·엔진 없음·키 없음)으로 임베딩하면 EmbeddingNotReady를 던진다.
호출부(대화 저장·검색)는 이를 잡아 로그만 남기고 건너뛴다 — 채팅 자체는 계속 된다.
"""
import asyncio
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Literal, Optional

import litellm

# 요청당 최대 입력 수 (Gemini 임베딩 API 한도)
_BATCH = 100

ENV_KEY = "EMBEDDING_PROVIDER"
DEFAULT_PROVIDER = "local"

Kind = Literal["document", "query"]


class EmbeddingNotReady(RuntimeError):
    """선택한 임베딩 방식을 아직 쓸 수 없다 (모델 받는 중·엔진 없음·키 없음). 메시지는 사용자 안내 문구."""


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    label: str
    model: str
    dims: int
    collection: str
    approx_bytes: int       # 받아야 하는 모델 크기 (gemini는 0)
    device: str             # CPU/GPU 안내
    privacy: str            # 대화 원문이 어디로 가는지


PROVIDERS: dict[str, ProviderInfo] = {
    "local": ProviderInfo(
        id="local", label="내장 (CPU)", model="intfloat/multilingual-e5-small", dims=384,
        collection="conversation_history__local", approx_bytes=135_400_000,
        device="CPU — 앱 안에서 바로 돈다 (설치·키 불필요)",
        privacy="대화가 이 PC 밖으로 나가지 않습니다.",
    ),
    "ollama": ProviderInfo(
        id="ollama", label="Ollama · Qwen3 Embedding", model="hf.co/mradermacher/Qwen3-Embedding-0.6B-GGUF:Q4_K_M",
        dims=1024, collection="conversation_history__ollama_qwen3", approx_bytes=396_500_000,
        device="GPU가 있으면 GPU, 없으면 CPU (Ollama 엔진 필요)",
        privacy="대화가 이 PC 밖으로 나가지 않습니다.",
    ),
    "gemini": ProviderInfo(
        id="gemini", label="Gemini (API)", model="gemini/gemini-embedding-001", dims=3072,
        collection="conversation_history", approx_bytes=0,
        device="Google 서버에서 계산 (GEMINI_API_KEY 필요)",
        privacy="질문·답변 원문이 Google로 전송됩니다.",
    ),
}


def _dir_size(d: Path) -> int:
    if not d.exists():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def _err_text(e: BaseException) -> str:
    return str(e) if str(e) else type(e).__name__


# ────────────────────────── gemini ──────────────────────────
class LiteLLMEmbeddings:
    """
    model 예: 'gemini/gemini-embedding-001' (3072차원).
    task_type 같은 추가 인자는 넘기지 않는다. 기존 Qdrant 인덱스가 이 방식으로 만들어졌으므로,
    문서/질의용 task_type을 따로 주면 벡터 공간이 어긋나 기존 대화 검색 결과가 달라진다.
    """

    def __init__(self, model: str):
        self.model = model

    async def aembed_documents(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for i in range(0, len(texts), _BATCH):
            resp = await litellm.aembedding(model=self.model, input=texts[i:i + _BATCH])
            out.extend(d["embedding"] for d in resp["data"])
        return out

    async def aembed_query(self, text: str) -> List[float]:
        return (await self.aembed_documents([text]))[0]


class GeminiProvider:
    info = PROVIDERS["gemini"]

    def __init__(self) -> None:
        self._client = LiteLLMEmbeddings(self.info.model)

    def problem(self) -> Optional[str]:
        if not os.getenv("GEMINI_API_KEY"):
            return "Gemini API 키(GEMINI_API_KEY)가 없습니다. API 키 화면에서 먼저 등록하세요."
        return None

    async def state(self) -> dict:
        return {"downloaded": True, "ready": self.problem() is None, "sizeBytes": 0,
                "preparing": False, "percent": None, "message": ""}

    def prepare(self) -> None:
        pass

    async def wait_ready(self) -> None:
        if p := self.problem():
            raise EmbeddingNotReady(p)

    async def embed(self, texts: List[str], kind: Kind) -> List[List[float]]:
        await self.wait_ready()
        return await self._client.aembed_documents(texts)


# ────────────────────────── local (fastembed) ──────────────────────────
# fastembed 기본 목록에는 한국어가 쓸 만한 300MB 이하 모델이 없다 (paraphrase-multilingual-MiniLM은 법률 질문 검색 시험에서
# 관련 문장보다 무관한 문장을 더 가깝게 놓았다). multilingual-e5-small(int8 ONNX)을 사용자 모델로 등록해 쓴다.
_LOCAL_SOURCE = "Xenova/multilingual-e5-small"
_LOCAL_FILE = "onnx/model_quantized.onnx"
# e5는 질의/문서 앞에 접두사를 붙여 학습했다
_E5_PREFIX = {"query": "query: ", "document": "passage: "}
# e5 입력 한도는 512토큰(한국어 약 700자). 긴 답변은 잘라 조각별 벡터의 평균을 쓴다 (CPU 시간을 위해 조각 수 제한)
_CHUNK_CHARS = 600
_MAX_CHUNKS = 6
_registered = False


class FastEmbedProvider:
    info = PROVIDERS["local"]

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self._model = None
        self._lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None
        self.status = "idle"            # idle | downloading | loading | ready | error
        self.message = ""
        self._peak = 0.0

    @property
    def repo_dir(self) -> Path:
        return self.cache_dir / ("models--" + _LOCAL_SOURCE.replace("/", "--"))

    def downloaded(self) -> bool:
        return any((self.repo_dir / "snapshots").glob(f"*/{_LOCAL_FILE}"))

    def problem(self) -> Optional[str]:
        return None   # 받기만 하면 된다 (받는 중 상태는 state()로 알린다)

    def _load_sync(self):
        global _registered
        with self._lock:
            if self._model is not None:
                return self._model
            from fastembed import TextEmbedding
            from fastembed.common.model_description import ModelSource, PoolingType
            if not _registered:
                TextEmbedding.add_custom_model(
                    model=self.info.model, pooling=PoolingType.MEAN, normalization=True,
                    sources=ModelSource(hf=_LOCAL_SOURCE), dim=self.info.dims, model_file=_LOCAL_FILE,
                    description="multilingual-e5-small int8 ONNX", license="mit", size_in_gb=0.13,
                )
                _registered = True
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            # cache_dir를 꼭 준다 — 안 주면 fastembed가 %TEMP%에 받는다 (앱 폴더만 지우면 PC에 남는 게 없어야 한다)
            kwargs = dict(cache_dir=str(self.cache_dir), cuda=False)
            if self.downloaded():
                try:   # 받아 둔 모델이면 인터넷 확인 없이 연다 (오프라인 PC)
                    self._model = TextEmbedding(self.info.model, local_files_only=True, **kwargs)
                    return self._model
                except Exception as e:
                    print(f"[embeddings] 내장 모델 로컬 열기 실패 → 다시 받기 시도: {_err_text(e)}")
            self._model = TextEmbedding(self.info.model, **kwargs)
            return self._model

    async def _prepare_run(self) -> None:
        t0 = time.time()
        try:
            await asyncio.to_thread(self._load_sync)
            self.status, self.message = "ready", ""
            print(f"[embeddings] 내장 임베딩 모델 준비 완료 ({time.time() - t0:.1f}s, {self.cache_dir})")
        except Exception as e:
            self.status, self.message = "error", f"내장 임베딩 모델을 준비하지 못했습니다: {_err_text(e)}"
            print(f"[embeddings] {self.message}")

    def prepare(self) -> None:
        """백그라운드로 받기·열기를 시작한다 (이미 준비됐거나 진행 중이면 그대로). 이벤트 루프 안에서 부른다."""
        if self._model is not None or (self._task is not None and not self._task.done()):
            return
        self.status = "loading" if self.downloaded() else "downloading"
        self.message = "모델 여는 중" if self.status == "loading" else f"모델 받는 중 (약 {self.info.approx_bytes // 1_000_000}MB)"
        self._task = asyncio.get_running_loop().create_task(self._prepare_run())

    async def wait_ready(self) -> None:
        self.prepare()
        if self._task is not None:
            await asyncio.shield(self._task)
        if self._model is None:
            raise EmbeddingNotReady(self.message or "내장 임베딩 모델을 준비하지 못했습니다.")

    def percent(self) -> Optional[float]:
        if self._model is not None or self.status == "ready":
            return 100.0
        if self.status != "downloading":
            return None
        self._peak = max(self._peak, min(99.0, 100 * _dir_size(self.repo_dir) / self.info.approx_bytes))
        return round(self._peak, 1)

    async def state(self) -> dict:
        ready = self._model is not None
        preparing = self.status in ("downloading", "loading") and not ready
        return {"downloaded": self.downloaded(), "ready": ready, "sizeBytes": await asyncio.to_thread(_dir_size, self.repo_dir),
                "preparing": preparing, "percent": self.percent() if preparing else None,
                "message": self.message if self.status == "error" or preparing else ""}

    def _embed_sync(self, texts: List[str], kind: Kind) -> List[List[float]]:
        import numpy as np
        prefix = _E5_PREFIX[kind]
        pieces: list[str] = []
        owners: list[int] = []
        for i, t in enumerate(texts):
            t = t or " "
            chunks = [t[j:j + _CHUNK_CHARS] for j in range(0, len(t), _CHUNK_CHARS)][:_MAX_CHUNKS]
            pieces.extend(prefix + c for c in chunks)
            owners.extend([i] * len(chunks))
        vecs = np.array(list(self._model.embed(pieces, batch_size=16)), dtype=np.float32)
        out = []
        owners_arr = np.array(owners)
        for i in range(len(texts)):
            v = vecs[owners_arr == i].mean(axis=0)
            out.append((v / (np.linalg.norm(v) or 1.0)).tolist())
        return out

    async def embed(self, texts: List[str], kind: Kind) -> List[List[float]]:
        if self._model is None:
            if self.downloaded():
                await self.wait_ready()          # 받아 둔 모델을 여는 데 몇 초
            else:
                self.prepare()
                raise EmbeddingNotReady("내장 임베딩 모델을 받는 중입니다. 받은 뒤부터 대화가 검색에 저장됩니다.")
        return await asyncio.to_thread(self._embed_sync, texts, kind)


# ────────────────────────── ollama ──────────────────────────
# Qwen3-Embedding은 질의에만 지시문을 붙인다 (문서는 그대로) — 모델 카드 권장 형식
_QWEN_QUERY = "Instruct: Given a question, retrieve relevant past conversation turns that answer it\nQuery:"
_OLLAMA_BATCH = 32


class OllamaEmbedProvider:
    info = PROVIDERS["ollama"]
    model_id = "ollama/" + PROVIDERS["ollama"].model     # PULL_MANAGER 키 (local_models.py 다운로드 재사용)

    def problem(self) -> Optional[str]:
        from api import services
        if services.current_engine_version() is None:
            return "Ollama 엔진이 설치되지 않았습니다. 아래 '런타임 버전' 카드에서 Ollama 엔진을 먼저 설치하세요."
        return None

    async def _installed_size(self) -> Optional[int]:
        """설치돼 있으면 크기, 없으면 0, Ollama에 못 붙으면 None."""
        from api.llm_gateway.local_models import installed_ollama_models, installed_size
        installed = await installed_ollama_models()
        if installed is None:
            return None
        return installed_size(self.info.model, installed) or 0

    async def state(self) -> dict:
        from api.llm_gateway.local_models import PULL_MANAGER
        pull = PULL_MANAGER.get(self.model_id)
        pulling = pull is not None and pull.status == "pulling"
        size = await self._installed_size() if self.problem() is None else None
        message = ""
        if pulling:
            message = pull.message
        elif pull is not None and pull.status == "error":
            message = pull.message
        elif size is None and self.problem() is None:
            message = "Ollama 엔진이 응답하지 않습니다 (시작하는 중일 수 있습니다)."
        return {"downloaded": bool(size), "ready": bool(size) and not pulling, "sizeBytes": size or 0,
                "preparing": pulling, "percent": pull.percent() if pulling else None, "message": message}

    async def _start_pull(self) -> None:
        from api import services
        from api.llm_gateway.local_models import PULL_MANAGER
        if not await asyncio.to_thread(services.ensure_ollama):
            raise EmbeddingNotReady(services.ollama_unavailable_message())
        if await self._installed_size():
            return
        PULL_MANAGER.start(self.model_id)

    def prepare(self) -> None:
        if self.problem() is None:
            asyncio.get_running_loop().create_task(self._safe_start_pull())

    async def _safe_start_pull(self) -> None:
        try:
            await self._start_pull()
        except Exception as e:
            print(f"[embeddings] Ollama 임베딩 모델 받기 시작 실패: {_err_text(e)}")

    async def wait_ready(self) -> None:
        from api.llm_gateway.local_models import PULL_MANAGER
        if p := self.problem():
            raise EmbeddingNotReady(p)
        await self._start_pull()
        while (pull := PULL_MANAGER.get(self.model_id)) is not None and pull.status == "pulling":
            await asyncio.sleep(1.0)
        if not await self._installed_size():
            msg = pull.message if pull is not None and pull.status == "error" else "모델을 받지 못했습니다."
            raise EmbeddingNotReady(f"Ollama 임베딩 모델({self.info.model}): {msg}")

    async def embed(self, texts: List[str], kind: Kind) -> List[List[float]]:
        import httpx
        from api import services
        from api.llm_gateway.local_runtime import ollama_base_url
        if p := self.problem():
            raise EmbeddingNotReady(p)
        if not services.OLLAMA.healthy() and not await asyncio.to_thread(services.ensure_ollama):
            raise EmbeddingNotReady(services.ollama_unavailable_message())
        inputs = [(_QWEN_QUERY + t) if kind == "query" else (t or " ") for t in texts]
        out: List[List[float]] = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            for i in range(0, len(inputs), _OLLAMA_BATCH):
                resp = await client.post(f"{ollama_base_url()}/api/embed",
                                         json={"model": self.info.model, "input": inputs[i:i + _OLLAMA_BATCH]})
                if resp.status_code == 404:
                    raise EmbeddingNotReady(f"Ollama 임베딩 모델({self.info.model})을 아직 받지 않았습니다.")
                resp.raise_for_status()
                out.extend(resp.json()["embeddings"])
        return out


# ────────────────────────── 창구 ──────────────────────────
class EmbeddingService:
    """
    대화 저장·검색이 쓰는 임베딩 창구. provider를 주지 않으면 지금 고른 방식(.env EMBEDDING_PROVIDER)으로 보낸다.
    재인덱싱처럼 방식을 정해 두고 돌려야 하는 작업은 provider를 명시한다 (도중에 사용자가 바꿔도 섞이지 않게).
    """

    def __init__(self, cache_dir: Path) -> None:
        self.providers = {
            "local": FastEmbedProvider(cache_dir),
            "ollama": OllamaEmbedProvider(),
            "gemini": GeminiProvider(),
        }

    @property
    def active(self) -> str:
        v = (os.getenv(ENV_KEY) or DEFAULT_PROVIDER).strip().lower()
        return v if v in PROVIDERS else DEFAULT_PROVIDER

    def set_active(self, provider: str) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"알 수 없는 임베딩 방식: {provider}")
        from api.env_store import write_env_values
        write_env_values({ENV_KEY: provider},
                         comment="대화 기록 검색용 임베딩 방식 (local | ollama | gemini) — 설정 화면에서 바꾼다")

    def info(self, provider: Optional[str] = None) -> ProviderInfo:
        return PROVIDERS[provider or self.active]

    def collection(self, provider: Optional[str] = None) -> str:
        return self.info(provider).collection

    def provider(self, provider: Optional[str] = None):
        return self.providers[provider or self.active]

    async def aembed_documents(self, texts: List[str], provider: Optional[str] = None) -> List[List[float]]:
        return await self.provider(provider).embed(list(texts), "document")

    async def aembed_query(self, text: str, provider: Optional[str] = None) -> List[float]:
        return (await self.provider(provider).embed([text], "query"))[0]

    @staticmethod
    def info_dict(info: ProviderInfo) -> dict:
        return asdict(info)
