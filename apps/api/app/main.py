import os
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .company_discovery import run_company_discovery
from .database import database_health, database_required, load_jobs_from_database, upsert_job, upsert_jobs
from .ingestion import run_ingestion
from .ai_chat import chat_with_rag
from .matcher import match_resume_to_job
from .models import (
    ApplicationCreate,
    ApplicationRecord,
    ApplicationUpdate,
    ChatRequest,
    ChatResponse,
    CompanyDiscoveryRunResult,
    CompanyProbeRunResult,
    CompanySource,
    IngestionRunResult,
    JobFeedResponse,
    JobPosting,
    MatchRequest,
    MatchResult,
    ResumeExtractResult,
)
from .openai_eval import evaluate_with_openai, openai_configured
from .resume_parser import extract_resume_text
from .seed import load_company_sources, load_seed_jobs
from .source_probe import probe_company_sources


app = FastAPI(
    title="AI Job Match API",
    description="Seattle SDE resume-job matching and application tracking API.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def load_jobs_for_app() -> dict[str, JobPosting]:
    try:
        database_jobs = load_jobs_from_database()
        if database_jobs:
            return {job.id: job for job in database_jobs}
        if database_required():
            raise RuntimeError("Postgres is connected, but job_postings is empty.")
    except Exception as exc:
        if database_required():
            raise RuntimeError(f"Database is required but unavailable: {exc}") from exc

    return {job.id: job for job in load_seed_jobs()}


jobs: dict[str, JobPosting] = load_jobs_for_app()
applications: dict[str, ApplicationRecord] = {}


@app.get("/health")
def health() -> dict[str, str]:
    sources = load_company_sources()
    db = database_health()
    return {
        "status": "ok",
        "openai": "configured" if openai_configured() else "not_configured",
        "embedding_provider": os.getenv("EMBEDDING_PROVIDER", "auto"),
        "jina": "configured" if os.getenv("JINA_API_KEY") else "not_configured",
        "gemini": "configured" if os.getenv("GEMINI_API_KEY") else "not_configured",
        "deepseek": "configured" if os.getenv("DEEPSEEK_API_KEY") else "not_configured",
        "database": db["database"],
        "pgvector": db["pgvector"],
        "jobs_total": str(len(jobs)),
        "postgres_jobs_total": db["jobs_total"],
        "sources_total": str(len(sources)),
        "sources_enabled": str(len([source for source in sources if source.enabled])),
    }


@app.get("/company-sources", response_model=list[CompanySource])
def list_company_sources() -> list[CompanySource]:
    return load_company_sources()


@app.post("/company-discovery/run", response_model=CompanyDiscoveryRunResult)
def run_company_discovery_endpoint(
    persist: bool = False,
    enable_verified: bool = False,
    use_search: bool = False,
    limit: int | None = None,
) -> CompanyDiscoveryRunResult:
    return run_company_discovery(
        persist=persist,
        enable_verified=enable_verified,
        use_search=use_search,
        limit=limit,
    )


@app.post("/company-sources/probe", response_model=CompanyProbeRunResult)
def probe_company_sources_endpoint(persist: bool = False, limit: int | None = None) -> CompanyProbeRunResult:
    return probe_company_sources(persist=persist, limit=limit)


@app.get("/jobs", response_model=list[JobPosting])
def list_jobs(query: str = "", location: str = "") -> list[JobPosting]:
    return filter_jobs(query=query, location=location)


@app.get("/jobs/feed", response_model=JobFeedResponse)
def list_job_feed(
    cursor: int = 0,
    limit: int = 6,
    query: str = "",
    location: str = "",
    audience: str = "",
) -> JobFeedResponse:
    filtered = filter_jobs(query=query, location=location, audience=audience)
    safe_cursor = max(0, cursor)
    safe_limit = min(20, max(1, limit))
    next_cursor = safe_cursor + safe_limit
    items = filtered[safe_cursor:next_cursor]

    return JobFeedResponse(
        items=items,
        next_cursor=next_cursor if next_cursor < len(filtered) else None,
        total=len(filtered),
    )


@app.post("/jobs/refresh", response_model=list[JobPosting])
def refresh_jobs() -> list[JobPosting]:
    run_ingestion(jobs)
    upsert_jobs(list(jobs.values()))
    return list(jobs.values())


@app.post("/ingestion/run", response_model=IngestionRunResult)
def run_job_ingestion() -> IngestionRunResult:
    result = run_ingestion(jobs)
    upsert_jobs(list(jobs.values()))
    return result


@app.get("/jobs/{job_id}", response_model=JobPosting)
def get_job(job_id: str) -> JobPosting:
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
    return jobs[job_id]


@app.post("/jobs", response_model=JobPosting)
def save_job(job: JobPosting) -> JobPosting:
    jobs[job.id] = job
    upsert_job(job)
    return job


@app.post("/resume/extract", response_model=ResumeExtractResult)
async def extract_resume(file: UploadFile = File(...)) -> ResumeExtractResult:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Resume file is empty.")

    try:
        text = extract_resume_text(file.filename or "resume.txt", content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if len(text) < 80:
        raise HTTPException(status_code=400, detail="Resume text is too short to evaluate.")

    return ResumeExtractResult(filename=file.filename or "resume", content=text)


@app.post("/match", response_model=list[MatchResult])
def match_jobs(request: MatchRequest) -> list[MatchResult]:
    selected_jobs = list(jobs.values())
    if request.job_ids:
        selected_jobs = [jobs[job_id] for job_id in request.job_ids if job_id in jobs]

    if not selected_jobs:
        raise HTTPException(status_code=404, detail="No matching jobs found.")

    results = [match_resume_to_job(request.resume, job) for job in selected_jobs]
    if request.use_ai and openai_configured():
        results = [evaluate_with_openai(request.resume, result.job, result) for result in results]

    return sorted(results, key=lambda result: result.score, reverse=True)


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    return chat_with_rag(request, list(jobs.values()))


@app.get("/applications", response_model=list[ApplicationRecord])
def list_applications() -> list[ApplicationRecord]:
    return list(applications.values())


@app.post("/applications", response_model=ApplicationRecord)
def create_application(payload: ApplicationCreate) -> ApplicationRecord:
    if payload.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")

    record = ApplicationRecord(
        id=str(uuid4()),
        job_id=payload.job_id,
        stage=payload.stage,
        notes=payload.notes,
        follow_up_on=payload.follow_up_on,
    )
    applications[record.id] = record
    return record


@app.patch("/applications/{application_id}", response_model=ApplicationRecord)
def update_application(application_id: str, payload: ApplicationUpdate) -> ApplicationRecord:
    if application_id not in applications:
        raise HTTPException(status_code=404, detail="Application not found.")

    existing = applications[application_id]
    updated = existing.model_copy(
        update={
            "stage": payload.stage if payload.stage is not None else existing.stage,
            "notes": payload.notes if payload.notes is not None else existing.notes,
            "follow_up_on": payload.follow_up_on,
        }
    )
    applications[application_id] = updated
    return updated


def filter_jobs(query: str = "", location: str = "", audience: str = "") -> list[JobPosting]:
    query_lower = query.lower().strip()
    location_lower = location.lower().strip()
    audience_lower = audience.lower().strip()

    results = list(jobs.values())
    if query_lower:
        results = [
            job
            for job in results
            if query_lower
            in f"{job.title} {job.company} {job.description} {' '.join(job.required_skills)}".lower()
        ]
    if location_lower:
        results = [job for job in results if location_lower in job.location.lower()]
    if audience_lower == "new-grad":
        results = [job for job in results if job.level in {"new-grad", "entry"}]
    elif audience_lower == "intern":
        results = [job for job in results if job.level == "intern"]

    return results
