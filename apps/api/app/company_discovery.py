import json
import os
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Iterable

from .ingestion import USER_AGENT, fetch_text
from .models import CompanyDiscoveryRunResult, CompanyRecord, CompanySource
from .seed import load_company_seed, load_company_sources, save_company_sources


CAREER_LINK_TERMS = (
    "careers",
    "jobs",
    "join us",
    "join-us",
    "work with us",
    "open roles",
    "open positions",
)

COMMON_CAREER_PATHS = (
    "/careers",
    "/jobs",
    "/company/careers",
    "/about/careers",
    "/careers/jobs",
    "/en/careers",
)

DEFAULT_ROLE_KEYWORDS = ["software", "sde", "backend", "full stack", "platform", "intern", "new grad"]
DEFAULT_LOCATION_KEYWORDS = ["seattle", "bellevue", "redmond", "kirkland", "kent", "remote"]
SEARCH_DISCOVERY_QUERIES = (
    'Seattle "software engineer" careers',
    'Bellevue "software engineer" careers',
    'Redmond "software engineer" careers',
    'Kirkland "software engineer" careers',
    'Seattle "software engineer intern" careers',
    'Seattle "new grad" "software engineer" careers',
    'Seattle startup careers "software engineer"',
    'Seattle AI startup careers "software engineer"',
    'site:boards.greenhouse.io Seattle "software engineer"',
    'site:jobs.lever.co Seattle "software engineer"',
    'site:jobs.ashbyhq.com Seattle "software engineer"',
    'site:myworkdayjobs.com Seattle "software engineer"',
)
SEARCH_NOISE_HOST_TERMS = (
    "google.",
    "linkedin.",
    "glassdoor.",
    "indeed.",
    "ziprecruiter.",
    "monster.",
    "builtin.",
    "builtinseattle.",
    "ycombinator.",
    "simplyhired.",
    "usnlx.",
    "fastaijobs.",
    "dice.",
    "wellfound.",
    "prosple.",
    "teamreddog.",
    "synergishr.",
    "levels.fyi",
    "teamblind.",
    "reddit.",
)
TOKEN_NAME_OVERRIDES = {
    "andurilindustries": "Anduril",
    "aurorainnovation": "Aurora",
    "bankofamerica": "Bank of America",
    "ffive": "F5",
    "ngrokinc": "ngrok",
    "securityscorecard": "SecurityScorecard",
}
SEARCH_COMPANY_HOST_STOPWORDS = {
    "www",
    "careers",
    "jobs",
    "boards",
    "job-boards",
    "greenhouse",
    "lever",
    "ashbyhq",
    "myworkdayjobs",
    "workdayjobs",
}
GENERIC_COMPANY_NAMES = {
    "career",
    "careers",
    "jobs",
    "job openings",
    "open roles",
    "open",
    "search",
    "software engineer",
    "software developer",
    "seattle",
    "bellevue",
    "redmond",
    "kirkland",
}
JOB_TITLE_TERMS = (
    "software engineer",
    "software developer",
    "research engineer",
    "backend engineer",
    "frontend engineer",
    "full stack",
    "entry level",
    "new grad",
    "intern",
    "internship",
    "employment",
    "job in",
    "sr.",
    "senior",
)


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = dict(attrs)
        self._href = attrs_dict.get("href")
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(self._text_parts).strip()))
            self._href = None
            self._text_parts = []


def run_company_discovery(
    persist: bool = False,
    enable_verified: bool = False,
    use_search: bool = False,
    limit: int | None = None,
) -> CompanyDiscoveryRunResult:
    existing_sources = {source.id: source for source in load_company_sources()}
    errors: list[str] = []
    records = load_company_seed()

    search_provider = configured_search_provider()
    api_needed: str | None = None
    if use_search:
        if search_provider == "none":
            api_needed = "Set SERPAPI_API_KEY or BING_SEARCH_API_KEY to discover companies beyond the curated seed list."
        else:
            try:
                records.extend(search_company_records(search_provider))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{search_provider} search failed: {exc}")

    deduped_records = dedupe_company_records(records)
    if limit:
        deduped_records = deduped_records[:limit]

    discovered_sources: list[CompanySource] = []
    sources_added = 0
    sources_updated = 0
    sources_unchanged = 0

    for record in deduped_records:
        try:
            source = source_from_company(record, enable_verified=enable_verified)
        except OSError as exc:
            errors.append(f"{record.name}: {exc}")
            continue

        if source is None:
            errors.append(f"{record.name}: career page not found")
            continue

        discovered_sources.append(source)
        current = existing_sources.get(source.id)
        if current is None:
            existing_sources[source.id] = source
            sources_added += 1
        elif current.model_dump(by_alias=True) != source.model_dump(by_alias=True):
            existing_sources[source.id] = merge_source(current, source, enable_verified=enable_verified)
            sources_updated += 1
        else:
            sources_unchanged += 1

    if persist:
        save_company_sources(list(existing_sources.values()))

    return CompanyDiscoveryRunResult(
        companies_seen=len(deduped_records),
        sources_found=len(discovered_sources),
        sources_added=sources_added,
        sources_updated=sources_updated,
        sources_unchanged=sources_unchanged,
        search_provider=search_provider,
        api_needed=api_needed,
        errors=errors,
        sources=discovered_sources,
    )


def source_from_company(record: CompanyRecord, enable_verified: bool = False) -> CompanySource | None:
    career_url = record.known_career_url or find_career_page(record.website_url)
    if not career_url:
        return None

    ats_type, board_token, career_source_url = detect_ats(career_url)
    extraction_strategy, probe_status, probe_notes = initial_probe_labels(ats_type, career_source_url)
    confidence_note = f"company confidence={record.confidence_score:.2f}; discovered_from={record.discovery_source}"
    if ats_type == "generic_html":
        confidence_note += "; deterministic parser may need DeepSeek fallback for non-JSON-LD pages"

    return CompanySource(
        id=record.id,
        company=record.name,
        careerUrl=career_source_url,
        atsType=ats_type,
        enabled=enable_verified,
        boardToken=board_token,
        priority=priority_from_confidence(record.confidence_score),
        crawlIntervalMinutes=interval_from_confidence(record.confidence_score),
        roleKeywords=DEFAULT_ROLE_KEYWORDS,
        locationKeywords=DEFAULT_LOCATION_KEYWORDS,
        extractionStrategy=extraction_strategy,
        probeStatus=probe_status,
        probeNotes=probe_notes,
        notes=confidence_note,
    )


def find_career_page(website_url: str) -> str | None:
    homepage = fetch_text(website_url)
    parser = LinkParser()
    parser.feed(homepage)
    base = normalized_origin(website_url)

    candidate_urls: list[str] = []
    for href, text in parser.links:
        combined = f"{href} {text}".lower()
        if any(term in combined for term in CAREER_LINK_TERMS):
            candidate_urls.append(urllib.parse.urljoin(website_url, href))

    candidate_urls.extend(f"{base}{path}" for path in COMMON_CAREER_PATHS)

    for candidate in dedupe_strings(candidate_urls):
        if career_page_looks_valid(candidate):
            return candidate

    return None


def career_page_looks_valid(url: str) -> bool:
    try:
        text = fetch_text(url)[:5000].lower()
    except OSError:
        return False
    return any(term in text for term in ("career", "jobs", "open roles", "job openings", "greenhouse", "lever"))


def detect_ats(career_url: str) -> tuple[str, str | None, str]:
    parsed = urllib.parse.urlparse(career_url)
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    if "greenhouse.io" in host and path_parts:
        token = path_parts[0]
        return "greenhouse", token, "https://boards-api.greenhouse.io/v1/boards/{boardToken}/jobs?content=true"

    if "lever.co" in host and path_parts:
        token = path_parts[0]
        return "lever", token, "https://api.lever.co/v0/postings/{boardToken}?mode=json"

    if any(marker in host for marker in ("ashbyhq.com", "workdayjobs.com", "smartrecruiters.com", "icims.com")):
        return "generic_html", None, career_url

    return "generic_html", None, career_url


def initial_probe_labels(ats_type: str, career_url: str) -> tuple[str, str, str]:
    host = urllib.parse.urlparse(career_url).netloc.lower()

    if ats_type in {"greenhouse", "lever"}:
        return "direct_api", "ready", f"{ats_type} public postings API"

    if "ashbyhq.com" in host:
        return "known_ats", "adapter_needed", "Ashby board detected"
    if "smartrecruiters.com" in host:
        return "known_ats", "adapter_needed", "SmartRecruiters board detected"
    if "workdayjobs.com" in host or "myworkdayjobs.com" in host:
        return "known_ats", "adapter_needed", "Workday board detected"
    if any(marker in host for marker in ("icims.com", "jobvite.com", "phenompeople.com", "eightfold.ai")):
        return "known_ats", "adapter_needed", "enterprise ATS detected"

    return "unprobed", "unprobed", ""


def merge_source(current: CompanySource, discovered: CompanySource, enable_verified: bool) -> CompanySource:
    return discovered.model_copy(
        update={
            "enabled": enable_verified or current.enabled,
            "notes": discovered.notes if discovered.notes else current.notes,
            "extraction_strategy": current.extraction_strategy
            if current.extraction_strategy != "unprobed"
            else discovered.extraction_strategy,
            "probe_status": current.probe_status if current.probe_status != "unprobed" else discovered.probe_status,
            "probe_notes": current.probe_notes or discovered.probe_notes,
        }
    )


def search_company_records(provider: str) -> list[CompanyRecord]:
    records: list[CompanyRecord] = []
    for query in SEARCH_DISCOVERY_QUERIES:
        for title, url in search_web(provider, query):
            if is_search_noise_url(url):
                continue

            career_url = career_url_from_search_result(url)
            if career_url is None and not is_company_homepage_candidate(url):
                continue

            company_name = company_name_from_search_result(title, career_url or url)
            if not company_name:
                continue

            career_like = career_url is not None
            records.append(
                CompanyRecord(
                    id=slug(company_name),
                    name=company_name,
                    websiteUrl=career_url or normalized_origin(url),
                    knownCareerUrl=career_url,
                    headquarters="Seattle area candidate",
                    industry="Technology",
                    discoverySource=provider,
                    confidenceScore=0.70 if career_like else 0.50,
                    notes=f"Discovered from query: {query}",
                )
            )
    return records


def configured_search_provider() -> str:
    if os.getenv("SERPAPI_API_KEY"):
        return "serpapi"
    if os.getenv("BING_SEARCH_API_KEY"):
        return "bing"
    return "none"


def search_web(provider: str, query: str) -> Iterable[tuple[str, str]]:
    if provider == "serpapi":
        params = urllib.parse.urlencode(
            {
                "engine": "google",
                "q": query,
                "api_key": os.environ["SERPAPI_API_KEY"],
                "num": "10",
            }
        )
        payload = fetch_remote_json(f"https://serpapi.com/search.json?{params}")
        for result in payload.get("organic_results", []):
            link = result.get("link")
            title = result.get("title")
            if isinstance(link, str) and isinstance(title, str):
                yield title, link

    if provider == "bing":
        params = urllib.parse.urlencode({"q": query, "count": "10"})
        payload = fetch_remote_json(
            f"https://api.bing.microsoft.com/v7.0/search?{params}",
            headers={"Ocp-Apim-Subscription-Key": os.environ["BING_SEARCH_API_KEY"]},
        )
        for result in payload.get("webPages", {}).get("value", []):
            link = result.get("url")
            title = result.get("name")
            if isinstance(link, str) and isinstance(title, str):
                yield title, link


def fetch_remote_json(url: str, headers: dict[str, str] | None = None) -> dict:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def dedupe_company_records(records: list[CompanyRecord]) -> list[CompanyRecord]:
    by_key: dict[str, CompanyRecord] = {}
    identity_to_key: dict[str, str] = {}

    for record in records:
        identities = record_identity_keys(record)
        key = next((identity_to_key[identity] for identity in identities if identity in identity_to_key), None)

        if key is None:
            key = identities[0]
            by_key[key] = record
        else:
            current = by_key[key]
            if current.discovery_source != "seed" and record.confidence_score > current.confidence_score:
                by_key[key] = record

        for identity in identities:
            identity_to_key[identity] = key

    return list(by_key.values())


def record_identity_keys(record: CompanyRecord) -> list[str]:
    identities = [f"id:{record.id}"]

    name_key = canonical_company_key(record.name)
    if name_key:
        identities.append(f"name:{name_key}")

    url_key = company_domain_key(record.known_career_url or record.website_url)
    if url_key:
        identities.append(f"url:{url_key}")

    return identities


def canonical_company_key(name: str) -> str | None:
    cleaned = re.sub(r"\b(inc|llc|corp|corporation|company|group|labs|technologies)\b", "", name, flags=re.IGNORECASE)
    key = re.sub(r"[^a-z0-9]+", "", cleaned.lower())
    return key if len(key) >= 3 else None


def company_domain_key(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    ats_name = company_name_from_ats_path(host, path_parts)
    if ats_name:
        return canonical_company_key(ats_name)

    if "myworkdayjobs.com" in host or "workdayjobs.com" in host:
        return canonical_company_key(host.split(".")[0])

    labels = [label for label in host.split(".") if label not in SEARCH_COMPANY_HOST_STOPWORDS]
    if len(labels) >= 2:
        return canonical_company_key(labels[-2])
    if labels:
        return canonical_company_key(labels[0])
    return None


def dedupe_strings(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        normalized = value.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result


def normalized_origin(url: str) -> str:
    parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def looks_like_career_url(url: str) -> bool:
    return any(
        term in url.lower()
        for term in ("career", "jobs", "greenhouse", "lever", "ashbyhq", "workday", "job-search")
    )


def is_search_noise_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(term in host for term in SEARCH_NOISE_HOST_TERMS)


def career_url_from_search_result(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    ats_root = ats_root_url(host, path_parts)
    if ats_root:
        return ats_root

    if not looks_like_career_url(url) or is_probable_job_detail_url(parsed):
        return None

    return url


def ats_root_url(host: str, path_parts: list[str]) -> str | None:
    if "boards-api.greenhouse.io" in host:
        try:
            token_index = path_parts.index("boards") + 1
            return f"https://boards.greenhouse.io/{path_parts[token_index]}"
        except (ValueError, IndexError):
            return None

    if "greenhouse.io" in host and path_parts:
        return f"https://boards.greenhouse.io/{path_parts[0]}"

    if "lever.co" in host and path_parts:
        return f"https://jobs.lever.co/{path_parts[0]}"

    if "ashbyhq.com" in host and path_parts:
        return f"https://jobs.ashbyhq.com/{path_parts[0]}"

    return None


def is_company_homepage_candidate(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    return len(path_parts) <= 1 and not parsed.query


def is_probable_job_detail_url(parsed: urllib.parse.ParseResult) -> bool:
    path = parsed.path.lower()
    path_parts = [part for part in path.split("/") if part]
    if parsed.query and any(term in parsed.query.lower() for term in ("gh_jid", "jobid", "job_id", "job=")):
        return True
    for marker in ("job", "jobs", "position", "positions"):
        if marker in path_parts and path_parts.index(marker) < len(path_parts) - 1:
            return True
    if re.search(r"/jobs?/[0-9a-f-]{5,}", path):
        return True
    if re.search(r"/positions?/[0-9a-f-]{5,}", path):
        return True
    if re.search(r"/job/[0-9a-f-]{5,}", path):
        return True
    return bool(re.search(r"/(software|backend|frontend|full-stack|data|machine-learning)-", path))


def company_name_from_search_result(title: str, url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    ats_name = company_name_from_ats_path(host, path_parts)
    if ats_name:
        return ats_name

    title_name = title_to_company_name(title)
    if title_name:
        return title_name

    return company_name_from_host(host)


def company_name_from_ats_path(host: str, path_parts: list[str]) -> str | None:
    if ("greenhouse.io" in host or "lever.co" in host or "ashbyhq.com" in host) and path_parts:
        return humanize_company_token(path_parts[0])

    if "myworkdayjobs.com" in host or "workdayjobs.com" in host:
        label = host.split(".")[0]
        return humanize_company_token(label)

    return None


def title_to_company_name(title: str) -> str | None:
    cleaned = re.sub(r"\s*[-|–].*$", "", title).strip()
    cleaned = re.sub(r"\s*\|.*$", "", cleaned).strip()
    lower_cleaned = cleaned.lower()

    patterns = (
        r"^jobs at (?P<name>.+)$",
        r"^careers at (?P<name>.+)$",
        r"^grow your career at (?P<name>[^:]+).*$",
        r"^(?P<name>.+?) careers$",
        r"^(?P<name>.+?) jobs$",
        r"^(?P<name>.+?) job openings$",
        r"^(?P<name>.+?) open roles$",
    )
    for pattern in patterns:
        match = re.match(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            cleaned = match.group("name").strip()
            break
    else:
        if any(term in lower_cleaned for term in JOB_TITLE_TERMS):
            return None

    cleaned = re.sub(r"\b(careers|jobs|job openings|open roles)\b", "", cleaned, flags=re.IGNORECASE).strip(" -:|")
    if cleaned.lower() in GENERIC_COMPANY_NAMES:
        return None
    return cleaned if 2 <= len(cleaned) <= 80 else None


def company_name_from_host(host: str) -> str | None:
    labels = [label for label in host.split(".") if label and label not in SEARCH_COMPANY_HOST_STOPWORDS]
    if not labels:
        return None
    label = labels[0] if labels[0] not in {"com", "org", "net", "io", "ai"} else ""
    return humanize_company_token(label) if label else None


def humanize_company_token(value: str) -> str:
    lowered = value.lower()
    if lowered in TOKEN_NAME_OVERRIDES:
        return TOKEN_NAME_OVERRIDES[lowered]

    for suffix in ("industries", "corporation", "corp", "inc", "jobs"):
        if lowered.endswith(suffix) and len(lowered) > len(suffix) + 2:
            value = value[: -len(suffix)]
            break

    cleaned = re.sub(r"[-_]+", " ", value)
    cleaned = re.sub(r"\binc\b|\bllc\b|\bcorp\b|\bcorporation\b", "", cleaned, flags=re.IGNORECASE).strip()
    return " ".join(part.upper() if len(part) <= 3 and part.isalpha() else part.capitalize() for part in cleaned.split())


def priority_from_confidence(confidence: float) -> int:
    if confidence >= 0.9:
        return 1
    if confidence >= 0.75:
        return 2
    return 3


def interval_from_confidence(confidence: float) -> int:
    if confidence >= 0.9:
        return 60
    if confidence >= 0.75:
        return 180
    return 360


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
