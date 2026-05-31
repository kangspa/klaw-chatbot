from api.load_variable import MODEL_MANAGER, AgentState, SEARX_WRAPPER, GEMINI_EMBEDDINGS, QDRANT_DB
from pydantic import BaseModel, Field
from typing import Optional, List, Annotated
from langgraph.prebuilt import InjectedState
from langchain_core.tools import InjectedToolCallId, tool

from langchain_core.runnables import RunnableConfig
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_community.tools.searx_search.tool import SearxSearchResults
from langgraph.types import Command
from crawl4ai import AsyncWebCrawler
from qdrant_client.http import models as rest

search_tool = SearxSearchResults(
    name="web_search", # 이름을 일반 검색으로 변경
    description="실시간 뉴스, 날씨, 트렌드, 기업 정보 등 모든 웹 정보를 검색할 때 사용합니다.",
    wrapper=SEARX_WRAPPER,
    num_results=5
)
@tool
async def web_search(query: str):
    """실시간 뉴스, 날씨, 트렌드 등 모든 웹 정보를 검색할 때 사용합니다."""
    # .arun() 메서드를 사용하면 비동기로 실행됩니다.
    return await search_tool.arun(query)

@tool
async def page_crawl4ai(url: str) -> str:
    """특정 웹페이지의 상세 내용을 크롤링합니다. `web_search` 툴을 통해 찾아낸 url의 내용을 파싱할 때 사용합니다."""
    async with AsyncWebCrawler(verbose=False) as crawler:
        result = await crawler.arun(url=url, bypass_cache=True)
        return result.markdown.raw_markdown


class EntitySelection(BaseModel):
    selected_keys: List[str] = Field(
        description="질문과 관련된 엔티티 메모리의 Key 목록 (없으면 빈 리스트)"
    )
@tool
async def search_entity(
    query: str,
    state: Annotated[AgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
):
    """현재 대화 맥락에서 필요한 엔티티(객체, 주제) 정보와 관련 객체까지 추출합니다."""
    entities = state.get("entity_memory", {})
    if not entities:
        return "저장된 엔티티 메모리가 없습니다."

    # 1. Key 목록 추출
    entity_keys = list(entities.keys())

    # 2. 모델 설정 및 구조화된 출력 적용
    model_name = config["configurable"].get("model", "gemini/gemini-2.5-flash-lite")
    llm = MODEL_MANAGER.get_model(model_name)
    structured_llm = llm.with_structured_output(EntitySelection)

    selector_prompt = f"""
    당신은 지식 관리자입니다. 사용자의 질문을 해결하기 위해 [엔티티 목록] 중 참조가 필요한 Key를 선택하세요.

    질문: "{query}"
    [엔티티 목록]: {entity_keys}

    주의: 목록에 존재하지 않는 Key는 절대 생성하지 마세요.
    """

    try:
        result = await structured_llm.ainvoke([SystemMessage(content=selector_prompt)])
        selected_keys = [k for k in result.selected_keys if k in entities]

        if not selected_keys:
            return "관련 정보를 찾지 못했습니다."

        current_turn = state.get("current_turn", 0)
        updated_entities = dict(entities)

        # 3. 직접 선택된 엔티티: last_turn 갱신
        for key in selected_keys:
            updated_entities[key]["last_turn"] = current_turn

        # 4. 1-hop 이웃 수집 (양방향)
        selected_set = set(selected_keys)
        neighbor_map = {}  # {neighbor_key: [(source_key, rel_type, rel_desc), ...]}

        # (a) 정방향: 선택된 키 → relations.target
        for key in selected_keys:
            for rel in entities[key].get("relations", []) or []:
                target = rel.get("target")
                if not target or target in selected_set or target not in entities:
                    continue
                neighbor_map.setdefault(target, []).append(
                    (key, rel.get("type", ""), rel.get("description", ""))
                )

        # (b) 역방향: 다른 엔티티 → 선택된 키를 target으로 갖는 경우
        for src_key, info in entities.items():
            if src_key in selected_set:
                continue
            for rel in info.get("relations", []) or []:
                target = rel.get("target")
                if target in selected_set and src_key in entities:
                    neighbor_map.setdefault(src_key, []).append(
                        (target, rel.get("type", ""), rel.get("description", ""))
                    )

        # 5. 이웃 중요도 필터 (importance 2 이하 제외 — 잡음 방지)
        # 필요 시 임계값 조정 가능
        MIN_NEIGHBOR_IMPORTANCE = 2
        filtered_neighbors = {
            k: v for k, v in neighbor_map.items()
            if entities[k].get("importance", 3) >= MIN_NEIGHBOR_IMPORTANCE
        }

        # 6. 결과 포맷팅 (직접 선택 vs 관련 객체 구분)
        primary_block = []
        for key in selected_keys:
            primary_block.append(f"[{key}]: {updated_entities[key]['description']}")

        related_block = []
        for nkey, edges in filtered_neighbors.items():
            edge_descs = []
            for src, rtype, rdesc in edges:
                edge_descs.append(f"{src} --({rtype})--> {nkey}" + (f" ({rdesc})" if rdesc else ""))
            related_block.append(
                f"[{nkey}]: {entities[nkey]['description']}\n  관계: " + "; ".join(edge_descs)
            )

        content = "## 직접 관련 객체\n" + "\n".join(primary_block)
        if related_block:
            content += "\n\n## 연관 객체 (1-hop)\n" + "\n".join(related_block)

        print(f"--- [메모리 활성화] 직접: {selected_keys} / 이웃: {list(filtered_neighbors.keys())} ---")

        return Command(
            update={
                "entity_memory": updated_entities,
                "messages": [ToolMessage(content=content, tool_call_id=tool_call_id)],
            },
            graph=Command.PARENT,
        )

    except Exception as e:
        print(f"엔티티 구조화 검색 실패: {e}")
        return "엔티티를 선택하는 중 오류가 발생했습니다."

@tool
async def search_memory(query: str, state: Annotated[AgentState, InjectedState], config: RunnableConfig):
    """과거 대화의 요약본(Short/Long memory)에서 질문과 관련된 맥락을 추출합니다."""
    long_mem = state.get("long_memory", "")
    short_mem = state.get("short_memory", [])
    
    # 모든 요약본을 하나의 컨텍스트로 결합
    all_context = f"[전체 흐름]: {long_mem}\n\n"
    all_context += "[최근 사건 요약]:\n" + "\n".join([f"- {s}" for s in short_mem])

    model_name = config["configurable"].get("model", "gemini/gemini-2.5-flash-lite")
    llm = MODEL_MANAGER.get_model(model_name)
    
    extractor_prompt = f"""
    당신은 기억 회상 전문가입니다. [과거 요약 정보]를 바탕으로 질문에 답변하는 데 도움이 될 맥락을 추출하세요.
    
    질문: "{query}"
    [과거 요약 정보]
    {all_context}
    
    [지침]
    - 질문과 직접적으로 관련된 팩트나 결정 사항이 있다면 요약해서 답변하세요.
    - 관련 내용이 없다면 "과거 대화에서 관련 맥락을 찾을 수 없습니다."라고 답하세요.
    """
    
    response = await llm.ainvoke([SystemMessage(content=extractor_prompt)])
    return response.content

@tool
async def search_history(
    query: Annotated[str, Field(description="벡터 검색을 위한 핵심 키워드나 문장")],
    state: Annotated[AgentState, InjectedState],
    config: RunnableConfig,
    search_mode: Annotated[Optional[str], Field(description=(
        "벡터 검색 대상: 'combined'(질문+답변 통합, 기본값), "
        "'query'(사용자 질문 중심), 'response'(AI 답변 중심)"
    ))] = "combined",
    entity_filter: Annotated[Optional[List[str]], Field(description=(
        "특정 엔티티가 명시적으로 등장한 대화로만 한정. entity_memory의 Key 사용. "
        "여러 개 지정 시 OR 매칭. 정확한 엔티티가 알려진 경우에만 사용."
    ))] = None,
    start_timestamp: Annotated[Optional[float], Field(description="조회 시작 유닉스 타임스탬프 (필요 시)")] = None,
    end_timestamp: Annotated[Optional[float], Field(description="조회 종료 유닉스 타임스탬프 (필요 시)")] = None,
    limit: Annotated[int, Field(description="가져올 결과 개수")] = 5,
):
    """
    과거 대화 기록에서 정보를 검색합니다.
    특정 주제를 찾거나, 특정 기간의 대화 내용을 확인해야 할 때 사용합니다.
    """
    params = config.get("configurable", {})
    user_id = params.get("user_id", "default_user")
    thread_id = params.get("thread_id", "unknown_thread")
    collection_name = "conversation_history"

    # 1. 기본 필터 설정 (현재 대화 세션만 조회)
    filter_conditions = [
        rest.FieldCondition(key="user_id", match=rest.MatchValue(value=user_id)),
        rest.FieldCondition(key="thread_id", match=rest.MatchValue(value=thread_id))
    ]

    # 2. 시간 범위 필터 추가 (LLM이 기간을 특정했을 경우)
    if start_timestamp or end_timestamp:
        range_query = {}
        if start_timestamp: range_query["gte"] = start_timestamp
        if end_timestamp: range_query["lte"] = end_timestamp

        filter_conditions.append(
            rest.FieldCondition(key="timestamp", range=rest.Range(**range_query))
        )

    # 3. 엔티티 필터 추가 (LLM이 특정 엔티티를 지정했을 경우, OR 매칭)
    if entity_filter:
        filter_conditions.append(
            rest.FieldCondition(
                key="entity_keys",
                match=rest.MatchAny(any=entity_filter)
            )
        )

    # 4. 검색 대상 named vector 결정 (잘못된 값이면 combined으로 폴백)
    valid_modes = {"combined", "query", "response"}
    search_mode = search_mode if search_mode in valid_modes else "combined"

    try:
        # 5. 쿼리 벡터화
        query_vector = await GEMINI_EMBEDDINGS.aembed_query(query)

        # 6. Qdrant 검색 실행 (named vector 사용)
        # using 인자 사용해서 검색 진행
        search_result = (await QDRANT_DB.query_points(
            collection_name=collection_name,
            query=query_vector,
            using=search_mode,
            query_filter=rest.Filter(must=filter_conditions),
            limit=limit,
        )).points

        if not search_result:
            return "일치하는 과거 대화 기록이 없습니다."

        # 7. 결과 포맷팅 (시간순 정렬 후 반환하면 문맥 파악에 유리)
        formatted_results = []
        for res in search_result:
            p = res.payload
            formatted_results.append({
                "time": p.get("created_at"),
                "entities": p.get("entity_keys", []),
                "content": f"User: {p.get('user_query')}\nAI: {p.get('ai_response')}"
            })

        # 시간순 정렬 (과거 -> 최신)
        formatted_results.sort(key=lambda x: x["time"])

        final_text = "\n\n".join([
            f"[{r['time']}] (관련 엔티티: {', '.join(r['entities']) if r['entities'] else '없음'})\n{r['content']}"
            for r in formatted_results
        ])
        return final_text

    except Exception as e:
        return f"검색 중 오류 발생: {str(e)}"