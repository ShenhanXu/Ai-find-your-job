import json
import re
import urllib.error
import urllib.parse
from collections import Counter

from .company_discovery import LinkParser, detect_ats
from .ingestion import fetch_text, source_url
from .models import CompanyProbeRunResult, CompanySource
from .seed import load_company_sources, save_company_sources


KNOWN_ATS_MARKERS = {
    "ashbyhq.com": "Ashby board",
    "smartrecruiters.com": "SmartRecruiters board",
    "workdayjobs.com": "Workday board",
    "myworkdayjobs.com": "Workday board",
    "icims.com": "iCIMS board",
    "jobvite.com": "Jobvite board",
    "phenompeople.com": "Phenom board",
    "eightfold.ai": "Eightfold board",
}
BLOCKED_STATUS_CODES = {401, 403, 406, 429}


def probe_company_sources(persist: bool = False, limit: int | None = None) -> CompanyProbeRunResult:
    sources = load_company_sources()
    selected_sources = sources[:limit] if limit else sources
    source_by_id = {source.id: source for source in sources}
    errors: list[str] = []
    probed: list[CompanySource] = []
    updated_count = 0

    for source in selected_sources:
        try:
            updated = probe_company_source(source)
        except Exception as exc:
            errors.append(f"{source.company}: {exc}")
            updated = source.model_copy(
                update={
                    "extraction_strategy": "manual_review",
                    "probe_status": "error",
                    "probe_notes": str(exc)[:180],
                }
            )

        if updated.model_dump(by_alias=True) != source.model_dump(by_alias=True):
            updated_count += 1
        source_by_id[source.id] = updated
        probed.append(updated)

    if persist:
        save_company_sources(list(source_by_id.values()))

    strategy_counts = Counter(source.extraction_strategy for source in source_by_id.values())
    status_counts = Counter(source.probe_status for source in source_by_id.values())

    return CompanyProbeRunResult(
        sources_seen=len(sources),
        sources_probed=len(selected_sources),
        sources_updated=updated_count,
        strategy_counts=dict(sorted(strategy_counts.items())),
        status_counts=dict(sorted(status_counts.items())),
        errors=errors,
        sources=probed,
    )


def probe_company_source(source: CompanySource) -> CompanySource:
    url = source_url(source)
    url_strategy = classify_url(source.ats_type, url)
    if url_strategy:
        return update_probe(source, *url_strategy)

    try:
        document = fetch_text(url)[:400_000]
    except urllib.error.HTTPError as exc:
        if exc.code in BLOCKED_STATUS_CODES:
            return update_probe(source, "needs_browser", "blocked", f"HTTP {exc.code}; browser/proxy required")
        return update_probe(source, "manual_review", "error", f"HTTP {exc.code}")
    except OSError as exc:
        return update_probe(source, "manual_review", "error", str(exc)[:180])

    linked_ats = linked_ats_source(source, document)
    if linked_ats:
        return linked_ats

    lower_doc = document.lower()
    if has_jobposting_json_ld(document):
        return update_probe(source, "json_ld", "ready", "JobPosting JSON-LD found")

    if "no open roles" in lower_doc or "no open positions" in lower_doc:
        return update_probe(source, "html_list", "no_open_roles", "careers page currently has no open roles")

    if looks_like_html_job_list(document):
        return update_probe(source, "html_list", "ready", "job-like links/cards found in HTML")

    if looks_like_js_rendered(lower_doc):
        return update_probe(source, "needs_browser", "adapter_needed", "likely JavaScript-rendered job search")

    return update_probe(source, "needs_ai", "ai_candidate", "no structured job signal found")


def classify_url(ats_type: str, url: str) -> tuple[str, str, str] | None:
    host = urllib.parse.urlparse(url).netloc.lower()

    if ats_type in {"greenhouse", "lever"}:
        return "direct_api", "ready", f"{ats_type} public postings API"

    for marker, label in KNOWN_ATS_MARKERS.items():
        if marker in host:
            return "known_ats", "adapter_needed", label

    return None


def linked_ats_source(source: CompanySource, document: str) -> CompanySource | None:
    parser = LinkParser()
    parser.feed(document)
    base = source.career_url
    candidates = [urllib.parse.urljoin(base, href) for href, _text in parser.links]
    candidates.extend(extract_url_candidates(document))

    for candidate in candidates:
        ats_type, board_token, career_url = detect_ats(candidate)
        if ats_type in {"greenhouse", "lever"} and board_token:
            return source.model_copy(
                update={
                    "career_url": career_url,
                    "ats_type": ats_type,
                    "board_token": board_token,
                    "extraction_strategy": "direct_api",
                    "probe_status": "ready",
                    "probe_notes": f"linked {ats_type} board found on careers page",
                }
            )

        strategy = classify_url("generic_html", candidate)
        if strategy and is_useful_known_ats_url(candidate):
            return update_probe(source, strategy[0], strategy[1], f"linked {strategy[2]} found on careers page")

    return None


def extract_url_candidates(document: str) -> list[str]:
    return re.findall(
        r"https?://[^\s\"'<>]+(?:greenhouse\.io|lever\.co|ashbyhq\.com|workdayjobs\.com|myworkdayjobs\.com|smartrecruiters\.com|icims\.com|jobvite\.com|phenompeople\.com|eightfold\.ai)[^\s\"'<>]*",
        document,
        flags=re.IGNORECASE,
    )


def is_useful_known_ats_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    if re.search(r"\.(css|js|png|jpg|jpeg|gif|svg|woff|woff2|ico)(?:$|\?)", path):
        return False
    if "login" in path or "in_iframe" in query:
        return False
    return any(term in f"{parsed.netloc.lower()} {path}" for term in ("career", "job", "requisition", "position"))


def has_jobposting_json_ld(document: str) -> bool:
    blocks = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        document,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for block in blocks:
        try:
            payload = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        if json_has_jobposting(payload):
            return True
    return False


def json_has_jobposting(payload: object) -> bool:
    if isinstance(payload, list):
        return any(json_has_jobposting(item) for item in payload)
    if not isinstance(payload, dict):
        return False
    if payload.get("@type") == "JobPosting":
        return True
    graph = payload.get("@graph")
    return isinstance(graph, list) and any(json_has_jobposting(item) for item in graph)


def looks_like_html_job_list(document: str) -> bool:
    lower_doc = document.lower()
    job_link_count = len(
        re.findall(r"<a[^>]+href=[\"'][^\"']*(?:job|career|position|opening)[^\"']*[\"']", lower_doc)
    )
    title_hits = len(
        re.findall(
            r"(software engineer|software developer|backend engineer|frontend engineer|machine learning engineer|data engineer|sde|intern)",
            lower_doc,
        )
    )
    location_hits = len(re.findall(r"(seattle|bellevue|redmond|kirkland|remote)", lower_doc))
    return job_link_count >= 3 and title_hits >= 1 and location_hits >= 1


def looks_like_js_rendered(lower_doc: str) -> bool:
    return any(
        marker in lower_doc
        for marker in (
            "__next_data__",
            "window.__apollo_state__",
            "search jobs",
            "job search",
            "workday",
            "greenhouse",
            "lever",
        )
    )


def update_probe(source: CompanySource, strategy: str, status: str, notes: str) -> CompanySource:
    return source.model_copy(
        update={
            "extraction_strategy": strategy,
            "probe_status": status,
            "probe_notes": notes,
        }
    )
