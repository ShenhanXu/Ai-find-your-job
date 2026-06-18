from .database import apply_schema, database_health, upsert_jobs
from .seed import load_seed_jobs


def main() -> None:
    apply_schema()
    count = upsert_jobs(load_seed_jobs())
    health = database_health()
    print(f"Seeded {count} jobs into Postgres.")
    print(f"Database: {health['database']}, pgvector: {health['pgvector']}, jobs: {health['jobs_total']}")


if __name__ == "__main__":
    main()
