import logging
import os
import time
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import psycopg

from .auth import create_access_token, hash_password, verify_access_token, verify_password
from .company_discovery import run_company_discovery
from .database import (
    create_user,
    database_health,
    database_required,
    ensure_account_schema,
    ensure_application_schema,
    ensure_pipeline_schema,
    find_application_by_job_db,
    get_application_db,
    list_applications_db,
    save_application_db,
    find_user_by_email,
    get_active_user_resume,
    get_user_by_id,
    list_user_resumes,
    load_jobs_from_database,
    save_user_resume,
    upsert_job,
    upsert_jobs,
)
from .ingestion import run_ingestion
from .ai_chat import chat_with_rag, chat_with_rag_stream
from .llm_client import aclose_clients
from .matcher import match_resume_to_job
from .models import (
    ApplicationCreate,
    ApplicationRecord,
    ApplicationStage,
    ApplicationUpdate,
    AuthRequest,
    AuthResponse,
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
    MatchWorkflowTraceResponse,
    ResumeExtractResult,
    SavedResume,
    UserPublic,
)
from .openai_eval import evaluate_with_openai, openai_configured
from .pipeline_admin import router as pipeline_router
from .resume_parser import extract_resume_text
from .seed import load_company_sources, load_seed_jobs
from .source_probe import probe_company_sources
from .workflow_trace import (
    finish_workflow_trace,
    internal_monitoring_requested,
    parse_trace_step_header,
    reset_workflow_trace,
    start_workflow_trace,
    trace_step,
)


app = FastAPI(
    title="AI Job Match API",
    description="Seattle SDE resume-job matching and application tracking API.",
    version="0.1.0",
)


def frontend_origins() -> list[str]:
    configured = os.getenv("FRONTEND_ORIGINS", "")
    defaults = ["http://localhost:3000", "http://127.0.0.1:3000"]
    origins = [origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()]
    return [*defaults, *origins]


app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_event_handler("shutdown", aclose_clients)
app.include_router(pipeline_router)


try:
    ensure_account_schema()
    ensure_application_schema()
    ensure_pipeline_schema()
except Exception as exc:
    if database_required():
        raise RuntimeError(f"Account schema could not be initialized: {exc}") from exc

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

JOBS_CACHE_TTL_SECONDS = int(os.getenv("JOBS_CACHE_TTL_SECONDS", "60"))
_jobs_refreshed_at = time.monotonic()


def refresh_jobs_cache(force: bool = False) -> None:
    """Pull jobs written by the ingestion pipeline into the in-memory cache.

    The pipeline workers write to Postgres out-of-process, so this API would
    otherwise serve its startup snapshot forever."""
    global _jobs_refreshed_at
    if not force and time.monotonic() - _jobs_refreshed_at < JOBS_CACHE_TTL_SECONDS:
        return
    _jobs_refreshed_at = time.monotonic()
    try:
        database_jobs = load_jobs_from_database()
    except Exception:
        return
    if database_jobs:
        jobs.clear()
        jobs.update({job.id: job for job in database_jobs})


applications_logger = logging.getLogger("app.applications")


# Applications persist to PostgreSQL; the in-memory dict is only the demo-mode
# fallback when the database is unavailable and not required.
def store_list_applications() -> list[ApplicationRecord]:
    try:
        return list_applications_db()
    except Exception as exc:
        if database_required():
            raise
        applications_logger.warning("applications DB unavailable, using memory: %s", exc)
        return list(applications.values())


def store_get_application(application_id: str) -> ApplicationRecord | None:
    try:
        return get_application_db(application_id)
    except Exception as exc:
        if database_required():
            raise
        applications_logger.warning("applications DB unavailable, using memory: %s", exc)
        return applications.get(application_id)


def store_find_application_by_job(job_id: str) -> ApplicationRecord | None:
    try:
        return find_application_by_job_db(job_id)
    except Exception as exc:
        if database_required():
            raise
        applications_logger.warning("applications DB unavailable, using memory: %s", exc)
        return next((record for record in applications.values() if record.job_id == job_id), None)


def store_save_application(record: ApplicationRecord) -> ApplicationRecord:
    try:
        return save_application_db(record)
    except Exception as exc:
        if database_required():
            raise
        applications_logger.warning("applications DB unavailable, using memory: %s", exc)
        applications[record.id] = record
        return record


def token_from_header(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def current_user(authorization: str | None = Header(default=None)) -> UserPublic:
    token = token_from_header(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Log in to continue.")

    user_id = verify_access_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Session expired. Log in again.")

    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists.")
    return user


def optional_user(authorization: str | None = Header(default=None)) -> UserPublic | None:
    token = token_from_header(authorization)
    if not token:
        return None
    user_id = verify_access_token(token)
    return get_user_by_id(user_id) if user_id else None


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


@app.post("/auth/register", response_model=AuthResponse)
def register(payload: AuthRequest) -> AuthResponse:
    try:
        user = create_user(payload.email, payload.name, hash_password(payload.password))
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="An account already exists for this email.") from exc

    return AuthResponse(token=create_access_token(user.id), user=user)


@app.post("/auth/login", response_model=AuthResponse)
def login(payload: AuthRequest) -> AuthResponse:
    record = find_user_by_email(payload.email)
    if not record:
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")

    user, password_hash = record
    if not verify_password(payload.password, password_hash):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")

    return AuthResponse(token=create_access_token(user.id), user=user)


@app.get("/auth/me", response_model=UserPublic)
def me(user: UserPublic = Depends(current_user)) -> UserPublic:
    return user


@app.get("/resumes", response_model=list[SavedResume])
def resumes(user: UserPublic = Depends(current_user)) -> list[SavedResume]:
    return list_user_resumes(user.id)


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
    refresh_jobs_cache()
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
    return jobs[job_id]


@app.post("/jobs", response_model=JobPosting)
def save_job(job: JobPosting) -> JobPosting:
    jobs[job.id] = job
    upsert_job(job)
    return job


@app.post("/resume/extract", response_model=ResumeExtractResult)
async def extract_resume(
    file: UploadFile = File(...),
    user: UserPublic | None = Depends(optional_user),
) -> ResumeExtractResult:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Resume file is empty.")

    try:
        text = extract_resume_text(file.filename or "resume.txt", content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if len(text) < 80:
        raise HTTPException(status_code=400, detail="Resume text is too short to evaluate.")

    saved_resume: SavedResume | None = None
    filename = file.filename or "resume"
    if user:
        saved_resume = save_user_resume(user.id, filename, text)

    return ResumeExtractResult(
        filename=filename,
        content=text,
        resume_id=saved_resume.id if saved_resume else None,
        saved=bool(saved_resume),
    )


@app.post("/match", response_model=list[MatchResult] | MatchWorkflowTraceResponse)
def match_jobs(
    request: MatchRequest,
    x_jobtrace_monitoring: str | None = Header(default=None, alias="x-jobtrace-monitoring"),
    x_jobtrace_internal_steps: str | None = Header(default=None, alias="x-jobtrace-internal-steps"),
    x_jobtrace_run_id: str | None = Header(default=None, alias="x-jobtrace-run-id"),
) -> list[MatchResult] | MatchWorkflowTraceResponse:
    enabled = internal_monitoring_requested(x_jobtrace_monitoring)
    token = start_workflow_trace(
        enabled=enabled,
        selected_steps=parse_trace_step_header(x_jobtrace_internal_steps),
        run_id=x_jobtrace_run_id,
        level=x_jobtrace_monitoring or "external",
    )
    try:
        if enabled:
            with trace_step("workflow_total", {"route": "/match"}):
                results = run_match_workflow(request)
        else:
            results = run_match_workflow(request)

        workflow_trace = finish_workflow_trace()
        if workflow_trace:
            return MatchWorkflowTraceResponse(results=results, workflow_trace=workflow_trace)
        return results
    finally:
        reset_workflow_trace(token)


def run_match_workflow(request: MatchRequest) -> list[MatchResult]:
    refresh_jobs_cache()
    with trace_step("select_jobs", {"requested_job_ids": len(request.job_ids or [])}):
        selected_jobs = list(jobs.values())
        if request.job_ids:
            selected_jobs = [jobs[job_id] for job_id in request.job_ids if job_id in jobs]

    if not selected_jobs:
        raise HTTPException(status_code=404, detail="No matching jobs found.")

    with trace_step("match_resume", {"job_count": len(selected_jobs)}):
        results = [match_resume_to_job(request.resume, job) for job in selected_jobs]
    if request.use_ai and openai_configured():
        with trace_step("ai_evaluation", {"job_count": len(results), "provider": "openai"}):
            results = [evaluate_with_openai(request.resume, result.job, result) for result in results]

    with trace_step("sort_results", {"result_count": len(results)}):
        return sorted(results, key=lambda result: result.score, reverse=True)


@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    user: UserPublic | None = Depends(optional_user),
    x_jobtrace_monitoring: str | None = Header(default=None, alias="x-jobtrace-monitoring"),
    x_jobtrace_internal_steps: str | None = Header(default=None, alias="x-jobtrace-internal-steps"),
    x_jobtrace_run_id: str | None = Header(default=None, alias="x-jobtrace-run-id"),
) -> ChatResponse:
    refresh_jobs_cache()
    request = request_with_user_resume(request, user)
    enabled = internal_monitoring_requested(x_jobtrace_monitoring)
    token = start_workflow_trace(
        enabled=enabled,
        selected_steps=parse_trace_step_header(x_jobtrace_internal_steps),
        run_id=x_jobtrace_run_id,
        level=x_jobtrace_monitoring or "external",
    )
    try:
        if enabled:
            with trace_step("workflow_total", {"route": "/chat"}):
                response = await chat_with_rag(request, list(jobs.values()), action_executor=execute_application_action_from_chat)
        else:
            response = await chat_with_rag(request, list(jobs.values()), action_executor=execute_application_action_from_chat)

        workflow_trace = finish_workflow_trace()
        if workflow_trace:
            return response.model_copy(update={"workflow_trace": workflow_trace})
        return response
    finally:
        reset_workflow_trace(token)


@app.post("/chat/stream")
async def stream_chat(request: ChatRequest, user: UserPublic | None = Depends(optional_user)) -> StreamingResponse:
    refresh_jobs_cache()
    request = request_with_user_resume(request, user)
    return StreamingResponse(
        chat_with_rag_stream(request, list(jobs.values()), action_executor=execute_application_action_from_chat),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def request_with_user_resume(request: ChatRequest, user: UserPublic | None) -> ChatRequest:
    if request.resume_context.strip() or not user:
        return request
    resume = get_active_user_resume(user.id)
    if not resume:
        return request
    return request.model_copy(update={"resume_context": resume.content})


def execute_application_action_from_chat(payload: dict[str, Any]) -> dict[str, Any]:
    job_id = str(payload.get("job_id", ""))
    if job_id not in jobs:
        raise ValueError(f"Job not found: {job_id}")

    stage = ApplicationStage(str(payload.get("stage", "saved")))
    notes = str(payload.get("notes", ""))
    follow_up_on = payload.get("follow_up_on")

    existing = store_find_application_by_job(job_id)
    if existing:
        updated = existing.model_copy(
            update={
                "stage": stage,
                "notes": notes or existing.notes,
                "follow_up_on": str(follow_up_on) if follow_up_on else existing.follow_up_on,
            }
        )
        store_save_application(updated)
        return {
            "status": "success",
            "action": "updated",
            "record": updated.model_dump(mode="json"),
        }

    record = ApplicationRecord(
        id=str(uuid4()),
        job_id=job_id,
        stage=stage,
        notes=notes,
        follow_up_on=str(follow_up_on) if follow_up_on else None,
    )
    store_save_application(record)
    return {
        "status": "success",
        "action": "created",
        "record": record.model_dump(mode="json"),
    }


@app.get("/applications", response_model=list[ApplicationRecord])
def list_applications() -> list[ApplicationRecord]:
    return store_list_applications()


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
    return store_save_application(record)


@app.patch("/applications/{application_id}", response_model=ApplicationRecord)
def update_application(application_id: str, payload: ApplicationUpdate) -> ApplicationRecord:
    existing = store_get_application(application_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Application not found.")

    updated = existing.model_copy(
        update={
            "stage": payload.stage if payload.stage is not None else existing.stage,
            "notes": payload.notes if payload.notes is not None else existing.notes,
            "follow_up_on": payload.follow_up_on,
        }
    )
    return store_save_application(updated)


def filter_jobs(query: str = "", location: str = "", audience: str = "") -> list[JobPosting]:
    refresh_jobs_cache()
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
