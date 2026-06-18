import json
from pathlib import Path

from .models import CompanyRecord, CompanySource, JobPosting


def data_path(filename: str) -> Path:
    current = Path(__file__).resolve()
    candidates = [Path.cwd() / "data" / filename]
    candidates.extend(parent / "data" / filename for parent in current.parents)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(f"Could not find data/{filename}")


def load_seed_jobs() -> list[JobPosting]:
    with data_path("seed_jobs.json").open("r", encoding="utf-8") as handle:
        rows = json.load(handle)
    return [JobPosting.model_validate(row) for row in rows]


def load_company_sources() -> list[CompanySource]:
    with data_path("company_sources.json").open("r", encoding="utf-8") as handle:
        rows = json.load(handle)
    return [CompanySource.model_validate(row) for row in rows]


def save_company_sources(sources: list[CompanySource]) -> None:
    data = [source.model_dump(by_alias=True) for source in sorted(sources, key=lambda item: item.id)]
    with data_path("company_sources.json").open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def load_company_seed() -> list[CompanyRecord]:
    with data_path("company_seed.json").open("r", encoding="utf-8") as handle:
        rows = json.load(handle)
    return [CompanyRecord.model_validate(row) for row in rows]
