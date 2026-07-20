import asyncio
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Iterator, Protocol

from .copilot_workflows import build_copilot_workflow
from .intent_router import resolve_job_reference, route_intent
from .llm_client import LLMRequestError, post_json, post_json_async, record_usage, stream_sse_lines
from .models import (
    ChatRequest,
    ChatResponse,
    ChatRetrievedJob,
    CopilotToolCall,
    CopilotWorkflow,
    IntentRoute,
    JobPosting,
    WorkflowAction,
    WorkflowJobCard,
)
from .openai_eval import call_responses_api_async, extract_output_text, openai_configured
from .workflow_trace import trace_step


OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
GEMINI_EMBEDDINGS_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
JINA_EMBEDDINGS_URL = "https://api.jina.ai/v1/embeddings"
EMBEDDING_DIMENSIONS = 1536
SEMANTIC_CACHE_THRESHOLD = float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.96"))
SEMANTIC_CACHE_MIN_TERM_JACCARD = float(os.getenv("SEMANTIC_CACHE_MIN_TERM_JACCARD", "0.82"))
SEMANTIC_CACHE_TTL_SECONDS = int(os.getenv("SEMANTIC_CACHE_TTL_SECONDS", "86400"))
PROMPT_TEMPLATE_VERSION = "job_chat_rag_v1"
ActionExecutor = Callable[[dict[str, Any]], dict[str, Any]]

QUESTION_STOPWORDS = {
    "a",
    "about",
    "all",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "best",
    "can",
    "compare",
    "could",
    "do",
    "does",
    "emphasize",
    "find",
    "fit",
    "for",
    "from",
    "give",
    "help",
    "i",
    "in",
    "job",
    "jobs",
    "match",
    "matching",
    "me",
    "my",
    "of",
    "on",
    "please",
    "position",
    "positions",
    "recommend",
    "role",
    "roles",
    "show",
    "should",
    "that",
    "the",
    "to",
    "using",
    "want",
    "what",
    "which",
    "with",
}


class EmbeddingProvider(Protocol):
    source: str

    def embed(self, text: str) -> list[float]:
        ...


class LocalEmbeddingProvider:
    source = "local-hashing-embedding"

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        tokens = re.findall(r"[a-z0-9+#.]+", text.lower())
        if not tokens:
            return vector

        terms = tokens + [f"{tokens[i]} {tokens[i + 1]}" for i in range(len(tokens) - 1)]
        for term in terms:
            digest = hashlib.sha256(term.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign

        return normalize_vector(vector)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


class OpenAIEmbeddingProvider:
    source = "openai-embeddings"

    def __init__(self) -> None:
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

    def embed(self, text: str) -> list[float]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured.")

        payload = post_json(
            OPENAI_EMBEDDINGS_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            body={"model": self.model, "input": text[:12000]},
            timeout=30,
            service="openai-embeddings",
            model=self.model,
        )
        embedding = payload["data"][0]["embedding"]
        return normalize_vector([float(value) for value in embedding])

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured.")

        payload = post_json(
            OPENAI_EMBEDDINGS_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            body={"model": self.model, "input": [text[:12000] for text in texts]},
            timeout=60,
            service="openai-embeddings",
            model=self.model,
        )
        rows = sorted(payload["data"], key=lambda row: row["index"])
        return [normalize_vector([float(value) for value in row["embedding"]]) for row in rows]


class GeminiEmbeddingProvider:
    source = "gemini-embeddings"

    def __init__(self) -> None:
        self.api_key = os.getenv("GEMINI_API_KEY", "")
        self.model = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2")
        self.dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", str(EMBEDDING_DIMENSIONS)))

    def embed(self, text: str) -> list[float]:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        payload = post_json(
            GEMINI_EMBEDDINGS_URL_TEMPLATE.format(model=self.model),
            headers={"x-goog-api-key": self.api_key},
            body={
                "content": {"parts": [{"text": text[:24000]}]},
                "output_dimensionality": self.dimensions,
            },
            timeout=45,
            service="gemini-embeddings",
            model=self.model,
        )
        embedding = payload["embedding"]["values"]
        return normalize_vector([float(value) for value in embedding])


class JinaEmbeddingProvider:
    source = "jina-embeddings"

    def __init__(self) -> None:
        self.api_key = os.getenv("JINA_API_KEY", "")
        self.model = os.getenv("JINA_EMBEDDING_MODEL", "jina-embeddings-v4")
        self.dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", str(EMBEDDING_DIMENSIONS)))

    def embed(self, text: str) -> list[float]:
        return self._embed(text, task=os.getenv("JINA_EMBEDDING_TASK", "retrieval.query"))

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text, task="retrieval.query")

    def embed_document(self, text: str) -> list[float]:
        return self._embed(text, task="retrieval.passage")

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not self.api_key:
            raise RuntimeError("JINA_API_KEY is not configured.")

        payload = post_json(
            JINA_EMBEDDINGS_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "ai-job-match/0.1 (+https://localhost)",
            },
            body={
                "model": self.model,
                "input": [text[:32000] for text in texts],
                "task": "retrieval.passage",
                "dimensions": self.dimensions,
                "normalized": True,
                "embedding_type": "float",
            },
            timeout=120,
            service="jina-embeddings",
            model=self.model,
        )
        rows = sorted(payload["data"], key=lambda row: row["index"])
        return [normalize_vector([float(value) for value in row["embedding"]]) for row in rows]

    def _embed(self, text: str, task: str) -> list[float]:
        if not self.api_key:
            raise RuntimeError("JINA_API_KEY is not configured.")

        payload = post_json(
            JINA_EMBEDDINGS_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "ai-job-match/0.1 (+https://localhost)",
            },
            body={
                "model": self.model,
                "input": [text[:32000]],
                "task": task,
                "dimensions": self.dimensions,
                "normalized": True,
                "embedding_type": "float",
            },
            timeout=60,
            service="jina-embeddings",
            model=self.model,
        )
        embedding = payload["data"][0]["embedding"]
        return normalize_vector([float(value) for value in embedding])


@dataclass
class CacheEntry:
    answer: str
    embedding: list[float]
    metadata: dict[str, Any]
    created_at: float


class InMemorySemanticCache:
    def __init__(self) -> None:
        self.exact: dict[str, CacheEntry] = {}
        self.semantic: list[CacheEntry] = []

    def get_exact(self, key: str) -> CacheEntry | None:
        entry = self.exact.get(key)
        if entry and not self._expired(entry):
            return entry
        return None

    def get_semantic(self, embedding: list[float], metadata: dict[str, Any]) -> tuple[CacheEntry | None, float | None]:
        best_entry: CacheEntry | None = None
        best_score = -1.0
        for entry in self.semantic:
            if self._expired(entry) or not metadata_compatible(metadata, entry.metadata):
                continue
            score = cosine_similarity(embedding, entry.embedding)
            if score > best_score:
                best_entry = entry
                best_score = score

        if best_entry and best_score >= SEMANTIC_CACHE_THRESHOLD:
            return best_entry, best_score
        return None, best_score if best_score >= 0 else None

    def set(self, key: str, entry: CacheEntry) -> None:
        self.exact[key] = entry
        self.semantic.append(entry)
        self.semantic = self.semantic[-250:]

    def _expired(self, entry: CacheEntry) -> bool:
        return time.time() - entry.created_at > SEMANTIC_CACHE_TTL_SECONDS


class RedisSemanticCache:
    def __init__(self) -> None:
        import redis

        self.client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        self.client.ping()

    def get_exact(self, key: str) -> CacheEntry | None:
        raw = self.client.get(f"chat:exact:{key}")
        return parse_cache_entry(raw) if raw else None

    def get_semantic(self, embedding: list[float], metadata: dict[str, Any]) -> tuple[CacheEntry | None, float | None]:
        best_entry: CacheEntry | None = None
        best_score = -1.0
        for raw in self.client.lrange("chat:semantic:index", 0, 249):
            entry = parse_cache_entry(raw)
            if not entry or not metadata_compatible(metadata, entry.metadata):
                continue
            score = cosine_similarity(embedding, entry.embedding)
            if score > best_score:
                best_entry = entry
                best_score = score

        if best_entry and best_score >= SEMANTIC_CACHE_THRESHOLD:
            return best_entry, best_score
        return None, best_score if best_score >= 0 else None

    def set(self, key: str, entry: CacheEntry) -> None:
        payload = serialize_cache_entry(entry)
        pipe = self.client.pipeline()
        pipe.setex(f"chat:exact:{key}", SEMANTIC_CACHE_TTL_SECONDS, payload)
        pipe.lpush("chat:semantic:index", payload)
        pipe.ltrim("chat:semantic:index", 0, 249)
        pipe.expire("chat:semantic:index", SEMANTIC_CACHE_TTL_SECONDS)
        pipe.execute()


_memory_cache = InMemorySemanticCache()


def get_embedding_provider() -> EmbeddingProvider:
    preferred = os.getenv("EMBEDDING_PROVIDER", "").lower().strip()
    if preferred == "jina":
        if not os.getenv("JINA_API_KEY"):
            raise RuntimeError("JINA_API_KEY is required when EMBEDDING_PROVIDER=jina.")
        return JinaEmbeddingProvider()
    if preferred == "gemini":
        if not os.getenv("GEMINI_API_KEY"):
            raise RuntimeError("GEMINI_API_KEY is required when EMBEDDING_PROVIDER=gemini.")
        return GeminiEmbeddingProvider()
    if preferred == "openai":
        if not openai_configured():
            raise RuntimeError("OPENAI_API_KEY is required when EMBEDDING_PROVIDER=openai.")
        return OpenAIEmbeddingProvider()
    if not preferred and os.getenv("JINA_API_KEY"):
        return JinaEmbeddingProvider()
    if not preferred and os.getenv("GEMINI_API_KEY"):
        return GeminiEmbeddingProvider()
    if not preferred and openai_configured():
        return OpenAIEmbeddingProvider()
    if os.getenv("ALLOW_LOCAL_EMBEDDINGS", "").lower() in {"1", "true", "yes", "on"}:
        return LocalEmbeddingProvider()
    raise RuntimeError("JINA_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY is required for real embeddings.")


def get_semantic_cache() -> InMemorySemanticCache | RedisSemanticCache:
    try:
        return RedisSemanticCache()
    except Exception as exc:
        if os.getenv("ALLOW_IN_MEMORY_CACHE", "").lower() not in {"1", "true", "yes", "on"}:
            raise RuntimeError(f"Redis semantic cache is unavailable: {exc}") from exc
        return _memory_cache


async def chat_with_rag(
    request: ChatRequest,
    jobs: list[JobPosting],
    action_executor: ActionExecutor | None = None,
) -> ChatResponse:
    warnings: list[str] = []
    if is_greeting(request.question):
        return ChatResponse(
            answer=(
                "Hi! Tell me the role, location, level, or tech stack you want. "
                "For example: Find Seattle new-grad backend jobs that match Java, Spring Boot, Redis, and AWS."
            ),
            cache_status="skipped",
            retrieval_source="none",
            llm_used=False,
            prompt_template=PROMPT_TEMPLATE_VERSION,
            retrieved_jobs=[],
            warnings=[],
        )

    if not meaningful_question(request.question):
        return ChatResponse(
            answer="Ask a specific job-search or resume-matching question, such as: Find Seattle backend new-grad jobs that match Java, Spring Boot, Redis, and AWS.",
            cache_status="skipped",
            retrieval_source="none",
            llm_used=False,
            prompt_template=PROMPT_TEMPLATE_VERSION,
            retrieved_jobs=[],
            warnings=["Question was too short or too vague to run retrieval."],
        )

    with trace_step("intent_router", {"job_count": len(jobs), "question_chars": len(request.question)}):
        route = await route_intent(request, jobs)
    with trace_step("non_rag_route", {"intent": route.intent, "source": route.source}):
        routed = await non_rag_route_response(request, jobs, route, action_executor)
    if routed:
        return routed

    try:
        with trace_step("embedding_provider"):
            provider = get_embedding_provider()
    except RuntimeError as exc:
        return ChatResponse(
            answer=(
                "Real AI retrieval is not fully configured yet. "
                "Set JINA_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY for embeddings."
            ),
            cache_status="skipped",
            retrieval_source="not_configured",
            llm_used=False,
            prompt_template=PROMPT_TEMPLATE_VERSION,
            retrieved_jobs=[],
            intent_route=route,
            warnings=[str(exc)],
        )
    selected_jobs = [job for job in jobs if not request.job_ids or job.id in request.job_ids]

    # An escalated clarification (or the LLM router) may provide a cleaner
    # retrieval query than the raw message, e.g. prior turn + follow-up combined.
    retrieval_question = str(route.entities.get("query") or "").strip() or request.question
    question_context = f"{retrieval_question}\n\nResume context:\n{request.resume_context[:4000]}"
    try:
        with trace_step(
            "jina_embedding",
            {"provider": provider.source, "context_chars": len(question_context)},
        ):
            query_embedding = await asyncio.to_thread(embed_query, provider, question_context)
    except Exception as exc:
        return ChatResponse(
            answer="Embedding generation failed, so I did not run semantic cache or RAG retrieval.",
            cache_status="skipped",
            retrieval_source="embedding_error",
            llm_used=False,
            prompt_template=PROMPT_TEMPLATE_VERSION,
            retrieved_jobs=[],
            intent_route=route,
            warnings=[str(exc)],
        )

    try:
        with trace_step("pgvector_search", {"candidate_jobs": len(selected_jobs), "top_k": request.top_k}):
            retrieved, retrieval_source = await asyncio.to_thread(
                retrieve_relevant_jobs,
                question=retrieval_question,
                resume_context=request.resume_context,
                jobs=selected_jobs,
                query_embedding=query_embedding,
                provider=provider,
                top_k=request.top_k,
                warnings=warnings,
            )
    except Exception as exc:
        return ChatResponse(
            answer="Postgres pgvector retrieval failed, so I did not return a pretend RAG answer.",
            cache_status="disabled",
            cache_similarity=None,
            retrieval_source="pgvector_error",
            llm_used=False,
            prompt_template=PROMPT_TEMPLATE_VERSION,
            retrieved_jobs=[],
            intent_route=route,
            warnings=[str(exc)],
        )
    with trace_step("workflow_build", {"retrieved_jobs": len(retrieved)}):
        workflow = build_copilot_workflow(request, retrieved, selected_jobs)
    with trace_step("prompt_build", {"retrieved_jobs": len(retrieved)}):
        prompt = build_prompt(request, retrieved, route)

    llm_used = False
    if request.use_llm and llm_configured():
        try:
            with trace_step("deepseek_llm_call", {"provider": llm_provider_name(), "prompt_chars": len(prompt)}):
                answer = (await call_chat_llm(prompt)).strip()
                if not answer:
                    raise ValueError("LLM returned an empty answer.")
            llm_used = True
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            answer = local_chat_answer(request, retrieved)
            warnings.append("LLM call failed; returned a local retrieval answer.")
    else:
        answer = local_chat_answer(request, retrieved)
        if request.use_llm:
            warnings.append("DEEPSEEK_API_KEY or OPENAI_API_KEY is not configured; returned a local retrieval answer.")

    response = ChatResponse(
        answer=answer,
        cache_status="disabled",
        cache_similarity=None,
        retrieval_source=retrieval_source,
        llm_used=llm_used,
        prompt_template=PROMPT_TEMPLATE_VERSION,
        retrieved_jobs=retrieved,
        intent_route=route,
        workflow=workflow,
        warnings=warnings,
    )
    return response


async def chat_with_rag_stream(
    request: ChatRequest,
    jobs: list[JobPosting],
    action_executor: ActionExecutor | None = None,
) -> AsyncIterator[str]:
    warnings: list[str] = []
    if is_greeting(request.question):
        for event in stream_completed_response(
            ChatResponse(
                answer=(
                    "Hi! Tell me the role, location, level, or tech stack you want. "
                    "For example: Find Seattle new-grad backend jobs that match Java, Spring Boot, Redis, and AWS."
                ),
                cache_status="skipped",
                retrieval_source="none",
                llm_used=False,
                prompt_template=PROMPT_TEMPLATE_VERSION,
                retrieved_jobs=[],
                warnings=[],
            )
        ):
            yield event
        return

    if not meaningful_question(request.question):
        for event in stream_completed_response(
            ChatResponse(
                answer="Ask a specific job-search or resume-matching question, such as: Find Seattle backend new-grad jobs that match Java, Spring Boot, Redis, and AWS.",
                cache_status="skipped",
                retrieval_source="none",
                llm_used=False,
                prompt_template=PROMPT_TEMPLATE_VERSION,
                retrieved_jobs=[],
                warnings=["Question was too short or too vague to run retrieval."],
            )
        ):
            yield event
        return

    route = await route_intent(request, jobs)
    routed = await non_rag_route_response(request, jobs, route, action_executor)
    if routed:
        for event in stream_completed_response(routed):
            yield event
        return

    try:
        provider = get_embedding_provider()
    except RuntimeError as exc:
        for event in stream_completed_response(
            ChatResponse(
                answer=(
                    "Real AI retrieval is not fully configured yet. "
                    "Set JINA_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY for embeddings."
                ),
                cache_status="skipped",
                retrieval_source="not_configured",
                llm_used=False,
                prompt_template=PROMPT_TEMPLATE_VERSION,
                retrieved_jobs=[],
                intent_route=route,
                warnings=[str(exc)],
            )
        ):
            yield event
        return

    selected_jobs = [job for job in jobs if not request.job_ids or job.id in request.job_ids]

    # An escalated clarification (or the LLM router) may provide a cleaner
    # retrieval query than the raw message, e.g. prior turn + follow-up combined.
    retrieval_question = str(route.entities.get("query") or "").strip() or request.question
    question_context = f"{retrieval_question}\n\nResume context:\n{request.resume_context[:4000]}"
    try:
        query_embedding = await asyncio.to_thread(embed_query, provider, question_context)
    except Exception as exc:
        for event in stream_completed_response(
            ChatResponse(
                answer="Embedding generation failed, so I did not run semantic cache or RAG retrieval.",
                cache_status="skipped",
                retrieval_source="embedding_error",
                llm_used=False,
                prompt_template=PROMPT_TEMPLATE_VERSION,
                retrieved_jobs=[],
                intent_route=route,
                warnings=[str(exc)],
            )
        ):
            yield event
        return

    try:
        retrieved, retrieval_source = await asyncio.to_thread(
            retrieve_relevant_jobs,
            question=retrieval_question,
            resume_context=request.resume_context,
            jobs=selected_jobs,
            query_embedding=query_embedding,
            provider=provider,
            top_k=request.top_k,
            warnings=warnings,
        )
    except Exception as exc:
        for event in stream_completed_response(
            ChatResponse(
                answer="Postgres pgvector retrieval failed, so I did not return a pretend RAG answer.",
                cache_status="disabled",
                cache_similarity=None,
                retrieval_source="pgvector_error",
                llm_used=False,
                prompt_template=PROMPT_TEMPLATE_VERSION,
                retrieved_jobs=[],
                intent_route=route,
                warnings=[str(exc)],
            )
        ):
            yield event
        return

    workflow = build_copilot_workflow(request, retrieved, selected_jobs)
    prompt = build_prompt(request, retrieved, route)
    answer_parts: list[str] = []
    llm_used = False
    if request.use_llm and llm_configured():
        try:
            async for chunk in call_chat_llm_stream(prompt):
                if not chunk:
                    continue
                answer_parts.append(chunk)
                yield sse_event("chunk", {"content": chunk})
            if not "".join(answer_parts).strip():
                raise ValueError("LLM returned an empty answer.")
            llm_used = True
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            answer_parts = [local_chat_answer(request, retrieved)]
            warnings.append("LLM call failed; returned a local retrieval answer.")
            for event in stream_text(answer_parts[0]):
                yield event
    else:
        answer_parts = [local_chat_answer(request, retrieved)]
        if request.use_llm:
            warnings.append("DEEPSEEK_API_KEY or OPENAI_API_KEY is not configured; returned a local retrieval answer.")
        for event in stream_text(answer_parts[0]):
            yield event

    response = ChatResponse(
        answer="".join(answer_parts).strip(),
        cache_status="disabled",
        cache_similarity=None,
        retrieval_source=retrieval_source,
        llm_used=llm_used,
        prompt_template=PROMPT_TEMPLATE_VERSION,
        retrieved_jobs=retrieved,
        intent_route=route,
        workflow=workflow,
        warnings=warnings,
    )
    yield sse_event("done", response.model_dump(mode="json"))


RAG_INTENTS = {
    "job_search",
    "job_detail_lookup",
    "job_compare",
    "resume_fit_analysis",
    "resume_tailoring",
    "skill_gap_analysis",
}


async def non_rag_route_response(
    request: ChatRequest,
    jobs: list[JobPosting],
    route: IntentRoute,
    action_executor: ActionExecutor | None,
) -> ChatResponse | None:
    if route.intent == "resume_tailoring" and not route.needs_retrieval:
        return await resume_audit_response(request, route)
    if route.intent in RAG_INTENTS:
        return None
    if route.intent == "platform_help":
        return platform_tooling_response(request.question, route)
    if route.intent == "small_talk":
        return small_talk_response(request, route)
    if route.intent in {"off_topic", "nonsense"}:
        return lightweight_redirect_response(request, route)
    if route.intent == "router_unavailable":
        return router_unavailable_response(request, route)
    if route.intent == "unsupported":
        return unsupported_response(request, route)
    if route.intent == "clarification_needed":
        return clarification_response(request, route)
    if route.intent == "application_status_query":
        return application_status_response(request, route)
    if route.intent == "application_action":
        return await application_action_response(request, jobs, route, action_executor)
    return clarification_response(request, route)


async def application_action_response(
    request: ChatRequest,
    jobs: list[JobPosting],
    route: IntentRoute,
    action_executor: ActionExecutor | None,
) -> ChatResponse:
    warnings: list[str] = []
    job = resolve_job_reference(route, request, jobs)
    if not job:
        return clarification_response(
            request,
            route.model_copy(
                update={
                    "intent": "clarification_needed",
                    "needs_retrieval": False,
                    "needs_action": False,
                    "missing_fields": sorted(set(route.missing_fields + ["job_id"])),
                    "reason": "The user asked for an application action, but the target job could not be resolved.",
                }
            ),
        )

    stage = normalized_application_stage(str(route.entities.get("stage", "saved")))
    action_name = str(route.entities.get("action", "save"))
    payload: dict[str, Any] = {
        "job_id": job.id,
        "stage": stage,
        "notes": str(route.entities.get("notes", "")),
        "follow_up_on": route.entities.get("follow_up_on"),
    }

    execution: dict[str, Any] | None = None
    if action_executor:
        try:
            execution = action_executor(payload)
        except (TypeError, ValueError) as exc:
            warnings.append(str(exc))

    workflow = build_application_action_workflow(route, job, payload, execution)
    retrieved = [
        ChatRetrievedJob(
            id=job.id,
            company=job.company,
            title=job.title,
            location=job.location,
            level=job.level,
            work_mode=job.work_mode,
            score=100,
            reason="Resolved by the intent router for an application workflow.",
        )
    ]
    fallback = application_action_answer(request, job, stage, action_name, execution, bool(action_executor), warnings)
    answer, llm_used = await response_from_observation(
        request=request,
        route=route,
        observation={
            "workflow": "application_action",
            "job": job.model_dump(by_alias=True),
            "action_payload": payload,
            "execution": execution or {"status": "prepared"},
        },
        fallback=fallback,
        warnings=warnings,
    )

    return ChatResponse(
        answer=answer,
        cache_status="disabled",
        cache_similarity=None,
        retrieval_source="intent:application_action",
        llm_used=llm_used,
        prompt_template=PROMPT_TEMPLATE_VERSION,
        retrieved_jobs=retrieved,
        intent_route=route,
        workflow=workflow,
        warnings=warnings,
    )


def build_application_action_workflow(
    route: IntentRoute,
    job: JobPosting,
    payload: dict[str, Any],
    execution: dict[str, Any] | None,
) -> CopilotWorkflow:
    status = "completed" if execution and execution.get("status") == "success" else "prepared"
    return CopilotWorkflow(
        title="Application action workflow",
        tool_calls=[
            CopilotToolCall(
                name="intent_router",
                title=f"Route intent: {route.intent}",
                status="completed",
                summary=route.reason,
            ),
            CopilotToolCall(
                name="resolve_job",
                title="Resolve target job",
                status="completed",
                summary=f"Matched request to {job.company} - {job.title}.",
            ),
            CopilotToolCall(
                name="prepare_application_action",
                title="Prepare tracker payload",
                status=status,
                summary=(
                    "Application tracker was updated."
                    if execution and execution.get("status") == "success"
                    else "Prepared a structured tracker action payload."
                ),
            ),
        ],
        job_cards=[
            WorkflowJobCard(
                job_id=job.id,
                company=job.company,
                title=job.title,
                location=job.location,
                level=job.level,
                work_mode=job.work_mode,
                score=100,
                fit_summary="Selected for an application-tracking action.",
                matched_skills=job.required_skills[:3],
                missing_skills=[],
            )
        ],
        actions=[
            WorkflowAction(
                label="Open role",
                intent="open_job",
                job_id=job.id,
                payload={"company": job.company, "title": job.title},
            ),
            WorkflowAction(
                label="Tracker updated" if execution and execution.get("status") == "success" else "Save to tracker",
                intent="save_application",
                job_id=job.id,
                payload={key: str(value) for key, value in payload.items() if value is not None},
            ),
        ],
    )


def application_action_answer(
    request: ChatRequest,
    job: JobPosting,
    stage: str,
    action_name: str,
    execution: dict[str, Any] | None,
    executor_was_available: bool,
    warnings: list[str],
) -> str:
    chinese = contains_cjk(request.question)
    if execution and execution.get("status") == "success":
        verb = "updated" if execution.get("action") == "updated" else "saved"
        if chinese:
            return f"已处理：{job.company} - {job.title} 已进入 tracker，状态是 `{stage}`。"
        return f"Done — I {verb} {job.company} - {job.title} in your tracker with stage `{stage}`."

    if executor_was_available and warnings:
        if chinese:
            return f"我找到了 {job.company} - {job.title}，但 tracker 更新失败了：{warnings[-1]}"
        return f"I found {job.company} - {job.title}, but the tracker update failed: {warnings[-1]}"

    if chinese:
        return (
            f"我找到了 {job.company} - {job.title}，并准备好了 `{action_name}` 的 tracker payload，"
            "但当前 chat 没有接入写入执行器。"
        )
    return (
        f"I found {job.company} - {job.title} and prepared the `{action_name}` tracker payload. "
        "This chat run did not receive a write executor, so no tracker state was changed."
    )


# Human-readable questions for router entity keys. Internal field names like
# `desired_action` must never leak into a user-facing clarification.
MISSING_FIELD_PROMPTS: dict[str, tuple[str, str]] = {
    "desired_action": ("你想让我帮你做什么？", "What would you like me to do?"),
    "job_id": ("你说的是哪个岗位？告诉我公司名或职位名就行。", "Which job do you mean? A company or title is enough."),
    "job_reference": ("你说的是哪个岗位？告诉我公司名或职位名就行。", "Which job do you mean? A company or title is enough."),
    "company": ("目标公司是哪家？", "Which company are you targeting?"),
    "job_title": ("目标职位叫什么？", "What is the target job title?"),
    "query": ("想找什么样的岗位？方向、地点或级别都可以说。", "What kind of role are you looking for? Direction, location, or level all help."),
    "location": ("想在哪个城市工作？", "Which location do you prefer?"),
    "stage": ("要把这个申请标记成什么状态？比如 saved、applied、interview。", "Which stage should I set, e.g. saved, applied, interview?"),
    "action": ("你想对这个申请做什么？保存、标记已投递，还是设置跟进？", "What should I do with this application: save it, mark applied, or set a follow-up?"),
}


def clarification_questions(route: IntentRoute, chinese: bool) -> list[str]:
    index = 0 if chinese else 1
    questions: list[str] = []
    for field in route.missing_fields:
        prompt = MISSING_FIELD_PROMPTS.get(field)
        if prompt and prompt[index] not in questions:
            questions.append(prompt[index])
    if not questions:
        questions.append(MISSING_FIELD_PROMPTS["desired_action"][index])
    return questions[:2]


def clarification_quick_replies(request: ChatRequest, route: IntentRoute) -> CopilotWorkflow:
    suggestions = (
        [
            ("推荐适合我的岗位", "帮我推荐适合我的岗位"),
            ("对比最匹配的几个岗位", "帮我对比和我背景最匹配的几个岗位"),
            ("分析简历和岗位的匹配度", "分析我的简历和这些岗位的匹配度"),
        ]
        if contains_cjk(request.question)
        else [
            ("Recommend jobs for me", "Recommend jobs that fit my background"),
            ("Compare my top matches", "Compare the jobs that best match my background"),
            ("Analyze my resume fit", "Analyze how well my resume fits these jobs"),
        ]
    )
    return CopilotWorkflow(
        title="Quick replies",
        tool_calls=[
            CopilotToolCall(
                name="intent_router",
                title="Route intent: clarification_needed",
                status="completed",
                summary=route.reason,
            )
        ],
        actions=[
            WorkflowAction(label=label, intent="suggest_reply", payload={"prompt": prompt})
            for label, prompt in suggestions
        ],
    )


def clarification_response(request: ChatRequest, route: IntentRoute) -> ChatResponse:
    chinese = contains_cjk(request.question)
    questions = clarification_questions(route, chinese)
    # Keep the marker prefixes in sync with intent_router.CLARIFICATION_MARKERS.
    if chinese:
        answer = "我需要再确认一下：" + " ".join(questions) + " 也可以直接点下面的快捷选项。"
    else:
        answer = "I need one more detail: " + " ".join(questions) + " You can also tap a quick option below."
    return ChatResponse(
        answer=answer,
        cache_status="skipped",
        cache_similarity=None,
        retrieval_source="intent:clarification",
        llm_used=False,
        prompt_template=PROMPT_TEMPLATE_VERSION,
        retrieved_jobs=[],
        intent_route=route,
        workflow=clarification_quick_replies(request, route),
        warnings=[],
    )


def unsupported_response(request: ChatRequest, route: IntentRoute) -> ChatResponse:
    if contains_cjk(request.question):
        answer = "这个请求超出了当前 job copilot 的范围。我现在能帮你找职位、比较岗位、分析简历匹配度、改简历、做 skill gap，以及管理申请 tracker。"
    else:
        answer = (
            "That is outside this job copilot's current scope. I can help with job search, job comparison, "
            "resume fit, resume tailoring, skill gaps, and application tracking."
        )
    return ChatResponse(
        answer=answer,
        cache_status="skipped",
        cache_similarity=None,
        retrieval_source="intent:unsupported",
        llm_used=False,
        prompt_template=PROMPT_TEMPLATE_VERSION,
        retrieved_jobs=[],
        intent_route=route,
        workflow=None,
        warnings=[],
    )


def small_talk_response(request: ChatRequest, route: IntentRoute) -> ChatResponse:
    if contains_cjk(request.question):
        answer = "我在。你可以直接说想找什么岗位、想比较哪些公司，或者让我看简历和岗位匹配度。"
    else:
        answer = "I am here. Tell me the role, company, resume question, or application task you want to work on."
    return ChatResponse(
        answer=answer,
        cache_status="skipped",
        cache_similarity=None,
        retrieval_source="intent:small_talk",
        llm_used=False,
        prompt_template=PROMPT_TEMPLATE_VERSION,
        retrieved_jobs=[],
        intent_route=route,
        workflow=None,
        warnings=[],
    )


def lightweight_redirect_response(request: ChatRequest, route: IntentRoute) -> ChatResponse:
    if contains_cjk(request.question):
        answer = "我先不展开这个。你要是想继续找工，可以问我职位推荐、简历匹配、改简历、skill gap 或申请 tracker。"
    else:
        answer = "I will keep this brief. For job search, ask me about role recommendations, resume fit, resume edits, skill gaps, or application tracking."
    return ChatResponse(
        answer=answer,
        cache_status="skipped",
        cache_similarity=None,
        retrieval_source=f"intent:{route.intent}",
        llm_used=False,
        prompt_template=PROMPT_TEMPLATE_VERSION,
        retrieved_jobs=[],
        intent_route=route,
        workflow=None,
        warnings=[],
    )


def router_unavailable_response(request: ChatRequest, route: IntentRoute) -> ChatResponse:
    if contains_cjk(request.question):
        answer = "我现在没法可靠判断你的请求类型。稍后重试，或者先把问题说得更具体一点。"
    else:
        answer = "I cannot reliably classify this request right now. Try again shortly, or make the job-search task more specific."
    return ChatResponse(
        answer=answer,
        cache_status="skipped",
        cache_similarity=None,
        retrieval_source="intent:router_unavailable",
        llm_used=False,
        prompt_template=PROMPT_TEMPLATE_VERSION,
        retrieved_jobs=[],
        intent_route=route,
        workflow=None,
        warnings=[route.reason] if route.reason else [],
    )


def application_status_response(request: ChatRequest, route: IntentRoute) -> ChatResponse:
    if contains_cjk(request.question):
        answer = "我识别到你想查 application tracker 状态。当前 chat router 已经能分到这个 intent；下一步可以把 applications 列表作为 observation 接进来，返回 saved/applied/follow-up board。"
    else:
        answer = (
            "I routed this as an application-status query. The next backend step is to attach the application records "
            "as an observation, then return a saved/applied/follow-up board."
        )
    return ChatResponse(
        answer=answer,
        cache_status="skipped",
        cache_similarity=None,
        retrieval_source="intent:application_status_query",
        llm_used=False,
        prompt_template=PROMPT_TEMPLATE_VERSION,
        retrieved_jobs=[],
        intent_route=route,
        workflow=CopilotWorkflow(
            title="Application status workflow",
            tool_calls=[
                CopilotToolCall(
                    name="intent_router",
                    title=f"Route intent: {route.intent}",
                    status="completed",
                    summary=route.reason,
                )
            ],
        ),
        warnings=[],
    )


async def resume_audit_response(request: ChatRequest, route: IntentRoute) -> ChatResponse:
    warnings: list[str] = []
    resume_context = request.resume_context.strip()
    workflow = CopilotWorkflow(
        title="Resume review workflow",
        tool_calls=[
            CopilotToolCall(
                name="intent_router",
                title=f"Route intent: {route.intent}",
                status="completed",
                summary=route.reason,
            ),
            CopilotToolCall(
                name="resume_context_check",
                title="Read resume context",
                status="completed" if resume_context else "needs_input",
                summary=(
                    "Resume context is available for direct review."
                    if resume_context
                    else "No resume text was attached to this chat request."
                ),
            ),
        ],
    )

    if not resume_context:
        if contains_cjk(request.question):
            answer = "可以，但我现在没读到你的 resume 内容。先上传或粘贴 resume，我就能直接做评估，不需要 job_id。"
        else:
            answer = "Yes, but I do not see resume text attached yet. Upload or paste it and I can review it directly without a job id."
        return ChatResponse(
            answer=answer,
            cache_status="skipped",
            cache_similarity=None,
            retrieval_source="intent:resume_audit",
            llm_used=False,
            prompt_template=PROMPT_TEMPLATE_VERSION,
            retrieved_jobs=[],
            intent_route=route,
            workflow=workflow,
            warnings=[],
        )

    fallback = local_resume_audit_answer(request, resume_context)
    answer, llm_used = await response_from_observation(
        request=request,
        route=route,
        observation={
            "workflow": "resume_audit",
            "resume_context": resume_context[:6000],
            "review_focus": ["positioning", "strengths", "gaps", "rewrite suggestions"],
            "retrieval": "skipped because the user asked for a direct resume review, not company/job matching",
        },
        fallback=fallback,
        warnings=warnings,
    )

    return ChatResponse(
        answer=answer,
        cache_status="disabled",
        cache_similarity=None,
        retrieval_source="intent:resume_audit",
        llm_used=llm_used,
        prompt_template=PROMPT_TEMPLATE_VERSION,
        retrieved_jobs=[],
        intent_route=route,
        workflow=workflow,
        warnings=warnings,
    )


def local_resume_audit_answer(request: ChatRequest, resume_context: str) -> str:
    skills = extract_visible_resume_terms(resume_context)
    visible = ", ".join(skills[:8]) if skills else "your projects, skills, and experience"
    if contains_cjk(request.question):
        return (
            "可以。我先按“简历体检”来评估，不需要 job_id。\n\n"
            f"我现在能看到的核心信号是：{visible}。\n\n"
            "建议优先检查三件事：第一，项目 bullet 是否写清楚影响和结果；第二，技术栈是否和目标岗位关键词贴近；第三，最强的后端/系统/云相关经历是否放在最显眼的位置。"
            "如果你接下来问“我的 resume 适合哪个公司”，我再去职位库里做匹配。"
        )
    return (
        "Yes. I will treat this as a direct resume audit, so no job id is needed.\n\n"
        f"The visible signals I can use are: {visible}.\n\n"
        "I would first check whether your project bullets show measurable impact, whether the stack matches your target roles, "
        "and whether your strongest backend, systems, or cloud experience is prominent."
    )


def extract_visible_resume_terms(resume_context: str) -> list[str]:
    candidates = [
        "Java",
        "Spring Boot",
        "Python",
        "FastAPI",
        "React",
        "TypeScript",
        "PostgreSQL",
        "Redis",
        "AWS",
        "Docker",
        "Kubernetes",
        "OpenTelemetry",
        "LLM",
        "RAG",
        "pgvector",
    ]
    text = resume_context.lower()
    return [term for term in candidates if term.lower() in text]


async def response_from_observation(
    request: ChatRequest,
    route: IntentRoute,
    observation: dict[str, Any],
    fallback: str,
    warnings: list[str],
) -> tuple[str, bool]:
    if not request.use_llm or not llm_configured():
        return fallback, False
    prompt = json.dumps(
        {
            "user_question": request.question,
            "intent_route": route.model_dump(mode="json"),
            "observation": observation,
            "fallback_answer": fallback,
        },
        ensure_ascii=False,
        indent=2,
    )
    try:
        with trace_step("deepseek_llm_call", {"provider": llm_provider_name(), "prompt_chars": len(prompt)}):
            answer = (
                await call_chat_llm(
                    prompt,
                    instructions=(
                        "You are an AI job-search copilot response generator. "
                        "Use the structured observation as the source of truth. "
                        "Do not claim actions succeeded unless observation.execution.status is success. "
                        "Keep the answer concise and action-oriented."
                    ),
                )
            ).strip()
            if not answer:
                raise ValueError("LLM returned an empty action response.")
        return answer, True
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        warnings.append(f"LLM response generation failed after routing; used template response. {exc}")
        return fallback, False


def normalized_application_stage(value: str) -> str:
    stage = value.lower().strip()
    if stage in {"saved", "applied", "oa", "interview", "rejected", "offer"}:
        return stage
    return "saved"


def retrieve_relevant_jobs(
    question: str,
    resume_context: str,
    jobs: list[JobPosting],
    query_embedding: list[float],
    provider: EmbeddingProvider,
    top_k: int,
    warnings: list[str],
) -> tuple[list[ChatRetrievedJob], str]:
    try:
        return retrieve_jobs_pgvector(question, resume_context, jobs, query_embedding, provider, top_k, warnings), "pgvector"
    except Exception:
        if os.getenv("ALLOW_MEMORY_VECTOR_SEARCH", "").lower() not in {"1", "true", "yes", "on"}:
            raise
        warnings.append("Demo mode: pgvector was unavailable; used in-memory vector retrieval.")
        return retrieve_jobs_memory(question, resume_context, jobs, query_embedding, provider, top_k), "memory-demo"


def retrieve_jobs_pgvector(
    question: str,
    resume_context: str,
    jobs: list[JobPosting],
    query_embedding: list[float],
    provider: EmbeddingProvider,
    top_k: int,
    warnings: list[str],
) -> list[ChatRetrievedJob]:
    import psycopg

    database_url = os.getenv("DATABASE_URL", "postgresql://jobmatch:jobmatch@localhost:5432/jobmatch")

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            filtered_ids = [job.id for job in structured_prefilter(question, jobs)] or [job.id for job in jobs]
            if os.getenv("SYNC_EMBEDDINGS_ON_CHAT", "").lower() in {"1", "true", "yes", "on"}:
                sync_missing_embeddings(cursor, provider, [job for job in jobs if job.id in filtered_ids])

            cursor.execute(
                """
                SELECT count(*)
                FROM job_postings
                WHERE id = ANY(%s) AND embedding IS NOT NULL
                """,
                (filtered_ids,),
            )
            embedded_count = cursor.fetchone()[0]
            if embedded_count < len(filtered_ids):
                warnings.append(
                    f"Embedding coverage is {embedded_count}/{len(filtered_ids)} jobs for this search; "
                    "run the embedding backfill job to expand pgvector retrieval coverage."
                )

            cursor.execute(
                """
                SELECT id, company, title, location, level, work_mode,
                       1 - (embedding <=> %s::vector) AS similarity,
                       description, required_skills, nice_to_have_skills
                FROM job_postings
                WHERE id = ANY(%s) AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (vector_literal(query_embedding), filtered_ids, vector_literal(query_embedding), top_k * 5),
            )
            rows = cursor.fetchall()

    return diversify_retrieved_jobs([
        ChatRetrievedJob(
            id=row[0],
            company=row[1],
            title=row[2],
            location=row[3],
            level=row[4],
            work_mode=row[5],
            score=round(max(0.0, min(1.0, float(row[6] or 0))) * 100),
            reason=job_reason_from_fields(question, row[7], row[8], row[9], resume_context),
        )
        for row in rows
    ])[:top_k]


def sync_missing_embeddings(cursor, provider: EmbeddingProvider, jobs: list[JobPosting]) -> None:
    for job in jobs:
        fingerprint = job_fingerprint(job)
        cursor.execute("SELECT fingerprint, embedding IS NOT NULL FROM job_postings WHERE id = %s", (job.id,))
        row = cursor.fetchone()
        if row and row[0] == fingerprint and row[1]:
            continue
        embedding = embed_document(provider, job_search_text(job), job.title)
        cursor.execute(
            """
            INSERT INTO job_postings (
              id, company, title, location, source, source_url, description,
              required_skills, nice_to_have_skills, level, work_mode, fingerprint, embedding
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
            ON CONFLICT (id) DO UPDATE SET
              company = EXCLUDED.company,
              title = EXCLUDED.title,
              location = EXCLUDED.location,
              source = EXCLUDED.source,
              source_url = EXCLUDED.source_url,
              description = EXCLUDED.description,
              required_skills = EXCLUDED.required_skills,
              nice_to_have_skills = EXCLUDED.nice_to_have_skills,
              level = EXCLUDED.level,
              work_mode = EXCLUDED.work_mode,
              fingerprint = EXCLUDED.fingerprint,
              embedding = EXCLUDED.embedding
            """,
            (
                job.id,
                job.company,
                job.title,
                job.location,
                job.source,
                job.source_url,
                job.description,
                job.required_skills,
                job.nice_to_have_skills,
                job.level,
                job.work_mode,
                fingerprint,
                vector_literal(embedding),
            ),
        )


def retrieve_jobs_memory(
    question: str,
    resume_context: str,
    jobs: list[JobPosting],
    query_embedding: list[float],
    provider: EmbeddingProvider,
    top_k: int,
) -> list[ChatRetrievedJob]:
    candidates = structured_prefilter(question, jobs) or jobs
    scored: list[tuple[float, JobPosting]] = []
    for job in candidates:
        job_embedding = embed_document(provider, job_search_text(job), job.title)
        score = cosine_similarity(query_embedding, job_embedding)
        scored.append((score, job))

    scored.sort(key=lambda item: item[0], reverse=True)
    retrieved: list[ChatRetrievedJob] = []
    for score, job in scored:
        retrieved.append(
            ChatRetrievedJob(
                id=job.id,
                company=job.company,
                title=job.title,
                location=job.location,
                level=job.level,
                work_mode=job.work_mode,
                score=round(max(0.0, min(1.0, score)) * 100),
                reason=job_reason(job, question, resume_context),
            )
        )
        if len(diversify_retrieved_jobs(retrieved)) >= top_k:
            break

    return diversify_retrieved_jobs(retrieved)[:top_k]


def diversify_retrieved_jobs(jobs: list[ChatRetrievedJob]) -> list[ChatRetrievedJob]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[ChatRetrievedJob] = []
    for job in jobs:
        key = (job.company.lower(), job.title.lower(), job.location.lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def structured_prefilter(question: str, jobs: list[JobPosting]) -> list[JobPosting]:
    text = question.lower()
    filtered = jobs
    location_keywords = ["seattle", "bellevue", "redmond", "kirkland", "remote"]
    matched_locations = [location for location in location_keywords if location in text]
    if matched_locations:
        filtered = [
            job
            for job in filtered
            if any(location in f"{job.location} {job.work_mode}".lower() for location in matched_locations)
        ]

    if "new grad" in text or "entry" in text or "junior" in text:
        filtered = [job for job in filtered if job.level in {"new-grad", "entry", "intern"}]
    elif "intern" in text or "internship" in text:
        filtered = [job for job in filtered if job.level == "intern"]

    return filtered


def build_prompt(
    request: ChatRequest,
    retrieved_jobs: list[ChatRetrievedJob],
    route: IntentRoute | None = None,
) -> str:
    job_context = [job.model_dump() for job in retrieved_jobs]
    payload = {
        "prompt_template": PROMPT_TEMPLATE_VERSION,
        "intent_route": route.model_dump(mode="json") if route else None,
        "user_question": request.question,
        "resume_context": request.resume_context[:6000],
        "retrieved_jobs": job_context,
        "instructions": [
            "Use only the retrieved jobs and provided resume context.",
            "Rank the most relevant jobs and explain the evidence.",
            "If the user asks for resume help, recommend which projects or skills to emphasize.",
            "Do not claim application results or hidden company facts.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def sse_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def stream_text(text: str) -> Iterator[str]:
    for chunk in re.findall(r"\S+\s*", text):
        yield sse_event("chunk", {"content": chunk})


def stream_completed_response(response: ChatResponse) -> Iterator[str]:
    yield from stream_text(response.answer)
    yield sse_event("done", response.model_dump(mode="json"))


DEFAULT_CHAT_INSTRUCTIONS = (
    "You are an AI job-search copilot. Use the retrieved RAG context to answer "
    "job-search, resume-matching, and resume-tailoring questions concisely. "
    "Do not invent hidden company facts."
)


def chat_max_output_tokens() -> int:
    return int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "1200"))


async def call_chat_llm(prompt: str, instructions: str | None = None) -> str:
    if os.getenv("DEEPSEEK_API_KEY"):
        return await call_deepseek_chat(prompt, instructions)

    api_key = os.getenv("OPENAI_API_KEY", "")
    body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
        "instructions": instructions or DEFAULT_CHAT_INSTRUCTIONS,
        "input": prompt,
        "max_output_tokens": chat_max_output_tokens(),
    }
    return extract_output_text(await call_responses_api_async(api_key, body))


async def call_chat_llm_stream(prompt: str, instructions: str | None = None) -> AsyncIterator[str]:
    if os.getenv("DEEPSEEK_API_KEY"):
        async for chunk in call_deepseek_chat_stream(prompt, instructions):
            yield chunk
        return

    for chunk in stream_plain_text(await call_chat_llm(prompt, instructions)):
        yield chunk


def deepseek_chat_body(prompt: str, instructions: str | None, model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": instructions or DEFAULT_CHAT_INSTRUCTIONS},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": chat_max_output_tokens(),
    }


async def call_deepseek_chat(prompt: str, instructions: str | None = None) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    payload = await post_json_async(
        DEEPSEEK_CHAT_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        body=deepseek_chat_body(prompt, instructions, model),
        timeout=60,
        service="deepseek-chat",
        model=model,
    )
    choices = payload.get("choices") or []
    content = (choices[0].get("message") or {}).get("content") if choices else None
    if not isinstance(content, str) or not content:
        raise LLMRequestError("deepseek-chat returned no message content.")
    return content


async def call_deepseek_chat_stream(prompt: str, instructions: str | None = None) -> AsyncIterator[str]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    body = deepseek_chat_body(prompt, instructions, model)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    async for raw_line in stream_sse_lines(
        DEEPSEEK_CHAT_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        body=body,
        timeout=60,
        service="deepseek-chat",
    ):
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if data == "[DONE]":
            break
        if not data:
            continue
        payload = json.loads(data)
        if isinstance(payload.get("usage"), dict):
            record_usage("deepseek-chat", model, payload["usage"])
        choices = payload.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            yield content


def stream_plain_text(text: str) -> Iterator[str]:
    for chunk in re.findall(r"\S+\s*", text):
        yield chunk


def local_chat_answer(request: ChatRequest, retrieved_jobs: list[ChatRetrievedJob]) -> str:
    if not retrieved_jobs:
        return "I could not find matching jobs from the current database. Try adding a target title, location, level, or skills."

    chinese = contains_cjk(request.question)
    lines: list[str]
    if chinese:
        lines = [
            "我从职位库里找到了最相关的岗位；详细 AI 生成暂时不可用，所以这里返回本地检索结果："
        ]
        for index, job in enumerate(retrieved_jobs[:5], start=1):
            lines.append(f"{index}. {job.company} - {job.title}（{job.location}，{job.level}）：{job.reason}")
        lines.append("等详细 AI 生成恢复后，我可以继续给出更细的匹配解释和简历修改建议。")
    else:
        lines = ["I retrieved the most relevant jobs from the vector index:"]
        for index, job in enumerate(retrieved_jobs[:5], start=1):
            lines.append(f"{index}. {job.company} - {job.title} ({job.location}, {job.level}): {job.reason}")
        lines.append("Detailed AI generation is unavailable right now, so this is the local retrieval answer.")
    return "\n".join(lines)


def platform_tooling_response(question: str = "", route: IntentRoute | None = None) -> ChatResponse:
    if contains_cjk(question):
        answer = (
            "站内 AI chat 不直接调用 MCP server。它的主路径是产品内 RAG：先把问题 embedding，"
            "用 pgvector 找相关 jobs，再把 retrieved jobs 交给 LLM 生成回答。\n\n"
            "MCP server 是给外部 AI agent 用的集成层，目前暴露这些 tools：\n"
            "1. `search_jobs` - 按 query、location、level、skills 搜索职位库。\n"
            "2. `get_job_details` - 根据 job IDs 返回完整职位描述和技能要求；它不做简历打分，让外部 AI agent 自己基于证据判断 fit。\n"
            "3. `prepare_application_action` - 校验 job 并生成结构化 application tracker payload。\n\n"
            "所以分工是：站内 chat 用 RAG 做产品体验；外部 agent 用 MCP tools 编排搜索、读取详情、准备申请动作。"
        )
    else:
        answer = (
            "This web chat does not call the MCP server. It uses the first-party RAG flow for job-search questions: "
            "embed the question, retrieve relevant jobs with pgvector, then ask the LLM to answer from that context.\n\n"
            "The MCP server is a separate external-agent integration layer. It exposes these tools:\n"
            "1. `search_jobs` - search the job database by query, location, level, and skills.\n"
            "2. `get_job_details` - return full job descriptions and skill requirements for selected job IDs; it does not score resumes, so the external AI agent evaluates fit from the returned evidence.\n"
            "3. `prepare_application_action` - validate a job and prepare a structured payload for the application tracker.\n\n"
            "So the split is: web chat uses RAG for the product experience; external agents use MCP tools to compose workflows outside the web UI."
        )
    return ChatResponse(
        answer=answer,
        cache_status="disabled",
        retrieval_source="platform_context",
        llm_used=False,
        prompt_template=PROMPT_TEMPLATE_VERSION,
        retrieved_jobs=[],
        intent_route=route,
        workflow=None,
        warnings=[],
    )


def cached_response(
    entry: CacheEntry,
    status: str,
    similarity: float | None,
    warnings: list[str],
) -> ChatResponse | None:
    try:
        response = ChatResponse.model_validate_json(entry.answer)
        if not response.answer.strip():
            return None
        return response.model_copy(
            update={
                "cache_status": status,
                "cache_similarity": similarity,
                "llm_used": False,
                "warnings": warnings + response.warnings,
            }
        )
    except ValueError:
        if not entry.answer.strip():
            return None
        return ChatResponse(
            answer=entry.answer,
            cache_status=status,
            cache_similarity=similarity,
            retrieval_source="cache",
            llm_used=False,
            prompt_template=PROMPT_TEMPLATE_VERSION,
            retrieved_jobs=[],
            warnings=warnings,
        )


def llm_configured() -> bool:
    return bool(os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY"))


def llm_provider_name() -> str:
    if os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return "none"


def embed_query(provider: EmbeddingProvider, text: str) -> list[float]:
    if isinstance(provider, JinaEmbeddingProvider):
        return provider.embed_query(text)
    if isinstance(provider, GeminiEmbeddingProvider) and provider.model == "gemini-embedding-2":
        return provider.embed(f"task: search result | query: {text}")
    return provider.embed(text)


def embed_document(provider: EmbeddingProvider, text: str, title: str | None = None) -> list[float]:
    if isinstance(provider, JinaEmbeddingProvider):
        return provider.embed_document(text)
    if isinstance(provider, GeminiEmbeddingProvider) and provider.model == "gemini-embedding-2":
        safe_title = title or "none"
        return provider.embed(f"title: {safe_title} | text: {text}")
    return provider.embed(text)


def exact_cache_key(request: ChatRequest, metadata: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "question": normalize_text(request.question),
            "resume_context": normalize_text(request.resume_context)[:1000],
            "use_llm": request.use_llm,
            "metadata": metadata,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_metadata(request: ChatRequest, jobs: list[JobPosting]) -> dict[str, Any]:
    fingerprint = hashlib.sha256("|".join(sorted(job.id for job in jobs)).encode("utf-8")).hexdigest()[:16]
    context_hash = hashlib.sha256(normalize_text(request.resume_context).encode("utf-8")).hexdigest()[:16]
    return {
        "conversation_id": request.conversation_id,
        "job_database_version": fingerprint,
        "prompt_template": PROMPT_TEMPLATE_VERSION,
        "embedding_model": embedding_model_version(),
        "answer_mode": "llm" if request.use_llm else "retrieval",
        "llm_model": llm_model_version(),
        "context_hash": context_hash,
        "intent": classify_intent(request.question),
        "question_task": classify_question_task(request.question),
        "question_constraints": question_constraints(request.question),
        "question_terms": significant_question_terms(request.question),
        "question_text": normalize_text(request.question),
    }


def classify_intent(question: str) -> str:
    text = question.lower()
    if any(token in text for token in ["resume", "bullet", "tailor", "emphasize", "skill gap", "missing skill", "简历", "经历"]):
        return "resume_help"
    if any(token in text for token in ["job", "role", "position", "match", "recommend", "符合", "岗位", "职位"]):
        return "job_search"
    return "general_chat"


def classify_question_task(question: str) -> str:
    text = question.lower()
    if any(token in text for token in ["compare", "which", "best", "top", "对比", "比较", "哪个"]):
        return "compare_jobs"
    if any(token in text for token in ["skill gap", "missing skill", "matrix", "skills", "技能", "缺"]):
        return "skill_matrix"
    if any(token in text for token in ["resume", "bullet", "tailor", "emphasize", "简历", "经历"]):
        return "resume_tailoring"
    if any(token in text for token in ["job", "role", "position", "match", "recommend", "岗位", "职位"]):
        return "job_recommendation"
    return "general"


def question_constraints(question: str) -> dict[str, list[str]]:
    text = question.lower()
    locations = [
        location
        for location in ["seattle", "bellevue", "redmond", "kirkland", "remote"]
        if location in text
    ]
    levels: list[str] = []
    if "new grad" in text or "new-grad" in text or "entry" in text or "junior" in text:
        levels.append("new-grad")
    if "intern" in text or "internship" in text:
        levels.append("intern")
    return {
        "locations": sorted(set(locations)),
        "levels": sorted(set(levels)),
    }


def significant_question_terms(question: str) -> list[str]:
    terms: set[str] = set()
    for token in re.findall(r"[a-z0-9+#.]+|[\u4e00-\u9fff]+", question.lower()):
        normalized = normalize_question_term(token)
        if not normalized or normalized in QUESTION_STOPWORDS:
            continue
        if len(normalized) < 2 and not any("\u4e00" <= char <= "\u9fff" for char in normalized):
            continue
        terms.add(normalized)
    return sorted(terms)


def normalize_question_term(term: str) -> str:
    aliases = {
        "postgres": "postgresql",
        "nextjs": "next.js",
        "react.js": "react",
        "nodejs": "node.js",
        "ts": "typescript",
        "js": "javascript",
        "newgrad": "new-grad",
        "new-grad": "new-grad",
        "fullstack": "full-stack",
    }
    return aliases.get(term.strip().lower(), term.strip().lower())


def metadata_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = [
        "conversation_id",
        "job_database_version",
        "prompt_template",
        "embedding_model",
        "answer_mode",
        "llm_model",
        "context_hash",
        "intent",
    ]
    return all(left.get(key) == right.get(key) for key in keys) and question_metadata_compatible(left, right)


def question_metadata_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("question_task") != right.get("question_task"):
        return False
    if left.get("question_constraints") != right.get("question_constraints"):
        return False

    left_terms = set(left.get("question_terms") or [])
    right_terms = set(right.get("question_terms") or [])
    if len(left_terms) < 2 or len(right_terms) < 2:
        return left.get("question_text") == right.get("question_text")

    overlap = left_terms & right_terms
    union = left_terms | right_terms
    if not union:
        return False
    return len(overlap) / len(union) >= SEMANTIC_CACHE_MIN_TERM_JACCARD


def embedding_model_version() -> str:
    provider = os.getenv("EMBEDDING_PROVIDER", "auto").lower().strip()
    if provider == "jina" or (not provider and os.getenv("JINA_API_KEY")):
        return f"jina:{os.getenv('JINA_EMBEDDING_MODEL', 'jina-embeddings-v4')}:{os.getenv('EMBEDDING_DIMENSIONS', str(EMBEDDING_DIMENSIONS))}"
    if provider == "gemini" or (not provider and os.getenv("GEMINI_API_KEY")):
        return f"gemini:{os.getenv('GEMINI_EMBEDDING_MODEL', 'gemini-embedding-2')}:{os.getenv('EMBEDDING_DIMENSIONS', str(EMBEDDING_DIMENSIONS))}"
    if provider == "openai" or (not provider and os.getenv("OPENAI_API_KEY")):
        return f"openai:{os.getenv('OPENAI_EMBEDDING_MODEL', 'text-embedding-3-small')}:{EMBEDDING_DIMENSIONS}"
    return "local-demo"


def llm_model_version() -> str:
    if os.getenv("DEEPSEEK_API_KEY"):
        return f"deepseek:{os.getenv('DEEPSEEK_MODEL', 'deepseek-v4-flash')}"
    if os.getenv("OPENAI_API_KEY"):
        return f"openai:{os.getenv('OPENAI_MODEL', 'gpt-5.4-mini')}"
    return "not_configured"


def serialize_cache_entry(entry: CacheEntry) -> str:
    return json.dumps(
        {
            "answer": entry.answer,
            "embedding": entry.embedding,
            "metadata": entry.metadata,
            "created_at": entry.created_at,
        },
        ensure_ascii=False,
    )


def parse_cache_entry(raw: str | None) -> CacheEntry | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return CacheEntry(
            answer=str(payload["answer"]),
            embedding=[float(value) for value in payload["embedding"]],
            metadata=dict(payload["metadata"]),
            created_at=float(payload["created_at"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def job_search_text(job: JobPosting) -> str:
    return " ".join(
        [
            job.company,
            job.title,
            job.location,
            job.level,
            job.work_mode,
            job.description,
            " ".join(job.required_skills),
            " ".join(job.nice_to_have_skills),
        ]
    )


def job_fingerprint(job: JobPosting) -> str:
    return hashlib.sha256(job_search_text(job).encode("utf-8")).hexdigest()


def job_reason(job: JobPosting, question: str, resume_context: str) -> str:
    return job_reason_from_fields(
        question,
        job.description,
        job.required_skills,
        job.nice_to_have_skills,
        resume_context,
    )


def job_reason_from_fields(
    question: str,
    description: str,
    required_skills: list[str],
    nice_to_have_skills: list[str],
    resume_context: str,
) -> str:
    text = f"{question} {resume_context}".lower()
    skills = required_skills + nice_to_have_skills
    matched = [skill for skill in skills if skill.lower() in text][:4]
    if matched:
        return f"Matches requested skills: {', '.join(matched)}."
    return description[:150].rstrip() + ("..." if len(description) > 150 else "")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def meaningful_question(text: str) -> bool:
    normalized = normalize_text(text)
    # CJK packs far more meaning per character ("\u6211\u60f3\u627e\u5b9e\u4e60" is a complete
    # request at 5 chars), so the length gate must not use the Latin threshold.
    min_length = 4 if contains_cjk(normalized) else 8
    if len(normalized) < min_length:
        return False

    tokens = re.findall(r"[a-z0-9+#.]+|[\u4e00-\u9fff]", normalized)
    return len(tokens) >= 2


def normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    limit = min(len(left), len(right))
    return sum(left[index] * right[index] for index in range(limit))


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"


def contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def is_greeting(text: str) -> bool:
    return normalize_text(text) in {"hi", "hello", "hey", "yo", "你好", "嗨", "哈喽"}
