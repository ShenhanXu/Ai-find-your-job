import fakeredis
import pytest

from app.models import CompanySource, JobPosting


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


def make_source(**overrides) -> CompanySource:
    defaults = {
        "id": "acme",
        "company": "Acme",
        "careerUrl": "https://boards-api.greenhouse.io/v1/boards/{boardToken}/jobs?content=true",
        "atsType": "greenhouse",
        "enabled": True,
        "boardToken": "acme",
        "priority": 1,
        "crawlIntervalMinutes": 30,
        "roleKeywords": [],
        "locationKeywords": [],
    }
    defaults.update(overrides)
    return CompanySource.model_validate(defaults)


def make_job(job_id: str = "acme-1", fingerprint: str = "fp-1", **overrides) -> JobPosting:
    defaults = {
        "id": job_id,
        "company": "Acme",
        "title": "Software Engineer",
        "location": "Seattle, WA",
        "source": "greenhouse",
        "sourceUrl": "https://example.com/jobs/1",
        "level": "mid",
        "workMode": "hybrid",
        "description": "Build backend services.",
        "requiredSkills": ["Python"],
        "niceToHaveSkills": ["AWS"],
        "fingerprint": fingerprint,
    }
    defaults.update(overrides)
    return JobPosting.model_validate(defaults)
