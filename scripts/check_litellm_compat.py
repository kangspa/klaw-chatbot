"""
LiteLLM 버전 호환성 점검 (마이 페이지 '런타임 버전'이 LiteLLM을 바꾸기 전에 실행한다).

프로젝트 패키지를 import하지 않으므로, 프로젝트 가상환경이 아닌 별도 환경에서도 돌릴 수 있다:
    uv run --isolated --no-project --with litellm==X --with mcp==<현재> --with langchain-litellm==<현재> \
        python scripts/check_litellm_compat.py gemini/gemini-3.5-flash-lite openai/gpt-6-luna ...

확인하는 것 (네트워크 호출 없음):
  1. 도구 호출 경로 — litellm 1.106+는 도구를 붙이면 mcp 2.3 전용 심볼을 import해 실패했다 (mcp<2 환경에서)
  2. mock 응답으로 tools를 붙인 completion 1회
  3. langchain_litellm.ChatLiteLLM 생성과 bind_tools
  4. 인자로 받은 모델들의 토큰 단가 계산 (설치된 버전에 내장된 가격표 기준)

마지막 줄에 'COMPAT_RESULT: {json}'을 출력한다. ok가 false면 바꾸면 안 되는 버전이다.
"""
import json
import os
import sys
import traceback

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"   # 원격 가격표가 아니라 이 버전에 내장된 가격표로 확인

# 우리 접두사 → LiteLLM 접두사 (api/llm_gateway/providers.py와 같게 유지)
_PREFIX = {"gemini": "gemini", "openai": "openai", "anthropic": "anthropic", "ollama": "ollama_chat",
           "ollama_chat": "ollama_chat", "lmstudio": "lm_studio", "vllm": "hosted_vllm", "huggingface": "huggingface"}
_LOCAL = {"ollama", "ollama_chat", "lmstudio", "vllm"}
# Hugging Face Inference Providers 모델은 LiteLLM 가격표에 원래 없다 (비용 0 집계) — 단가 확인에서 뺀다
_NO_PRICE = {"huggingface"}


def main() -> int:
    result: dict = {"ok": False, "version": None, "checks": {}, "errors": [], "missingPrices": []}
    try:
        import importlib
        import importlib.metadata as md

        import litellm
        result["version"] = md.version("litellm")

        # 1) 도구 호출 경로 (모듈이 없는 버전은 그 경로를 쓰지 않으므로 통과)
        try:
            importlib.import_module("litellm.responses.mcp.chat_completions_handler")
            result["checks"]["toolPath"] = True
        except ModuleNotFoundError as e:
            result["checks"]["toolPath"] = e.name.startswith("litellm.responses")
            if not result["checks"]["toolPath"]:
                result["errors"].append(f"도구 호출 경로: {type(e).__name__}: {e}")
        except Exception as e:
            result["checks"]["toolPath"] = False
            result["errors"].append(f"도구 호출 경로: {type(e).__name__}: {e}")

        # 2) tools를 붙인 completion (mock 응답 — 네트워크·키 불필요)
        tool = {"type": "function", "function": {"name": "search_law", "description": "법령 검색",
                                                 "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                                                                "required": ["query"]}}}
        try:
            litellm.completion(model="openai/gpt-6-luna", messages=[{"role": "user", "content": "hi"}],
                               tools=[tool], mock_response="ok")
            result["checks"]["mockCompletion"] = True
        except Exception as e:
            result["checks"]["mockCompletion"] = False
            result["errors"].append(f"도구 포함 completion: {type(e).__name__}: {e}")

        # 3) LangChain 연동
        try:
            from langchain_core.tools import tool as lc_tool
            from langchain_litellm import ChatLiteLLM

            @lc_tool
            def search_law(query: str) -> str:
                """법령 검색"""
                return query

            ChatLiteLLM(model="openai/gpt-6-luna", api_key="dummy").bind_tools([search_law])
            result["checks"]["langchain"] = True
        except Exception as e:
            result["checks"]["langchain"] = False
            result["errors"].append(f"langchain-litellm: {type(e).__name__}: {e}")

        # 4) 등록 모델 단가 (로컬 모델은 0원으로 처리하므로 제외)
        for model_id in sys.argv[1:]:
            provider, _, name = model_id.partition("/")
            if provider in _LOCAL or provider in _NO_PRICE or provider not in _PREFIX:
                continue
            try:
                litellm.cost_per_token(model=f"{_PREFIX[provider]}/{name}", prompt_tokens=1000, completion_tokens=100)
            except Exception:
                result["missingPrices"].append(model_id)
        result["checks"]["prices"] = not result["missingPrices"]

        result["ok"] = all(result["checks"].values())
    except Exception as e:
        result["errors"].append(f"{type(e).__name__}: {e}")
        result["errors"].append(traceback.format_exc(limit=3))
    print("COMPAT_RESULT: " + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
