import { fallbackJobs } from "./sampleData";
import type {
  AuthResponse,
  ChatResponse,
  ChatHistoryMessage,
  CompanySource,
  JobFeedResponse,
  JobPosting,
  MatchResult,
  ResumeExtractResult,
  SavedResume,
  User
} from "./types";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export function normalizeJob(row: Record<string, unknown>): JobPosting {
  return {
    id: String(row.id),
    company: String(row.company),
    title: String(row.title),
    location: String(row.location),
    source: String(row.source),
    sourceUrl: String(row.sourceUrl ?? row.source_url ?? ""),
    level: String(row.level),
    workMode: String(row.workMode ?? row.work_mode),
    description: String(row.description),
    requiredSkills: (row.requiredSkills ?? row.required_skills ?? []) as string[],
    niceToHaveSkills: (row.niceToHaveSkills ?? row.nice_to_have_skills ?? []) as string[]
  };
}

export function normalizeCompanySource(row: Record<string, unknown>): CompanySource {
  return {
    id: String(row.id),
    company: String(row.company),
    careerUrl: String(row.careerUrl ?? row.career_url ?? ""),
    atsType: String(row.atsType ?? row.ats_type ?? "generic_html"),
    enabled: Boolean(row.enabled),
    priority: Number(row.priority ?? 3),
    crawlIntervalMinutes: Number(row.crawlIntervalMinutes ?? row.crawl_interval_minutes ?? 360),
    extractionStrategy: String(row.extractionStrategy ?? row.extraction_strategy ?? "unprobed"),
    probeStatus: String(row.probeStatus ?? row.probe_status ?? "unprobed"),
    probeNotes: String(row.probeNotes ?? row.probe_notes ?? ""),
    notes: String(row.notes ?? "")
  };
}

export function normalizeMatch(row: Record<string, unknown>): MatchResult {
  const breakdown = (row.breakdown ?? {}) as Record<string, number>;
  return {
    job: normalizeJob(row.job as Record<string, unknown>),
    score: Number(row.score),
    evaluation_source: row.evaluation_source === "openai" ? "openai" : "local",
    matched_skills: (row.matched_skills ?? []) as string[],
    missing_skills: (row.missing_skills ?? []) as string[],
    risks: (row.risks ?? []) as string[],
    bullet_suggestions: (row.bullet_suggestions ?? []) as string[],
    ai_summary: (row.ai_summary as string | null) ?? null,
    ai_strengths: (row.ai_strengths ?? []) as string[],
    interview_focus: (row.interview_focus ?? []) as string[],
    breakdown: {
      semantic_fit: Number(breakdown.semantic_fit ?? 0),
      required_skills: Number(breakdown.required_skills ?? 0),
      nice_to_have_skills: Number(breakdown.nice_to_have_skills ?? 0),
      level_fit: Number(breakdown.level_fit ?? 0),
      location_fit: Number(breakdown.location_fit ?? 0)
    }
  };
}

export function fallbackJobById(jobId: string): JobPosting | undefined {
  return fallbackJobs.find((job) => job.id === jobId);
}

export async function registerAccount(payload: {
  email: string;
  password: string;
  name?: string;
}): Promise<AuthResponse> {
  return authRequest("/auth/register", payload);
}

export async function loginAccount(payload: {
  email: string;
  password: string;
}): Promise<AuthResponse> {
  return authRequest("/auth/login", payload);
}

export async function fetchCurrentUser(token: string): Promise<User> {
  const response = await fetch(`${API_URL}/auth/me`, {
    headers: authHeaders(token)
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, "Session check failed"));
  }
  return (await response.json()) as User;
}

export async function fetchSavedResumes(token: string): Promise<SavedResume[]> {
  const response = await fetch(`${API_URL}/resumes`, {
    headers: authHeaders(token)
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, "Resume request failed"));
  }
  return (await response.json()) as SavedResume[];
}

async function authRequest(path: string, payload: { email: string; password: string; name?: string }) {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, "Authentication failed"));
  }
  return (await response.json()) as AuthResponse;
}

export async function fetchJobFeed(payload: {
  cursor?: number;
  limit?: number;
  query?: string;
  location?: string;
  audience?: string;
}): Promise<JobFeedResponse> {
  const params = new URLSearchParams();
  params.set("cursor", String(payload.cursor ?? 0));
  params.set("limit", String(payload.limit ?? 20));
  if (payload.query) params.set("query", payload.query);
  if (payload.location) params.set("location", payload.location);
  if (payload.audience) params.set("audience", payload.audience);

  const response = await fetch(`${API_URL}/jobs/feed?${params.toString()}`);
  if (!response.ok) {
    throw new Error(`Job feed request failed with ${response.status}`);
  }

  const data = (await response.json()) as {
    items: Record<string, unknown>[];
    next_cursor: number | null;
    total: number;
  };

  return {
    items: data.items.map(normalizeJob),
    next_cursor: data.next_cursor,
    total: Number(data.total)
  };
}

export async function fetchJobById(jobId: string): Promise<JobPosting> {
  const response = await fetch(`${API_URL}/jobs/${encodeURIComponent(jobId)}`);
  if (!response.ok) {
    throw new Error(`Job request failed with ${response.status}`);
  }
  return normalizeJob((await response.json()) as Record<string, unknown>);
}

export async function extractResume(file: File, token?: string): Promise<ResumeExtractResult> {
  const formData = new FormData();
  formData.set("file", file);

  const response = await fetch(`${API_URL}/resume/extract`, {
    method: "POST",
    headers: authHeaders(token),
    body: formData
  });

  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, "Resume upload failed"));
  }

  const data = (await response.json()) as Record<string, unknown>;
  return {
    filename: String(data.filename ?? file.name),
    content: String(data.content ?? ""),
    resume_id: typeof data.resume_id === "string" ? data.resume_id : null,
    saved: Boolean(data.saved)
  };
}

export async function matchResume(payload: {
  content: string;
  target_role?: string;
  job_ids?: string[];
  use_ai?: boolean;
}): Promise<MatchResult[]> {
  const response = await fetch(`${API_URL}/match`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      resume: {
        content: payload.content,
        target_role: payload.target_role ?? "Seattle SDE"
      },
      job_ids: payload.job_ids,
      use_ai: payload.use_ai ?? false
    })
  });

  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, "Resume match failed"));
  }

  const data = (await response.json()) as Record<string, unknown>[];
  return data.map(normalizeMatch);
}

export async function askJobCopilot(payload: {
  question: string;
  resume_context?: string;
  conversation_id?: string;
  messages?: ChatHistoryMessage[];
  job_ids?: string[];
  top_k?: number;
  use_llm?: boolean;
  token?: string;
}): Promise<ChatResponse> {
  const response = await fetch(`${API_URL}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(payload.token)
    },
    body: JSON.stringify({
      question: payload.question,
      resume_context: payload.resume_context ?? "",
      conversation_id: payload.conversation_id ?? "web",
      messages: payload.messages ?? [],
      job_ids: payload.job_ids,
      top_k: payload.top_k ?? 5,
      use_llm: payload.use_llm ?? true
    })
  });

  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, "Chat request failed"));
  }

  return (await response.json()) as ChatResponse;
}

export async function streamJobCopilot(
  payload: {
    question: string;
    resume_context?: string;
    conversation_id?: string;
    messages?: ChatHistoryMessage[];
    job_ids?: string[];
    top_k?: number;
    use_llm?: boolean;
    token?: string;
  },
  handlers: {
    onChunk?: (content: string) => void;
    onDone?: (response: ChatResponse) => void;
  } = {}
): Promise<ChatResponse> {
  const response = await fetch(`${API_URL}/chat/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(payload.token)
    },
    body: JSON.stringify({
      question: payload.question,
      resume_context: payload.resume_context ?? "",
      conversation_id: payload.conversation_id ?? "web",
      messages: payload.messages ?? [],
      job_ids: payload.job_ids,
      top_k: payload.top_k ?? 5,
      use_llm: payload.use_llm ?? true
    })
  });

  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, "Chat request failed"));
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("Chat request failed: streaming is not available in this browser.");
  }

  const decoder = new TextDecoder();
  let buffer = "";
  let finalResponse: ChatResponse | null = null;

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      handleStreamEvent(block, handlers, (responsePayload) => {
        finalResponse = responsePayload;
      });
      boundary = buffer.indexOf("\n\n");
    }

    if (done) break;
  }

  if (buffer.trim()) {
    handleStreamEvent(buffer, handlers, (responsePayload) => {
      finalResponse = responsePayload;
    });
  }

  if (!finalResponse) {
    throw new Error("Chat request ended before the final response arrived.");
  }

  return finalResponse;
}

function handleStreamEvent(
  block: string,
  handlers: {
    onChunk?: (content: string) => void;
    onDone?: (response: ChatResponse) => void;
  },
  setFinalResponse: (response: ChatResponse) => void
) {
  const event = parseServerSentEvent(block);
  if (!event) return;

  const payload = JSON.parse(event.data) as Record<string, unknown>;
  if (event.event === "chunk") {
    if (typeof payload.content === "string") {
      handlers.onChunk?.(payload.content);
    }
    return;
  }

  if (event.event === "done") {
    const responsePayload = payload as ChatResponse;
    handlers.onDone?.(responsePayload);
    setFinalResponse(responsePayload);
  }
}

function parseServerSentEvent(block: string): { event: string; data: string } | null {
  const lines = block.split(/\r?\n/);
  let event = "message";
  const data: string[] = [];

  for (const line of lines) {
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      data.push(line.slice("data:".length).trimStart());
    }
  }

  if (!data.length) return null;
  return { event, data: data.join("\n") };
}

function authHeaders(token?: string): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function apiErrorMessage(response: Response, fallback: string) {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (Array.isArray(body.detail)) {
      const details = body.detail
        .map((item) => {
          if (item && typeof item === "object" && "msg" in item) {
            return String((item as { msg: unknown }).msg);
          }
          return String(item);
        })
        .join("; ");
      return `${fallback}: ${details}`;
    }
    if (typeof body.detail === "string") {
      return `${fallback}: ${body.detail}`;
    }
  } catch {
    // Keep the generic status message when the API does not return JSON.
  }
  return `${fallback} with ${response.status}`;
}
