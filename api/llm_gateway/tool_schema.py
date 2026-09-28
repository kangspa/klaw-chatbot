"""
제공자별 도구 스키마 호환 처리.

문제: Gemini 함수 선언은 JSON Schema의 일부(OpenAPI 3.0 부분집합)만 받는다.
langchain-google-genai는 변환 중 지원하지 않는 키를 버리며 "Key '...' is not supported" 경고를 남기는데,
그중 additionalProperties가 버려지면 '어떤 키든 받는 자유 형식 객체' 인자가 속성 없는 빈 객체가 된다.
이때 Gemini는 그 인자를 객체가 아니라 JSON 문자열('{"a": 1}')로 채우고, MCP 서버(zod)는
"expected record, received string"으로 거부한다.
  예) korean-law-mcp의 execute_tool.params, search_decisions.options, get_decision_text.options

해결:
  - Gemini에는 자유 형식 객체 인자를 '문자열(JSON 객체)'로 명시한 스키마를 넘기고,
    지원하지 않는 additionalProperties는 미리 제거한다 (→ 변환 경고도 사라진다).
  - 응답의 도구 호출 인자 중 이 인자가 문자열이면 객체로 되돌려 도구에 넘긴다.
  - 다른 제공자는 원래 스키마를 그대로 받으므로 건드리지 않는다.
"""
import copy
import json
from typing import Any, Sequence

from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

_JSON_STRING_HINT = 'JSON 객체를 문자열로 작성하세요 (예: {"key": "value"}).'


def _is_free_form_object(schema: dict) -> bool:
    return schema.get("type") == "object" and not schema.get("properties")


def _strip_additional_properties(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_additional_properties(v) for k, v in node.items() if k != "additionalProperties"}
    if isinstance(node, list):
        return [_strip_additional_properties(v) for v in node]
    return node


def free_form_object_params(tools: Sequence[Any]) -> dict[str, frozenset[str]]:
    """도구별로 '자유 형식 객체' 최상위 인자 이름을 모은다."""
    out: dict[str, frozenset[str]] = {}
    for tool in tools:
        fn = convert_to_openai_tool(tool)["function"]
        props = (fn.get("parameters") or {}).get("properties") or {}
        names = frozenset(n for n, p in props.items() if isinstance(p, dict) and _is_free_form_object(p))
        if names:
            out[fn["name"]] = names
    return out


def gemini_tool_specs(tools: Sequence[Any]) -> list[dict]:
    """Gemini가 온전히 이해할 수 있는 OpenAI 형식 도구 스키마로 변환한다."""
    specs = []
    for tool in tools:
        spec = copy.deepcopy(convert_to_openai_tool(tool))
        params = spec["function"].get("parameters") or {}
        for name, prop in (params.get("properties") or {}).items():
            if isinstance(prop, dict) and _is_free_form_object(prop):
                desc = (prop.get("description") or "").strip()
                params["properties"][name] = {"type": "string", "description": f"{desc} {_JSON_STRING_HINT}".strip()}
        spec["function"]["parameters"] = _strip_additional_properties(params)
        specs.append(spec)
    return specs


def restore_json_object_args(msg: AIMessage, object_params: dict[str, frozenset[str]]) -> AIMessage:
    """도구 호출 인자 중 자유 형식 객체 인자가 JSON 문자열로 왔으면 객체로 되돌린다."""
    if not object_params or not msg.tool_calls:
        return msg
    changed = False
    calls = []
    for call in msg.tool_calls:
        names = object_params.get(call.get("name"), ())
        args = dict(call.get("args") or {})
        for n in names:
            value = args.get(n)
            if isinstance(value, str):
                try:
                    parsed = json.loads(value) if value.strip() else {}
                except json.JSONDecodeError:
                    continue   # 고치지 못하면 그대로 둔다 → 도구가 형식 오류를 돌려주고 모델이 다시 시도
                if isinstance(parsed, dict):
                    args[n] = parsed
                    changed = True
        calls.append({**call, "args": args})
    return msg.model_copy(update={"tool_calls": calls}) if changed else msg
