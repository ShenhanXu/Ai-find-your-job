import asyncio
import json
from app import ai_chat, intent_router
from app.ai_chat import cache_metadata, metadata_compatible
from app.models import ChatRequest
from app.seed import load_seed_jobs
from app.workflow_trace import finish_workflow_trace, parse_trace_step_header, reset_workflow_trace, start_workflow_trace
from uuid import uuid4


def chat_with_rag(*args, **kwargs):
    """Sync wrapper: chat_with_rag is async since the httpx/llm_client refactor."""
    return asyncio.run(ai_chat.chat_with_rag(*args, **kwargs))


def route_intent(*args, **kwargs):
    """Sync wrapper: route_intent is async since the httpx/llm_client refactor."""
    return asyncio.run(intent_router.route_intent(*args, **kwargs))


def enable_demo_mode(monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_EMBEDDINGS", "true")
    monkeypatch.setenv("ALLOW_IN_MEMORY_CACHE", "true")
    monkeypatch.setenv("ALLOW_MEMORY_VECTOR_SEARCH", "true")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:0/0")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def router_payload(intent, confidence=0.9, needs_retrieval=False, missing_fields=None, reason="Test router result."):
    return {
        "intent": intent,
        "confidence": confidence,
        "needs_retrieval": needs_retrieval,
        "needs_action": False,
        "entities": {
            "action": None,
            "stage": None,
            "job_id": None,
            "company": None,
            "job_title": None,
            "query": None,
            "location": None,
            "audience": None,
            "limit": None,
            "follow_up_on": None,
            "requested_status": None,
            "job_reference": None,
            "focus": [],
        },
        "missing_fields": missing_fields or [],
        "reason": reason,
    }


def test_chat_retrieves_relevant_jobs_without_llm(monkeypatch):
    enable_demo_mode(monkeypatch)
    response = chat_with_rag(
        ChatRequest(
            question="Find Seattle new grad backend jobs with Java Spring Boot AWS",
            use_llm=False,
            top_k=3,
        ),
        load_seed_jobs(),
    )

    assert response.retrieved_jobs
    assert response.llm_used is False
    assert response.prompt_template == "job_chat_rag_v1"
    assert response.workflow is not None
    assert response.workflow.job_cards
    assert any(tool.name == "recommend_jobs" for tool in response.workflow.tool_calls)


def test_chat_records_selected_internal_workflow_steps(monkeypatch):
    enable_demo_mode(monkeypatch)
    token = start_workflow_trace(
        enabled=True,
        selected_steps=parse_trace_step_header("intent_router,jina_embedding,pgvector_search,workflow_build,prompt_build"),
        run_id="test-internal-trace",
        level="internal",
    )
    try:
        response = chat_with_rag(
            ChatRequest(
                question="Find Seattle backend jobs with Java Spring Boot AWS",
                use_llm=False,
                top_k=3,
            ),
            load_seed_jobs(),
        )
        trace = finish_workflow_trace()
    finally:
        reset_workflow_trace(token)

    assert response.retrieved_jobs
    assert trace is not None
    step_names = [step["name"] for step in trace["steps"]]
    assert step_names == ["intent_router", "jina_embedding", "pgvector_search", "workflow_build", "prompt_build"]


def test_chat_cache_is_disabled_for_repeated_question(monkeypatch):
    enable_demo_mode(monkeypatch)
    request = ChatRequest(
        question="Find backend roles with PostgreSQL Redis Docker in Bellevue",
        conversation_id=f"test-cache-{uuid4()}",
        use_llm=False,
        top_k=3,
    )

    first = chat_with_rag(request, load_seed_jobs())
    second = chat_with_rag(request, load_seed_jobs())

    assert first.cache_status == "disabled"
    assert second.cache_status == "disabled"


def test_cache_disabled_for_similar_questions(monkeypatch):
    enable_demo_mode(monkeypatch)
    conversation_id = f"test-cache-focus-{uuid4()}"
    jobs = load_seed_jobs()

    first = chat_with_rag(
        ChatRequest(
            question="Find Bellevue backend roles with PostgreSQL Redis Docker",
            conversation_id=conversation_id,
            use_llm=False,
            top_k=3,
        ),
        jobs,
    )
    second = chat_with_rag(
        ChatRequest(
            question="Find Seattle backend roles with PostgreSQL Redis Docker",
            conversation_id=conversation_id,
            use_llm=False,
            top_k=3,
        ),
        jobs,
    )

    assert first.cache_status == "disabled"
    assert second.cache_status == "disabled"


def test_question_metadata_allows_paraphrases_but_rejects_different_constraints(monkeypatch):
    enable_demo_mode(monkeypatch)
    jobs = load_seed_jobs()
    conversation_id = f"test-metadata-{uuid4()}"
    base = cache_metadata(
        ChatRequest(
            question="Find Bellevue backend roles with PostgreSQL Redis Docker",
            conversation_id=conversation_id,
            use_llm=False,
        ),
        jobs,
    )
    paraphrase = cache_metadata(
        ChatRequest(
            question="Show Bellevue backend roles using Redis Docker and PostgreSQL",
            conversation_id=conversation_id,
            use_llm=False,
        ),
        jobs,
    )
    different_location = cache_metadata(
        ChatRequest(
            question="Show Seattle backend roles using Redis Docker and PostgreSQL",
            conversation_id=conversation_id,
            use_llm=False,
        ),
        jobs,
    )
    different_task = cache_metadata(
        ChatRequest(
            question="Build a missing-skill matrix for Bellevue backend roles using Redis Docker and PostgreSQL",
            conversation_id=conversation_id,
            use_llm=False,
        ),
        jobs,
    )

    assert metadata_compatible(base, paraphrase)
    assert not metadata_compatible(base, different_location)
    assert not metadata_compatible(base, different_task)


def test_chat_skips_vague_question(monkeypatch):
    enable_demo_mode(monkeypatch)
    response = chat_with_rag(ChatRequest(question="???"), load_seed_jobs())

    assert response.cache_status == "skipped"
    assert response.retrieved_jobs == []


def test_chat_handles_short_greeting(monkeypatch):
    enable_demo_mode(monkeypatch)
    response = chat_with_rag(ChatRequest(question="hi"), load_seed_jobs())

    assert response.cache_status == "skipped"
    assert response.retrieval_source == "none"
    assert response.retrieved_jobs == []
    assert response.answer.startswith("Hi!")


def test_chat_routes_mcp_questions_to_platform_context(monkeypatch):
    enable_demo_mode(monkeypatch)
    response = chat_with_rag(
        ChatRequest(question="What MCP tools are available?", use_llm=False),
        load_seed_jobs(),
    )

    assert response.cache_status == "disabled"
    assert response.retrieval_source == "platform_context"
    assert response.retrieved_jobs == []
    assert "search_jobs" in response.answer
    assert "get_job_details" in response.answer
    assert "match_resume" not in response.answer


def test_chat_routes_application_action_without_rag(monkeypatch):
    enable_demo_mode(monkeypatch)
    response = chat_with_rag(
        ChatRequest(question="Save the Nvidia new grad cloud services job to my tracker", use_llm=False),
        load_seed_jobs(),
    )

    assert response.retrieval_source == "intent:application_action"
    assert response.intent_route is not None
    assert response.intent_route.intent == "application_action"
    assert response.retrieved_jobs
    assert response.workflow is not None
    assert any(tool.name == "prepare_application_action" for tool in response.workflow.tool_calls)


def test_router_routes_chinese_resume_review_without_llm_dependency(monkeypatch):
    enable_demo_mode(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-router-key")

    def fake_router(prompt):
        raise AssertionError("Clear resume review requests should not need the LLM router.")

    monkeypatch.setattr("app.intent_router.call_deepseek_router", fake_router)

    route = route_intent(
        ChatRequest(
            question="帮我看一下我的resume",
            resume_context="Java Spring Boot React PostgreSQL AWS backend resume with internship projects.",
            use_llm=True,
        ),
        load_seed_jobs(),
    )

    assert route.intent == "resume_tailoring"
    assert route.needs_retrieval is False
    assert route.source == "rules"


def test_router_routes_chinese_resume_company_fit_without_llm_dependency(monkeypatch):
    enable_demo_mode(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-router-key")

    def fake_router(prompt):
        raise AssertionError("Clear resume fit requests should not need the LLM router.")

    monkeypatch.setattr("app.intent_router.call_deepseek_router", fake_router)

    route = route_intent(
        ChatRequest(
            question="我的resume适合哪个公司",
            resume_context="Java Spring Boot React PostgreSQL AWS backend resume with internship projects.",
            use_llm=True,
        ),
        load_seed_jobs(),
    )

    assert route.intent == "resume_fit_analysis"
    assert route.needs_retrieval is True
    assert route.source == "rules"


def test_llm_clarification_is_respected(monkeypatch):
    enable_demo_mode(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-router-key")

    async def fake_router(prompt):
        return json.dumps(
            router_payload(
                "clarification_needed",
                confidence=0.78,
                needs_retrieval=False,
                missing_fields=["desired_action"],
                reason="The request is job-search related but missing the action.",
            )
        )

    monkeypatch.setattr("app.intent_router.call_deepseek_router", fake_router)

    route = route_intent(
        ChatRequest(
            question="这个呢",
            resume_context="Java Spring Boot React PostgreSQL AWS backend resume with internship projects.",
            use_llm=True,
        ),
        load_seed_jobs(),
    )

    assert route.intent == "clarification_needed"
    assert route.needs_retrieval is False
    assert route.source == "llm"


def test_chat_resume_review_ignores_assistant_clarification_history(monkeypatch):
    enable_demo_mode(monkeypatch)

    response = chat_with_rag(
        ChatRequest(
            question="我没别的意思，你帮我看一下我的resume可以吗",
            resume_context="Java Spring Boot React PostgreSQL AWS backend resume with internship projects.",
            messages=[
                {
                    "role": "assistant",
                    "content": "我需要再确认一下：请补充 job_id。你可以说想找什么岗位、哪个公司/职位，或者要保存/标记哪个申请。",
                }
            ],
            use_llm=False,
            top_k=3,
        ),
        load_seed_jobs(),
    )

    assert response.intent_route is not None
    assert response.intent_route.intent == "resume_tailoring"
    assert response.retrieval_source == "intent:resume_audit"
    assert "job_id" not in response.intent_route.missing_fields
    assert response.retrieved_jobs == []


def test_router_failure_returns_router_unavailable(monkeypatch):
    enable_demo_mode(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-router-key")

    def broken_router(prompt):
        raise OSError("router is down")

    monkeypatch.setattr("app.intent_router.call_deepseek_router", broken_router)

    route = route_intent(
        ChatRequest(question="Find backend jobs in Seattle", use_llm=True),
        load_seed_jobs(),
    )

    assert route.intent == "router_unavailable"
    assert route.source == "system"
    assert route.needs_retrieval is False


def test_chat_application_action_can_execute_tracker_update(monkeypatch):
    enable_demo_mode(monkeypatch)
    captured = {}

    def fake_executor(payload):
        captured.update(payload)
        return {
            "status": "success",
            "action": "created",
            "record": {"id": "app-1", **payload},
        }

    response = chat_with_rag(
        ChatRequest(question="Save the Nvidia new grad cloud services job", use_llm=False),
        load_seed_jobs(),
        action_executor=fake_executor,
    )

    assert response.retrieval_source == "intent:application_action"
    assert captured["stage"] == "saved"
    assert captured["job_id"] == response.retrieved_jobs[0].id
    assert "saved" in response.answer.lower()


def test_chat_routes_unsupported_questions_without_rag(monkeypatch):
    enable_demo_mode(monkeypatch)
    response = chat_with_rag(
        ChatRequest(question="What is the weather in Seattle tomorrow?", use_llm=False),
        load_seed_jobs(),
    )

    assert response.retrieval_source == "intent:unsupported"
    assert response.intent_route is not None
    assert response.intent_route.intent == "unsupported"
    assert response.retrieved_jobs == []


def test_chat_is_transparent_when_embeddings_are_not_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ALLOW_LOCAL_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)

    response = chat_with_rag(
        ChatRequest(question="Find backend jobs with Java and Redis", use_llm=False),
        load_seed_jobs(),
    )

    assert response.cache_status == "skipped"
    assert response.retrieval_source == "not_configured"
    assert "JINA_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY" in response.warnings[0]


def test_low_confidence_job_intent_retrieves_instead_of_clarifying():
    from app.intent_router import normalize_route

    route = normalize_route(
        {"intent": "resume_fit_analysis", "confidence": 0.4, "entities": {}, "missing_fields": [], "reason": "unsure"},
        source="llm",
    )

    assert route.intent == "job_search"
    assert route.needs_retrieval is True
    assert route.missing_fields == []


def test_low_confidence_action_intent_still_clarifies():
    from app.intent_router import normalize_route

    route = normalize_route(
        {"intent": "application_action", "confidence": 0.4, "entities": {}, "missing_fields": [], "reason": "unsure"},
        source="llm",
    )

    assert route.intent == "clarification_needed"
    assert route.needs_action is False


def test_repeated_clarification_escalates_to_retrieval():
    from app.intent_router import escalate_repeated_clarification, normalize_route

    clarification = normalize_route(
        {"intent": "clarification_needed", "confidence": 0.9, "entities": {}, "missing_fields": ["desired_action"], "reason": ""},
        source="llm",
    )
    request = ChatRequest(
        question="我的action是找到适合我的职位",
        messages=[
            {"role": "user", "content": "好的，你看看我找intern好还是全职好"},
            {"role": "assistant", "content": "我需要再确认一下：你想让我帮你做什么？"},
        ],
    )

    escalated = escalate_repeated_clarification(clarification, request)

    assert escalated.intent == "job_search"
    assert escalated.needs_retrieval is True
    assert escalated.source == "system"
    assert "找intern好还是全职好" in escalated.entities["query"]
    assert "我的action是找到适合我的职位" in escalated.entities["query"]


def test_first_clarification_is_not_escalated():
    from app.intent_router import escalate_repeated_clarification, normalize_route

    clarification = normalize_route(
        {"intent": "clarification_needed", "confidence": 0.9, "entities": {}, "missing_fields": ["desired_action"], "reason": ""},
        source="llm",
    )
    request = ChatRequest(question="这个呢", messages=[])

    assert escalate_repeated_clarification(clarification, request).intent == "clarification_needed"


def test_clarification_reply_is_human_and_offers_quick_replies():
    from app.ai_chat import clarification_response
    from app.intent_router import is_clarification_text, normalize_route

    route = normalize_route(
        {"intent": "clarification_needed", "confidence": 0.9, "entities": {}, "missing_fields": ["desired_action"], "reason": ""},
        source="llm",
    )
    response = clarification_response(ChatRequest(question="这个呢"), route)

    assert "desired_action" not in response.answer
    assert is_clarification_text(response.answer)
    assert response.workflow is not None
    quick_replies = [action for action in response.workflow.actions if action.intent == "suggest_reply"]
    assert len(quick_replies) == 3
    assert all(action.payload.get("prompt") for action in quick_replies)


def test_screenshot_flow_clarification_then_answer(monkeypatch):
    enable_demo_mode(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-router-key")

    async def clarification_router(prompt):
        return json.dumps(
            router_payload(
                "clarification_needed",
                confidence=0.7,
                missing_fields=["desired_action"],
                reason="Ambiguous request.",
            )
        )

    monkeypatch.setattr("app.intent_router.call_deepseek_router", clarification_router)

    async def fake_chat_llm(prompt, instructions=None):
        return "根据你的背景，intern 和全职各有匹配的岗位，这里是对比。"

    monkeypatch.setattr("app.ai_chat.call_chat_llm", fake_chat_llm)

    first = chat_with_rag(
        ChatRequest(question="好的，你看看我找intern好还是全职好", use_llm=True, top_k=3),
        load_seed_jobs(),
    )

    second = chat_with_rag(
        ChatRequest(
            question="我的action是找到适合我的职位",
            messages=[
                {"role": "user", "content": "好的，你看看我找intern好还是全职好"},
                {"role": "assistant", "content": first.answer},
            ],
            use_llm=True,
            top_k=3,
        ),
        load_seed_jobs(),
    )

    # Whatever the first turn routed to, the second turn must never be a
    # repeated clarification: it must answer with retrieval.
    assert second.intent_route is not None
    assert second.intent_route.intent != "clarification_needed"
    assert second.retrieved_jobs


def test_short_chinese_questions_are_meaningful(monkeypatch):
    enable_demo_mode(monkeypatch)

    response = chat_with_rag(ChatRequest(question="我想找实习", use_llm=False, top_k=3), load_seed_jobs())

    # 5 CJK chars is a complete job-search request; it must reach routing,
    # not be rejected by the Latin-calibrated length gate.
    assert response.intent_route is not None
    assert response.retrieval_source != "none"


def test_application_status_queries_route_to_status_not_clarification(monkeypatch):
    enable_demo_mode(monkeypatch)

    for question in ["What jobs have I applied to so far?", "我的申请进度怎么样了"]:
        response = chat_with_rag(ChatRequest(question=question, use_llm=False), load_seed_jobs())
        assert response.intent_route is not None, question
        assert response.intent_route.intent == "application_status_query", question


def test_save_request_is_not_mistaken_for_status_query(monkeypatch):
    enable_demo_mode(monkeypatch)

    response = chat_with_rag(ChatRequest(question="帮我保存这个岗位", use_llm=False), load_seed_jobs())

    # No resolvable job -> a clarifying ask is correct; a status query is not.
    assert response.intent_route is not None
    assert response.intent_route.intent in {"application_action", "clarification_needed"}
    assert response.intent_route.intent != "application_status_query"


def test_generic_clarification_for_job_domain_question_becomes_retrieval():
    from app.intent_router import normalize_route, prefer_retrieval_over_generic_clarification

    generic = normalize_route(
        {"intent": "clarification_needed", "confidence": 0.8, "entities": {}, "missing_fields": ["desired_action"], "reason": ""},
        source="llm",
    )
    request = ChatRequest(question="好的，你看看我找intern好还是全职好")

    route = prefer_retrieval_over_generic_clarification(generic, request)

    assert route.intent == "job_search"
    assert route.needs_retrieval is True


def test_specific_clarification_is_preserved():
    from app.intent_router import normalize_route, prefer_retrieval_over_generic_clarification

    specific = normalize_route(
        {"intent": "clarification_needed", "confidence": 0.8, "entities": {}, "missing_fields": ["job_id"], "reason": ""},
        source="llm",
    )
    request = ChatRequest(question="这个岗位的要求是什么")

    route = prefer_retrieval_over_generic_clarification(specific, request)

    assert route.intent == "clarification_needed"
