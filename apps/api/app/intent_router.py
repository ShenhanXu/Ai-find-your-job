import json
import os
import re
from typing import Any

from pydantic import ValidationError

from .llm_client import LLMRequestError, post_json_async
from .models import ChatRequest, IntentRoute, JobPosting
from .openai_eval import call_responses_api_async, extract_output_text, openai_configured


DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"

ROUTER_INTENTS = [
    "job_search",
    "job_detail_lookup",
    "job_compare",
    "resume_fit_analysis",
    "resume_tailoring",
    "skill_gap_analysis",
    "application_action",
    "application_status_query",
    "platform_help",
    "small_talk",
    "off_topic",
    "nonsense",
    "clarification_needed",
    "unsupported",
]

SYSTEM_INTENTS = ["router_unavailable"]
SUPPORTED_INTENTS = ROUTER_INTENTS + SYSTEM_INTENTS

INTENT_DEFAULTS: dict[str, tuple[bool, bool]] = {
    "job_search": (True, False),
    "job_detail_lookup": (True, False),
    "job_compare": (True, False),
    "resume_fit_analysis": (True, False),
    "resume_tailoring": (True, False),
    "skill_gap_analysis": (True, False),
    "application_action": (True, True),
    "application_status_query": (False, False),
    "platform_help": (False, False),
    "small_talk": (False, False),
    "off_topic": (False, False),
    "nonsense": (False, False),
    "clarification_needed": (False, False),
    "unsupported": (False, False),
    "router_unavailable": (False, False),
}

ROUTER_ENTITY_FIELDS = [
    "action",
    "stage",
    "job_id",
    "company",
    "job_title",
    "query",
    "location",
    "audience",
    "limit",
    "follow_up_on",
    "requested_status",
    "job_reference",
    "focus",
]

ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {"type": "string", "enum": ROUTER_INTENTS},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "needs_retrieval": {"type": "boolean"},
        "needs_action": {"type": "boolean"},
        "entities": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {"type": ["string", "null"]},
                "stage": {"type": ["string", "null"]},
                "job_id": {"type": ["string", "null"]},
                "company": {"type": ["string", "null"]},
                "job_title": {"type": ["string", "null"]},
                "query": {"type": ["string", "null"]},
                "location": {"type": ["string", "null"]},
                "audience": {"type": ["string", "null"]},
                "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 10},
                "follow_up_on": {"type": ["string", "null"]},
                "requested_status": {"type": ["string", "null"]},
                "job_reference": {"type": ["string", "null"]},
                "focus": {"type": "array", "items": {"type": "string"}},
            },
            "required": ROUTER_ENTITY_FIELDS,
        },
        "missing_fields": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": [
        "intent",
        "confidence",
        "needs_retrieval",
        "needs_action",
        "entities",
        "missing_fields",
        "reason",
    ],
}


# Stable prefixes of the clarification answers generated in ai_chat.py.
# Keep these in sync with clarification_response so repeat-clarification
# detection keeps working if the copy changes.
CLARIFICATION_MARKERS = ("我需要再确认一下", "I need one more detail")


def is_clarification_text(text: str) -> bool:
    return any(marker in text for marker in CLARIFICATION_MARKERS)


def previous_clarification_question(request: ChatRequest) -> str | None:
    """If the conversation is sitting behind an assistant clarification ask,
    return the user question that triggered it (may be empty). Otherwise None."""
    messages = request.messages
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role != "assistant" or not message.content.strip():
            continue
        if not is_clarification_text(message.content):
            return None
        for prior in range(index - 1, -1, -1):
            if messages[prior].role == "user" and messages[prior].content.strip():
                return messages[prior].content.strip()
        return ""
    return None


def escalate_repeated_clarification(route: IntentRoute, request: ChatRequest) -> IntentRoute:
    """Never ask a clarification twice in a row. If the user already answered
    one, retrieve over the combined context instead of interrogating again."""
    if route.intent != "clarification_needed":
        return route
    prior_question = previous_clarification_question(request)
    if prior_question is None:
        return route

    combined = f"{prior_question}\n{request.question}".strip()
    return IntentRoute(
        intent="job_search",
        confidence=max(route.confidence, 0.6),
        needs_retrieval=True,
        needs_action=False,
        entities={**route.entities, "query": combined},
        missing_fields=[],
        reason=(
            "User already answered a clarification question; retrieving over the combined "
            "context instead of asking again."
        ),
        source="system",
    )


async def route_intent(request: ChatRequest, jobs: list[JobPosting], use_llm: bool | None = None) -> IntentRoute:
    route = await resolve_route(request, jobs, use_llm)
    return escalate_repeated_clarification(route, request)


async def resolve_route(request: ChatRequest, jobs: list[JobPosting], use_llm: bool | None = None) -> IntentRoute:
    if use_llm is None:
        use_llm = request.use_llm and os.getenv("INTENT_ROUTER_MODE", "hybrid").lower() != "rules"

    rule_route = route_with_rules(request, jobs)
    if use_llm and is_trusted_rule_route(rule_route):
        return rule_route

    if use_llm:
        if not router_llm_configured():
            return make_route(
                "router_unavailable",
                0.0,
                {"query": request.question.strip()},
                "Intent router LLM is not configured.",
                source="system",
            )
        try:
            llm_route = await route_with_llm(request, jobs)
            rescued = rescue_llm_route(llm_route, rule_route)
            return prefer_retrieval_over_generic_clarification(rescued, request)
        except (OSError, ValueError, KeyError, json.JSONDecodeError, ValidationError):
            return make_route(
                "router_unavailable",
                0.0,
                {"query": request.question.strip()},
                "Intent router LLM failed before it could classify the request.",
                source="system",
            )

    return rule_route


def router_llm_configured() -> bool:
    return bool(os.getenv("DEEPSEEK_API_KEY") or openai_configured())


async def route_with_llm(request: ChatRequest, jobs: list[JobPosting]) -> IntentRoute:
    prompt = build_router_prompt(request, jobs)
    if os.getenv("DEEPSEEK_API_KEY"):
        raw_text = await call_deepseek_router(prompt)
    else:
        raw_text = await call_openai_router(prompt)

    payload = json.loads(extract_json_object(raw_text))
    return normalize_route(payload, source="llm")


async def call_openai_router(prompt: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "")
    body = {
        "model": os.getenv("OPENAI_ROUTER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.4-mini")),
        "instructions": (
            "You are an intent router for an AI job-search product. "
            "Return only JSON that follows the provided schema. "
            "Choose the best supported intent from the list. "
            "If the request is out of scope, choose unsupported. "
            "If it is job-search related but missing required context, choose clarification_needed. "
            "Do not use clarification_needed for greetings, off-topic messages, or nonsense."
        ),
        "input": prompt,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "job_copilot_intent_route",
                "strict": True,
                "schema": ROUTER_SCHEMA,
            }
        },
        "max_output_tokens": 900,
    }
    return extract_output_text(await call_responses_api_async(api_key, body))


async def call_deepseek_router(prompt: str) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    model = os.getenv("DEEPSEEK_ROUTER_MODEL", os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"))
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an intent router for an AI job-search product. "
                    "Return only valid JSON. Do not include markdown. "
                    "Choose one supported intent and fill every entity key, using null or [] when unknown. "
                    "Do not use clarification_needed unless a job-search task is present but lacks required context."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
    }
    payload = await post_json_async(
        DEEPSEEK_CHAT_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        body=body,
        timeout=30,
        service="deepseek-router",
        model=model,
    )
    choices = payload.get("choices") or []
    content = (choices[0].get("message") or {}).get("content") if choices else None
    if not isinstance(content, str) or not content:
        raise LLMRequestError("deepseek-router returned no message content.")
    return content


def build_router_prompt(request: ChatRequest, jobs: list[JobPosting]) -> str:
    current_jobs = [job for job in jobs if request.job_ids and job.id in request.job_ids]
    if not current_jobs:
        current_jobs = jobs[:8]

    payload = {
        "supported_intents": ROUTER_INTENTS,
        "intent_descriptions": {
            "job_search": "Find or recommend jobs from the job database.",
            "job_detail_lookup": "Show details, requirements, source link, or full description for a specific job.",
            "job_compare": "Compare multiple jobs or rank which one is best.",
            "resume_fit_analysis": "Analyze whether the candidate is a good fit for one or more jobs.",
            "resume_tailoring": "Suggest resume bullets, project framing, or resume edits for target jobs.",
            "skill_gap_analysis": "Identify missing skills or build a skill-gap matrix.",
            "application_action": "Save, track, mark applied, set follow-up, or update application stage.",
            "application_status_query": "Query saved/applied/interview/follow-up application tracker records.",
            "platform_help": "Explain what the assistant or platform can do.",
            "small_talk": "Friendly greeting, thanks, brief social chat, or identity question without a job-search task.",
            "off_topic": "Understandable message that is not about jobs, resumes, applications, or this platform.",
            "nonsense": "Gibberish, repeated characters, venting, or text with no actionable intent.",
            "clarification_needed": "The request is job-search related but lacks enough context to choose or run a workflow.",
            "unsupported": "The user asks for a concrete capability outside the job-search product scope.",
        },
        "routing_rules": [
            "The current user_question is authoritative for intent. Conversation history is only supporting context.",
            "Never infer application_action from assistant messages or prior assistant clarification text.",
            "Use user_history and current_job_ids to resolve phrases like 'this one' or 'that role'.",
            "Do not invent unsupported tools or actions.",
            "Do not choose application_action unless the user asks to change or prepare tracker state.",
            "Use unsupported when the user requests a real task the product cannot perform, such as travel booking, medical advice, legal advice, tax advice, finance advice, or weather forecasts.",
            "Use off_topic for understandable conversation that is not asking the product to do job-search work.",
            "Use nonsense for gibberish, repeated symbols, incoherent text, or a message with no actionable intent.",
            "Use small_talk for greetings, thanks, brief social chat, or asking what the assistant is.",
            "When a user asks whether their resume fits companies, jobs, roles, or postings, choose resume_fit_analysis.",
            "When a user asks to review, improve, tailor, rewrite, or strengthen their resume, choose resume_tailoring.",
            "When a user asks for jobs, companies, roles, ranking, top matches, or recommendations, choose job_search or job_compare and set needs_retrieval=true.",
            "Use clarification_needed only when the user is trying to do a job-search, resume, or application workflow but the missing context prevents a useful answer.",
            "If assistant_context shows the assistant just asked a clarification question, treat the current user_question as the answer to it: combine both turns to pick a concrete intent. Never return clarification_needed twice in a row.",
            "Prefer answering over asking: when torn between clarification_needed and a read-only intent like job_search or resume_fit_analysis, choose the read-only intent with needs_retrieval=true.",
        ],
        "required_entity_keys": ROUTER_ENTITY_FIELDS,
        "user_question": request.question,
        "user_history": [
            {"role": message.role, "content": message.content[:1000]}
            for message in request.messages[-8:]
            if message.role == "user" and message.content.strip()
        ],
        "assistant_context": [
            {"role": message.role, "content": message.content[:1000]}
            for message in request.messages[-4:]
            if message.role == "assistant" and message.content.strip()
        ],
        "assistant_context_rule": (
            "Assistant context can help resolve references, but must never create an application action "
            "or missing field by itself."
        ),
        "conversation_history": [
            {"role": message.role, "content": message.content[:1000]}
            for message in request.messages[-8:]
            if message.role == "user" and message.content.strip()
        ],
        "has_resume_context": bool(request.resume_context.strip()),
        "current_job_ids": request.job_ids or [],
        "job_hints": [
            {
                "id": job.id,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "level": job.level,
                "skills": job.required_skills[:5],
            }
            for job in current_jobs[:8]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def route_with_rules(request: ChatRequest, jobs: list[JobPosting]) -> IntentRoute:
    question = normalize_text(request.question)
    context_text = route_context_text(request)
    entities = extract_entities(request, jobs)

    if is_nonsense(question):
        return make_route("nonsense", 0.86, entities, "User message has no actionable intent.")
    if is_small_talk(question):
        return make_route("small_talk", 0.86, entities, "User is making brief social chat.")
    if is_resume_fit_request(question):
        return make_route(
            "resume_fit_analysis",
            0.93,
            entities,
            "Current user request asks which companies, jobs, or roles fit the resume.",
        )
    if is_resume_review_request(question):
        route = make_route(
            "resume_tailoring",
            0.93,
            entities,
            "Current user request asks for resume review, evaluation, or improvement.",
        )
        if not resume_review_needs_job_context(question):
            focus = list(entities.get("focus", []))
            if "resume_audit" not in focus:
                focus.append("resume_audit")
            return route.model_copy(update={"needs_retrieval": False, "entities": {**entities, "focus": focus}})
        return route
    if is_skill_gap_request(question):
        return make_route(
            "skill_gap_analysis",
            0.88,
            entities,
            "Current user request asks for missing skills or a skill gap analysis.",
        )
    if is_job_compare_request(question):
        return make_route(
            "job_compare",
            0.84,
            entities,
            "Current user request asks to compare or rank jobs.",
        )
    if is_job_detail_request(question):
        return make_route(
            "job_detail_lookup",
            0.84,
            entities,
            "Current user request asks for details about a specific job.",
        )
    if is_platform_help(question):
        return make_route("platform_help", 0.9, entities, "User is asking what the system can do.")
    if is_unsupported(question):
        return make_route("unsupported", 0.88, entities, "Request is outside the job-search product scope.")
    if is_application_status_query(question):
        return make_route("application_status_query", 0.86, entities, "User is asking about tracker status.")
    if is_application_action(question):
        if not resolve_job_reference(make_route("application_action", 0.8, entities, ""), request, jobs):
            missing = ["job_id"]
        else:
            missing = []
        return make_route(
            "application_action",
            0.88,
            entities,
            "User wants to save, track, or update an application.",
            missing_fields=missing,
        )

    if is_job_search_request(question):
        return make_route(
            "job_search",
            0.82,
            entities,
            "Current user request asks for job search or job recommendations.",
        )
    if is_job_domain_request(context_text):
        return make_route(
            "job_search",
            0.62,
            entities,
            "LLM routing is disabled; using the generic retrieval workflow for an in-domain request.",
        )
    if not meaningful_text(question):
        return make_route(
            "nonsense",
            0.72,
            entities,
            "User request is too short or vague to route.",
        )

    return make_route(
        "off_topic",
        0.65,
        entities,
        "LLM routing is disabled and the request does not look like a job-search workflow.",
    )


def is_trusted_rule_route(route: IntentRoute) -> bool:
    if route.intent in {
        "resume_fit_analysis",
        "resume_tailoring",
        "skill_gap_analysis",
        "job_compare",
        "job_detail_lookup",
        "application_action",
        "application_status_query",
        "platform_help",
        "small_talk",
        "off_topic",
        "nonsense",
        "unsupported",
    }:
        return route.confidence >= 0.8
    if route.intent == "job_search":
        return route.confidence >= 0.8
    return False


# Missing fields that amount to "what do you want?" — if that is ALL the LLM
# would ask about a clearly job-domain question, retrieval beats interrogation.
# Specific gaps (job_id, company, stage, ...) still justify a clarification.
GENERIC_MISSING_FIELDS = {"desired_action", "query"}


def prefer_retrieval_over_generic_clarification(route: IntentRoute, request: ChatRequest) -> IntentRoute:
    if route.intent != "clarification_needed":
        return route
    if set(route.missing_fields) - GENERIC_MISSING_FIELDS:
        return route
    if not is_job_domain_request(route_context_text(request)):
        return route
    return make_route(
        "job_search",
        max(route.confidence, 0.6),
        route.entities,
        "Question is job-domain and the clarification would only ask for a generic goal; answering with retrieval instead.",
        source="system",
    )


def rescue_llm_route(llm_route: IntentRoute, rule_route: IntentRoute) -> IntentRoute:
    if not is_trusted_rule_route(rule_route):
        return llm_route
    if llm_route.intent == "clarification_needed":
        return rule_route.model_copy(
            update={
                "reason": f"{rule_route.reason} Overrode an over-cautious LLM clarification.",
            }
        )
    if rule_route.intent in {"resume_fit_analysis", "resume_tailoring"} and llm_route.intent == "application_action":
        return rule_route.model_copy(
            update={
                "reason": f"{rule_route.reason} Ignored tracker intent inferred from history.",
            }
        )
    return llm_route


# Read-only intents where a low-confidence guess is safe to answer via
# retrieval: worst case is a less-focused answer, never a wrong side effect.
RETRIEVAL_SAFE_INTENTS = {
    "job_search",
    "job_detail_lookup",
    "job_compare",
    "resume_fit_analysis",
    "resume_tailoring",
    "skill_gap_analysis",
}


def normalize_route(payload: dict[str, Any], source: str) -> IntentRoute:
    intent = str(payload.get("intent", "clarification_needed"))
    if intent not in SUPPORTED_INTENTS:
        intent = "clarification_needed"

    defaults = INTENT_DEFAULTS[intent]
    raw_entities = payload.get("entities") if isinstance(payload.get("entities"), dict) else {}
    entities = normalize_entities(raw_entities)
    route = IntentRoute(
        intent=intent,  # type: ignore[arg-type]
        confidence=clamp_confidence(payload.get("confidence", 0.5)),
        needs_retrieval=bool(payload.get("needs_retrieval", defaults[0])),
        needs_action=bool(payload.get("needs_action", defaults[1])),
        entities=entities,
        missing_fields=[str(item) for item in payload.get("missing_fields", []) if str(item).strip()],
        reason=str(payload.get("reason", "")),
        source=source,
    )
    if route.confidence < 0.55 and route.intent not in {"unsupported", "clarification_needed", "small_talk", "off_topic", "nonsense"}:
        # Retrieval is cheap and a decent answer beats an interrogation: for
        # read-only job intents, fall back to generic retrieval. Clarify only
        # when acting on a guess could do the wrong thing (e.g. tracker writes).
        if route.intent in RETRIEVAL_SAFE_INTENTS:
            return route.model_copy(
                update={
                    "intent": "job_search",
                    "needs_retrieval": True,
                    "needs_action": False,
                    "missing_fields": [],
                    "reason": f"Router confidence was low for {route.intent}; answering with generic retrieval instead of asking a clarification.",
                }
            )
        return route.model_copy(
            update={
                "intent": "clarification_needed",
                "needs_retrieval": False,
                "needs_action": False,
                "missing_fields": sorted(set(route.missing_fields + ["desired_action"])),
                "reason": f"Router confidence was low for {route.intent}; asking a clarification first.",
            }
        )
    return route


def normalize_entities(raw: dict[str, Any]) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    for field in ROUTER_ENTITY_FIELDS:
        value = raw.get(field)
        if isinstance(value, str):
            value = value.strip()
            if value:
                entities[field] = value
        elif isinstance(value, list):
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            if cleaned:
                entities[field] = cleaned
        elif value is not None:
            entities[field] = value
    return entities


def make_route(
    intent: str,
    confidence: float,
    entities: dict[str, Any],
    reason: str,
    missing_fields: list[str] | None = None,
    source: str = "rules",
) -> IntentRoute:
    defaults = INTENT_DEFAULTS[intent]
    return IntentRoute(
        intent=intent,  # type: ignore[arg-type]
        confidence=confidence,
        needs_retrieval=defaults[0],
        needs_action=defaults[1],
        entities=entities,
        missing_fields=missing_fields or [],
        reason=reason,
        source=source,
    )


def extract_entities(request: ChatRequest, jobs: list[JobPosting]) -> dict[str, Any]:
    text = route_context_text(request)
    question = normalize_text(request.question)
    entities: dict[str, Any] = {"query": request.question.strip()}

    if request.job_ids:
        entities["job_id"] = request.job_ids[0]
        if has_current_reference(question) or has_current_reference(text):
            entities["job_reference"] = "current_selected_job"

    for location in ["Seattle", "Bellevue", "Redmond", "Kirkland", "Remote"]:
        if location.lower() in text:
            entities["location"] = location
            break

    if re.search(r"\b(new grad|new-grad|entry|junior)\b", text):
        entities["audience"] = "new-grad"
    elif re.search(r"\b(intern|internship)\b", text):
        entities["audience"] = "intern"

    limit_match = re.search(r"\b(?:top|first|limit)\s+([1-9]|10)\b", text)
    if limit_match:
        entities["limit"] = int(limit_match.group(1))

    stage = extract_stage(question)
    if stage:
        entities["stage"] = stage
    action = extract_action(question, stage)
    if action:
        entities["action"] = action

    job = resolve_job_from_text(text, jobs)
    if job:
        entities.setdefault("job_id", job.id)
        entities["company"] = job.company
        entities["job_title"] = job.title

    return entities


def resolve_job_reference(route: IntentRoute, request: ChatRequest, jobs: list[JobPosting]) -> JobPosting | None:
    job_by_id = {job.id: job for job in jobs}
    job_id = route.entities.get("job_id")
    if isinstance(job_id, str) and job_id in job_by_id:
        return job_by_id[job_id]

    if request.job_ids:
        for selected_id in request.job_ids:
            if selected_id in job_by_id and (has_current_reference(route_context_text(request)) or len(request.job_ids) == 1):
                return job_by_id[selected_id]

    company = str(route.entities.get("company", "")).strip().lower()
    title = str(route.entities.get("job_title", "")).strip().lower()
    query = " ".join(
        str(route.entities.get(key, ""))
        for key in ["company", "job_title", "query", "job_reference"]
    )
    text = normalize_text(f"{request.question} {query}")

    scored: list[tuple[int, JobPosting]] = []
    for job in jobs:
        score = 0
        if company and company in job.company.lower():
            score += 8
        if title:
            score += token_overlap(title, job.title.lower()) * 2
        if job.company.lower() in text:
            score += 6
        score += token_overlap(text, f"{job.company} {job.title}".lower())
        if score:
            scored.append((score, job))

    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored else None


def resolve_job_from_text(text: str, jobs: list[JobPosting]) -> JobPosting | None:
    scored: list[tuple[int, JobPosting]] = []
    for job in jobs:
        score = 0
        if job.company.lower() in text:
            score += 6
        score += token_overlap(text, job.title.lower())
        if score >= 2:
            scored.append((score, job))

    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored else None


def route_context_text(request: ChatRequest) -> str:
    history = " ".join(
        message.content
        for message in request.messages[-6:]
        if message.role == "user" and message.content.strip()
    )
    return normalize_text(f"{history} {request.question}")


def is_platform_help(text: str) -> bool:
    if "mcp" in text or "tools/list" in text or "tools/call" in text:
        return True
    return any(token in text for token in ["what can you do", "available tools", "capabilities", "你能做", "能帮我", "工具", "能力"])


def is_unsupported(text: str) -> bool:
    unsupported_tokens = [
        "weather",
        "flight",
        "hotel",
        "stock",
        "crypto",
        "medical",
        "legal advice",
        "tax advice",
        "天气",
        "机票",
        "酒店",
        "股票",
        "加密货币",
        "医疗",
        "法律",
    ]
    return any(token in text for token in unsupported_tokens)


def is_application_action(text: str) -> bool:
    return any(
        token in text
        for token in [
            "save",
            "saved",
            "track",
            "tracker",
            "mark",
            "applied",
            "apply",
            "follow up",
            "follow-up",
            "oa",
            "interview",
            "offer",
            "rejected",
            "保存",
            "加入",
            "标记",
            "申请",
            "投递",
            "跟进",
            "面试",
        ]
    )


def is_application_status_query(text: str) -> bool:
    # Tokens must be query-shaped ("have i applied", "申请进度") — a bare "我保存"
    # also matches save REQUESTS like "帮我保存这个岗位" and misroutes them here.
    return any(
        token in text
        for token in [
            "my saved jobs",
            "my applications",
            "application status",
            "application progress",
            "applied to so far",
            "have i applied",
            "what jobs have i applied",
            "follow up this week",
            "我保存的",
            "保存了哪些",
            "申请了哪些",
            "投了哪些",
            "我的申请",
            "申请状态",
            "申请进度",
            "投递进度",
            "申请记录",
        ]
    )


def is_resume_review_request(text: str) -> bool:
    has_resume = any(token in text for token in ["resume", "cv", "简历"])
    if not has_resume:
        return False
    return any(
        token in text
        for token in [
            "review",
            "look at",
            "look over",
            "check",
            "evaluate",
            "assess",
            "feedback",
            "improve",
            "optimize",
            "polish",
            "rewrite",
            "tailor",
            "strengthen",
            "看一下",
            "看下",
            "帮我看",
            "评估",
            "评价",
            "检查",
            "建议",
            "优化",
            "润色",
            "修改",
            "改一下",
            "可以吗",
        ]
    )


def is_resume_fit_request(text: str) -> bool:
    has_resume = any(token in text for token in ["resume", "cv", "简历"])
    fit_tokens = ["fit", "match", "matches", "suitable", "适合", "匹配", "符合"]
    target_tokens = [
        "company",
        "companies",
        "job",
        "jobs",
        "role",
        "roles",
        "position",
        "positions",
        "哪个公司",
        "哪些公司",
        "什么公司",
        "哪个岗位",
        "哪些岗位",
        "什么岗位",
        "职位",
        "岗位",
        "公司",
    ]
    return has_resume and any(token in text for token in fit_tokens) and any(token in text for token in target_tokens)


def resume_review_needs_job_context(text: str) -> bool:
    return any(
        token in text
        for token in [
            "for ",
            "target",
            "job",
            "jobs",
            "role",
            "roles",
            "position",
            "positions",
            "company",
            "companies",
            "backend",
            "frontend",
            "full stack",
            "cloud",
            "data",
            "针对",
            "岗位",
            "职位",
            "公司",
            "后端",
            "前端",
            "全栈",
            "云",
            "数据",
            "机器学习",
            "人工智能",
        ]
    )


def is_skill_gap_request(text: str) -> bool:
    return any(token in text for token in ["skill gap", "missing skill", "skills gap", "缺什么", "缺哪些", "技能差距", "补什么"])


def is_job_compare_request(text: str) -> bool:
    return any(token in text for token in ["compare", "rank", "which is better", "best fit", "对比", "比较", "排名", "哪个更好", "最适合"])


def is_job_detail_request(text: str) -> bool:
    return any(token in text for token in ["detail", "details", "description", "requirements", "source link", "详情", "要求", "链接", "jd"])


def is_job_search_request(text: str) -> bool:
    return any(
        token in text
        for token in [
            "find job",
            "find jobs",
            "search job",
            "search jobs",
            "recommend",
            "recommendation",
            "job matches",
            "找岗位",
            "找职位",
            "推荐岗位",
            "推荐职位",
            "推荐工作",
            "找工",
            "找实习",
            "找工作",
        ]
    )


def is_small_talk(text: str) -> bool:
    compact = re.sub(r"[\s!?.。！？,，]+", "", text)
    return compact in {"hi", "hello", "hey", "thanks", "thankyou", "你好", "谢谢", "嗨", "哈喽"} or any(
        phrase in text
        for phrase in [
            "who are you",
            "what are you",
            "你是谁",
            "你是什么",
        ]
    )


def is_nonsense(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return True
    if re.fullmatch(r"[\W_]+", compact):
        return True
    if len(compact) >= 6 and len(set(compact)) <= 2:
        return True
    return False


def is_job_domain_request(text: str) -> bool:
    domain_tokens = [
        "job",
        "jobs",
        "role",
        "roles",
        "position",
        "positions",
        "company",
        "companies",
        "resume",
        "cv",
        "application",
        "tracker",
        "skill",
        "interview",
        "offer",
        "sde",
        "engineer",
        "seattle",
        "bellevue",
        "redmond",
        "kirkland",
        "remote",
        "岗位",
        "职位",
        "公司",
        "简历",
        "申请",
        "投递",
        "面试",
        "技能",
        "找工",
        "实习",
        "intern",
        "全职",
    ]
    return any(token in text for token in domain_tokens)


def extract_stage(text: str) -> str | None:
    if any(token in text for token in ["applied", "apply", "申请", "投递"]):
        return "applied"
    if "oa" in text:
        return "oa"
    if any(token in text for token in ["interview", "面试"]):
        return "interview"
    if any(token in text for token in ["rejected", "拒"]):
        return "rejected"
    if any(token in text for token in ["offer", "录用"]):
        return "offer"
    if any(token in text for token in ["save", "saved", "track", "tracker", "保存", "加入"]):
        return "saved"
    return None


def extract_action(text: str, stage: str | None) -> str | None:
    if any(token in text for token in ["follow up", "follow-up", "跟进"]):
        return "set_follow_up"
    if stage and stage != "saved":
        return "update_stage"
    if stage == "saved":
        return "save"
    return None


def has_current_reference(text: str) -> bool:
    return any(token in text for token in ["this", "that", "current", "selected", "top one", "this one", "这个", "那个", "当前", "上面"])


def token_overlap(left: str, right: str) -> int:
    left_tokens = set(re.findall(r"[a-z0-9+#.]+", left.lower()))
    right_tokens = set(re.findall(r"[a-z0-9+#.]+", right.lower()))
    ignored = {"job", "jobs", "role", "roles", "engineer", "software", "developer", "sde"}
    return len((left_tokens - ignored) & (right_tokens - ignored))


def meaningful_text(text: str) -> bool:
    tokens = re.findall(r"[a-z0-9+#.]+|[\u4e00-\u9fff]", text.lower())
    # CJK packs more meaning per character; the Latin 8-char floor would
    # reject complete requests like "\u6211\u60f3\u627e\u5b9e\u4e60".
    min_length = 4 if re.search(r"[\u4e00-\u9fff]", text) else 8
    return len(text.strip()) >= min_length and len(tokens) >= 2


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def clamp_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in router response.")
    return stripped[start : end + 1]
