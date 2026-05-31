from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional, Any
from langchain_core.messages import HumanMessage, AIMessage

# CHATBOT_AGENT는 main.py의 lifespan startup에서 compile되어
# request.app.state.chatbot_agent에 저장된다 (AsyncPostgresSaver가 이벤트 루프 필요).
from langchain_core.callbacks import BaseCallbackHandler

import threading
import traceback
import litellm
from langchain_core.outputs import LLMResult, ChatGeneration


class LiteLLMCostCallback(BaseCallbackHandler):
    """
    LiteLLM 내장 가격표(litellm.cost_per_token)를 사용해 호출별 비용을 누적.
    OpenAI / Anthropic / Gemini / Ollama / HuggingFace 등 LiteLLM이 지원하는
    모든 모델의 비용을 자동 계산 (Gemini 200K 임계 등 티어 가격도 LiteLLM 측에서 처리).

    각 LLM 호출이 끝날 때 AIMessage.usage_metadata에서 토큰 수,
    response_metadata에서 모델명을 추출해 비용으로 환산.
    """

    def __init__(self, default_model: Optional[str] = None) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.default_model = default_model  # response_metadata에 model이 없을 때 폴백
        self.total_input = 0
        self.total_output = 0
        self.total_cost = 0.0
        self.per_call: list[dict[str, Any]] = []

    @staticmethod
    def _resolve_model(msg: AIMessage, fallback: Optional[str]) -> Optional[str]:
        # ChatLiteLLM은 response_metadata에 'model_name' 또는 'model'을 넣음 (구현/버전 차이)
        meta = msg.response_metadata or {}
        return meta.get("model_name") or meta.get("model") or fallback

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        try:
            generation = response.generations[0][0]
        except IndexError:
            return
        if not isinstance(generation, ChatGeneration):
            return
        msg = generation.message
        if not isinstance(msg, AIMessage) or not msg.usage_metadata:
            return

        input_tokens = msg.usage_metadata.get("input_tokens", 0)
        output_tokens = msg.usage_metadata.get("output_tokens", 0)
        model_name = self._resolve_model(msg, self.default_model) or "unknown"

        cost = 0.0
        try:
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=model_name,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
            )
            cost = float(prompt_cost) + float(completion_cost)
        except Exception as e:
            # 모델이 LiteLLM 가격 DB에 없거나 모델명 형식이 달라 매칭 실패한 경우
            print(f"[cost] '{model_name}' 비용 계산 실패: {e}")

        with self._lock:
            self.total_input += input_tokens
            self.total_output += output_tokens
            self.total_cost += cost
            self.per_call.append({
                "model": model_name,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
            })

router = APIRouter()

class PlanChatRequest(BaseModel):
    userId: str = Field(..., description="사용자 ID")
    projectId: str = Field(..., description="프로젝트 ID (thread_id로 사용됨)")
    isotime: str = Field(..., description="클라이언트가 보낸 오프셋(Offset)이 포함된 ISO 8601 형식의 시간 문자열 (형식:YYYY-MM-DDTHH:mm:ss+HH:mm) (예시:2026-04-07T23:16:04+09:00)")
    message: str = Field(..., description="사용자 입력 메시지")
    model: Optional[str] = Field(
        default="gemini/gemini-2.5-flash-lite",
        description=(
            "사용할 AI 모델 명. LiteLLM 표준 prefix 형식을 사용합니다. "
            "예: 'gemini/gemini-2.5-flash-lite', 'openai/gpt-4o-mini', "
            "'anthropic/claude-sonnet-4-5', 'ollama/llama3.1', "
            "'huggingface/meta-llama/Meta-Llama-3-8B-Instruct'"
        ),
    )

class PlanChatResponse(BaseModel):
    response: str = Field(..., description="AI 응답 메시지")

@router.post("/chat", response_model=PlanChatResponse)
async def post_plan_chat(data: PlanChatRequest, request: Request):
    """
    영상 기획 단계(Plan)에서 사용자와 대화를 나누며 기획 포맷을 채우기 위한 End-point
    """
    # lifespan에서 compile된 에이전트를 app.state에서 꺼내 사용
    chatbot_agent = request.app.state.chatbot_agent

    # thread_id 구성 (사용자별, 프로젝트별로 상태가 유지되도록 함)
    # 사용자가 언급한 "프로젝트와 일치하는 thread_id"를 그대로 사용하거나 조합해서 사용
    config = {
        "configurable": {
            "user_id": data.userId,
            "thread_id": data.projectId,
            "isotime": data.isotime,
            "model": data.model,
        }
    }

    # 사용자 입력을 HumanMessage 형식으로 변환
    input_message = HumanMessage(content=data.message)

    try:
        # CHATBOT_AGENT를 통해 대화 진행
        # compile 시 checkpointer가 설정되어 있어 thread_id를 기반으로 이전 상태를 자동으로 불러옴
        cb = LiteLLMCostCallback(default_model=data.model)
        result = await chatbot_agent.ainvoke(
            {"messages": [input_message]},
            # 기존 configurable 유지 + callbacks 추가
            config={**config, "callbacks": [cb]},
        )
        print(f"Per-call breakdown: {cb.per_call}")
        print(f"Total Input: {cb.total_input} / Output: {cb.total_output}")
        print(f"Estimated cost: ${cb.total_cost:.6f}")

        # 결과에서 마지막 AI 메시지 추출
        messages = result.get("messages", [])
        last_ai_message = ""
        if messages:
            for msg in reversed(messages):
                if isinstance(msg, AIMessage):
                    last_ai_message = msg.content
                    break

        return PlanChatResponse(
            response=last_ai_message,
        )

    except Exception as e:
        # 일부 MCP/RPC 예외는 str(e)가 비어있어 단순 print로는 원인 추적이 불가능
        # → 예외 타입과 전체 traceback을 함께 로깅
        err_type = type(e).__name__
        err_repr = repr(e)
        tb_str = traceback.format_exc()
        print(f"PLAN Chat Error [{err_type}]: {err_repr}")
        print(tb_str)
        raise HTTPException(
            status_code=500,
            detail=f"에이전트 호출 중 오류가 발생했습니다: [{err_type}] {err_repr}"
        )

@router.get("/{projectId}/history")
async def get_draft_history(projectId: str, request: Request):
    """
    특정 프로젝트의 대화 기록을 조회하는 End-point
    """
    chatbot_agent = request.app.state.chatbot_agent
    config = {"configurable": {"thread_id": projectId}}

    try:
        # 현재 상태 조회 (AsyncPostgresSaver는 aget_state로 비동기 호출)
        state = await chatbot_agent.aget_state(config)
        if not state or not state.values:
            return { "messages": [] }
        
        values = state.values
        messages = []
        for msg in values.get("messages", []):
            role = "user" if isinstance(msg, HumanMessage) else "assistant"
            messages.append({"role": role, "content": msg.content})
            
        return { "messages": messages }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
