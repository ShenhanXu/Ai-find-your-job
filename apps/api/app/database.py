import hashlib
import os
from pathlib import Path
from typing import Any

from .models import ApplicationRecord, JobPosting, SavedResume, UserPublic


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


def ensure_account_schema() -> None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                  email TEXT UNIQUE NOT NULL,
                  name TEXT NOT NULL DEFAULT '',
                  password_hash TEXT NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS resumes (
                  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                  title TEXT NOT NULL,
                  raw_text TEXT NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute("ALTER TABLE resumes ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE")
            cursor.execute("ALTER TABLE resumes ADD COLUMN IF NOT EXISTS filename TEXT NOT NULL DEFAULT ''")
            cursor.execute("ALTER TABLE resumes ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT true")
            cursor.execute("ALTER TABLE resumes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
            cursor.execute("CREATE INDEX IF NOT EXISTS resumes_user_id_idx ON resumes(user_id)")
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS resumes_one_active_per_user_idx
                ON resumes(user_id)
                WHERE active AND user_id IS NOT NULL
                """
            )


def ensure_application_schema() -> None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS applications (
                  id UUID PRIMARY KEY,
                  job_id TEXT NOT NULL,
                  stage TEXT NOT NULL DEFAULT 'saved',
                  notes TEXT NOT NULL DEFAULT '',
                  follow_up_on TEXT,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS applications_job_id_idx ON applications (job_id)"
            )


def list_applications_db() -> list[ApplicationRecord]:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, job_id, stage, notes, follow_up_on FROM applications ORDER BY updated_at DESC"
            )
            return [application_from_row(row) for row in cursor.fetchall()]


def get_application_db(application_id: str) -> ApplicationRecord | None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, job_id, stage, notes, follow_up_on FROM applications WHERE id = %s",
                (application_id,),
            )
            row = cursor.fetchone()
            return application_from_row(row) if row else None


def find_application_by_job_db(job_id: str) -> ApplicationRecord | None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, job_id, stage, notes, follow_up_on FROM applications WHERE job_id = %s ORDER BY updated_at DESC LIMIT 1",
                (job_id,),
            )
            row = cursor.fetchone()
            return application_from_row(row) if row else None


def save_application_db(record: ApplicationRecord) -> ApplicationRecord:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO applications (id, job_id, stage, notes, follow_up_on)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                  job_id = EXCLUDED.job_id,
                  stage = EXCLUDED.stage,
                  notes = EXCLUDED.notes,
                  follow_up_on = EXCLUDED.follow_up_on,
                  updated_at = now()
                """,
                (record.id, record.job_id, record.stage.value, record.notes, record.follow_up_on),
            )
    return record


def application_from_row(row: Any) -> ApplicationRecord:
    return ApplicationRecord(
        id=str(row[0]),
        job_id=row[1],
        stage=row[2],
        notes=row[3] or "",
        follow_up_on=str(row[4]) if row[4] else None,
    )


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
                cursor.execute("SELECT count(*) FROM users")
                user_count = cursor.fetchone()[0]
                cursor.execute("SELECT count(*) FROM resumes WHERE user_id IS NOT NULL")
                resume_count = cursor.fetchone()[0]
        return {
            "database": "connected",
            "pgvector": str(vector_row[0]) if vector_row else "not_installed",
            "jobs_total": str(job_count),
            "users_total": str(user_count),
            "saved_resumes_total": str(resume_count),
        }
    except Exception as exc:
        return {
            "database": "unavailable",
            "pgvector": "unknown",
            "jobs_total": "unknown",
            "users_total": "unknown",
            "saved_resumes_total": "unknown",
            "database_error": str(exc),
        }


def create_user(email: str, name: str, password_hash: str) -> UserPublic:
    normalized_email = email.strip().lower()
    display_name = name.strip() or normalized_email.split("@")[0]
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (email, name, password_hash)
                VALUES (%s, %s, %s)
                RETURNING id, email, name
                """,
                (normalized_email, display_name, password_hash),
            )
            row = cursor.fetchone()
    return user_from_row(row)


def find_user_by_email(email: str) -> tuple[UserPublic, str] | None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, email, name, password_hash FROM users WHERE email = %s",
                (email.strip().lower(),),
            )
            row = cursor.fetchone()
    if not row:
        return None
    return user_from_row(row[:3]), row[3]


def get_user_by_id(user_id: str) -> UserPublic | None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, email, name FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
    return user_from_row(row) if row else None


def save_user_resume(user_id: str, filename: str, content: str) -> SavedResume:
    title = filename.strip() or "Uploaded resume"
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE resumes SET active = false, updated_at = now() WHERE user_id = %s", (user_id,))
            cursor.execute(
                """
                INSERT INTO resumes (user_id, title, filename, raw_text, active, updated_at)
                VALUES (%s, %s, %s, %s, true, now())
                RETURNING id, title, filename, raw_text, active, created_at, updated_at
                """,
                (user_id, title, filename, content),
            )
            row = cursor.fetchone()
    return resume_from_row(row)


def list_user_resumes(user_id: str) -> list[SavedResume]:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, title, filename, raw_text, active, created_at, updated_at
                FROM resumes
                WHERE user_id = %s
                ORDER BY active DESC, updated_at DESC, created_at DESC
                """,
                (user_id,),
            )
            rows = cursor.fetchall()
    return [resume_from_row(row) for row in rows]


def get_active_user_resume(user_id: str) -> SavedResume | None:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, title, filename, raw_text, active, created_at, updated_at
                FROM resumes
                WHERE user_id = %s
                ORDER BY active DESC, updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()
    return resume_from_row(row) if row else None


def user_from_row(row: Any) -> UserPublic:
    return UserPublic(id=str(row[0]), email=row[1], name=row[2])


def resume_from_row(row: Any) -> SavedResume:
    return SavedResume(
        id=str(row[0]),
        title=row[1],
        filename=row[2],
        content=row[3],
        active=bool(row[4]),
        created_at=row[5].isoformat(),
        updated_at=row[6].isoformat(),
    )


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
