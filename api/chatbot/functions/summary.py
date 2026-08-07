from api.load_variable import MODEL_MANAGER, AgentState, GEMINI_EMBEDDINGS, QDRANT_DB
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import SystemMessage, AIMessage, RemoveMessage, HumanMessage
from qdrant_client.models import PointStruct, VectorParams, Distance
from datetime import datetime
import uuid, json
from langgraph.graph import StateGraph, END
from api.chatbot.functions.llm_utils import resilient_ainvoke, is_empty_answer

async def manage_turns(state: AgentState, config: RunnableConfig):
    print("--- 대화 턴 인덱스 추출 및 관리 시작 ---")
    messages = state["messages"]
    # state.get()으로 가져올 때 기본값을 리스트로 설정하여 에러 방지
    turns = state.get("conversation_turns", [])
    current_turn = state.get("current_turn", 0)
    
    if not messages:
        return {"conversation_turns": turns}

    human_idx = -1
    ai_idx = -1

    # 1. 메시지 리스트를 역순으로 탐색하여 인덱스 추출
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        
        # 최신 AI 메시지 인덱스 찾기 (실제 답변 내용이 있는 경우)
        if isinstance(msg, AIMessage) and msg.content and ai_idx == -1:
            ai_idx = i
            
        # AI 메시지를 찾은 상태에서 가장 가까운 Human 메시지 인덱스 찾기
        elif isinstance(msg, HumanMessage) and human_idx == -1:
            human_idx = i
            
        # 둘 다 찾았으면 탐색 중단
        if human_idx != -1 and ai_idx != -1:
            break

    # 2. 유효한 턴이 발견되었을 때만 처리
    if human_idx != -1 and ai_idx != -1:
        new_turn_indices = [human_idx, ai_idx]
        
        # 중복 저장 방지: 마지막에 저장된 인덱스 쌍과 다를 때만 추가
        # (노드가 중복 실행되더라도 동일한 턴이 여러 번 기록되지 않도록 함)
        if not turns or turns[-1] != new_turn_indices:
            turns.append(new_turn_indices)
            print(f"새로운 턴 등록: Human_Idx({human_idx}), AI_Idx({ai_idx})")
    else:
        print("완전한 대화 턴(Human + AI)을 찾지 못했습니다.")

    return {
        "conversation_turns": turns,
        "current_turn": current_turn+1 if (human_idx != -1 and ai_idx != -1) else current_turn,
    }

async def save_conversation(state: AgentState, config: RunnableConfig):
    print("--- Qdrant에 대화 내역 저장 (named vectors + entity_keys) ---")
    messages = state["messages"]
    turns = state.get("conversation_turns", [])
    params = config.get("configurable", {})

    if not turns:
        print("저장할 대화 턴이 없습니다.")
        return {}

    # 1. 최신 사용자 메시지와 AI 메시지 객체 추출
    # turns[-1] = [human_idx, ai_idx]
    # 주의: 그래프 순서상 remove_messages 이후에 실행되므로, turns/messages는 이미 시프트 정렬된 상태
    try:
        user_msg_obj = messages[turns[-1][0]]
        ai_msg_obj = messages[turns[-1][1]]
    except (IndexError, KeyError) as e:
        print(f"턴 인덱스 접근 실패(저장 skip): {e}")
        return {}

    user_content = user_msg_obj.content
    ai_content = ai_msg_obj.content

    # 2. 메타데이터 추출 (user_id 등)
    user_id = params.get("user_id", "default_user")
    thread_id = params.get("thread_id", "unknown_thread")

    client_iso = params.get("isotime")
    if client_iso:
        # 클라이언트가 보낸 시간이 있으면 그것을 기준으로 Unix Timestamp 계산
        iso_timestamp = client_iso
        unix_timestamp = datetime.fromisoformat(client_iso).timestamp()
    else:
        # 없으면 서버 로컬 시간으로 생성
        now_obj = datetime.now().astimezone()
        iso_timestamp = now_obj.isoformat()
        unix_timestamp = now_obj.timestamp()

    # 3. Qdrant 컬렉션
    collection_name = "conversation_history"

    # 4. 저장용 텍스트 구성
    # - combined: 질문+답변 통합 (인과 맥락 보존, 일반 검색용)
    # - query:    질문만 (사용자 의도/표현 중심 검색용)
    # - response: 답변만 (AI 답변 내용 중심 검색용)
    combined_text = f"User: {user_content}\nAI: {ai_content}"

    # 5. 이번 턴에 update_entity가 추출/갱신한 엔티티 키 (payload 필터용)
    entity_keys = state.get("last_turn_entities", []) or []

    # 6. point id = turn_id
    #    엔드포인트가 이번 턴의 turn_id(UUID)를 config로 내려준다. Postgres chat_messages.turn_id와
    #    같은 값이므로 삭제/브랜치/재생성이 Postgres와 Qdrant에서 동일한 범위로 적용된다.
    #    특히 '다시 실행'은 같은 turn_id로 upsert되어 기존 point를 그대로 덮어쓴다.
    point_id = params.get("turn_id") or str(uuid.uuid4())

    try:
        # 7. 3개 벡터를 배치 임베딩 (호출 1회로 처리하여 비용/지연 최소화)
        vectors = await GEMINI_EMBEDDINGS.aembed_documents(
            [combined_text, user_content, ai_content]
        )
        combined_vec, query_vec, response_vec = vectors

        await QDRANT_DB.upsert(
            collection_name=collection_name,
            points=[
                PointStruct(
                    id=point_id,
                    vector={
                        "combined": combined_vec,
                        "query": query_vec,
                        "response": response_vec,
                    },
                    payload={
                        "user_id": user_id,
                        "thread_id": thread_id,
                        "created_at": iso_timestamp,
                        "timestamp": unix_timestamp,
                        "user_query": user_content,
                        "ai_response": ai_content,
                        "entity_keys": entity_keys,
                    }
                )
            ]
        )
        print(f"Qdrant 저장 성공 (User: {user_id}, Turn: {point_id}, Entities: {entity_keys})")
    except Exception as e:
        print(f"Qdrant 저장 중 오류 발생: {e}")

    return {}

async def summary_turn(state: AgentState, config: RunnableConfig):
    print("--- 대화쌍(turn) 요약 ---")
    messages = state["messages"]
    # state.get()으로 가져올 때 기본값을 리스트로 설정하여 에러 방지
    turns = state.get("conversation_turns", [])
    check_turn = state.get("check_turn", 0)
    turn_memory = state.get("turn_memory", [])

    # 1. 대화쌍이 4개 이상인 경우에만 요약 진행 (가장 오래된 0번 턴 대상)
    if len(turns) > 3:
        # 모델 설정 (기본값 설정)
        model_name = config["configurable"].get("model", "gemini/gemini-2.5-flash-lite")

        # 요약 대상이 되는 0번 턴의 인덱스 추출
        start, end = turns[0]
        target_messages = messages[start : end + 1]

        # 2. 요약 품질을 높이기 위한 프롬프트 구성
        # 이전 요약 내역(turn_memory)이 있다면 맥락 유지에 도움이 됩니다.
        history_context = "\n".join(turn_memory[-2:]) if turn_memory else "없음"
        
        prompt = f"""
        당신은 대화 관리 전문가입니다. 제공되는 [현재 대화 턴]을 분석하여 핵심 내용을 요약하세요.

        [지침]
        1. 사용자의 의도와 AI의 최종 해결책/답변이 반드시 포함되어야 합니다.
        2. 이전 대화 맥락을 참고하되, 이번 턴의 새로운 정보 위주로 작성하세요.
        3. 결과는 300자 이내의 한국어 평서문으로 작성하세요.

        [이전 대화 요약 맥락]
        {history_context}

        [현재 대화 턴]
        """
        # 3. LLM 호출 (메모리 요약은 best-effort: 실패/빈응답이면 이번 요약은 건너뜀)
        try:
            response = await resilient_ainvoke(
                model_name, None, [SystemMessage(content=prompt)] + target_messages, max_retries=1,
            )
        except Exception as e:
            print(f"--- turn 요약 skip (LLM 오류): {e} ---")
            return {"turn_memory": turn_memory, "check_turn": check_turn}
        if response is None or is_empty_answer(response):
            print("--- turn 요약 skip (빈 응답) ---")
            return {"turn_memory": turn_memory, "check_turn": check_turn}
        turn_summary = response.content.strip()

        # 4. 메모리에 추가 (이후 remove_messages 노드로 넘어가서 실제 인덱스/메시지 삭제 처리)
        turn_memory.append(turn_summary)
        print(f"새로운 턴 요약 완료: {turn_summary[:50]}...")

        # check_turn 업데이트 및 turn_memory는 10개 유지
        check_turn += 1
        if len(turn_memory) > 10:
            turn_memory.pop(0)

    # 변경된 turn_memory 상태 반환 (turns는 remove_messages에서 pop할 것이므로 여기선 유지)
    return {
        "turn_memory": turn_memory,
        "check_turn": check_turn
    }

async def remove_messages(state: AgentState, config: RunnableConfig):
    print("--- 3턴 이상된 오래된 대화 내역은 삭제 ---")
    messages = state["messages"]
    # state.get()으로 가져올 때 기본값을 리스트로 설정하여 에러 방지
    turns = state.get("conversation_turns", [])

    # 3턴 이하라면 삭제할 필요가 없으므로 상태 그대로 반환
    if len(turns) <= 3:
        return {}

    # 1. 가장 오래된 턴의 시작과 끝 인덱스 추출
    start, end = turns.pop(0) # 0번 턴을 꺼내면서 리스트에서 제거
    
    # 2. 해당 범위의 메시지 객체들을 슬라이싱으로 가져옴 (end+1 까지)
    target_messages = messages[start : end + 1]
    
    # 3. 삭제 명령(RemoveMessage) 생성
    # 각 메시지의 고유 ID를 기반으로 삭제 리스트 구성
    delete_actions = []
    for m in target_messages:
        if m.id:
            delete_actions.append(RemoveMessage(id=m.id))
        else:
            # ID가 없을 경우 로그를 남기도록 구성
            print(f"DEBUG: ID 누락 메세지 발견(삭제 skip): {m.content[:20]}")
    
    # 4. 중요: 삭제 후 남은 턴들의 인덱스 보정 (Index Shift 대응)
    # 삭제된 메시지의 개수만큼 뒤에 있는 인덱스들을 앞으로 당겨줌
    offset = len(target_messages)
    updated_turns = [[idx - offset for idx in t] for t in turns]

    print(f"삭제된 메시지 수: {offset}, 남은 턴 수: {len(updated_turns)}")

    # 5. 상태 업데이트
    # LangGraph의 add_messages 리듀서가 RemoveMessage를 받으면 해당 ID의 메시지를 지웁니다.
    return {
        "messages": delete_actions,
        "conversation_turns": updated_turns
    }

async def summary_short(state: AgentState, config: RunnableConfig):
    print("--- 단기 기억(short_memory) 업데이트 체크 ---")
    turn_memory = state.get("turn_memory", [])
    check_turn = state.get("check_turn", 0)
    short_memory = state.get("short_memory", [])
    check_short = state.get("check_short", 0)

    # 1. 대화쌍 업데이트가 7번 되었는지 확인
    if check_turn >= 7:
        print(f"--- check_turn {check_turn} 달성: short_memory 요약 진행 ---")
        
        # 모델 설정
        model_name = config["configurable"].get("model", "gemini/gemini-2.5-flash-lite")

        # 2. 요약 대상: 현재 turn_memory에 쌓인 모든 요약문들
        combined_turns = "\n\n".join([f"- {summary}" for summary in turn_memory])

        prompt = f"""
        당신은 대화의 흐름을 정리하는 요약 전문가입니다. 
        제공된 여러 개의 대화 턴 요약본들을 읽고, 전체적인 맥락과 핵심 사건을 하나의 유기적인 단락으로 통합 요약하세요.

        [지침]
        1. 개별적인 질문-답변 나열이 아닌, 하나의 이야기 흐름처럼 작성하세요.
        2. 사용자의 주요 관심사 변화와 결정된 사항을 중심으로 기술하세요.
        3. 결과는 500자 이내의 한국어 평서문으로 작성하세요.

        [요약할 대화 턴 목록]
        {combined_turns}
        """

        # 3. LLM 호출 및 결과 저장 (best-effort: 실패/빈응답이면 이번 단기요약은 건너뜀)
        try:
            response = await resilient_ainvoke(model_name, None, [SystemMessage(content=prompt)], max_retries=1)
        except Exception as e:
            print(f"--- short_memory 요약 skip (LLM 오류): {e} ---")
            return {"short_memory": short_memory, "check_short": check_short, "check_turn": check_turn}
        if response is None or is_empty_answer(response):
            print("--- short_memory 요약 skip (빈 응답) ---")
            return {"short_memory": short_memory, "check_short": check_short, "check_turn": check_turn}
        short_summary = response.content.strip()

        # 4. 상태 업데이트
        short_memory.append(short_summary)
        check_short += 1  # short_memory 업데이트 횟수 증가
        check_turn = 0    # turn 주기 초기화

        print(f"새로운 단기 기억 생성 완료: {short_summary[:50]}...")
        
        # short_memory의 갯수를 5개로 유지
        if len(short_memory) > 5:
            short_memory.pop(0)
    
    return {
        "short_memory": short_memory,
        "check_short": check_short,
        "check_turn": check_turn
    }

async def summary_long(state: AgentState, config: RunnableConfig):
    print("--- 장기 기억(long_memory) 업데이트 체크 ---")
    short_memory = state.get("short_memory", [])
    check_short = state.get("check_short", 0)
    long_memory = state.get("long_memory", "")

    # 1. 단기 기억 업데이트가 4번 되었는지 확인 (약 28턴 주기)
    if check_short >= 4:
        print(f"--- check_short {check_short} 달성: long_memory 통합 요약 진행 ---")
        
        # 모델 설정
        model_name = config["configurable"].get("model", "gemini/gemini-2.5-flash-lite")

        # 2. 요약 대상: 기존 long_memory + 현재 short_memory 리스트
        combined_shorts = "\n\n".join([f"[단락 {i+1}]: {s}" for i, s in enumerate(short_memory)])
        
        prompt = f"""
        당신은 방대한 대화 내역을 관리하는 역사학자이자 요약 전문가입니다. 
        기존의 [장기 기억]과 새롭게 추가된 [단기 기억 단락들]을 통합하여, 전체 대화의 맥락을 관통하는 하나의 압축된 요약문을 작성하세요.

        [지침]
        1. 세부적인 지엽적 정보보다는 사용자의 핵심 목표, 성향, 그리고 지금까지 도달한 최종 상태 위주로 정리하세요.
        2. 기존 장기 기억의 중요한 흐름을 유지하면서 새로운 단기 기억의 내용을 자연스럽게 병합하세요.
        3. 결과는 전체 대화의 '히스토리 요약' 형태로, 1000자 이내의 한국어 평서문으로 작성하세요.

        [기존 장기 기억]
        {long_memory if long_memory else "기존 기록 없음"}

        [새로 통합할 단기 기억 단락들]
        {combined_shorts}
        """

        # 3. LLM 호출 및 장기 기억 갱신 (best-effort: 실패/빈응답이면 이번 장기요약은 건너뜀)
        try:
            response = await resilient_ainvoke(model_name, None, [SystemMessage(content=prompt)], max_retries=1)
        except Exception as e:
            print(f"--- long_memory 요약 skip (LLM 오류): {e} ---")
            return {"long_memory": long_memory}
        if response is None or is_empty_answer(response):
            print("--- long_memory 요약 skip (빈 응답) ---")
            return {"long_memory": long_memory}
        updated_long_memory = response.content.strip()

        print("장기 기억 통합 및 단기 기억 초기화 완료")

        # 4. 상태 업데이트 (장기 기억 갱신 및 단기 기억 초기화)
        return {
            "long_memory": updated_long_memory,
            "check_short": 0
        }

    # 조건 미충족 시 상태 유지
    return {"long_memory": long_memory}

async def update_entity(state: AgentState, config: RunnableConfig):
    print("--- 지능적 엔티티 추출 및 중요도 업데이트 ---")
    
    current_messages = state["messages"] 
    entity_memory = state.get("entity_memory", {})
    current_turn = state.get("current_turn", 0)

    if not current_messages:
        return {}

    model_name = config["configurable"].get("model", "gemini/gemini-2.5-flash-lite")

    # 기존 메모리 컨텍스트 (JSON 직렬화)
    memory_context = json.dumps(entity_memory, ensure_ascii=False, indent=2)
    
    prompt = f"""
    당신은 사용자의 대화 흐름에서 지식 지도를 구축하는 전문가입니다.
    [최근 대화 흐름]을 분석하여 주요 객체 및 주제를 추출하고, [기존 메모리]를 지능적으로 업데이트하세요.

    [지침]
    1. 객체 및 주제 추출: 인물, 물건뿐만 아니라 대화의 핵심 '주제(Topic)'를 포착하세요.
    2. 중요도(importance) 평가:
    - 각 객체의 중요도를 1(매우 낮음)에서 5(매우 높음) 사이로 책정하세요.
    - 처음 등장한 객체는 대화 내 비중을 보고 판단합니다.
    - 기존에 있던 객체가 다시 깊게 다뤄진다면 중요도를 상향 조정하세요.
    - 기존 객체가 현재 대화 맥락에서 더 이상 중요하게 다뤄지지 않거나, 단순 확인용 정보로 전락했다면 중요도를 하향 조정하세요.
    3. 지능적 병합 및 갱신:
    - 유사 객체는 더 포괄적인 명칭(Key)으로 통합하되, 명칭 변경 시 반드시 `previous_key`에 이전 Key를 기록하세요.
    - 설명(description)은 기존 내용에 현재 대화의 새로운 맥락을 더하여 풍부하게 업데이트하세요.
    - 병합되거나 수정된 객체라도 당신이 판단하기에 '동일한 대상'이라면 기존의 의미적 맥락을 유지해야 합니다.
    4. 관계(relations) 추출:
    - 객체 간의 의미적 연결을 표현하세요. 예: 소속, 소유, 참여, 인과, 상위/하위, 대립, 언급 등.
    - 관계는 **단방향**으로 작성합니다. 양방향 의미라면 양쪽 객체에 각각 기재하세요.
    - `target`은 반드시 **이번 응답에 출력하는 객체명** 또는 **[기존 메모리]에 존재하는 Key** 중 하나여야 합니다. (존재하지 않는 객체로 엣지를 만들지 마세요)
    - `type`은 한국어 명사형으로 짧게 (예: "당사자", "소속", "인용", "관련 법령").
    - 확실하지 않은 관계는 추출하지 마세요. 추출할 관계가 없다면 빈 배열 `[]`을 반환하세요.
    - 기존 메모리에 이미 있는 관계라도 여전히 유효하다면 다시 포함시키고, 더 이상 유효하지 않다면 누락시켜 자연스럽게 제거되게 하세요.
    5. 출력 형식: 반드시 아래 구조의 JSON으로만 응답하세요.
    {{
        "객체명": {{
            "description": "상세 설명(300자 이내)",
            "importance": 정수(1~5),
            "previous_key": "이전_Key_명칭(없으면 null)",
            "relations": [
                {{"target": "다른_객체명", "type": "관계_종류", "description": "관계에 대한 짧은 보충(50자 이내, 선택)"}}
            ]
        }}
    }}

    [기존 메모리]
    {memory_context}

    [최근 대화 흐름]
    """
    # best-effort: 엔티티 추출 LLM이 실패/빈응답이면 기존 엔티티 메모리를 유지하고 건너뜀
    try:
        response = await resilient_ainvoke(
            model_name, None, [SystemMessage(content=prompt)] + current_messages, max_retries=1,
        )
    except Exception as e:
        print(f"--- 엔티티 업데이트 skip (LLM 오류): {e} ---")
        return {"entity_memory": entity_memory, "last_turn_entities": []}
    if response is None or is_empty_answer(response):
        print("--- 엔티티 업데이트 skip (빈 응답) ---")
        return {"entity_memory": entity_memory, "last_turn_entities": []}

    try:
        raw_content = response.content.strip().replace("```json", "").replace("```", "")
        suggested_entities = json.loads(raw_content)

        # 1. 기존 메모리를 베이스로 시작 (보존성 확보)
        final_updated_memory = entity_memory.copy()

        # previous_key → new_key 매핑 사전 계산 (관계 타겟 리매핑용)
        key_remap = {}
        for new_key, info in suggested_entities.items():
            prev_key = info.get("previous_key")
            if prev_key and prev_key != new_key:
                key_remap[prev_key] = new_key

        for new_key, info in suggested_entities.items():
            prev_key = info.get("previous_key")

            # 2. last_turn 결정 로직 (계승 여부 확인)
            if new_key in entity_memory:
                inherited_last_turn = entity_memory[new_key].get("last_turn", current_turn)
            elif prev_key and prev_key in entity_memory:
                inherited_last_turn = entity_memory[prev_key].get("last_turn", current_turn)
                # 3. 병합된 경우: 기존 키(prev_key)를 final에서 즉시 삭제
                if prev_key in final_updated_memory:
                    del final_updated_memory[prev_key]
                print(f"--- [병합/수정] '{prev_key}'가 '{new_key}'로 통합되었습니다. ---")
            else:
                inherited_last_turn = current_turn

            # 4. 관계 정제 및 검증
            raw_relations = info.get("relations", []) or []
            cleaned_relations = []
            seen = set()  # (target, type) 중복 제거용
            for rel in raw_relations:
                if not isinstance(rel, dict):
                    continue
                target = rel.get("target")
                rel_type = rel.get("type")
                if not target or not rel_type:
                    continue
                # 자기 자신 참조 제외
                if target == new_key:
                    continue
                # previous_key 리매핑 적용 (LLM이 옛 명칭으로 가리킨 경우 대비)
                if target in key_remap:
                    target = key_remap[target]
                dedup_key = (target, rel_type)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                cleaned_relations.append({
                    "target": target,
                    "type": rel_type,
                    "description": rel.get("description", "")
                })

            # 5. final_updated_memory에 직접 업데이트/추가
            final_updated_memory[new_key] = {
                "description": info.get("description", ""),
                "importance": info.get("importance", 3),
                "last_turn": inherited_last_turn,
                "relations": cleaned_relations,
            }

        # 6. 다른 엔티티(이번 턴에 갱신 안 된 것들)의 relations 중 prev_key 타겟도 새 키로 리매핑
        if key_remap:
            for k, v in final_updated_memory.items():
                rels = v.get("relations", [])
                if not rels:
                    continue
                remapped = []
                seen = set()
                for rel in rels:
                    t = key_remap.get(rel["target"], rel["target"])
                    dedup_key = (t, rel["type"])
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    remapped.append({**rel, "target": t})
                v["relations"] = remapped

        # 7. 존재하지 않는 타겟을 가리키는 dangling 엣지 정리
        valid_keys = set(final_updated_memory.keys())
        for k, v in final_updated_memory.items():
            v["relations"] = [r for r in v.get("relations", []) if r["target"] in valid_keys]

        # 8. 이번 턴에 추출/갱신된 엔티티 키 목록 — save_conversation의 entity_keys payload용
        # suggested_entities의 key가 canonical name (병합 후 new_key 기준)
        last_turn_entities = [k for k in suggested_entities.keys() if k in final_updated_memory]

        print(f"엔티티 메모리 업데이트 완료 (총 {len(final_updated_memory)}개, 이번 턴 갱신: {last_turn_entities})")
        return {
            "entity_memory": final_updated_memory,
            "last_turn_entities": last_turn_entities,
        }

    except Exception as e:
        print(f"엔티티 업데이트 실패: {e}")
        return {
            "entity_memory": entity_memory,
            "last_turn_entities": [],
        }

async def remove_entity(state: AgentState, config: RunnableConfig):
    print("--- 엔티티 망각(Forget) 프로세스 시작 ---")
    entity_memory = state.get("entity_memory", {})
    # 전체 대화의 흐름을 파악하기 위한 누적 턴 수 (State에 추가 필요)
    current_turn = state.get("current_turn", 0) 
    
    if not entity_memory:
        return {}

    # 중요도별 망각 임계치 (Threshold) 설정
    # 중요도가 낮을수록(1) 빨리 잊고, 높을수록(5) 오래 기억합니다.
    # 예: 중요도 1 = 10턴 미언급 시 삭제, 중요도 5 = 100턴 미언급 시 삭제
    FORGET_THRESHOLDS = {
        1: 10,  # 일시적인 주제 (오늘 날씨 등)
        2: 20,
        3: 30,  # 일반적인 주제
        4: 50,
        5: 100  # 핵심 엔티티 (사용자 이름, 직업 등)
    }

    updated_memory = entity_memory.copy()
    deleted_entities = []

    for key, info in entity_memory.items():
        importance = info.get("importance", 3) # 기본값 3
        last_seen = info.get("last_turn", current_turn)
        
        # 해당 중요도의 임계값 가져오기
        threshold = FORGET_THRESHOLDS.get(importance, 30)
        
        # 망각 조건 체크: (현재 턴 - 마지막 언급 턴) > 임계값
        if (current_turn - last_seen) > threshold:
            del updated_memory[key]
            deleted_entities.append(key)

    # 삭제된 엔티티를 가리키는 다른 엔티티의 relations 정리
    if deleted_entities:
        deleted_set = set(deleted_entities)
        for k, v in updated_memory.items():
            rels = v.get("relations", [])
            if rels:
                v["relations"] = [r for r in rels if r.get("target") not in deleted_set]
        print(f"--- [망각 완료] {len(deleted_entities)}개의 객체 삭제: {deleted_entities}")
    else:
        print("--- 삭제할 오래된 엔티티가 없습니다. ---")

    return {"entity_memory": updated_memory}

# --- 메모리 관리 전용 서브그래프 ---
memory_builder = StateGraph(AgentState)

# 노드 등록
memory_builder.add_node("manage_turns", manage_turns)
memory_builder.add_node("save_conversation", save_conversation)
memory_builder.add_node("summary_turn", summary_turn)
memory_builder.add_node("remove_messages", remove_messages)
memory_builder.add_node("summary_short", summary_short)
memory_builder.add_node("summary_long", summary_long)
memory_builder.add_node("update_entity", update_entity)
memory_builder.add_node("remove_entity", remove_entity)

# 선형 연결
memory_builder.set_entry_point("manage_turns")
memory_builder.add_edge("manage_turns", "summary_turn")
memory_builder.add_edge("summary_turn", "remove_messages")
memory_builder.add_edge("remove_messages", "summary_short")
memory_builder.add_edge("summary_short", "summary_long")
memory_builder.add_edge("summary_long", "update_entity")
memory_builder.add_edge("update_entity", "save_conversation")
memory_builder.add_edge("save_conversation", "remove_entity")
memory_builder.add_edge("remove_entity", END)

# 서브그래프 컴파일
memory_manager_graph = memory_builder.compile()

async def ensure_conversation_history_collection():
    """서버 시작 시 호출 - conversation_history 컬렉션과 payload 인덱스를 보장"""
    from qdrant_client.http import models as rest

    collection_name = "conversation_history"

    # 1. 컬렉션 보장
    if await QDRANT_DB.collection_exists(collection_name):
        print(f"Qdrant 컬렉션 '{collection_name}' 이미 존재")
    else:
        # 임베딩 차원을 실제 임베딩 1회로 동적 감지 (모델 변경에도 대응)
        sample_vec = await GEMINI_EMBEDDINGS.aembed_query("dimension probe")
        dim = len(sample_vec)

        await QDRANT_DB.create_collection(
            collection_name=collection_name,
            vectors_config={
                "combined": VectorParams(size=dim, distance=Distance.COSINE),
                "query":    VectorParams(size=dim, distance=Distance.COSINE),
                "response": VectorParams(size=dim, distance=Distance.COSINE),
            },
        )
        print(f"Qdrant 컬렉션 '{collection_name}' 생성 완료 (dim={dim})")

    # 2. payload index 보장 (컬렉션 존재 여부와 무관하게 idempotent)
    #    동일한 (field_name, schema)로 다시 호출해도 Qdrant는 에러 없이 무시함
    index_specs = [
        ("user_id",     rest.PayloadSchemaType.KEYWORD),
        ("thread_id",   rest.PayloadSchemaType.KEYWORD),
        ("entity_keys", rest.PayloadSchemaType.KEYWORD),
        ("timestamp",   rest.PayloadSchemaType.FLOAT),
    ]
    for field_name, schema in index_specs:
        try:
            await QDRANT_DB.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=schema,
            )
        except Exception as e:
            print(f"[Qdrant] payload index '{field_name}' 등록 skip: {e}")