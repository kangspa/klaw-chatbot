"""
LiteLLM 채팅 모델(ChatLiteLLM)을 감싸, 앱 전체가 같은 형태의 입출력을 쓰도록 맞추는 래퍼.

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

비용 집계: 인스턴스 metadata의 gateway_model이 콜백(on_chat_model_start)으로 전달되어
CostTrackingCallback이 호출(run_id)마다 어떤 모델의 단가를 쓸지 안다.
"""
import re
from typing import Any, AsyncIterator, Optional, Sequence

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import ConfigDict

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


def normalize_output(msg: AIMessage, model_id: str) -> AIMessage:
    text = _THINK_RE.sub("", content_to_text(msg.content))
    meta = {**(msg.response_metadata or {}),
            "finish_reason": _finish_reason(msg),
            "gateway_model": model_id}
    return msg.model_copy(update={"content": text, "response_metadata": meta})


class _ThinkFilter:
    """스트리밍 중 <think>…</think> 구간을 걸러낸다 (태그가 조각 경계에 걸쳐 와도 처리)."""

    def __init__(self) -> None:
        self.inside = False
        self.buf = ""

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while self.buf:
            tag = "</think>" if self.inside else "<think>"
            i = self.buf.find(tag)
            if i >= 0:
                if not self.inside:
                    out.append(self.buf[:i])
                self.buf = self.buf[i + len(tag):]
                if self.inside:
                    self.buf = self.buf.lstrip()
                self.inside = not self.inside
                continue
            # 태그 앞부분일 수 있는 꼬리는 다음 조각까지 남겨 둔다
            keep = next((k for k in range(min(len(tag) - 1, len(self.buf)), 0, -1) if tag.startswith(self.buf[-k:])), 0)
            if not self.inside:
                out.append(self.buf[:len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:] if keep else ""
            break
        return "".join(out)

    def flush(self) -> str:
        rest, self.buf = ("" if self.inside else self.buf), ""
        return rest


class GatewayChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_id: str
    """'접두사/모델명' 형식의 게이트웨이 모델 ID (예: 'anthropic/claude-sonnet-5')"""
    base: BaseChatModel
    """제공자별 원본 채팅 모델"""
    runnable: Optional[Runnable] = None
    """도구가 바인딩된 원본 모델. None이면 base를 그대로 호출한다."""

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
        return self.model_copy(update={"runnable": self.base.bind_tools(tools, **kwargs)})

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
        return ChatResult(generations=[ChatGeneration(message=normalize_output(msg, self.model_id))])

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

    async def _astream(self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
                       run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
                       **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        """
        토큰 스트리밍 (화면에 답변을 흘려 보여 주기 위해 — astream_events로 실행할 때 쓰인다).
        _agenerate와 같은 보정을 한다: 입력 정리, <think> 구간 제거, 마지막 조각에 finish_reason·gateway_model.
        사용량(usage_metadata)은 LiteLLM이 마지막 조각에 실어 보내 합쳐지므로 비용 집계도 그대로 된다.
        """
        if stop is not None:
            kwargs["stop"] = stop
        think = _ThinkFilter()
        reason = None
        async for chunk in self._target().astream(prepare_messages(messages), _NO_CALLBACKS, **kwargs):
            if not isinstance(chunk, AIMessageChunk):
                continue
            meta = chunk.response_metadata or {}
            reason = meta.get("finish_reason") or meta.get("stop_reason") or meta.get("done_reason") or reason
            text = think.feed(content_to_text(chunk.content))
            # response_metadata는 조각을 합칠 때 문자열끼리 이어 붙여지므로(finish_reason → 'stopstop') 마지막 조각에만 싣는다
            out = chunk.model_copy(update={"content": text, "response_metadata": {}})
            # on_llm_new_token(토큰 이벤트)은 BaseChatModel이 yield된 조각마다 스스로 보낸다 —
            # 여기서도 보내면 화면에 글자가 두 번씩 나온다 ('사사용용자자')
            yield ChatGenerationChunk(message=out)
        tail = think.flush()
        final = AIMessageChunk(content=tail, response_metadata={"finish_reason": reason or "stop", "gateway_model": self.model_id})
        yield ChatGenerationChunk(message=final)
