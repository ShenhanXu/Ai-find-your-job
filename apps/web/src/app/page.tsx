"use client";

import {
  ArrowRight,
  AlertCircle,
  Bot,
  BriefcaseBusiness,
  Building2,
  CheckCircle2,
  Database,
  ExternalLink,
  FileText,
  Filter,
  GraduationCap,
  MapPin,
  Search,
  SendHorizontal,
  Settings2,
  Sparkles,
  Trash2,
  Upload,
  UserRound
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { askJobCopilot, extractResume, fetchJobFeed, matchResume } from "@/lib/api";
import type { ChatResponse, JobPosting, MatchResult } from "@/lib/types";

type AudienceFilter = "" | "new-grad" | "intern";
type ChatMessage = {
  id: string;
  role: "assistant" | "user";
  content: string;
  response?: ChatResponse;
  status?: string;
};

const PAGE_SIZE = 12;
const CHAT_STORAGE_KEY = "ai-job-match:home-chat:v1";
const DEFAULT_CHAT_MESSAGES: ChatMessage[] = [
  {
    id: "welcome",
    role: "assistant",
    content:
      "Tell me what kind of role you want. I can retrieve a small set of matching jobs first, then use that context for ranking or resume tailoring."
  }
];

type StoredChatState = {
  messages: ChatMessage[];
  resumeContext: string;
};

export default function Home() {
  const [query, setQuery] = useState("");
  const [location, setLocation] = useState("All");
  const [audience, setAudience] = useState<AudienceFilter>("");
  const [jobs, setJobs] = useState<JobPosting[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(0);
  const [totalJobs, setTotalJobs] = useState(0);
  const [feedLoading, setFeedLoading] = useState(false);
  const [feedError, setFeedError] = useState("");
  const [resumeContext, setResumeContext] = useState("");
  const [chatQuestion, setChatQuestion] = useState("");
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>(DEFAULT_CHAT_MESSAGES);
  const [chatLoading, setChatLoading] = useState(false);
  const [chatStorageReady, setChatStorageReady] = useState(false);
  const [showContext, setShowContext] = useState(false);
  const [resumeFileName, setResumeFileName] = useState("");
  const [resumeStatus, setResumeStatus] = useState("");
  const [resumeError, setResumeError] = useState("");
  const [resumeLoading, setResumeLoading] = useState(false);
  const [matchResults, setMatchResults] = useState<MatchResult[]>([]);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const chatScrollRef = useRef<HTMLDivElement | null>(null);

  const hasMore = nextCursor !== null;

  useEffect(() => {
    const storedChat = readStoredChatState();
    if (storedChat) {
      setChatMessages(storedChat.messages);
      setResumeContext(storedChat.resumeContext);
    }
    setChatStorageReady(true);
  }, []);

  useEffect(() => {
    if (!chatStorageReady) return;
    writeStoredChatState({ messages: chatMessages, resumeContext });
  }, [chatMessages, chatStorageReady, resumeContext]);

  useEffect(() => {
    let cancelled = false;
    setFeedLoading(true);
    setFeedError("");

    fetchJobFeed({
      cursor: 0,
      limit: PAGE_SIZE,
      query,
      location: location === "All" ? "" : location,
      audience
    })
      .then((response) => {
        if (cancelled) return;
        setJobs(response.items);
        setNextCursor(response.next_cursor);
        setTotalJobs(response.total);
      })
      .catch((error) => {
        if (cancelled) return;
        setJobs([]);
        setNextCursor(null);
        setTotalJobs(0);
        setFeedError(error instanceof Error ? error.message : "Job feed request failed");
      })
      .finally(() => {
        if (!cancelled) setFeedLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [query, location, audience]);

  const loadMoreJobs = useCallback(async () => {
    if (feedLoading || nextCursor === null) return;

    setFeedLoading(true);
    setFeedError("");
    try {
      const response = await fetchJobFeed({
        cursor: nextCursor,
        limit: PAGE_SIZE,
        query,
        location: location === "All" ? "" : location,
        audience
      });
      setJobs((current) => [...current, ...response.items]);
      setNextCursor(response.next_cursor);
      setTotalJobs(response.total);
    } catch (error) {
      setFeedError(error instanceof Error ? error.message : "Job feed request failed");
    } finally {
      setFeedLoading(false);
    }
  }, [audience, feedLoading, location, nextCursor, query]);

  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting) && hasMore) {
          loadMoreJobs();
        }
      },
      { rootMargin: "360px 0px" }
    );

    observer.observe(node);
    return () => observer.disconnect();
  }, [hasMore, loadMoreJobs]);

  useEffect(() => {
    chatScrollRef.current?.scrollTo({
      top: chatScrollRef.current.scrollHeight,
      behavior: "smooth"
    });
  }, [chatMessages, chatLoading]);

  function resetFilters() {
    setQuery("");
    setLocation("All");
    setAudience("");
  }

  function resetChatHistory() {
    setChatMessages(DEFAULT_CHAT_MESSAGES);
    setResumeContext("");
    setChatQuestion("");

    if (typeof window === "undefined") return;
    try {
      window.localStorage.removeItem(CHAT_STORAGE_KEY);
    } catch {
      // Chat has already been reset in memory, so storage cleanup can fail silently.
    }
  }

  async function uploadAndMatchResume(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;

    setResumeLoading(true);
    setResumeError("");
    setResumeStatus("Extracting resume text");
    setMatchResults([]);
    setResumeFileName(file.name);

    try {
      const extracted = await extractResume(file);
      setResumeContext(extracted.content);
      setResumeStatus("Comparing resume with listed jobs");

      const results = await matchResume({
        content: extracted.content,
        target_role: "Seattle SDE",
        use_ai: false
      });
      setMatchResults(results);
      setResumeStatus(`Compared ${results.length} jobs from ${extracted.filename}`);
    } catch (error) {
      setResumeError(error instanceof Error ? error.message : "Resume upload failed");
      setResumeStatus("");
    } finally {
      setResumeLoading(false);
    }
  }

  async function submitChat(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const question = chatQuestion.trim();
    if (!question) {
      return;
    }

    setChatLoading(true);
    setChatQuestion("");
    const userMessage: ChatMessage = {
      id: `user-${Date.now()}`,
      role: "user",
      content: question
    };
    setChatMessages((messages) => [...messages, userMessage]);

    try {
      const response = await askJobCopilot({
        question,
        resume_context: resumeContext,
        conversation_id: "job-search-home",
        top_k: 5,
        use_llm: true
      });
      setChatMessages((messages) => [
        ...messages,
        {
          id: `assistant-${Date.now()}`,
          role: "assistant",
          content: response.answer,
          response,
          status:
            response.cache_status === "miss"
              ? "RAG retrieval completed"
              : response.cache_status === "skipped"
                ? "Retrieval skipped"
                : `Answered from ${response.cache_status} cache`
        }
      ]);
    } catch (error) {
      setChatMessages((messages) => [
        ...messages,
        {
          id: `assistant-error-${Date.now()}`,
          role: "assistant",
          content: error instanceof Error ? error.message : "Chat request failed",
          status: "Request failed"
        }
      ]);
    } finally {
      setChatLoading(false);
    }
  }

  return (
    <main className="feedShell">
      <header className="feedTopbar">
        <Link className="brandMark" href="/">
          <BriefcaseBusiness size={22} />
          <span>Seattle SDE Jobs</span>
        </Link>
        <div className="topbarActions">
          <span className="statusCluster compactStatus">
            <span className="status online" />
            Postgres database
          </span>
        </div>
      </header>

      <section className="feedHero">
        <div>
          <p className="eyebrow">Backend job feed</p>
          <h1>Browse Seattle-area software jobs from Postgres.</h1>
        </div>
        <div className="databaseBadge" aria-label="Local database size">
          <Database size={22} />
          <strong>{totalJobs}</strong>
          <span>jobs</span>
        </div>
      </section>

      <section className="aiChatPanel" aria-label="AI job copilot">
        <header className="chatHeader">
          <div>
            <span className="eyebrow">
              <Bot size={15} />
              AI job copilot
            </span>
            <h2>Chat with your job search assistant.</h2>
          </div>
          <div className="chatActions">
            <button className="contextToggle" type="button" onClick={() => setShowContext((value) => !value)}>
              <Settings2 size={16} />
              Context
            </button>
            <button
              className="contextToggle"
              type="button"
              onClick={resetChatHistory}
              aria-label="Clear chat history"
              title="Clear chat history"
            >
              <Trash2 size={16} />
              Clear
            </button>
          </div>
        </header>

        {showContext ? (
          <label className="contextDrawer">
            Candidate context
            <textarea
              className="chatContext"
              value={resumeContext}
              onChange={(event) => setResumeContext(event.target.value)}
              placeholder="Optional: paste resume summary, target role, must-have skills, location preference..."
            />
          </label>
        ) : null}

        <div className="chatThread" ref={chatScrollRef} aria-live="polite">
          {chatMessages.map((message) => (
            <article className={`chatBubble ${message.role}`} key={message.id}>
              {message.role === "assistant" ? (
                <div className="assistantAvatar">
                  <Bot size={15} />
                </div>
              ) : null}
              <div className="bubbleBody">
                {message.status ? <span className="messageStatus">{message.status}</span> : null}
                {message.response ? (
                  <div className="chatPipelineBadges">
                    <span>Cache: {message.response.cache_status}</span>
                    <span>Retrieval: {message.response.retrieval_source}</span>
                    <span>Template: {message.response.prompt_template}</span>
                  </div>
                ) : null}
                <p className="chatAnswer">{message.content}</p>
                {message.response?.retrieved_jobs.length ? (
                  <div className="retrievedJobs">
                    {message.response.retrieved_jobs.map((job) => (
                      <Link className="retrievedJob" href={`/jobs/${job.id}`} key={job.id}>
                        <strong>{job.company} / {job.title}</strong>
                        <span>{job.location} / {job.level} / {job.score}% match</span>
                      </Link>
                    ))}
                  </div>
                ) : null}
                {message.response?.warnings.length ? (
                  <p className="chatWarning">{message.response.warnings.join(" ")}</p>
                ) : null}
              </div>
            </article>
          ))}
          {chatLoading ? (
            <article className="chatBubble assistant">
              <div className="assistantAvatar">
                <Bot size={15} />
              </div>
              <div className="bubbleBody typingBubble">
                <span />
                <span />
                <span />
              </div>
            </article>
          ) : null}
        </div>

        <form className="chatComposer" onSubmit={submitChat}>
          <input
            value={chatQuestion}
            onChange={(event) => setChatQuestion(event.target.value)}
            placeholder="Ask for jobs, match fit, or resume tailoring..."
          />
          <button className="chatSendButton" type="submit" disabled={chatLoading || !chatQuestion.trim()}>
            {chatLoading ? <Sparkles className="spin" size={18} /> : <SendHorizontal size={18} />}
          </button>
        </form>

        <div className="quickPrompts" aria-label="Quick prompts">
          {[
            "Find backend new-grad jobs that match Java, Spring Boot, Redis, and AWS.",
            "Which jobs fit a full-stack React and Spring Boot resume?",
            "What should I emphasize for cloud backend roles?"
          ].map((prompt) => (
            <button
              type="button"
              key={prompt}
              onClick={() => {
                setChatQuestion(prompt);
              }}
            >
              {prompt}
            </button>
          ))}
        </div>
      </section>

      <section className="resumeMatchPanel" aria-label="Resume job matching">
        <div className="resumeMatchHeader">
          <div>
            <span className="eyebrow">
              <FileText size={15} />
              Resume match
            </span>
            <h2>Upload your resume and compare it with the job database.</h2>
          </div>
          <label className="resumeUploadButton">
            {resumeLoading ? <Sparkles className="spin" size={17} /> : <Upload size={17} />}
            {resumeLoading ? "Working" : "Upload resume"}
            <input accept=".pdf,.docx,.txt,.md" type="file" onChange={uploadAndMatchResume} />
          </label>
        </div>

        <div className="resumeMatchStatus">
          {resumeError ? (
            <span className="errorStatus">
              <AlertCircle size={15} />
              {resumeError}
            </span>
          ) : resumeStatus ? (
            <span>
              <CheckCircle2 size={15} />
              {resumeStatus}
            </span>
          ) : (
            <span>PDF, DOCX, TXT, or MD files are supported.</span>
          )}
          {resumeFileName ? <strong>{resumeFileName}</strong> : null}
        </div>

        {matchResults.length ? (
          <div className="resumeMatchResults" aria-label="Top resume matches">
            {matchResults.slice(0, 5).map((result) => (
              <article className="resumeMatchCard" key={result.job.id}>
                <div className="resumeMatchScore">
                  <strong>{result.score}</strong>
                  <span>match</span>
                </div>
                <div className="resumeMatchBody">
                  <span className="company">
                    <Building2 size={14} />
                    {result.job.company}
                  </span>
                  <h3>{result.job.title}</h3>
                  <p>{result.job.location} / {result.job.level} / {result.job.workMode}</p>
                  <div className="resumeMatchChips">
                    {result.matched_skills.slice(0, 5).map((skill) => (
                      <span className="chip matched" key={skill}>{skill}</span>
                    ))}
                    {result.missing_skills.slice(0, 3).map((skill) => (
                      <span className="chip missing" key={skill}>{skill}</span>
                    ))}
                  </div>
                </div>
                <Link className="secondaryButton" href={`/jobs/${result.job.id}`}>
                  Details
                  <ArrowRight size={16} />
                </Link>
              </article>
            ))}
          </div>
        ) : null}
      </section>

      <section className="feedControls" aria-label="Job filters">
        <label>
          <Search size={16} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search company, title, skill" />
        </label>
        <label>
          <MapPin size={16} />
          <select value={location} onChange={(event) => setLocation(event.target.value)}>
            <option>All</option>
            <option>Seattle</option>
            <option>Bellevue</option>
            <option>Redmond</option>
            <option>Kirkland</option>
          </select>
        </label>
        <div className="segmentedControl" aria-label="Candidate level">
          <button className={!audience ? "active" : ""} type="button" onClick={() => setAudience("")}>
            <Filter size={15} />
            All
          </button>
          <button className={audience === "new-grad" ? "active" : ""} type="button" onClick={() => setAudience("new-grad")}>
            <GraduationCap size={15} />
            New grad
          </button>
          <button className={audience === "intern" ? "active" : ""} type="button" onClick={() => setAudience("intern")}>
            <UserRound size={15} />
            Intern
          </button>
        </div>
      </section>

      <section className="feedStats">
        <span>{jobs.length} shown</span>
        <span>{totalJobs} matching jobs</span>
        <span>Loaded from /jobs/feed</span>
        <button className="sourceToggle" type="button" onClick={resetFilters}>
          Reset filters
        </button>
      </section>

      <section className="jobFeed" aria-label="Listed jobs">
        {jobs.map((job) => (
          <article className="feedJobCard" key={job.id}>
            <div className="feedJobHeader">
              <div>
                <span className="company">
                  <Building2 size={15} />
                  {job.company}
                </span>
                <h2>{job.title}</h2>
                <p>{job.location} / {job.workMode} / {job.level}</p>
              </div>
              <span className="sourcePill">{job.source}</span>
            </div>

            <p className="feedDescription">{job.description}</p>

            <div className="skillRows">
              <div>
                {job.requiredSkills.slice(0, 6).map((skill) => (
                  <span className="chip matched" key={skill}>{skill}</span>
                ))}
              </div>
              <div>
                {job.niceToHaveSkills.slice(0, 4).map((skill) => (
                  <span className="chip neutral" key={skill}>{skill}</span>
                ))}
              </div>
            </div>

            <div className="feedActions">
              <Link className="secondaryButton" href={`/jobs/${job.id}`}>
                Details
                <ArrowRight size={17} />
              </Link>
              {job.sourceUrl ? (
                <a className="applyLink" href={job.sourceUrl} target="_blank" rel="noreferrer">
                  Search source
                  <ExternalLink size={16} />
                </a>
              ) : null}
            </div>
          </article>
        ))}

        {feedError ? <div className="emptyState">{feedError}</div> : null}

        {!feedError && !feedLoading && jobs.length === 0 ? (
          <div className="emptyState">No jobs match this filter.</div>
        ) : null}

        <div className="feedSentinel" ref={sentinelRef}>
          {feedLoading ? (
            <span>Loading jobs</span>
          ) : hasMore ? (
            <span>Scroll for more jobs</span>
          ) : (
            <span>No more listed jobs for this filter</span>
          )}
        </div>
      </section>
    </main>
  );
}

function readStoredChatState(): StoredChatState | null {
  if (typeof window === "undefined") return null;

  try {
    const rawState = window.localStorage.getItem(CHAT_STORAGE_KEY);
    if (!rawState) return null;

    const parsed = JSON.parse(rawState) as unknown;
    if (!isRecord(parsed) || !Array.isArray(parsed.messages)) return null;

    const messages = parsed.messages.map(toChatMessage).filter(isPresent);
    if (!messages.length) return null;

    return {
      messages,
      resumeContext: typeof parsed.resumeContext === "string" ? parsed.resumeContext : ""
    };
  } catch {
    return null;
  }
}

function writeStoredChatState(state: StoredChatState) {
  if (typeof window === "undefined") return;

  try {
    window.localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Browsing modes or storage quotas can block localStorage; chat still works in memory.
  }
}

function toChatMessage(value: unknown): ChatMessage | null {
  if (!isRecord(value)) return null;
  if (value.role !== "assistant" && value.role !== "user") return null;
  if (typeof value.id !== "string" || typeof value.content !== "string") return null;

  const message: ChatMessage = {
    id: value.id,
    role: value.role,
    content: value.content
  };

  if (typeof value.status === "string") {
    message.status = value.status;
  }

  const response = toChatResponse(value.response);
  if (response) {
    message.response = response;
  }

  return message;
}

function toChatResponse(value: unknown): ChatResponse | null {
  if (!isRecord(value) || typeof value.answer !== "string") return null;

  const retrievedJobs = Array.isArray(value.retrieved_jobs)
    ? value.retrieved_jobs.map(toRetrievedJob).filter(isPresent)
    : [];

  return {
    answer: value.answer,
    cache_status: typeof value.cache_status === "string" ? value.cache_status : "unknown",
    cache_similarity: typeof value.cache_similarity === "number" ? value.cache_similarity : null,
    retrieval_source: typeof value.retrieval_source === "string" ? value.retrieval_source : "unknown",
    llm_used: Boolean(value.llm_used),
    prompt_template: typeof value.prompt_template === "string" ? value.prompt_template : "unknown",
    retrieved_jobs: retrievedJobs,
    warnings: Array.isArray(value.warnings) ? value.warnings.filter(isString) : []
  };
}

function toRetrievedJob(value: unknown): ChatResponse["retrieved_jobs"][number] | null {
  if (!isRecord(value)) return null;

  const id = value.id;
  const company = value.company;
  const title = value.title;
  const location = value.location;
  const level = value.level;
  const workMode = value.work_mode;
  const score = value.score;
  const reason = value.reason;

  if (
    typeof id !== "string" ||
    typeof company !== "string" ||
    typeof title !== "string" ||
    typeof location !== "string" ||
    typeof level !== "string" ||
    typeof workMode !== "string" ||
    typeof score !== "number" ||
    typeof reason !== "string"
  ) {
    return null;
  }

  return {
    id,
    company,
    title,
    location,
    level,
    work_mode: workMode,
    score,
    reason
  };
}

function isPresent<T>(value: T | null): value is T {
  return value !== null;
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
