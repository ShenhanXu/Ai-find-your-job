# AI Job Match & Application Tracker

AI-powered Seattle SDE job matcher that scores resume fit, explains missing skills, suggests targeted resume bullets, and tracks applications from saved to offer.

## What It Does

- Browse Seattle-area SDE roles in an infinite-scroll listing feed.
- Filter listed jobs by new grad or intern roles.
- Refresh the listed Seattle SDE job feed through an incremental crawler.
- Open a single listing detail page before doing resume comparison.
- View the original apply/source link for every listing without signing in.
- Sign in before uploading a resume.
- Upload a PDF, DOCX, TXT, or Markdown resume and extract resume text.
- Save job postings from compliant sources or manual JD import.
- Extract required and nice-to-have skills from postings.
- Explain match score with semantic fit, required skills, level fit, and location fit.
- Use the OpenAI Responses API only when the user clicks compare on a single listing.
- Suggest resume bullet rewrites without inventing experience.
- Track applications across saved, applied, OA, interview, rejected, and offer.

## Tech Stack

- Frontend: Next.js, React, TypeScript
- Backend: FastAPI, Pydantic
- Data: PostgreSQL, pgvector, Redis
- Infra: Docker Compose, AWS deployment notes

## Project Structure

```text
apps/
  api/      FastAPI app and matching logic
  web/      Next.js dashboard
data/       Seattle SDE seed jobs
infra/      Postgres schema and AWS deployment notes
```

## Quick Start

Run the full stack with Docker:

```bash
cp .env.example .env
docker compose up --build
```

Then open:

- Web app: http://localhost:3000
- API docs: http://localhost:8000/docs

Run only the API:

```bash
cd apps/api
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Run only the web app:

```bash
cd apps/web
npm install
npm run dev
```

## API

```text
GET  /health
GET  /company-sources
POST /company-discovery/run
GET  /jobs
GET  /jobs/feed
GET  /jobs/{job_id}
POST /jobs/refresh
POST /ingestion/run
POST /jobs
POST /resume/extract
POST /match
GET  /applications
POST /applications
PATCH /applications/{application_id}
```

Example match request:

```json
{
  "resume": {
    "content": "Seattle software engineer with Python, Java, AWS, SQL, React, TypeScript, distributed systems, and CI/CD experience.",
    "target_role": "Seattle SDE"
  },
  "use_ai": true
}
```

## Matching Model

The app uses a deterministic scoring layer so the demo works without paid APIs:

```text
35% semantic overlap
30% required skills coverage
10% nice-to-have skills coverage
15% level fit
10% location fit
```

The intended production path is:

1. Store resume and job chunks in PostgreSQL with pgvector.
2. Generate embeddings for resumes and job descriptions.
3. Use OpenAI structured outputs for missing skills and bullet rewrite suggestions.
4. Keep deterministic scoring as a guardrail so results are explainable.

## OpenAI Setup

Set these values in `.env`:

```bash
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.4-mini
```

When `OPENAI_API_KEY` is present, `/match` calls the OpenAI Responses API and asks for a strict JSON evaluation. When it is missing or the API call fails, `/match` falls back to the local scoring layer.

## Compliant Job Ingestion

The product should avoid scraping LinkedIn, Indeed, or other sites that prohibit automated extraction. The recommended ingestion plan is:

- Company career pages and ATS connectors first.
- Greenhouse Job Board API
- Lever Postings API
- JSON-LD and static company careers pages
- Adzuna API
- USAJOBS API for public sector roles
- Manual JD paste or URL save for restricted boards

This is important for a portfolio project: it shows product judgment, not just technical ambition.

## Crawler Design

Company sources live in `data/company_sources.json`. The crawler:

1. Loads enabled company sources.
2. Fetches the careers source using the configured connector.
3. Parses ATS JSON, generic JSON, or JobPosting JSON-LD.
4. Filters for Seattle-area SDE/new-grad/intern relevance.
5. Computes a stable fingerprint for each job.
6. Upserts only new or changed jobs.

DeepSeek/OpenAI extraction is intentionally not called during crawling yet. The crawler reports `needs_ai_extraction` when a page cannot be parsed deterministically; that is the future hook for a cheap model fallback.

## Company Discovery

Real Seattle-area company seeds live in `data/company_seed.json`. The discovery worker:

1. Starts from curated real companies and known official career URLs.
2. Converts each company into a `company_sources` candidate.
3. Detects Greenhouse and Lever career URLs when possible.
4. Can optionally use a search API to discover new companies beyond the seed list.
5. Persists sources only when `persist=true`.
6. Leaves sources disabled by default so the app does not suddenly crawl dozens of external sites.

Run discovery:

```bash
curl -X POST "http://localhost:8000/company-discovery/run?persist=true"
```

To enable newly discovered sources immediately:

```bash
curl -X POST "http://localhost:8000/company-discovery/run?persist=true&enable_verified=true"
```

Optional APIs for dynamic discovery:

- `SERPAPI_API_KEY`: Google-style search results through SerpApi.
- `BING_SEARCH_API_KEY`: Bing Web Search API.

No search API is required for the curated seed path. A search API is only needed when you want the worker to discover brand-new companies automatically.

## Demo Positioning

Portfolio tagline:

> AI-powered Seattle SDE job matcher that aggregates compliant job feeds, scores resume fit with embeddings, explains missing skills, suggests targeted resume bullets, and tracks applications end to end.

## Next Milestones

- Add PDF and DOCX resume parsing.
- Persist jobs, resumes, match reports, and tracker records in PostgreSQL.
- Add pgvector embedding search.
- Add Greenhouse and Lever company-board import workers.
- Add auth and per-user workspaces.
- Deploy web and API with a public demo URL.
