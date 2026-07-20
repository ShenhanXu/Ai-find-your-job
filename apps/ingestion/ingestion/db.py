from app.database import ensure_pipeline_schema, get_connection
from app.models import CompanySource, JobPosting


__all__ = ["ensure_pipeline_schema", "SourceStore", "JobStore"]


class SourceStore:
    def sync_from_json(self, sources: list[CompanySource]) -> int:
        """Upsert configuration columns from data/company_sources.json, preserving runtime state columns."""
        with get_connection() as conn:
            with conn.cursor() as cursor:
                for source in sources:
                    cursor.execute(
                        """
                        INSERT INTO company_sources (
                          id, company, career_url, ats_type, enabled, board_token,
                          priority, crawl_interval_minutes, role_keywords, location_keywords, notes
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                          company = EXCLUDED.company,
                          career_url = EXCLUDED.career_url,
                          ats_type = EXCLUDED.ats_type,
                          enabled = EXCLUDED.enabled,
                          board_token = EXCLUDED.board_token,
                          priority = EXCLUDED.priority,
                          crawl_interval_minutes = EXCLUDED.crawl_interval_minutes,
                          role_keywords = EXCLUDED.role_keywords,
                          location_keywords = EXCLUDED.location_keywords,
                          notes = EXCLUDED.notes
                        """,
                        (
                            source.id,
                            source.company,
                            source.career_url,
                            source.ats_type,
                            source.enabled,
                            source.board_token,
                            source.priority,
                            source.crawl_interval_minutes,
                            source.role_keywords,
                            source.location_keywords,
                            source.notes,
                        ),
                    )
                # The JSON file is the configuration authority: rows it no longer
                # lists (stale discoveries from earlier runs) must stop being crawled.
                cursor.execute(
                    "UPDATE company_sources SET enabled = false WHERE NOT (id = ANY(%s))",
                    ([source.id for source in sources],),
                )
        return len(sources)

    def due_sources(self, ats_types: tuple[str, ...]) -> list[CompanySource]:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, company, career_url, ats_type, enabled, board_token,
                           priority, crawl_interval_minutes, role_keywords, location_keywords
                    FROM company_sources
                    WHERE enabled
                      AND ats_type = ANY(%s)
                      AND (
                        last_enqueued_at IS NULL
                        OR last_enqueued_at < now() - crawl_interval_minutes * interval '1 minute'
                      )
                    ORDER BY priority ASC, id ASC
                    """,
                    (list(ats_types),),
                )
                rows = cursor.fetchall()

        return [
            CompanySource(
                id=row[0],
                company=row[1],
                careerUrl=row[2],
                atsType=row[3],
                enabled=row[4],
                boardToken=row[5],
                priority=row[6],
                crawlIntervalMinutes=row[7],
                roleKeywords=list(row[8] or []),
                locationKeywords=list(row[9] or []),
            )
            for row in rows
        ]

    def mark_enqueued(self, source_ids: list[str]) -> None:
        if not source_ids:
            return
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE company_sources SET last_enqueued_at = now() WHERE id = ANY(%s)",
                    (source_ids,),
                )

    def mark_success(self, source_id: str) -> None:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE company_sources SET last_crawled_at = now(), last_success_at = now(), last_error = NULL WHERE id = %s",
                    (source_id,),
                )

    def mark_error(self, source_id: str, message: str) -> None:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE company_sources SET last_crawled_at = now(), last_error = %s, last_error_at = now() WHERE id = %s",
                    (message[:500], source_id),
                )


UPSERT_OUTCOME_ADDED = "added"
UPSERT_OUTCOME_UPDATED = "updated"
UPSERT_OUTCOME_UNCHANGED = "unchanged"


class JobStore:
    def upsert(self, job: JobPosting, source_id: str | None) -> tuple[str, bool]:
        """Idempotent write. Returns (outcome, needs_embedding).

        needs_embedding is True whenever the row ends up without a vector — including
        the 'unchanged' replay case, so a consumer that crashed between the DB commit
        and the embed-task publish self-heals on redelivery instead of losing the task."""
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT fingerprint, embedding IS NOT NULL FROM job_postings WHERE id = %s", (job.id,))
                row = cursor.fetchone()
                if row is None:
                    outcome = UPSERT_OUTCOME_ADDED
                elif row[0] != job.fingerprint:
                    outcome = UPSERT_OUTCOME_UPDATED
                else:
                    outcome = UPSERT_OUTCOME_UNCHANGED
                needs_embedding = outcome != UPSERT_OUTCOME_UNCHANGED or not row[1]

                cursor.execute(
                    """
                    INSERT INTO job_postings (
                      id, company, title, location, source, source_url, description,
                      required_skills, nice_to_have_skills, level, work_mode, fingerprint,
                      source_id, status, first_seen_at, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', now(), now())
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
                      fingerprint = EXCLUDED.fingerprint,
                      source_id = EXCLUDED.source_id,
                      status = 'open',
                      last_seen_at = now()
                    """,
                    (
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
                        job.fingerprint,
                        source_id,
                    ),
                )
        return outcome, needs_embedding

    def fetch_for_embedding(self, job_ids: list[str]) -> list[JobPosting]:
        if not job_ids:
            return []
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, company, title, location, source, source_url, level, work_mode,
                           description, required_skills, nice_to_have_skills, fingerprint
                    FROM job_postings
                    WHERE id = ANY(%s) AND embedding IS NULL
                    """,
                    (job_ids,),
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

    def set_embedding(self, job_id: str, fingerprint: str | None, vector_value: str) -> bool:
        """Guarded by fingerprint: if the job changed after this embed task was queued, skip —
        the upsert already emitted a fresh task for the new fingerprint."""
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE job_postings SET embedding = %s::vector WHERE id = %s AND fingerprint = %s",
                    (vector_value, job_id, fingerprint),
                )
                return cursor.rowcount > 0

    def close_stale_jobs(self, cycles: int) -> int:
        """Close jobs their source stopped returning. Requires a crawl success newer than the job's
        last sighting so a broken source never closes its own jobs. Jobs of disabled sources are
        closed outright — nothing will ever crawl them again."""
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE job_postings j
                    SET status = 'closed'
                    FROM company_sources s
                    WHERE j.source_id = s.id
                      AND j.status = 'open'
                      AND s.enabled
                      AND s.last_success_at IS NOT NULL
                      AND j.last_seen_at < s.last_success_at
                      AND j.last_seen_at < now() - s.crawl_interval_minutes * %s * interval '1 minute'
                    """,
                    (cycles,),
                )
                closed = cursor.rowcount
                cursor.execute(
                    """
                    UPDATE job_postings j
                    SET status = 'closed'
                    FROM company_sources s
                    WHERE j.source_id = s.id AND j.status = 'open' AND NOT s.enabled
                    """
                )
                return closed + cursor.rowcount
