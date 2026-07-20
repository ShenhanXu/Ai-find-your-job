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


def test_clean_html_strips_escaped_greenhouse_markup():
    from app.ingestion import clean_html

    escaped = "&lt;p style=&quot;text-align: center;&quot;&gt;&lt;strong&gt;Senior PM&lt;/strong&gt;&lt;/p&gt; &lt;p&gt;Build &amp;amp; ship.&lt;/p&gt;"
    assert clean_html(escaped) == "Senior PM Build & ship."


def test_clean_html_strips_plain_markup():
    from app.ingestion import clean_html

    assert clean_html("<p>Build <b>Python</b> services.</p>") == "Build Python services."


def test_infer_level_word_boundaries_and_title_priority():
    from app.ingestion import infer_level

    assert infer_level("Senior Product Manager", "our internal tools and international teams") == "senior"
    assert infer_level("Software Engineer Intern", "work with senior engineers") == "intern"
    assert infer_level("Software Engineer", "join our internship program") == "intern"
    assert infer_level("Software Engineer II", "backend role") == "mid"
    assert infer_level("Software Engineer I", "backend role") == "new-grad"
    assert infer_level("Software Engineer", "backend role in Seattle") == "mid"
