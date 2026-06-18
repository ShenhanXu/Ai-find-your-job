from app.ai_chat import chat_with_rag
from app.models import ChatRequest
from app.seed import load_seed_jobs
from uuid import uuid4


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


def test_chat_uses_cache_for_repeated_question(monkeypatch):
    enable_demo_mode(monkeypatch)
    request = ChatRequest(
        question="Find backend roles with PostgreSQL Redis Docker in Bellevue",
        conversation_id=f"test-cache-{uuid4()}",
        use_llm=False,
        top_k=3,
    )

    first = chat_with_rag(request, load_seed_jobs())
    second = chat_with_rag(request, load_seed_jobs())

    assert first.cache_status == "miss"
    assert second.cache_status in {"exact", "semantic"}


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


def test_chat_is_transparent_when_embeddings_are_not_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ALLOW_LOCAL_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)

    response = chat_with_rag(
        ChatRequest(question="Find backend jobs with Java and Redis"),
        load_seed_jobs(),
    )

    assert response.cache_status == "skipped"
    assert response.retrieval_source == "not_configured"
    assert "JINA_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY" in response.warnings[0]
