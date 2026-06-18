import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .models import ChatRequest, ChatResponse, ChatRetrievedJob, JobPosting
from .openai_eval import call_responses_api, extract_output_text, openai_configured


OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
GEMINI_EMBEDDINGS_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
JINA_EMBEDDINGS_URL = "https://api.jina.ai/v1/embeddings"
EMBEDDING_DIMENSIONS = 1536
SEMANTIC_CACHE_THRESHOLD = float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.92"))
SEMANTIC_CACHE_TTL_SECONDS = int(os.getenv("SEMANTIC_CACHE_TTL_SECONDS", "86400"))
PROMPT_TEMPLATE_VERSION = "job_chat_rag_v1"


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


class OpenAIEmbeddingProvider:
    source = "openai-embeddings"

    def __init__(self) -> None:
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

    def embed(self, text: str) -> list[float]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured.")

        request = urllib.request.Request(
            OPENAI_EMBEDDINGS_URL,
            data=json.dumps({"model": self.model, "input": text[:12000]}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))

        embedding = payload["data"][0]["embedding"]
        return normalize_vector([float(value) for value in embedding])


class GeminiEmbeddingProvider:
    source = "gemini-embeddings"

    def __init__(self) -> None:
        self.api_key = os.getenv("GEMINI_API_KEY", "")
        self.model = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2")
        self.dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", str(EMBEDDING_DIMENSIONS)))

    def embed(self, text: str) -> list[float]:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        request = urllib.request.Request(
            GEMINI_EMBEDDINGS_URL_TEMPLATE.format(model=self.model),
            data=json.dumps(
                {
                    "content": {"parts": [{"text": text[:24000]}]},
                    "output_dimensionality": self.dimensions,
                }
            ).encode("utf-8"),
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))

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

    def _embed(self, text: str, task: str) -> list[float]:
        if not self.api_key:
            raise RuntimeError("JINA_API_KEY is not configured.")

        request = urllib.request.Request(
            JINA_EMBEDDINGS_URL,
            data=json.dumps(
                {
                    "model": self.model,
                    "input": [text[:32000]],
                    "task": task,
                    "dimensions": self.dimensions,
                    "normalized": True,
                    "embedding_type": "float",
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ai-job-match/0.1 (+https://localhost)",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))

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


def chat_with_rag(request: ChatRequest, jobs: list[JobPosting]) -> ChatResponse:
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

    try:
        provider = get_embedding_provider()
        cache = get_semantic_cache()
    except RuntimeError as exc:
        return ChatResponse(
            answer=(
                "Real AI retrieval is not fully configured yet. "
                "Set JINA_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY for embeddings and make sure Redis is running."
            ),
            cache_status="skipped",
            retrieval_source="not_configured",
            llm_used=False,
            prompt_template=PROMPT_TEMPLATE_VERSION,
            retrieved_jobs=[],
            warnings=[str(exc)],
        )
    selected_jobs = [job for job in jobs if not request.job_ids or job.id in request.job_ids]
    metadata = cache_metadata(request, selected_jobs)
    exact_key = exact_cache_key(request, metadata)

    exact = cache.get_exact(exact_key)
    if exact:
        return cached_response(exact, "exact", None, warnings)

    question_context = f"{request.question}\n\nResume context:\n{request.resume_context[:4000]}"
    try:
        query_embedding = embed_query(provider, question_context)
    except Exception as exc:
        return ChatResponse(
            answer="Embedding generation failed, so I did not run semantic cache or RAG retrieval.",
            cache_status="skipped",
            retrieval_source="embedding_error",
            llm_used=False,
            prompt_template=PROMPT_TEMPLATE_VERSION,
            retrieved_jobs=[],
            warnings=[str(exc)],
        )

    semantic, similarity = cache.get_semantic(query_embedding, metadata)
    if semantic:
        return cached_response(semantic, "semantic", similarity, warnings)

    try:
        retrieved, retrieval_source = retrieve_relevant_jobs(
            question=request.question,
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
            cache_status="miss",
            cache_similarity=similarity,
            retrieval_source="pgvector_error",
            llm_used=False,
            prompt_template=PROMPT_TEMPLATE_VERSION,
            retrieved_jobs=[],
            warnings=[str(exc)],
        )
    prompt = build_prompt(request, retrieved)

    llm_used = False
    if request.use_llm and llm_configured():
        try:
            answer = call_chat_llm(prompt)
            llm_used = True
        except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError):
            answer = local_chat_answer(request, retrieved)
            warnings.append("LLM call failed; returned local RAG answer.")
    else:
        answer = local_chat_answer(request, retrieved)
        if request.use_llm:
            warnings.append("DEEPSEEK_API_KEY or OPENAI_API_KEY is not configured; returned local RAG answer.")

    response = ChatResponse(
        answer=answer,
        cache_status="miss",
        cache_similarity=similarity,
        retrieval_source=retrieval_source,
        llm_used=llm_used,
        prompt_template=PROMPT_TEMPLATE_VERSION,
        retrieved_jobs=retrieved,
        warnings=warnings,
    )
    cache.set(
        exact_key,
        CacheEntry(
            answer=response.model_dump_json(),
            embedding=query_embedding,
            metadata=metadata,
            created_at=time.time(),
        ),
    )
    return response


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


def build_prompt(request: ChatRequest, retrieved_jobs: list[ChatRetrievedJob]) -> str:
    job_context = [job.model_dump() for job in retrieved_jobs]
    payload = {
        "prompt_template": PROMPT_TEMPLATE_VERSION,
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


def call_chat_llm(prompt: str) -> str:
    if os.getenv("DEEPSEEK_API_KEY"):
        return call_deepseek_chat(prompt)

    api_key = os.getenv("OPENAI_API_KEY", "")
    body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
        "instructions": (
            "You are an AI job-search copilot. You use RAG context to answer job-search, "
            "resume-matching, and resume-tailoring questions concisely."
        ),
        "input": prompt,
        "max_output_tokens": 900,
    }
    return extract_output_text(call_responses_api(api_key, body))


def call_deepseek_chat(prompt: str) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    body = {
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an AI job-search copilot. Use the retrieved RAG context to answer "
                    "job-search, resume-matching, and resume-tailoring questions concisely. "
                    "Do not invent hidden company facts."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 900,
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        DEEPSEEK_CHAT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))

    return payload["choices"][0]["message"]["content"]


def local_chat_answer(request: ChatRequest, retrieved_jobs: list[ChatRetrievedJob]) -> str:
    if not retrieved_jobs:
        return "I could not find matching jobs from the current database. Try adding a target title, location, level, or skills."

    chinese = contains_cjk(request.question)
    lines: list[str]
    if chinese:
        lines = [
            "我先用向量检索从职位库里找了最相关的岗位；本地没有配置 LLM key，所以这里返回的是 RAG 检索版结果："
        ]
        for index, job in enumerate(retrieved_jobs[:5], start=1):
            lines.append(f"{index}. {job.company} - {job.title}（{job.location}，{job.level}）：{job.reason}")
        lines.append("如果接上 LLM，这些 top jobs 会作为 prompt context，再生成更细的匹配解释和简历修改建议。")
    else:
        lines = ["I retrieved the most relevant jobs from the vector index:"]
        for index, job in enumerate(retrieved_jobs[:5], start=1):
            lines.append(f"{index}. {job.company} - {job.title} ({job.location}, {job.level}): {job.reason}")
        lines.append("With an LLM key configured, these retrieved jobs become prompt context for ranking and resume tailoring.")
    return "\n".join(lines)


def cached_response(entry: CacheEntry, status: str, similarity: float | None, warnings: list[str]) -> ChatResponse:
    try:
        response = ChatResponse.model_validate_json(entry.answer)
        return response.model_copy(
            update={
                "cache_status": status,
                "cache_similarity": similarity,
                "llm_used": False,
                "warnings": warnings + response.warnings,
            }
        )
    except ValueError:
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
        "context_hash": context_hash,
        "intent": classify_intent(request.question),
    }


def classify_intent(question: str) -> str:
    text = question.lower()
    if any(token in text for token in ["job", "role", "position", "match", "recommend", "符合", "岗位", "职位"]):
        return "job_search"
    if any(token in text for token in ["resume", "bullet", "简历", "经历"]):
        return "resume_help"
    return "general_chat"


def metadata_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ["conversation_id", "job_database_version", "prompt_template", "embedding_model", "context_hash", "intent"]
    return all(left.get(key) == right.get(key) for key in keys)


def embedding_model_version() -> str:
    provider = os.getenv("EMBEDDING_PROVIDER", "auto").lower().strip()
    if provider == "jina" or (not provider and os.getenv("JINA_API_KEY")):
        return f"jina:{os.getenv('JINA_EMBEDDING_MODEL', 'jina-embeddings-v4')}:{os.getenv('EMBEDDING_DIMENSIONS', str(EMBEDDING_DIMENSIONS))}"
    if provider == "gemini" or (not provider and os.getenv("GEMINI_API_KEY")):
        return f"gemini:{os.getenv('GEMINI_EMBEDDING_MODEL', 'gemini-embedding-2')}:{os.getenv('EMBEDDING_DIMENSIONS', str(EMBEDDING_DIMENSIONS))}"
    if provider == "openai" or (not provider and os.getenv("OPENAI_API_KEY")):
        return f"openai:{os.getenv('OPENAI_EMBEDDING_MODEL', 'text-embedding-3-small')}:{EMBEDDING_DIMENSIONS}"
    return "local-demo"


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
    if len(normalized) < 8:
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
