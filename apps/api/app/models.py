from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ApplicationStage(str, Enum):
    saved = "saved"
    applied = "applied"
    oa = "oa"
    interview = "interview"
    rejected = "rejected"
    offer = "offer"


class JobPosting(BaseModel):
    id: str
    company: str
    title: str
    location: str
    source: str
    source_url: Optional[str] = Field(default=None, alias="sourceUrl")
    level: str
    work_mode: str = Field(alias="workMode")
    description: str
    required_skills: list[str] = Field(alias="requiredSkills")
    nice_to_have_skills: list[str] = Field(alias="niceToHaveSkills")
    fingerprint: str | None = None

    model_config = {"populate_by_name": True}


class CompanySource(BaseModel):
    id: str
    company: str
    career_url: str = Field(alias="careerUrl")
    ats_type: str = Field(alias="atsType")
    enabled: bool = True
    board_token: str | None = Field(default=None, alias="boardToken")
    priority: int = 3
    crawl_interval_minutes: int = Field(default=240, alias="crawlIntervalMinutes")
    role_keywords: list[str] = Field(default_factory=list, alias="roleKeywords")
    location_keywords: list[str] = Field(default_factory=list, alias="locationKeywords")
    extraction_strategy: str = Field(default="unprobed", alias="extractionStrategy")
    probe_status: str = Field(default="unprobed", alias="probeStatus")
    probe_notes: str = Field(default="", alias="probeNotes")
    notes: str = ""

    model_config = {"populate_by_name": True}


class CompanyRecord(BaseModel):
    id: str
    name: str
    website_url: str = Field(alias="websiteUrl")
    headquarters: str = ""
    industry: str = "Technology"
    known_career_url: str | None = Field(default=None, alias="knownCareerUrl")
    discovery_source: str = Field(default="seed", alias="discoverySource")
    confidence_score: float = Field(default=0.75, alias="confidenceScore")
    notes: str = ""

    model_config = {"populate_by_name": True}


class CompanyDiscoveryRunResult(BaseModel):
    companies_seen: int
    sources_found: int
    sources_added: int
    sources_updated: int
    sources_unchanged: int
    search_provider: str
    api_needed: str | None = None
    errors: list[str] = Field(default_factory=list)
    sources: list[CompanySource] = Field(default_factory=list)


class CompanyProbeRunResult(BaseModel):
    sources_seen: int
    sources_probed: int
    sources_updated: int
    strategy_counts: dict[str, int]
    status_counts: dict[str, int]
    errors: list[str] = Field(default_factory=list)
    sources: list[CompanySource] = Field(default_factory=list)


class CrawlError(BaseModel):
    source_id: str
    company: str
    message: str


class IngestionRunResult(BaseModel):
    sources_seen: int
    sources_crawled: int
    jobs_seen: int
    jobs_added: int
    jobs_updated: int
    jobs_unchanged: int
    needs_ai_extraction: int
    errors: list[CrawlError] = Field(default_factory=list)


class JobFeedResponse(BaseModel):
    items: list[JobPosting]
    next_cursor: int | None = None
    total: int


class ResumeInput(BaseModel):
    content: str = Field(min_length=80)
    target_role: str = "Seattle SDE"


class ResumeExtractResult(BaseModel):
    filename: str
    content: str
    resume_id: str | None = None
    saved: bool = False


class UserPublic(BaseModel):
    id: str
    email: str
    name: str


class AuthRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=8)
    name: str = ""


class AuthResponse(BaseModel):
    token: str
    user: UserPublic


class SavedResume(BaseModel):
    id: str
    title: str
    filename: str
    content: str
    active: bool
    created_at: str
    updated_at: str


class MatchBreakdown(BaseModel):
    semantic_fit: int
    required_skills: int
    nice_to_have_skills: int
    level_fit: int
    location_fit: int


class MatchResult(BaseModel):
    job: JobPosting
    score: int
    evaluation_source: str = "local"
    matched_skills: list[str]
    missing_skills: list[str]
    risks: list[str]
    bullet_suggestions: list[str]
    ai_summary: str | None = None
    ai_strengths: list[str] = Field(default_factory=list)
    interview_focus: list[str] = Field(default_factory=list)
    breakdown: MatchBreakdown


class MatchRequest(BaseModel):
    resume: ResumeInput
    job_ids: list[str] | None = None
    use_ai: bool = True


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    question: str = Field(min_length=1)
    resume_context: str = ""
    conversation_id: str = "default"
    messages: list[ChatMessage] = Field(default_factory=list)
    job_ids: list[str] | None = None
    top_k: int = Field(default=5, ge=1, le=10)
    use_llm: bool = True


class ChatRetrievedJob(BaseModel):
    id: str
    company: str
    title: str
    location: str
    level: str
    work_mode: str
    score: int
    reason: str


class CopilotToolCall(BaseModel):
    name: str
    title: str
    status: str = "completed"
    summary: str = ""


class WorkflowJobCard(BaseModel):
    job_id: str
    company: str
    title: str
    location: str
    level: str
    work_mode: str
    score: int
    fit_summary: str
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)


class SkillMatrixRow(BaseModel):
    skill: str
    status: str
    evidence: str
    jobs: list[str] = Field(default_factory=list)


class ResumeChecklistItem(BaseModel):
    title: str
    priority: str
    detail: str
    related_skills: list[str] = Field(default_factory=list)


class WorkflowAction(BaseModel):
    label: str
    intent: str
    job_id: str | None = None
    payload: dict[str, str] = Field(default_factory=dict)


class CopilotWorkflow(BaseModel):
    title: str = "Generated workflow"
    tool_calls: list[CopilotToolCall] = Field(default_factory=list)
    job_cards: list[WorkflowJobCard] = Field(default_factory=list)
    skill_matrix: list[SkillMatrixRow] = Field(default_factory=list)
    resume_checklist: list[ResumeChecklistItem] = Field(default_factory=list)
    actions: list[WorkflowAction] = Field(default_factory=list)


class IntentRoute(BaseModel):
    intent: Literal[
        "job_search",
        "job_detail_lookup",
        "job_compare",
        "resume_fit_analysis",
        "resume_tailoring",
        "skill_gap_analysis",
        "application_action",
        "application_status_query",
        "platform_help",
        "small_talk",
        "off_topic",
        "nonsense",
        "clarification_needed",
        "unsupported",
        "router_unavailable",
    ]
    confidence: float = Field(ge=0, le=1)
    needs_retrieval: bool
    needs_action: bool
    entities: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    reason: str = ""
    source: str = "rules"


class WorkflowTraceStep(BaseModel):
    id: str
    name: str
    status: str
    duration_ms: float
    started_at: str
    ended_at: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class WorkflowTrace(BaseModel):
    id: str
    run_id: str = ""
    level: str = "internal"
    duration_ms: float
    started_at: str
    ended_at: str
    steps: list[WorkflowTraceStep] = Field(default_factory=list)


class MatchWorkflowTraceResponse(BaseModel):
    results: list[MatchResult]
    workflow_trace: WorkflowTrace


class ChatResponse(BaseModel):
    answer: str
    cache_status: str
    cache_similarity: float | None = None
    retrieval_source: str
    llm_used: bool
    prompt_template: str
    retrieved_jobs: list[ChatRetrievedJob] = Field(default_factory=list)
    intent_route: IntentRoute | None = None
    workflow: CopilotWorkflow | None = None
    workflow_trace: WorkflowTrace | None = None
    warnings: list[str] = Field(default_factory=list)


class ApplicationRecord(BaseModel):
    id: str
    job_id: str
    stage: ApplicationStage
    notes: str = ""
    follow_up_on: Optional[str] = None


class ApplicationCreate(BaseModel):
    job_id: str
    stage: ApplicationStage = ApplicationStage.saved
    notes: str = ""
    follow_up_on: Optional[str] = None


class ApplicationUpdate(BaseModel):
    stage: ApplicationStage | None = None
    notes: str | None = None
    follow_up_on: Optional[str] = None
