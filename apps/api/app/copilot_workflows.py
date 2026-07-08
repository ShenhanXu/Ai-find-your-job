from collections import Counter, defaultdict

from .matcher import skill_present
from .models import (
    ChatRequest,
    ChatRetrievedJob,
    CopilotToolCall,
    CopilotWorkflow,
    JobPosting,
    ResumeChecklistItem,
    SkillMatrixRow,
    WorkflowAction,
    WorkflowJobCard,
)


def build_copilot_workflow(
    request: ChatRequest,
    retrieved_jobs: list[ChatRetrievedJob],
    jobs: list[JobPosting],
) -> CopilotWorkflow | None:
    if not retrieved_jobs:
        return None

    job_by_id = {job.id: job for job in jobs}
    ranked_jobs = [job_by_id[result.id] for result in retrieved_jobs if result.id in job_by_id]
    if not ranked_jobs:
        return None

    tool_calls = selected_tool_calls(request, bool(request.resume_context.strip()))
    job_cards = build_job_cards(request, retrieved_jobs, ranked_jobs)
    skill_matrix = build_skill_matrix(request, ranked_jobs)
    checklist = build_resume_checklist(skill_matrix, ranked_jobs)
    actions = build_workflow_actions(retrieved_jobs, skill_matrix, checklist)

    return CopilotWorkflow(
        title=workflow_title(request),
        tool_calls=tool_calls,
        job_cards=job_cards,
        skill_matrix=skill_matrix,
        resume_checklist=checklist,
        actions=actions,
    )


def selected_tool_calls(request: ChatRequest, has_resume_context: bool) -> list[CopilotToolCall]:
    text = request.question.lower()
    calls = [
        CopilotToolCall(
            name="recommend_jobs",
            title="Rank matching roles",
            summary="Ranked retrieved jobs by vector similarity and structured fit signals.",
        )
    ]

    if any(token in text for token in ["compare", "which", "best", "top", "matrix", "对比", "比较", "哪个"]):
        calls.append(
            CopilotToolCall(
                name="compare_jobs",
                title="Build comparison cards",
                summary="Converted top retrieved roles into side-by-side job fit cards.",
            )
        )

    if has_resume_context or any(token in text for token in ["resume", "skill", "bullet", "tailor", "简历", "技能"]):
        calls.extend(
            [
                CopilotToolCall(
                    name="build_skill_matrix",
                    title="Analyze skill gaps",
                    summary="Mapped required and nice-to-have skills across retrieved roles.",
                ),
                CopilotToolCall(
                    name="build_resume_checklist",
                    title="Create tailoring checklist",
                    summary="Generated resume edits from missing high-frequency skills and role patterns.",
                ),
            ]
        )

    return calls


def build_job_cards(
    request: ChatRequest,
    retrieved_jobs: list[ChatRetrievedJob],
    ranked_jobs: list[JobPosting],
) -> list[WorkflowJobCard]:
    retrieved_by_id = {job.id: job for job in retrieved_jobs}
    cards: list[WorkflowJobCard] = []
    for job in ranked_jobs[:4]:
        retrieved = retrieved_by_id[job.id]
        matched = matching_skills(request.resume_context, job)
        missing = [skill for skill in job.required_skills if skill not in matched][:4]
        cards.append(
            WorkflowJobCard(
                job_id=job.id,
                company=job.company,
                title=job.title,
                location=job.location,
                level=job.level,
                work_mode=job.work_mode,
                score=retrieved.score,
                fit_summary=retrieved.reason,
                matched_skills=matched[:5],
                missing_skills=missing,
            )
        )
    return cards


def build_skill_matrix(request: ChatRequest, jobs: list[JobPosting]) -> list[SkillMatrixRow]:
    skill_jobs: dict[str, set[str]] = defaultdict(set)
    required_counts: Counter[str] = Counter()
    optional_counts: Counter[str] = Counter()
    for job in jobs[:5]:
        for skill in job.required_skills:
            required_counts[skill] += 1
            skill_jobs[skill].add(job.company)
        for skill in job.nice_to_have_skills:
            optional_counts[skill] += 1
            skill_jobs[skill].add(job.company)

    ranked_skills = sorted(
        set(required_counts) | set(optional_counts),
        key=lambda skill: (required_counts[skill] * 2 + optional_counts[skill], skill.lower()),
        reverse=True,
    )

    rows: list[SkillMatrixRow] = []
    for skill in ranked_skills[:8]:
        required = required_counts[skill] > 0
        present = bool(request.resume_context.strip()) and skill_present(skill, request.resume_context)
        if present:
            status = "matched"
            evidence = "Detected in candidate context."
        elif required:
            status = "missing"
            evidence = f"Required by {required_counts[skill]} retrieved role{plural(required_counts[skill])}."
        else:
            status = "optional"
            evidence = f"Nice-to-have signal across {optional_counts[skill]} retrieved role{plural(optional_counts[skill])}."

        rows.append(
            SkillMatrixRow(
                skill=skill,
                status=status,
                evidence=evidence,
                jobs=sorted(skill_jobs[skill])[:3],
            )
        )

    return rows


def build_resume_checklist(matrix: list[SkillMatrixRow], jobs: list[JobPosting]) -> list[ResumeChecklistItem]:
    missing_required = [row for row in matrix if row.status == "missing"]
    optional = [row for row in matrix if row.status == "optional"]
    checklist: list[ResumeChecklistItem] = []

    if missing_required:
        skills = [row.skill for row in missing_required[:3]]
        checklist.append(
            ResumeChecklistItem(
                title="Close the highest-impact skill gaps",
                priority="High",
                detail=(
                    "Add truthful project evidence for "
                    + ", ".join(skills)
                    + " if you have used them; otherwise treat them as interview prep targets."
                ),
                related_skills=skills,
            )
        )

    if jobs:
        top_job = jobs[0]
        checklist.append(
            ResumeChecklistItem(
                title="Rewrite one project bullet for the top role",
                priority="High",
                detail=(
                    f"Lead with impact, then name the stack that maps to {top_job.company} - {top_job.title}."
                ),
                related_skills=top_job.required_skills[:3],
            )
        )

    if optional:
        skills = [row.skill for row in optional[:3]]
        checklist.append(
            ResumeChecklistItem(
                title="Add secondary keywords only where truthful",
                priority="Medium",
                detail="Use optional keywords as supporting context, not as inflated experience claims.",
                related_skills=skills,
            )
        )

    return checklist[:4]


def build_workflow_actions(
    retrieved_jobs: list[ChatRetrievedJob],
    matrix: list[SkillMatrixRow],
    checklist: list[ResumeChecklistItem],
) -> list[WorkflowAction]:
    actions: list[WorkflowAction] = []
    if retrieved_jobs:
        top = retrieved_jobs[0]
        actions.append(
            WorkflowAction(
                label="Open top role",
                intent="open_job",
                job_id=top.id,
                payload={"company": top.company, "title": top.title},
            )
        )
        actions.append(
            WorkflowAction(
                label="Save to tracker",
                intent="save_application",
                job_id=top.id,
                payload={"stage": "saved"},
            )
        )
    if matrix:
        actions.append(
            WorkflowAction(
                label="Review skill gaps",
                intent="review_skill_matrix",
                payload={"missing_count": str(len([row for row in matrix if row.status == "missing"]))},
            )
        )
    if checklist:
        actions.append(
            WorkflowAction(
                label="Tailor resume",
                intent="tailor_resume",
                payload={"items": str(len(checklist))},
            )
        )
    return actions[:4]


def workflow_title(request: ChatRequest) -> str:
    text = request.question.lower()
    if any(token in text for token in ["resume", "bullet", "tailor", "简历"]):
        return "Resume tailoring workflow"
    if any(token in text for token in ["compare", "which", "best", "比较", "哪个"]):
        return "Job comparison workflow"
    return "Role recommendation workflow"


def matching_skills(resume_context: str, job: JobPosting) -> list[str]:
    if not resume_context.strip():
        return []
    return [skill for skill in [*job.required_skills, *job.nice_to_have_skills] if skill_present(skill, resume_context)]


def plural(count: int) -> str:
    return "" if count == 1 else "s"
