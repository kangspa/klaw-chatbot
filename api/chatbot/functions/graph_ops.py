"""
LangGraph 체크포인트(thread state) 조작 유틸.

Postgres(사용자에게 보여줄 원문)와 Qdrant(의미 검색용 벡터)를 수정할 때,
실행 중인 대화 상태도 함께 맞춰주지 않으면 다음 턴에서 이미 지운 메시지가
다시 LLM 프롬프트에 들어가게 된다. 이 모듈이 그 정합성을 담당한다.

주의: 현재 체크포인터는 MemorySaver(휘발성)이다. 서버를 재시작하면 state가 비므로
      아래 함수들은 "state가 없으면 조용히 통과"하도록 작성되어 있다.
      Postgres/Qdrant 쪽 조작은 그와 무관하게 항상 수행된다.
"""

from typing import Any, Optional

from langchain_core.messages import RemoveMessage

# 요약 관련 파생 채널. 롤백 시 이전 체크포인트 값으로 되돌린다.
# entity_memory / tool_counts는 merge_dict 리듀서라 '되돌리기'가 성립하지 않으므로 제외한다.
# (엔티티는 remove_entity 노드의 망각 로직이 시간이 지나며 자연스럽게 정리한다)
DERIVED_MEMORY_KEYS = (
    "turn_memory",
    "check_turn",
    "short_memory",
    "check_short",
    "long_memory",
)


def _cfg(thread_id: str, user_id: Optional[str] = None) -> dict[str, Any]:
    configurable: dict[str, Any] = {"thread_id": thread_id}
    if user_id:
        configurable["user_id"] = user_id
    return {"configurable": configurable}


async def get_state_values(agent, thread_id: str) -> dict[str, Any]:
    """thread의 현재 state values. 없으면 빈 dict."""
    try:
        state = await agent.aget_state(_cfg(thread_id))
    except Exception as e:
        print(f"[graph] state 조회 실패(무시): {type(e).__name__}: {e}")
        return {}
    if not state or not state.values:
        return {}
    return dict(state.values)


def _index_of(messages: list, graph_message_id: Optional[str]) -> Optional[int]:
    if not graph_message_id:
        return None
    for i, m in enumerate(messages):
        if getattr(m, "id", None) == graph_message_id:
            return i
    return None


def _trim_turns(turns: list, cut: int) -> list:
    """messages[:cut]만 남길 때 유효한 turn 인덱스 쌍만 유지."""
    return [t for t in (turns or []) if t and max(t) < cut]


async def _restore_derived_memory(agent, thread_id: str, cut_index: int) -> dict[str, Any]:
    """
    체크포인트 히스토리를 거슬러 올라가, cut_index 지점의 메시지가 아직 없던 시점의
    요약/엔티티 채널 값을 찾아 반환한다. (턴을 되돌릴 때 요약이 중복 누적되는 것을 방지)
    실패하면 빈 dict → 호출부는 파생 채널을 건드리지 않는다.
    """
    try:
        async for snap in agent.aget_state_history(_cfg(thread_id)):
            values = snap.values or {}
            if len(values.get("messages", [])) <= cut_index:
                return {k: values[k] for k in DERIVED_MEMORY_KEYS if k in values}
    except Exception as e:
        print(f"[graph] 파생 메모리 롤백 skip: {type(e).__name__}: {e}")
    return {}


async def truncate_from(
    agent,
    thread_id: str,
    from_graph_message_id: Optional[str],
    user_id: Optional[str] = None,
) -> bool:
    """
    state의 메시지 목록에서 from_graph_message_id 이후(자기 자신 포함)를 모두 제거한다.
    삭제 / 재생성 두 경우 모두 이 함수를 쓴다. (중간의 ToolMessage / tool_call AIMessage도 함께 제거)

    반환값: 실제로 state를 수정했는지 여부.
    """
    values = await get_state_values(agent, thread_id)
    messages = values.get("messages", [])
    if not messages:
        return False

    idx = _index_of(messages, from_graph_message_id)
    if idx is None:
        # MemorySaver가 이미 오래된 메시지를 정리했거나 서버가 재시작된 경우 → 조작할 것이 없음
        print(f"[graph] thread '{thread_id}' state에서 메시지 '{from_graph_message_id}'를 찾지 못함 (skip)")
        return False

    removals = [RemoveMessage(id=m.id) for m in messages[idx:] if getattr(m, "id", None)]
    old_turns = values.get("conversation_turns", []) or []
    new_turns = _trim_turns(old_turns, idx)
    dropped = max(0, len(old_turns) - len(new_turns))

    update: dict[str, Any] = {
        "messages": removals,
        "conversation_turns": new_turns,
        "current_turn": max(0, values.get("current_turn", 0) - dropped),
    }
    update.update(await _restore_derived_memory(agent, thread_id, idx))

    try:
        await agent.aupdate_state(_cfg(thread_id, user_id), update)
        print(f"[graph] thread '{thread_id}': 메시지 {len(removals)}개 제거, 턴 {dropped}개 롤백")
        return True
    except Exception as e:
        print(f"[graph] state 수정 실패(무시): {type(e).__name__}: {e}")
        return False


async def fork_thread(
    agent,
    src_thread_id: str,
    dst_thread_id: str,
    upto_graph_message_id: Optional[str],
    user_id: Optional[str] = None,
) -> bool:
    """
    브랜치 생성: src thread의 state를 upto_graph_message_id(포함)까지 잘라 dst thread로 복사한다.

    - 메시지 객체를 그대로 복사하므로 id가 보존된다 → Postgres의 graph_message_id가 그대로 유효.
    - dst thread는 비어 있으므로 add_messages 리듀서가 순서대로 append 한다.
    - 요약/엔티티 등 파생 메모리도 함께 복사해 브랜치가 같은 맥락에서 이어지도록 한다.
    """
    values = await get_state_values(agent, src_thread_id)
    messages = values.get("messages", [])
    if not messages:
        print(f"[graph] thread '{src_thread_id}'에 복사할 state가 없습니다 (skip)")
        return False

    idx = _index_of(messages, upto_graph_message_id)
    cut = len(messages) if idx is None else idx + 1
    kept = messages[:cut]
    new_turns = _trim_turns(values.get("conversation_turns", []), cut)

    forked = {k: v for k, v in values.items() if k != "messages"}
    forked["messages"] = kept
    forked["conversation_turns"] = new_turns
    forked["current_turn"] = max(0, values.get("current_turn", 0) - (
        len(values.get("conversation_turns", []) or []) - len(new_turns)
    ))

    try:
        await agent.aupdate_state(_cfg(dst_thread_id, user_id), forked)
        print(f"[graph] '{src_thread_id}' → '{dst_thread_id}' 브랜치 복사 완료 (메시지 {len(kept)}개)")
        return True
    except Exception as e:
        print(f"[graph] 브랜치 state 복사 실패(무시): {type(e).__name__}: {e}")
        return False


async def clear_thread(agent, thread_id: str) -> None:
    """세션 삭제 시 state의 메시지를 모두 비운다 (MemorySaver에는 thread 삭제 API가 없음)."""
    values = await get_state_values(agent, thread_id)
    messages = values.get("messages", [])
    if not messages:
        return
    removals = [RemoveMessage(id=m.id) for m in messages if getattr(m, "id", None)]
    try:
        await agent.aupdate_state(_cfg(thread_id), {
            "messages": removals,
            "conversation_turns": [],
            "current_turn": 0,
            "turn_memory": [],
            "check_turn": 0,
            "short_memory": [],
            "check_short": 0,
            "long_memory": "",
        })
        print(f"[graph] thread '{thread_id}' state 초기화")
    except Exception as e:
        print(f"[graph] state 초기화 실패(무시): {type(e).__name__}: {e}")
