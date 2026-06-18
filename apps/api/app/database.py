import hashlib
import os
from pathlib import Path
from typing import Any

from .models import JobPosting


DEFAULT_DATABASE_URL = "postgresql://jobmatch:jobmatch@localhost:5432/jobmatch"


def bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def database_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def database_required() -> bool:
    return bool_env("REQUIRE_DATABASE", False)


def get_connection():
    import psycopg

    return psycopg.connect(database_url())


def apply_schema() -> None:
    schema_path = schema_file_path()
    statements = schema_path.read_text(encoding="utf-8")
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(statements)


def schema_file_path() -> Path:
    roots = [Path.cwd(), *Path(__file__).resolve().parents]
    for root in roots:
        candidate = root / "infra" / "postgres" / "init.sql"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Could not find infra/postgres/init.sql")


def database_health() -> dict[str, str]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                vector_row = cursor.fetchone()
                cursor.execute("SELECT count(*) FROM job_postings")
                job_count = cursor.fetchone()[0]
        return {
            "database": "connected",
            "pgvector": str(vector_row[0]) if vector_row else "not_installed",
            "jobs_total": str(job_count),
        }
    except Exception as exc:
        return {
            "database": "unavailable",
            "pgvector": "unknown",
            "jobs_total": "unknown",
            "database_error": str(exc),
        }


def load_jobs_from_database() -> list[JobPosting]:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, company, title, location, source, source_url, level, work_mode,
                       description, required_skills, nice_to_have_skills, fingerprint
                FROM job_postings
                ORDER BY created_at DESC, company ASC, title ASC
                """
            )
            rows = cursor.fetchall()

    return [
        JobPosting(
            id=row[0],
            company=row[1],
            title=row[2],
            location=row[3],
            source=row[4],
            sourceUrl=row[5],
            level=row[6],
            workMode=row[7],
            description=row[8],
            requiredSkills=list(row[9] or []),
            niceToHaveSkills=list(row[10] or []),
            fingerprint=row[11],
        )
        for row in rows
    ]


def upsert_jobs(jobs: list[JobPosting]) -> int:
    if not jobs:
        return 0

    with get_connection() as conn:
        with conn.cursor() as cursor:
            for job in jobs:
                cursor.execute(
                    """
                    INSERT INTO job_postings (
                      id, company, title, location, source, source_url, description,
                      required_skills, nice_to_have_skills, level, work_mode, fingerprint
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                      embedding = CASE
                        WHEN job_postings.fingerprint IS DISTINCT FROM EXCLUDED.fingerprint
                        THEN NULL
                        ELSE job_postings.embedding
                      END,
                      fingerprint = EXCLUDED.fingerprint
                    """,
                    job_values(job),
                )
    return len(jobs)


def upsert_job(job: JobPosting) -> JobPosting:
    upsert_jobs([job])
    return job


def job_values(job: JobPosting) -> tuple[Any, ...]:
    return (
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
        job.fingerprint or job_fingerprint(job),
    )


def job_fingerprint(job: JobPosting) -> str:
    return hashlib.sha256(job_search_text(job).encode("utf-8")).hexdigest()


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
