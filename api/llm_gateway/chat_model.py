"""
제공자별 LangChain 채팅 모델을 감싸, 앱 전체가 같은 형태의 입출력을 쓰도록 맞추는 래퍼.

그 자체가 BaseChatModel이므로 LangGraph 노드에서 ainvoke / bind_tools / with_structured_output을
그대로 쓸 수 있고, 콜백(on_llm_end)도 이 래퍼 기준으로 호출 1건당 한 번만 발생한다.

입력 보정
  - 시스템 메시지만 있는 프롬프트 → 마지막 지시문을 사용자 턴으로 전환 (Anthropic·Gemini는 사용자 턴이 없으면 400)
  - AI 메시지로 끝나는 프롬프트 → 사용자 턴 추가 (Gemini 3.x는 model 턴으로 끝나면 400)
출력 보정
  - content가 블록 리스트(Gemini·Anthropic)면 텍스트만 이어 붙인 문자열로 변환
    → 호출부는 항상 str을 받고, 다른 제공자로 모델을 바꿔도 대화 이력이 그대로 호환된다.
    도구 호출(tool_calls)과 Gemini thought signature(additional_kwargs)는 그대로 보존된다.
  - 로컬 추론 모델이 본문에 섞어 보내는 <think>...</think> 제거
  - response_metadata에 finish_reason(제공자별 키 통일)과 gateway_model 기록
  - 도구 호출 인자 중 자유 형식 객체가 JSON 문자열로 왔으면 객체로 복원 (tool_schema.py — Gemini)

비용 집계: 인스턴스 metadata의 gateway_model이 콜백(on_chat_model_start)으로 전달되어
CostTrackingCallback이 호출(run_id)마다 어떤 모델의 단가를 쓸지 안다.
"""
import re
from typing import Any, Optional, Sequence

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import ConfigDict

from api.llm_gateway.tool_schema import free_form_object_params, gemini_tool_specs, restore_json_object_args

# 보정용으로 덧붙이는 사용자 턴. 어느 제공자에도 무해한 지시문이라 분기 없이 적용한다.
TRAILING_USER_TURN = "위 내용을 바탕으로 시스템 지시에 따라 응답하세요."

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# 래퍼 안쪽 호출에는 콜백을 넘기지 않는다. 넘기면 같은 호출이 비용 집계에 두 번 잡힌다.
_NO_CALLBACKS = {"callbacks": []}


def prepare_messages(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    msgs = list(messages)
    if msgs and all(isinstance(m, SystemMessage) for m in msgs):
        # 지시문만 있는 프롬프트(요약·추출 등)는 마지막 지시문을 사용자 턴으로 옮긴다.
        # 보정 문구를 덧붙이면 일부 모델(Claude Opus 5.5)이 그 문구에 대한 언급을 답변에 섞는다.
        return msgs[:-1] + [HumanMessage(content=msgs[-1].content)]
    if msgs and isinstance(msgs[-1], AIMessage):
        msgs.append(HumanMessage(content=TRAILING_USER_TURN))
    return msgs


def content_to_text(content: Any) -> str:
    """문자열 또는 콘텐츠 블록 리스트에서 사용자에게 보일 텍스트만 추출한다 (thinking 블록 등 제외)."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _finish_reason(msg: AIMessage) -> str:
    # OpenAI Chat Completions·Gemini: finish_reason / Anthropic: stop_reason / Ollama: done_reason
    meta = msg.response_metadata or {}
    reason = meta.get("finish_reason") or meta.get("stop_reason") or meta.get("done_reason")
    if reason:
        return str(reason)
    # OpenAI Responses API(langchain-openai가 도구 바인딩 시 자동 전환)는 status로만 알려준다
    status = meta.get("status")
    if status == "completed":
        return "tool_calls" if msg.tool_calls else "stop"
    if status == "incomplete":
        detail = (meta.get("incomplete_details") or {}).get("reason")
        return "length" if detail == "max_output_tokens" else str(detail or "incomplete")
    return "unknown"


def normalize_output(msg: AIMessage, model_id: str,
                     object_params: Optional[dict[str, frozenset[str]]] = None) -> AIMessage:
    msg = restore_json_object_args(msg, object_params or {})
    text = _THINK_RE.sub("", content_to_text(msg.content))
    meta = {**(msg.response_metadata or {}),
            "finish_reason": _finish_reason(msg),
            "gateway_model": model_id}
    return msg.model_copy(update={"content": text, "response_metadata": meta})


class GatewayChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_id: str
    """'접두사/모델명' 형식의 게이트웨이 모델 ID (예: 'anthropic/claude-sonnet-5')"""
    base: BaseChatModel
    """제공자별 원본 채팅 모델"""
    runnable: Optional[Runnable] = None
    """도구가 바인딩된 원본 모델. None이면 base를 그대로 호출한다."""
    object_params: dict[str, frozenset[str]] = {}
    """바인딩한 도구별 자유 형식 객체 인자 이름 (응답에서 JSON 문자열을 객체로 복원할 대상)"""

    @property
    def _llm_type(self) -> str:
        return "llm-gateway"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_id": self.model_id}

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any) -> "GatewayChatModel":
        # with_structured_output이 넘기는 추적용 인자는 제공자 API로 새지 않도록 여기서 뗀다.
        kwargs.pop("ls_structured_output_format", None)
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        # Gemini는 자유 형식 객체 인자를 이해하지 못하므로 호환 스키마로 바꿔 넘긴다 (tool_schema.py)
        is_gemini = self.model_id.split("/", 1)[0] == "gemini"
        bound = self.base.bind_tools(gemini_tool_specs(tools) if is_gemini else tools, **kwargs)
        return self.model_copy(update={"runnable": bound, "object_params": free_form_object_params(tools)})

    def with_structured_output(self, schema: Any, *, include_raw: bool = False, **kwargs: Any) -> Runnable:
        """
        제공자별 with_structured_output을 그대로 사용한다. 기본 구현(도구 강제 호출)은
        tool_choice=any/tool을 지원하지 않는 모델(예: Claude Opus 5.5)에서 400이 나기 때문이다.
        (langchain-anthropic 등은 이런 모델을 알고 자동으로 우회 방식을 고른다.)
        입력 보정은 동일하게 적용하고, 비용 집계는 metadata의 gateway_model로 이어진다.
        """
        native = self.base.with_structured_output(schema, include_raw=include_raw, **kwargs)
        to_messages = RunnableLambda(lambda x: prepare_messages(self._convert_input(x).to_messages()))
        return (to_messages | native).with_config(metadata=self.metadata)

    def _target(self) -> Runnable:
        return self.runnable if self.runnable is not None else self.base

    def _wrap(self, msg: AIMessage) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(
            message=normalize_output(msg, self.model_id, self.object_params))])

    def _generate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        if stop is not None:
            kwargs["stop"] = stop
        return self._wrap(self._target().invoke(prepare_messages(messages), _NO_CALLBACKS, **kwargs))

    async def _agenerate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
                         run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
                         **kwargs: Any) -> ChatResult:
        if stop is not None:
            kwargs["stop"] = stop
        return self._wrap(await self._target().ainvoke(prepare_messages(messages), _NO_CALLBACKS, **kwargs))
