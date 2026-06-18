import { fallbackJobs } from "./sampleData";
import type { ChatResponse, CompanySource, JobFeedResponse, JobPosting, MatchResult, ResumeExtractResult } from "./types";

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

export async function extractResume(file: File): Promise<ResumeExtractResult> {
  const formData = new FormData();
  formData.set("file", file);

  const response = await fetch(`${API_URL}/resume/extract`, {
    method: "POST",
    body: formData
  });

  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, "Resume upload failed"));
  }

  const data = (await response.json()) as Record<string, unknown>;
  return {
    filename: String(data.filename ?? file.name),
    content: String(data.content ?? "")
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
  top_k?: number;
  use_llm?: boolean;
}): Promise<ChatResponse> {
  const response = await fetch(`${API_URL}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question: payload.question,
      resume_context: payload.resume_context ?? "",
      conversation_id: payload.conversation_id ?? "web",
      top_k: payload.top_k ?? 5,
      use_llm: payload.use_llm ?? true
    })
  });

  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, "Chat request failed"));
  }

  return (await response.json()) as ChatResponse;
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
