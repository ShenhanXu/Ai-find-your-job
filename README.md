# Seattle SDE Jobs

A local-first job board prototype for browsing Seattle-area software engineering roles.

## Current Scope

- 100 seeded software job listings stored in PostgreSQL.
- Search by company, title, description, level, work mode, and skills.
- Filter by Seattle-area location.
- Filter for all roles, new grad / entry roles, or internships.
- Open a dedicated job detail page.
- Ask an AI job copilot question that checks Redis cache, retrieves relevant jobs with pgvector, and builds a RAG prompt.
- Use `data/seed_jobs.json` only as seed input for PostgreSQL.

## Tech Stack

- Frontend: Next.js, React, TypeScript
- Chat backend: FastAPI `POST /chat`
- Job feed: FastAPI `/jobs/feed` backed by PostgreSQL
- RAG retrieval: PostgreSQL + pgvector
- Embeddings: Jina embeddings via `JINA_API_KEY`
- LLM responses: DeepSeek chat completions via `DEEPSEEK_API_KEY`
- Semantic cache: Redis
- Prompt templates: versioned `job_chat_rag_v1`
- Backend seed input: `data/seed_jobs.json`

Demo-only fallback switches exist for tests:

```text
ALLOW_LOCAL_EMBEDDINGS=true
ALLOW_IN_MEMORY_CACHE=true
ALLOW_MEMORY_VECTOR_SEARCH=true
```

Leave those off for the real project.

## Run

```bash
cd apps/web
npm install
npm run build
npm start
```

Open:

```text
http://localhost:3000
```

Run the API:

```bash
cd apps/api
DATABASE_URL=postgresql://jobmatch:jobmatch@localhost:5432/jobmatch \
REDIS_URL=redis://localhost:6379/0 \
REQUIRE_DATABASE=true \
EMBEDDING_PROVIDER=jina \
python -m uvicorn app.main:app --reload --port 8000
```

Seed PostgreSQL:

```bash
DATABASE_URL=postgresql://jobmatch:jobmatch@localhost:5432/jobmatch \
python -m app.seed_database
```

Backfill real job embeddings after setting `JINA_API_KEY`:

```bash
DATABASE_URL=postgresql://jobmatch:jobmatch@localhost:5432/jobmatch \
JINA_API_KEY=... \
EMBEDDING_PROVIDER=jina \
python -m app.backfill_embeddings
```

## Notes

The AI copilot retrieves a small, relevant job context first, then asks the LLM only when needed. In real mode, missing embeddings, Redis, or pgvector are reported clearly instead of silently falling back to fake local retrieval.
