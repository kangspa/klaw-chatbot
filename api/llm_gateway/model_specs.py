"""
Ollama 모델의 다운로드 크기·예상 VRAM — 받기 전에도 마이 페이지에 보여 주기 위해.

  크기 : 레지스트리 manifest의 레이어 크기 합 (모델 가중치 + 비전 프로젝터 등)
         registry.ollama.ai/v2/<저장소>/manifests/<태그>  ·  hf.co 모델은 hf.co/v2/<저장소>/manifests/<태그>
  VRAM : 가중치 + KV 캐시(문맥 길이 num_ctx 기준) + 계산용 여유
         KV 캐시 계산에 필요한 구조 값(층 수·KV 헤드 수·헤드 크기)은 모델 파일(GGUF)의 앞부분 메타데이터만
         HTTP Range로 몇 MB 읽어 얻는다 (전체를 받지 않는다). 받은 모델은 Ollama /api/show 값을 쓴다.
         슬라이딩 윈도 등으로 실제 KV가 더 작은 모델도 있어 '최대 예상'으로 보여 준다.
결과는 .runtime/model_specs.json에 캐시한다 (모델 파일이 바뀌면 digest가 달라져 다시 계산).
"""
import asyncio
import json
import struct
from typing import Any, Optional

import httpx

from api import stack

CACHE_FILE = stack.RUNTIME_DIR / "model_specs.json"
GGUF_HEAD = 6 * 1024 * 1024        # 메타데이터를 찾으려고 읽는 앞부분 (구조 값은 보통 처음 수 KB 안에 있다)
OVERHEAD = 600 * 1024 * 1024       # 계산 그래프·런타임 여유 (대략)
MODEL_MEDIA = "application/vnd.ollama.image.model"
EXTRA_MEDIA = ("application/vnd.ollama.image.projector",)
_lock = asyncio.Lock()


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CACHE_FILE)


def _registry(name: str) -> tuple[str, str, str]:
    """Ollama 모델명 → (레지스트리 주소, 저장소, 태그)."""
    if name.startswith(("hf.co/", "huggingface.co/")):
        rest = name.split("/", 1)[1]
        repo, _, tag = rest.partition(":")
        return "https://huggingface.co", repo, tag or "latest"
    repo, _, tag = name.partition(":")
    if "/" not in repo:
        repo = f"library/{repo}"
    return "https://registry.ollama.ai", repo, tag or "latest"


# ────────────────────────── GGUF 메타데이터 ──────────────────────────
_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}


class _Short(Exception):
    pass


def _gguf_meta(buf: bytes, wanted_suffixes: tuple[str, ...]) -> dict[str, Any]:
    """GGUF 앞부분에서 키-값 메타데이터를 읽는다 (버퍼가 끝나면 거기까지)."""
    if buf[:4] != b"GGUF":
        return {}
    pos = 4 + 4 + 8   # magic, version, tensor_count
    (count,) = struct.unpack_from("<Q", buf, pos)
    pos += 8
    out: dict[str, Any] = {}

    def need(n):
        if pos + n > len(buf):
            raise _Short

    def read_str():
        nonlocal pos
        need(8)
        (n,) = struct.unpack_from("<Q", buf, pos)
        pos += 8
        need(n)
        s = buf[pos:pos + n].decode("utf-8", "replace")
        pos += n
        return s

    def read_val(t, keep=True):
        nonlocal pos
        if t in _SCALAR:
            fmt = _SCALAR[t]
            size = struct.calcsize(fmt)
            need(size)
            (v,) = struct.unpack_from(fmt, buf, pos)
            pos += size
            return v
        if t == 8:
            return read_str()
        if t == 9:
            need(12)
            et, n = struct.unpack_from("<IQ", buf, pos)
            pos += 12
            if et in _SCALAR and not keep:
                size = struct.calcsize(_SCALAR[et]) * n
                need(size)
                pos += size
                return None
            return [read_val(et, keep) for _ in range(n)]
        raise ValueError(f"알 수 없는 GGUF 형식 {t}")

    try:
        for _ in range(count):
            key = read_str()
            need(4)
            (t,) = struct.unpack_from("<I", buf, pos)
            pos += 4
            keep = key == "general.architecture" or key.endswith(wanted_suffixes)
            v = read_val(t, keep)
            if keep:
                out[key] = v
    except _Short:
        pass
    return out


_ARCH_KEYS = (".block_count", ".attention.head_count", ".attention.head_count_kv", ".attention.key_length",
              ".attention.value_length", ".embedding_length", ".attention.sliding_window", ".context_length",
              ".attention.sliding_window_pattern", ".attention.key_length_swa", ".attention.value_length_swa",
              ".full_attention_interval", ".ssm.inner_size", ".ssm.state_size", ".ssm.conv_kernel")


def _kv_bytes(meta: dict[str, Any], num_ctx: int) -> Optional[int]:
    """
    문맥 num_ctx에서의 KV 캐시 크기(f16). 모델 구조를 반영한다:
      - 슬라이딩 윈도 층(sliding_window_pattern): 창 크기만큼만 (예: Gemma 4 — 6층 중 5층이 1024토큰)
      - 혼합 구조(full_attention_interval): 전체 주의 층만 KV, 나머지 선형(SSM) 층은 작은 고정 상태 (예: Qwen 3.5/3.8)
    """
    arch = meta.get("general.architecture")
    if not arch:
        return None
    g = lambda k: meta.get(f"{arch}.{k}")   # noqa: E731
    layers = g("block_count")
    heads = g("attention.head_count")
    if not layers or not heads:
        return None
    kv = g("attention.head_count_kv") or heads
    kv_heads = kv if isinstance(kv, list) else [kv] * layers
    emb = g("embedding_length")
    head_dim = emb // (heads if isinstance(heads, int) else max(heads)) if emb else None
    k_len, v_len = g("attention.key_length") or head_dim, g("attention.value_length") or head_dim
    if not k_len or not v_len:
        return None
    k_swa, v_swa = g("attention.key_length_swa") or k_len, g("attention.value_length_swa") or v_len
    ctx = min(num_ctx, g("context_length") or num_ctx)
    window = g("attention.sliding_window")
    pattern = g("attention.sliding_window_pattern")
    interval = g("full_attention_interval")
    total, linear_layers = 0, 0
    for i in range(layers):
        if interval and (i + 1) % interval != 0:
            linear_layers += 1
            continue
        swa = bool(pattern[i]) if isinstance(pattern, list) and i < len(pattern) else False
        length = min(window, ctx) if (swa and window) else ctx
        k, v = (k_swa, v_swa) if swa else (k_len, v_len)
        total += kv_heads[i] * (k + v) * length * 2
    if linear_layers and g("ssm.inner_size") and g("ssm.state_size"):
        conv = g("ssm.conv_kernel") or 4
        total += linear_layers * g("ssm.inner_size") * (g("ssm.state_size") + conv) * 4   # 선형 층 상태 (f32)
    return int(total)


# ────────────────────────── 조회 ──────────────────────────
async def _remote(client: httpx.AsyncClient, name: str) -> dict:
    base, repo, tag = _registry(name)
    r = await client.get(f"{base}/v2/{repo}/manifests/{tag}", follow_redirects=True,
                         headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    r.raise_for_status()
    layers = r.json().get("layers", [])
    model = next((l for l in layers if l.get("mediaType") == MODEL_MEDIA), None)
    weights = sum(l.get("size", 0) for l in layers if l.get("mediaType") in (MODEL_MEDIA, *EXTRA_MEDIA))
    total = sum(l.get("size", 0) for l in layers)
    meta = {}
    if model:
        b = await client.get(f"{base}/v2/{repo}/blobs/{model['digest']}", headers={"Range": f"bytes=0-{GGUF_HEAD - 1}"},
                             follow_redirects=True)
        if b.status_code in (200, 206):
            meta = _gguf_meta(b.content[:GGUF_HEAD], _ARCH_KEYS)
    return {"digest": model["digest"] if model else None, "downloadBytes": total, "weightsBytes": weights, "meta": meta}


async def _installed(client: httpx.AsyncClient, base_url: str, name: str) -> Optional[dict]:
    r = await client.post(f"{base_url}/api/show", json={"model": name})
    if r.status_code != 200:
        return None
    info = r.json().get("model_info") or {}
    return {k: v for k, v in info.items() if k == "general.architecture" or k.endswith(_ARCH_KEYS)}


async def specs_for(names: list[str], num_ctx: int, installed_sizes: dict[str, int], base_url: Optional[str]) -> dict[str, dict]:
    """{이름: {downloadBytes, vramBytes, kvBytes, numCtx}} — 실패한 모델은 빠진다."""
    async with _lock:
        cache = _load_cache()
        out: dict[str, dict] = {}
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "klaw-chatbot"}) as client:
            async def one(name: str):
                entry = cache.get(name)
                try:
                    if entry is None:
                        entry = await _remote(client, name)
                        if not entry["meta"] and base_url and name in installed_sizes:
                            entry["meta"] = await _installed(client, base_url, name) or {}
                        cache[name] = entry
                except Exception as e:
                    print(f"[model_specs] {name} 정보를 가져오지 못했습니다: {type(e).__name__}: {e}")
                    if name not in installed_sizes:
                        return
                    entry = {"downloadBytes": installed_sizes[name], "weightsBytes": installed_sizes[name], "meta": {}}
                weights = entry.get("weightsBytes") or installed_sizes.get(name) or 0
                kv = _kv_bytes(entry.get("meta") or {}, num_ctx)
                out[name] = {
                    "downloadBytes": installed_sizes.get(name) or entry.get("downloadBytes"),
                    "kvBytes": kv,
                    "vramBytes": (weights + (kv or 0) + OVERHEAD) if weights else None,
                    "numCtx": num_ctx,
                }
            await asyncio.gather(*(one(n) for n in names))
        _save_cache(cache)
        return out


def gpu_vram_bytes() -> Optional[int]:
    """Ollama가 감지한 GPU 메모리 중 가장 큰 것 (엔진 로그 기준, 없으면 None)."""
    from api.llm_gateway import local_runtime as lr
    d = lr.read_discovery(lr.ollama_log_text())
    best = None
    for dev in (d or {}).get("devices", []):
        if dev.get("library") not in ("CUDA", "ROCm", "Vulkan", "Metal"):
            continue
        txt = str(dev.get("total", "")).replace(" ", "")
        try:
            num = float("".join(c for c in txt if c.isdigit() or c == "."))
        except ValueError:
            continue
        mult = 1024 ** 3 if "GiB" in txt else 1e9 if "GB" in txt else 1024 ** 2 if "MiB" in txt else 1
        best = max(best or 0, int(num * mult))
    return best
