"""Close the hand-written seed postings once real crawled jobs are flowing.

Seed rows have no source_id, so the staleness sweep never touches them; this
one-shot command retires them explicitly. It targets the exact ids from
data/seed_jobs.json — jobs written by the legacy /jobs/refresh path also lack a
source_id and must not be swept up. Run with --force to skip the real-data
safety check.
"""

import argparse

from app.database import get_connection
from app.seed import load_seed_jobs

from .db import ensure_pipeline_schema


MIN_REAL_JOBS = 50


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="retire seed jobs even with few real jobs in the database")
    args = parser.parse_args()

    ensure_pipeline_schema()
    seed_ids = [job.id for job in load_seed_jobs()]
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM job_postings WHERE source_id IS NOT NULL AND status = 'open'")
            real_jobs = cursor.fetchone()[0]
            if real_jobs < MIN_REAL_JOBS and not args.force:
                print(f"Only {real_jobs} crawled jobs are open (< {MIN_REAL_JOBS}); keeping seed jobs. Use --force to override.")
                return

            cursor.execute(
                "UPDATE job_postings SET status = 'closed' WHERE id = ANY(%s) AND status = 'open'",
                (seed_ids,),
            )
            print(f"Retired {cursor.rowcount} seed jobs ({real_jobs} crawled jobs remain open).")


if __name__ == "__main__":
    main()
