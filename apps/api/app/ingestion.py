import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .matcher import SKILL_ALIASES
from .models import CompanySource, CrawlError, IngestionRunResult, JobPosting
from .seed import data_path, load_company_sources


USER_AGENT = "AIJobMatchBot/0.1 (+portfolio crawler; contact: local-dev)"
MAX_HTML_LIST_PAGES = 30


@dataclass
class CrawlSourceResult:
    jobs: list[JobPosting]
    needs_ai_extraction: int = 0


def run_ingestion(
    existing_jobs: dict[str, JobPosting],
    sources: list[CompanySource] | None = None,
) -> IngestionRunResult:
    sources = load_company_sources() if sources is None else sources
    enabled_sources = [source for source in sources if source.enabled]
    errors: list[CrawlError] = []
    jobs_seen = 0
    jobs_added = 0
    jobs_updated = 0
    jobs_unchanged = 0
    needs_ai_extraction = 0

    for source in enabled_sources:
        try:
            result = crawl_source(source)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(CrawlError(source_id=source.id, company=source.company, message=str(exc)))
            continue

        needs_ai_extraction += result.needs_ai_extraction
        for job in result.jobs:
            jobs_seen += 1
            current = existing_jobs.get(job.id)
            if current is None:
                existing_jobs[job.id] = job
                jobs_added += 1
            elif current.fingerprint != job.fingerprint:
                existing_jobs[job.id] = job
                jobs_updated += 1
            else:
                jobs_unchanged += 1

    return IngestionRunResult(
        sources_seen=len(sources),
        sources_crawled=len(enabled_sources),
        jobs_seen=jobs_seen,
        jobs_added=jobs_added,
        jobs_updated=jobs_updated,
        jobs_unchanged=jobs_unchanged,
        needs_ai_extraction=needs_ai_extraction,
        errors=errors,
    )


def crawl_source(source: CompanySource) -> CrawlSourceResult:
    if source.ats_type == "greenhouse":
        rows = fetch_json(source_url(source)).get("jobs", [])
        return CrawlSourceResult([job for row in rows if (job := greenhouse_job(source, row))])

    if source.ats_type == "lever":
        rows = fetch_json(source_url(source))
        return CrawlSourceResult([job for row in rows if (job := lever_job(source, row))])

    if source.ats_type == "generic_json":
        payload = fetch_json(source_url(source))
        rows = payload.get("jobs", payload if isinstance(payload, list) else [])
        return CrawlSourceResult([job for row in rows if (job := generic_json_job(source, row))])

    if source.ats_type == "generic_html":
        initial_url = source_url(source)
        document = fetch_text(initial_url)
        jobs = parse_json_ld_jobs(source, document)
        if jobs:
            return CrawlSourceResult(jobs)

        html_jobs = parse_paginated_html_list_jobs(source, document, initial_url)
        needs_ai = 0 if html_jobs else 1
        return CrawlSourceResult(html_jobs, needs_ai_extraction=needs_ai)

    raise ValueError(f"Unsupported ATS type: {source.ats_type}")


def source_url(source: CompanySource) -> str:
    if source.board_token:
        return source.career_url.replace("{boardToken}", source.board_token)
    return source.career_url


def fetch_json(url: str) -> Any:
    return json.loads(fetch_text(url))


def fetch_text(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8", errors="replace")

    path = data_path(url) if not Path(url).is_absolute() else Path(url)
    return path.read_text(encoding="utf-8")


def greenhouse_job(source: CompanySource, row: dict[str, Any]) -> JobPosting | None:
    title = str(row.get("title", "")).strip()
    location = nested(row, ["location", "name"]) or "Unknown"
    description = clean_html(str(row.get("content", "")))
    url = str(row.get("absolute_url") or row.get("url") or source.career_url)
    return build_job(
        source=source,
        source_job_id=str(row.get("id") or url or title),
        title=title,
        location=location,
        description=description,
        url=url,
        raw=row,
        source_label="greenhouse",
    )


def lever_job(source: CompanySource, row: dict[str, Any]) -> JobPosting | None:
    title = str(row.get("text", "")).strip()
    categories = row.get("categories") or {}
    location = str(categories.get("location") or row.get("workplaceType") or "Unknown")
    sections = [str(row.get("descriptionPlain") or row.get("description") or "")]
    for item in row.get("lists", []):
        sections.append(str(item.get("text") or ""))
    description = clean_html("\n".join(sections))
    url = str(row.get("hostedUrl") or row.get("applyUrl") or source.career_url)
    return build_job(
        source=source,
        source_job_id=str(row.get("id") or url or title),
        title=title,
        location=location,
        description=description,
        url=url,
        raw=row,
        source_label="lever",
    )


def generic_json_job(source: CompanySource, row: dict[str, Any]) -> JobPosting | None:
    title = str(row.get("title", "")).strip()
    location = str(row.get("location") or row.get("jobLocation") or "Unknown")
    description = clean_html(str(row.get("description") or row.get("summary") or ""))
    url = str(row.get("url") or row.get("applyUrl") or source.career_url)
    required_skills = row.get("requiredSkills")
    nice_to_have = row.get("niceToHaveSkills")
    return build_job(
        source=source,
        source_job_id=str(row.get("id") or url or title),
        title=title,
        location=location,
        description=description,
        url=url,
        raw=row,
        source_label=source.ats_type,
        required_skills=required_skills if isinstance(required_skills, list) else None,
        nice_to_have=nice_to_have if isinstance(nice_to_have, list) else None,
        level=str(row.get("level")) if row.get("level") else None,
        work_mode=str(row.get("workMode")) if row.get("workMode") else None,
    )


def parse_json_ld_jobs(source: CompanySource, document: str) -> list[JobPosting]:
    jobs: list[JobPosting] = []
    blocks = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        document,
        flags=re.IGNORECASE | re.DOTALL,
    )

    for block in blocks:
        try:
            payload = json.loads(html.unescape(block.strip()))
        except json.JSONDecodeError:
            continue

        for item in flatten_json_ld(payload):
            if item.get("@type") != "JobPosting":
                continue
            title = str(item.get("title", "")).strip()
            description = clean_html(str(item.get("description") or ""))
            location = json_ld_location(item.get("jobLocation")) or "Unknown"
            url = str(item.get("url") or item.get("sameAs") or source.career_url)
            job = build_job(
                source=source,
                source_job_id=url or title,
                title=title,
                location=location,
                description=description,
                url=url,
                raw=item,
                source_label="json-ld",
            )
            if job:
                jobs.append(job)

    return jobs


def parse_paginated_html_list_jobs(source: CompanySource, document: str, initial_url: str) -> list[JobPosting]:
    jobs = parse_html_list_jobs(source, document, initial_url)
    total_pages = facetwp_total_pages(document)
    if total_pages <= 1:
        return jobs

    for page_number in range(2, min(total_pages, MAX_HTML_LIST_PAGES) + 1):
        page_url = paginated_url(initial_url, page_number)
        try:
            page_document = fetch_text(page_url)
        except OSError:
            continue
        jobs.extend(parse_html_list_jobs(source, page_document, page_url))

    return dedupe_jobs(jobs)


def parse_html_list_jobs(source: CompanySource, document: str, page_url: str) -> list[JobPosting]:
    return parse_inner_grid_jobs(source, document, page_url)


def parse_inner_grid_jobs(source: CompanySource, document: str, page_url: str) -> list[JobPosting]:
    jobs: list[JobPosting] = []
    blocks = re.findall(r"<li[^>]+class=[\"'][^\"']*inner-grid[^\"']*[\"'][^>]*>.*?</li>", document, flags=re.I | re.S)

    for block in blocks:
        link = re.search(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", block, flags=re.I | re.S)
        if not link:
            continue

        url = urllib.parse.urljoin(page_url, html.unescape(link.group(1)))
        title = clean_html(link.group(2))
        spans = [clean_html(span) for span in re.findall(r"<span[^>]*>(.*?)</span>", block, flags=re.I | re.S)]
        spans = [span for span in spans if span and span != "•"]
        department = spans[0] if spans else ""
        work_flexibility = spans[1] if len(spans) > 2 else ""
        location = spans[-1] if len(spans) > 1 else "Unknown"
        description = "\n".join(
            part
            for part in (
                title,
                f"Team: {department}" if department else "",
                f"Work flexibility: {work_flexibility}" if work_flexibility else "",
                f"Location: {location}" if location else "",
            )
            if part
        )

        job = build_job(
            source=source,
            source_job_id=url,
            title=title,
            location=location,
            description=description,
            url=url,
            raw={"title": title, "department": department, "work_flexibility": work_flexibility, "page_url": page_url},
            source_label="html-list",
        )
        if job:
            jobs.append(job)

    return jobs


def facetwp_total_pages(document: str) -> int:
    match = re.search(r'"total_pages"\s*:\s*(\d+)', document)
    if not match:
        return 1
    return max(1, int(match.group(1)))


def paginated_url(initial_url: str, page_number: int) -> str:
    parsed = urllib.parse.urlparse(initial_url)
    path = parsed.path
    path = re.sub(r"/page/\d+/?$", "/", path)
    if not path.endswith("/"):
        path += "/"
    path = f"{path}page/{page_number}/"
    return urllib.parse.urlunparse(parsed._replace(path=path, query=""))


def dedupe_jobs(jobs: list[JobPosting]) -> list[JobPosting]:
    by_id: dict[str, JobPosting] = {}
    for job in jobs:
        by_id[job.id] = job
    return list(by_id.values())


def build_job(
    source: CompanySource,
    source_job_id: str,
    title: str,
    location: str,
    description: str,
    url: str,
    raw: dict[str, Any],
    source_label: str,
    required_skills: list[str] | None = None,
    nice_to_have: list[str] | None = None,
    level: str | None = None,
    work_mode: str | None = None,
) -> JobPosting | None:
    if not title or not passes_source_filters(source, title, location, description):
        return None

    inferred_required = required_skills or infer_skills(f"{title}\n{description}")[:8]
    inferred_nice = nice_to_have or infer_nice_skills(inferred_required)
    normalized_description = description or title
    source_url_value = url or source.career_url
    fingerprint = job_fingerprint(title, location, normalized_description, source_url_value, raw)

    return JobPosting(
        id=make_job_id(source, source_job_id),
        company=source.company,
        title=title,
        location=location,
        source=source_label,
        sourceUrl=source_url_value,
        level=level or infer_level(title, normalized_description),
        workMode=work_mode or infer_work_mode(location, normalized_description),
        description=normalized_description,
        requiredSkills=inferred_required,
        niceToHaveSkills=inferred_nice,
        fingerprint=fingerprint,
    )


def passes_source_filters(source: CompanySource, title: str, location: str, description: str) -> bool:
    text = f"{title} {description}".lower()
    location_text = location.lower()
    role_match = not source.role_keywords or any(keyword.lower() in text for keyword in source.role_keywords)
    location_match = not source.location_keywords or any(
        matches_location_keyword(keyword, location_text, text) for keyword in source.location_keywords
    )
    return role_match and location_match


def matches_location_keyword(keyword: str, location_text: str, text: str) -> bool:
    normalized_keyword = keyword.lower()
    if normalized_keyword != "remote":
        return normalized_keyword in location_text or normalized_keyword in text

    remote_signal = "remote" in location_text or "live and work anywhere" in text
    us_signal = any(
        term in location_text or term in text
        for term in (
            "remote-us",
            "remote us",
            "remote - us",
            "remote-usa",
            "remote usa",
            "remote - usa",
            "remote, usa",
            "usa - remote",
            "united states",
            "remote, united states",
        )
    )
    return remote_signal and us_signal


def infer_skills(text: str) -> list[str]:
    normalized = text.lower()
    skills = []
    for skill, aliases in SKILL_ALIASES.items():
        if any(re.search(rf"(?<![a-z0-9+#.]){re.escape(alias.lower())}(?![a-z0-9+#.])", normalized) for alias in aliases):
            skills.append(skill)
    return skills or ["Software Engineering"]


def infer_nice_skills(required: list[str]) -> list[str]:
    defaults = ["Testing", "CI/CD", "Observability", "System Design", "Docker", "AWS"]
    return [skill for skill in defaults if skill not in required][:4]


def infer_level(title: str, description: str) -> str:
    text = f"{title} {description}".lower()
    if "intern" in text:
        return "intern"
    if "new grad" in text or "university grad" in text or "graduate" in text or "software engineer i" in text:
        return "new-grad"
    if "senior" in text or "staff" in text or "principal" in text:
        return "senior"
    if "associate" in text or "entry" in text:
        return "entry"
    return "mid"


def infer_work_mode(location: str, description: str) -> str:
    text = f"{location} {description}".lower()
    if "remote" in text:
        return "remote"
    if "hybrid" in text:
        return "hybrid"
    return "onsite"


def clean_html(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    normalized = re.sub(r"\s+", " ", html.unescape(without_tags)).strip()
    return normalized


def flatten_json_ld(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for row in payload for item in flatten_json_ld(row)]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("@graph"), list):
        return [item for row in payload["@graph"] for item in flatten_json_ld(row)]
    return [payload]


def json_ld_location(value: Any) -> str | None:
    if isinstance(value, list):
        return json_ld_location(value[0]) if value else None
    if not isinstance(value, dict):
        return None
    address = value.get("address")
    if isinstance(address, dict):
        city = address.get("addressLocality")
        region = address.get("addressRegion")
        return ", ".join(str(part) for part in (city, region) if part)
    if isinstance(value.get("name"), str):
        return value["name"]
    return None


def nested(row: dict[str, Any], keys: list[str]) -> str | None:
    current: Any = row
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return str(current) if current else None


def make_job_id(source: CompanySource, source_job_id: str) -> str:
    stable = re.sub(r"[^a-z0-9]+", "-", f"{source.id}-{source_job_id}".lower()).strip("-")
    if len(stable) <= 96:
        return stable
    digest = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:12]
    return f"{stable[:80].strip('-')}-{digest}"


def job_fingerprint(title: str, location: str, description: str, url: str, raw: dict[str, Any]) -> str:
    content = json.dumps(
        {
            "title": title,
            "location": location,
            "description": description,
            "url": url,
            "updated_at": raw.get("updated_at") or raw.get("updatedAt") or raw.get("updatedDate"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
