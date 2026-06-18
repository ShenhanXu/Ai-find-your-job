import re

from .models import JobPosting, MatchBreakdown, MatchResult, ResumeInput


SKILL_ALIASES: dict[str, list[str]] = {
    "A/B Testing": ["a/b testing", "ab testing", "experimentation"],
    "API Design": ["api design", "rest api", "graphql", "backend api"],
    "AWS": ["aws", "amazon web services", "lambda", "dynamodb", "cloudwatch"],
    "Azure": ["azure", "microsoft cloud"],
    "Backend": ["backend", "server side", "server-side", "api"],
    "C#": ["c#", "c sharp", "dotnet", ".net"],
    "CI/CD": ["ci/cd", "continuous integration", "github actions", "jenkins"],
    "Cloud": ["cloud", "aws", "azure", "gcp"],
    "Data Structures": ["data structures", "data structure"],
    "Distributed Systems": ["distributed systems", "distributed service", "microservices"],
    "Go": ["go", "golang"],
    "Java": ["java", "spring", "jvm"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Next.js": ["next.js", "nextjs"],
    "Observability": ["observability", "metrics", "logs", "tracing", "telemetry"],
    "Product Engineering": ["product engineering", "product-minded", "product thinking"],
    "React": ["react", "react.js", "frontend"],
    "REST API": ["rest api", "restful", "http api"],
    "SQL": ["sql", "postgres", "postgresql", "mysql", "database"],
    "System Design": ["system design", "architecture", "scalability"],
    "TypeScript": ["typescript", "ts"],
}


def tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.]+", text.lower()))


def skill_present(skill: str, resume_text: str) -> bool:
    normalized = resume_text.lower()
    aliases = SKILL_ALIASES.get(skill, [skill.lower()])
    return any(alias_present(alias, normalized) for alias in aliases)


def alias_present(alias: str, normalized_text: str) -> bool:
    normalized_alias = alias.lower().strip()
    pattern = re.escape(normalized_alias).replace(r"\ ", r"\s+")
    return re.search(rf"(?<![a-z0-9+#.]){pattern}(?![a-z0-9+#.])", normalized_text) is not None


def overlap_score(resume_text: str, job_text: str) -> int:
    resume_tokens = tokenize(resume_text)
    job_tokens = tokenize(job_text)
    if not resume_tokens or not job_tokens:
        return 0

    overlap = resume_tokens.intersection(job_tokens)
    return min(100, round((len(overlap) / max(18, len(job_tokens))) * 220))


def level_score(resume_text: str, job: JobPosting) -> tuple[int, list[str]]:
    text = resume_text.lower()
    risks: list[str] = []

    senior_signals = ["senior", "lead", "staff", "principal", "5+", "6+", "7+"]
    entry_signals = ["intern", "new grad", "capstone", "coursework", "teaching assistant"]

    if job.level in {"senior"} and not any(signal in text for signal in senior_signals):
        risks.append("Role appears senior; resume should show ownership, scale, and mentoring signals.")
        return 55, risks

    if job.level in {"entry", "new-grad"} and any(signal in text for signal in entry_signals):
        return 95, risks

    return 82, risks


def location_score(resume_text: str, job: JobPosting) -> int:
    text = resume_text.lower()
    location = job.location.lower()
    if "remote" in text or "seattle" in text or "bellevue" in text or "redmond" in text:
        return 100
    if "wa" in location or "seattle" in location or "bellevue" in location or "redmond" in location:
        return 78
    return 70


def bullet_suggestions(job: JobPosting, matched: list[str], missing: list[str]) -> list[str]:
    strongest = ", ".join(matched[:3]) if matched else "backend fundamentals"
    suggestions = [
        f"Rewrite one project bullet to lead with impact, then name the stack: built {job.title.lower()}-relevant functionality using {strongest}.",
        f"Add a scale or reliability metric where truthful, such as latency, throughput, test coverage, uptime, users, or cost reduction.",
    ]

    if missing:
        suggestions.append(
            f"If you have real exposure to {missing[0]}, add one concise bullet that shows what you built, measured, or debugged with it."
        )

    return suggestions


def match_resume_to_job(resume: ResumeInput, job: JobPosting) -> MatchResult:
    resume_text = resume.content
    required = job.required_skills
    nice = job.nice_to_have_skills

    matched_required = [skill for skill in required if skill_present(skill, resume_text)]
    matched_nice = [skill for skill in nice if skill_present(skill, resume_text)]
    missing = [skill for skill in required if skill not in matched_required]

    required_score = round((len(matched_required) / max(1, len(required))) * 100)
    nice_score = round((len(matched_nice) / max(1, len(nice))) * 100)
    semantic = overlap_score(resume_text, f"{job.title} {job.description}")
    level, risks = level_score(resume_text, job)
    location = location_score(resume_text, job)

    weighted = (
        semantic * 0.35
        + required_score * 0.30
        + nice_score * 0.10
        + level * 0.15
        + location * 0.10
    )

    if required_score < 50:
        risks.append("Less than half of required skills were found in the resume text.")
    if "System Design" in missing and job.level in {"mid", "senior"}:
        risks.append("System design is a likely interview screen; add evidence of architecture decisions.")

    return MatchResult(
        job=job,
        score=max(1, min(99, round(weighted))),
        evaluation_source="local",
        matched_skills=matched_required + matched_nice,
        missing_skills=missing,
        risks=risks,
        bullet_suggestions=bullet_suggestions(job, matched_required + matched_nice, missing),
        breakdown=MatchBreakdown(
            semantic_fit=semantic,
            required_skills=required_score,
            nice_to_have_skills=nice_score,
            level_fit=level,
            location_fit=location,
        ),
    )
