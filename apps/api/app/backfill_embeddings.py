import os
import time
import urllib.error

from .ai_chat import embed_document, get_embedding_provider, vector_literal
from .database import get_connection, job_fingerprint, job_search_text
from .models import JobPosting


TRANSIENT_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


def main() -> None:
    limit = int(os.getenv("EMBEDDING_BACKFILL_LIMIT", "1000"))
    delay_seconds = float(os.getenv("EMBEDDING_BACKFILL_DELAY_SECONDS", "0"))
    max_retries = int(os.getenv("EMBEDDING_BACKFILL_MAX_RETRIES", "5"))
    provider = get_embedding_provider()

    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, company, title, location, source, source_url, level, work_mode,
                       description, required_skills, nice_to_have_skills, fingerprint
                FROM job_postings
                WHERE embedding IS NULL
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()

            updated = 0
            for row in rows:
                job = JobPosting(
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
                embedding = embed_with_retry(provider, job, max_retries)
                cursor.execute(
                    """
                    UPDATE job_postings
                    SET embedding = %s::vector, fingerprint = %s
                    WHERE id = %s
                    """,
                    (vector_literal(embedding), job_fingerprint(job), job.id),
                )
                conn.commit()
                updated += 1
                print(f"Backfilled {updated}/{len(rows)}: {job.id}")
                if delay_seconds:
                    time.sleep(delay_seconds)

    print(f"Backfilled {updated} job embeddings.")


def embed_with_retry(provider, job: JobPosting, max_retries: int) -> list[float]:
    for attempt in range(max_retries + 1):
        try:
            return embed_document(provider, job_search_text(job), job.title)
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_HTTP_STATUS_CODES or attempt >= max_retries:
                raise
            wait_seconds = retry_delay_seconds(exc, attempt)
            print(f"Embedding provider returned HTTP {exc.code}; retrying in {wait_seconds:.0f}s.")
            time.sleep(wait_seconds)

    raise RuntimeError("Embedding retry loop ended unexpectedly.")


def retry_delay_seconds(exc: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = exc.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return min(60.0, 5.0 * (attempt + 1))


if __name__ == "__main__":
    main()
