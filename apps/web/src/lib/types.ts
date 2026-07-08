export type JobPosting = {
  id: string;
  company: string;
  title: string;
  location: string;
  source: string;
  sourceUrl?: string;
  level: string;
  workMode: string;
  description: string;
  requiredSkills: string[];
  niceToHaveSkills: string[];
};

export type JobFeedResponse = {
  items: JobPosting[];
  next_cursor: number | null;
  total: number;
};

export type CompanySource = {
  id: string;
  company: string;
  careerUrl: string;
  atsType: string;
  enabled: boolean;
  priority: number;
  crawlIntervalMinutes: number;
  extractionStrategy: string;
  probeStatus: string;
  probeNotes: string;
  notes: string;
};

export type CompanyDiscoveryRunResponse = {
  companies_seen: number;
  sources_found: number;
  sources_added: number;
  sources_updated: number;
  sources_unchanged: number;
  search_provider: string;
  api_needed?: string | null;
  errors: string[];
  sources: CompanySource[];
};

export type MatchBreakdown = {
  semantic_fit: number;
  required_skills: number;
  nice_to_have_skills: number;
  level_fit: number;
  location_fit: number;
};

export type MatchResult = {
  job: JobPosting;
  score: number;
  evaluation_source: "local" | "openai";
  matched_skills: string[];
  missing_skills: string[];
  risks: string[];
  bullet_suggestions: string[];
  ai_summary?: string | null;
  ai_strengths: string[];
  interview_focus: string[];
  breakdown: MatchBreakdown;
};

export type ResumeExtractResult = {
  filename: string;
  content: string;
  resume_id?: string | null;
  saved?: boolean;
};

export type User = {
  id: string;
  email: string;
  name: string;
};

export type AuthResponse = {
  token: string;
  user: User;
};

export type SavedResume = {
  id: string;
  title: string;
  filename: string;
  content: string;
  active: boolean;
  created_at: string;
  updated_at: string;
};

export type ChatRetrievedJob = {
  id: string;
  company: string;
  title: string;
  location: string;
  level: string;
  work_mode: string;
  score: number;
  reason: string;
};

export type ChatHistoryMessage = {
  role: "assistant" | "user";
  content: string;
};

export type IntentRoute = {
  intent: string;
  confidence: number;
  needs_retrieval: boolean;
  needs_action: boolean;
  entities: Record<string, unknown>;
  missing_fields: string[];
  reason: string;
  source: string;
};

export type ChatResponse = {
  answer: string;
  cache_status: string;
  cache_similarity?: number | null;
  retrieval_source: string;
  llm_used: boolean;
  prompt_template: string;
  retrieved_jobs: ChatRetrievedJob[];
  intent_route?: IntentRoute | null;
  workflow?: CopilotWorkflow | null;
  warnings: string[];
};

export type CopilotToolCall = {
  name: string;
  title: string;
  status: string;
  summary: string;
};

export type WorkflowJobCard = {
  job_id: string;
  company: string;
  title: string;
  location: string;
  level: string;
  work_mode: string;
  score: number;
  fit_summary: string;
  matched_skills: string[];
  missing_skills: string[];
};

export type SkillMatrixRow = {
  skill: string;
  status: string;
  evidence: string;
  jobs: string[];
};

export type ResumeChecklistItem = {
  title: string;
  priority: string;
  detail: string;
  related_skills: string[];
};

export type WorkflowAction = {
  label: string;
  intent: string;
  job_id?: string | null;
  payload: Record<string, string>;
};

export type CopilotWorkflow = {
  title: string;
  tool_calls: CopilotToolCall[];
  job_cards: WorkflowJobCard[];
  skill_matrix: SkillMatrixRow[];
  resume_checklist: ResumeChecklistItem[];
  actions: WorkflowAction[];
};

export type ApplicationStage = "saved" | "applied" | "oa" | "interview" | "rejected" | "offer";

export type ApplicationRecord = {
  id: string;
  jobId: string;
  stage: ApplicationStage;
  notes: string;
  followUpOn?: string;
};
