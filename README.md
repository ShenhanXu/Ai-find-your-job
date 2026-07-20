# Seattle SDE Jobs

A local-first job board prototype for browsing Seattle-area software engineering roles.

## Current Scope

- Live job listings ingested from 36 real Greenhouse/Lever boards by a distributed pipeline
  (scheduler → Redis Streams → crawl workers → idempotent upserter → incremental embedder);
  see [apps/ingestion](apps/ingestion/README.md) and [docs/INGESTION_PIPELINE_DESIGN.md](docs/INGESTION_PIPELINE_DESIGN.md).
- Priority sources re-crawl every 30 minutes; postings a company takes down are auto-closed.
- Search by company, title, description, level, work mode, and skills.
- Filter by Seattle-area location.
- Filter for all roles, new grad / entry roles, or internships.
- Open a dedicated job detail page.
- Ask an AI job copilot question that retrieves relevant jobs with pgvector and builds a RAG prompt.
- Route chat requests through a hybrid intent router that selects RAG, tool workflows, clarifying questions, or unsupported fallbacks.
- Stream chat answers into a single AI bubble as the LLM generates text.
- Render generative UI workflow blocks from copilot tool outputs: comparison cards, skill-gap matrix, resume checklist, and actions.
- Expose job search, job detail lookup, and application action preparation through an MCP-compatible stdio server.
- Use `data/seed_jobs.json` only as a demo-mode fallback when no database is available;
  retire seed rows with `python -m ingestion.retire_seed` once real data is flowing.

## Tech Stack

- Frontend: Next.js, React, TypeScript
- Chat backend: FastAPI `POST /chat`
- Job feed: FastAPI `/jobs/feed` backed by PostgreSQL
- RAG retrieval: PostgreSQL + pgvector
- Embeddings: Jina embeddings via `JINA_API_KEY`
- LLM responses: DeepSeek chat completions via `DEEPSEEK_API_KEY`
- Cache: disabled in the chat path for answer correctness
- AI tool layer: LLM/rule hybrid intent router + typed copilot workflow tools + MCP stdio server
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

Run the MCP server for local AI-agent integrations:

```bash
cd apps/api
python -m app.mcp_server
```

The MCP server supports:

```text
search_jobs
get_job_details
prepare_application_action
```

## Notes

The AI copilot retrieves a small, relevant job context first, then asks the LLM only when needed. Chat response caching is currently disabled so different questions never reuse stale answers. In real mode, missing embeddings or pgvector are reported clearly instead of silently falling back to fake local retrieval.
