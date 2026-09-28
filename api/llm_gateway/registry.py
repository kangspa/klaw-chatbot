"""
DB에 등록된 모델 목록을 메모리에 캐시하는 레지스트리.

API로 전달받은 모델 ID는 여기서 확인한다 — 등록되지 않았거나 비활성인 모델은 거부하고,
모델이 없으면 기본 모델(is_default)을 쓴다. 캐시는 ttl초마다 DB에서 다시 읽으므로
DB에서 모델·단가를 바꾸면 서버 재시작 없이 반영된다.
"""
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from api.llm_gateway.pricing import PriceRow


class ModelNotAvailableError(ValueError):
    """등록되지 않았거나 비활성화된 모델을 요청한 경우."""


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    group: str
    sort_order: int = 0
    enabled: bool = True
    is_default: bool = False
    fixed_temperature: bool = False
    options: dict = field(default_factory=dict)
    prices: tuple[PriceRow, ...] = ()

    @property
    def build_version(self) -> str:
        """모델 인스턴스 생성에 영향을 주는 설정의 지문. 바뀌면 캐시된 인스턴스를 새로 만든다."""
        raw = json.dumps([self.fixed_temperature, self.options], sort_keys=True, default=str)
        return hashlib.sha1(raw.encode()).hexdigest()[:12]


class ModelRegistry:
    def __init__(self, loader: Callable[[], Awaitable[list[ModelSpec]]], ttl: float = 30.0):
        self._loader = loader
        self._ttl = ttl
        self._models: dict[str, ModelSpec] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._loaded_at < self._ttl:
            return
        async with self._lock:
            if not force and time.monotonic() - self._loaded_at < self._ttl:
                return
            try:
                specs = await self._loader()
            except Exception as e:
                # DB 일시 장애 시 직전 목록으로 계속 동작한다 (처음부터 비어 있으면 호출이 거부됨)
                print(f"[llm_gateway] 모델 목록 갱신 실패(직전 목록 유지): {type(e).__name__}: {e}")
                self._loaded_at = time.monotonic()
                return
            self._models = {s.id: s for s in specs}
            self._loaded_at = time.monotonic()

    def cached(self, model_id: str) -> Optional[ModelSpec]:
        """DB 조회 없이 캐시에서만 찾는다 (동기 콜백용 — 비용 집계)."""
        return self._models.get(model_id)

    async def resolve(self, model_id: Optional[str]) -> ModelSpec:
        """요청 모델 ID를 확인해 사양을 돌려준다. None/빈 값이면 기본 모델."""
        await self.refresh()
        if not model_id:
            default = next((s for s in self._models.values() if s.is_default and s.enabled), None)
            if default is None:
                raise ModelNotAvailableError("기본 모델이 설정되어 있지 않습니다 (llm_models.is_default).")
            return default
        spec = self._models.get(model_id)
        if spec is None:
            raise ModelNotAvailableError(f"등록되지 않은 모델입니다: {model_id}")
        if not spec.enabled:
            raise ModelNotAvailableError(f"비활성화된 모델입니다: {model_id}")
        return spec

    async def list_enabled(self) -> list[ModelSpec]:
        await self.refresh()
        return sorted((s for s in self._models.values() if s.enabled), key=lambda s: (s.sort_order, s.id))
