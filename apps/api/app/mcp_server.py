import json
import sys
from typing import Any

from .database import load_jobs_from_database
from .models import ApplicationStage
from .seed import load_seed_jobs


MCP_PROTOCOL_VERSION = "2025-06-18"


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_jobs",
        "description": "Search Seattle-area software roles by query, location, and candidate level.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Company, title, skill, or description keyword."},
                "location": {"type": "string", "description": "Seattle, Bellevue, Redmond, Kirkland, remote, or blank."},
                "audience": {"type": "string", "enum": ["", "new-grad", "intern"], "default": ""},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
        },
    },
    {
        "name": "get_job_details",
        "description": "Return full job descriptions and skill requirements for selected job IDs. This tool does not score resumes; the external AI agent should evaluate fit from the returned evidence.",
        "inputSchema": {
            "type": "object",
            "required": ["job_ids"],
            "properties": {
                "job_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
        },
    },
    {
        "name": "prepare_application_action",
        "description": "Validate a job and return a structured application-tracker action payload.",
        "inputSchema": {
            "type": "object",
            "required": ["job_id"],
            "properties": {
                "job_id": {"type": "string"},
                "stage": {
                    "type": "string",
                    "enum": ["saved", "applied", "oa", "interview", "rejected", "offer"],
                    "default": "saved",
                },
                "notes": {"type": "string", "default": ""},
                "follow_up_on": {"type": "string", "description": "Optional YYYY-MM-DD date."},
            },
        },
    },
]


def main() -> None:
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        response = handle_jsonrpc(raw_line)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def handle_jsonrpc(raw_message: str) -> dict[str, Any] | None:
    try:
        message = json.loads(raw_message)
    except json.JSONDecodeError as exc:
        return jsonrpc_error(None, -32700, f"Parse error: {exc}")

    if not isinstance(message, dict):
        return jsonrpc_error(None, -32600, "Invalid request.")

    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return jsonrpc_result(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ai-job-intelligence", "version": "0.1.0"},
            },
        )
    if method == "tools/list":
        return jsonrpc_result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        return handle_tool_call(request_id, params)

    return jsonrpc_error(request_id, -32601, f"Unsupported method: {method}")


def handle_tool_call(request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    if not isinstance(name, str):
        return jsonrpc_error(request_id, -32602, "tools/call requires a tool name.")

    try:
        result = call_tool(name, arguments)
        return jsonrpc_result(request_id, tool_result(result))
    except ValueError as exc:
        return jsonrpc_result(request_id, tool_result({"error": str(exc)}, is_error=True))


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "search_jobs":
        return search_jobs(arguments)
    if name == "get_job_details":
        return get_job_details(arguments)
    if name == "prepare_application_action":
        return prepare_application_action(arguments)
    raise ValueError(f"Unknown tool: {name}")


def search_jobs(arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments.get("query", ""))
    location = str(arguments.get("location", ""))
    audience = str(arguments.get("audience", ""))
    limit = bounded_limit(arguments.get("limit", 5))

    jobs = filter_jobs(load_jobs_for_tools(), query=query, location=location, audience=audience)
    return {
        "total": len(jobs),
        "items": [job_summary(job) for job in jobs[:limit]],
    }


def get_job_details(arguments: dict[str, Any]) -> dict[str, Any]:
    job_ids = arguments.get("job_ids")
    selected_ids = {str(job_id) for job_id in job_ids} if isinstance(job_ids, list) else set()
    if not selected_ids:
        raise ValueError("get_job_details requires at least one job_id.")

    jobs = load_jobs_for_tools()
    selected = [job for job in jobs if job.id in selected_ids]
    missing = sorted(selected_ids - {job.id for job in selected})

    return {
        "items": [
            {
                **job_summary(job),
                "description": job.description,
                "source": job.source,
            }
            for job in selected
        ],
        "missing_job_ids": missing,
    }


def prepare_application_action(arguments: dict[str, Any]) -> dict[str, Any]:
    job_id = str(arguments.get("job_id", ""))
    stage = ApplicationStage(str(arguments.get("stage", "saved")))
    notes = str(arguments.get("notes", ""))
    follow_up_on = arguments.get("follow_up_on")

    jobs = {job.id: job for job in load_jobs_for_tools()}
    if job_id not in jobs:
        raise ValueError(f"Job not found: {job_id}")

    payload = {
        "job_id": job_id,
        "stage": stage.value,
        "notes": notes,
        "follow_up_on": str(follow_up_on) if follow_up_on else None,
    }
    return {
        "action": "create_or_update_application",
        "job": job_summary(jobs[job_id]),
        "payload": payload,
        "api_hint": "POST /applications",
    }


def load_jobs_for_tools():
    try:
        database_jobs = load_jobs_from_database()
        if database_jobs:
            return database_jobs
    except Exception:
        pass
    return load_seed_jobs()


def filter_jobs(jobs, query: str = "", location: str = "", audience: str = ""):
    query_lower = query.lower().strip()
    location_lower = location.lower().strip()
    audience_lower = audience.lower().strip()

    if query_lower:
        jobs = [
            job
            for job in jobs
            if query_lower
            in f"{job.company} {job.title} {job.description} {' '.join(job.required_skills)} {' '.join(job.nice_to_have_skills)}".lower()
        ]
    if location_lower:
        jobs = [job for job in jobs if location_lower in f"{job.location} {job.work_mode}".lower()]
    if audience_lower == "new-grad":
        jobs = [job for job in jobs if job.level in {"new-grad", "entry"}]
    elif audience_lower == "intern":
        jobs = [job for job in jobs if job.level == "intern"]
    return jobs


def job_summary(job) -> dict[str, Any]:
    return {
        "id": job.id,
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "level": job.level,
        "work_mode": job.work_mode,
        "source_url": job.source_url,
        "required_skills": job.required_skills,
        "nice_to_have_skills": job.nice_to_have_skills,
    }


def bounded_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 5
    return max(1, min(10, limit))


def tool_result(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    result = {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
    }
    if is_error:
        result["isError"] = True
    return result


def jsonrpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


if __name__ == "__main__":
    main()
