import json
import os
from typing import Any

from .llm_client import post_json, post_json_async
from .models import JobPosting, MatchResult, ResumeInput


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


EVALUATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Overall fit score for this resume and job.",
        },
        "summary": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "missing_skills": {"type": "array", "items": {"type": "string"}},
        "resume_gaps": {"type": "array", "items": {"type": "string"}},
        "bullet_suggestions": {"type": "array", "items": {"type": "string"}},
        "interview_focus": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "score",
        "summary",
        "strengths",
        "missing_skills",
        "resume_gaps",
        "bullet_suggestions",
        "interview_focus",
    ],
}


def openai_configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def evaluate_with_openai(resume: ResumeInput, job: JobPosting, base: MatchResult) -> MatchResult:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return base

    prompt_payload = {
        "target_role": resume.target_role,
        "job": job.model_dump(by_alias=True),
        "resume": resume.content[:18000],
        "local_baseline": {
            "score": base.score,
            "matched_skills": base.matched_skills,
            "missing_skills": base.missing_skills,
            "risks": base.risks,
        },
    }

    request_body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
        "instructions": (
            "You are an SDE resume reviewer for Seattle-area software engineering roles. "
            "Evaluate only evidence present in the resume. Do not invent experience. "
            "Return concise JSON that follows the schema."
        ),
        "input": json.dumps(prompt_payload),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "resume_job_evaluation",
                "strict": True,
                "schema": EVALUATION_SCHEMA,
            }
        },
        "max_output_tokens": 1200,
    }

    try:
        raw = call_responses_api(api_key, request_body)
        parsed = json.loads(extract_output_text(raw))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return base.model_copy(update={"evaluation_source": "local"})

    return base.model_copy(
        update={
            "score": clamp_score(parsed.get("score"), base.score),
            "evaluation_source": "openai",
            "missing_skills": parsed.get("missing_skills") or base.missing_skills,
            "risks": parsed.get("resume_gaps") or base.risks,
            "bullet_suggestions": parsed.get("bullet_suggestions") or base.bullet_suggestions,
            "ai_summary": parsed.get("summary"),
            "ai_strengths": parsed.get("strengths") or [],
            "interview_focus": parsed.get("interview_focus") or [],
        }
    )


def call_responses_api(api_key: str, body: dict[str, Any]) -> dict[str, Any]:
    return post_json(
        OPENAI_RESPONSES_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        body=body,
        timeout=45,
        service="openai-responses",
        model=str(body.get("model", "")),
    )


async def call_responses_api_async(api_key: str, body: dict[str, Any]) -> dict[str, Any]:
    return await post_json_async(
        OPENAI_RESPONSES_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        body=body,
        timeout=45,
        service="openai-responses",
        model=str(body.get("model", "")),
    )


def extract_output_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]

    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]

    raise ValueError("No output text returned by OpenAI.")


def clamp_score(value: Any, fallback: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return fallback

