from app.ingestion import run_ingestion
from app.models import CompanySource, JobPosting
from app.seed import load_seed_jobs


def test_ingestion_adds_fixture_jobs_and_is_incremental():
    existing: dict[str, JobPosting] = {job.id: job for job in load_seed_jobs()}
    sources = [
        CompanySource(
            id="fixture-pacific-ai",
            company="Pacific AI Labs",
            careerUrl="fixtures/pacific_ai_jobs.html",
            atsType="generic_html",
            enabled=True,
            roleKeywords=["software", "sde", "backend", "full stack", "intern", "new grad"],
            locationKeywords=["seattle", "bellevue", "redmond", "kirkland", "remote"],
        ),
        CompanySource(
            id="fixture-cascade-cloud",
            company="Cascade Cloud",
            careerUrl="fixtures/cascade_cloud_jobs.json",
            atsType="generic_json",
            enabled=True,
            roleKeywords=["software", "sde", "platform", "cloud", "new grad"],
            locationKeywords=["seattle", "bellevue", "redmond", "kirkland", "remote"],
        ),
    ]

    first = run_ingestion(existing, sources)
    second = run_ingestion(existing, sources)

    assert first.jobs_added >= 4
    assert first.jobs_seen >= 4
    assert first.needs_ai_extraction == 0
    assert second.jobs_added == 0
    assert second.jobs_updated == 0
    assert second.jobs_unchanged >= first.jobs_seen
    assert any(job.company == "Pacific AI Labs" for job in existing.values())
    assert any(job.company == "Cascade Cloud" for job in existing.values())
