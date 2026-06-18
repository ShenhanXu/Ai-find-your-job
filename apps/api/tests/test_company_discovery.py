from app.company_discovery import detect_ats, run_company_discovery


def test_company_discovery_builds_real_seed_sources_without_external_api():
    result = run_company_discovery(persist=False, enable_verified=False, use_search=False, limit=5)

    assert result.companies_seen == 5
    assert result.sources_found == 5
    assert result.search_provider == "none"
    assert result.api_needed is None
    assert all(not source.enabled for source in result.sources)
    assert {source.company for source in result.sources} >= {"Amazon", "Microsoft"}


def test_detects_greenhouse_and_lever_tokens():
    greenhouse = detect_ats("https://boards.greenhouse.io/exampleboard")
    lever = detect_ats("https://jobs.lever.co/exampleboard")

    assert greenhouse == (
        "greenhouse",
        "exampleboard",
        "https://boards-api.greenhouse.io/v1/boards/{boardToken}/jobs?content=true",
    )
    assert lever == (
        "lever",
        "exampleboard",
        "https://api.lever.co/v0/postings/{boardToken}?mode=json",
    )

