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
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  extractResume,
  fetchCurrentUser,
  fetchJobFeed,
  fetchSavedResumes,
  loginAccount,
  matchResume,
  registerAccount,
  streamJobCopilot
} from "@/lib/api";
import type {
  ChatResponse,
  CopilotWorkflow,
  JobPosting,
  MatchResult,
  SavedResume,
  User,
  WorkflowAction
} from "@/lib/types";

type AudienceFilter = "" | "new-grad" | "intern";
type AuthMode = "login" | "register";
type ChatMessage = {
  id: string;
  role: "assistant" | "user";
  content: string;
  response?: ChatResponse;
  status?: string;
};

const PAGE_SIZE = 12;
const CHAT_STORAGE_KEY = "ai-job-match:home-chat:v1";
const AUTH_STORAGE_KEY = "ai-job-match:auth-token:v1";
const DEFAULT_CHAT_MESSAGES: ChatMessage[] = [
  {
    id: "welcome",
    role: "assistant",
    content:
      "Tell me what kind of role you want. I can compare the job database with your resume context and answer with specific roles, fit signals, and resume tailoring ideas."
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
  const [authToken, setAuthToken] = useState("");
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [savedResumes, setSavedResumes] = useState<SavedResume[]>([]);
  const [authMode, setAuthMode] = useState<AuthMode>("login");
  const [authEmail, setAuthEmail] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authName, setAuthName] = useState("");
  const [authStatus, setAuthStatus] = useState("");
  const [authError, setAuthError] = useState("");
  const [authLoading, setAuthLoading] = useState(false);
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
    writeStoredChatState({ messages: chatMessages, resumeContext: currentUser ? "" : resumeContext });
  }, [chatMessages, chatStorageReady, currentUser, resumeContext]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const storedToken = window.localStorage.getItem(AUTH_STORAGE_KEY);
    if (!storedToken) return;

    setAuthToken(storedToken);
    setAuthStatus("Restoring session");
    fetchCurrentUser(storedToken)
      .then(async (user) => {
        setCurrentUser(user);
        setAuthEmail(user.email);
        await refreshSavedResumes(storedToken);
        setAuthStatus("");
      })
      .catch(() => {
        window.localStorage.removeItem(AUTH_STORAGE_KEY);
        setAuthToken("");
        setCurrentUser(null);
        setAuthStatus("");
      });
  }, []);

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
    setChatQuestion("");
    if (!currentUser) {
      setResumeContext("");
      setResumeFileName("");
      setResumeStatus("");
      setMatchResults([]);
    }

    if (typeof window === "undefined") return;
    try {
      window.localStorage.removeItem(CHAT_STORAGE_KEY);
    } catch {
      // Chat has already been reset in memory, so storage cleanup can fail silently.
    }
  }

  async function refreshSavedResumes(token = authToken) {
    if (!token) return;
    const resumes = await fetchSavedResumes(token);
    setSavedResumes(resumes);
    const activeResume = resumes.find((resume) => resume.active) ?? resumes[0];
    if (activeResume) {
      setResumeContext(activeResume.content);
      setResumeFileName(activeResume.filename || activeResume.title);
      setResumeStatus(`Loaded saved resume: ${activeResume.filename || activeResume.title}`);
    } else {
      setResumeContext("");
      setResumeFileName("");
      setResumeStatus("No saved resume yet. Upload one to save it to your account.");
      setMatchResults([]);
    }
  }

  async function submitAuth(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setAuthError("");
    setAuthStatus("");

    if (!authEmail.trim() || !authPassword) {
      setAuthError("Enter your email and password.");
      return;
    }

    setAuthLoading(true);
    try {
      const response =
        authMode === "register"
          ? await registerAccount({
              email: authEmail.trim(),
              password: authPassword,
              name: authName.trim()
            })
          : await loginAccount({
              email: authEmail.trim(),
              password: authPassword
            });

      setAuthToken(response.token);
      setCurrentUser(response.user);
      setAuthPassword("");
      setAuthStatus(`Signed in as ${response.user.name}`);
      if (typeof window !== "undefined") {
        window.localStorage.setItem(AUTH_STORAGE_KEY, response.token);
      }
      await refreshSavedResumes(response.token);
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "Authentication failed");
    } finally {
      setAuthLoading(false);
    }
  }

  function logout() {
    setAuthToken("");
    setCurrentUser(null);
    setSavedResumes([]);
    setResumeContext("");
    setResumeFileName("");
    setResumeStatus("");
    setResumeError("");
    setMatchResults([]);
    setAuthPassword("");
    setAuthStatus("");
    if (typeof window !== "undefined") {
      window.localStorage.removeItem(AUTH_STORAGE_KEY);
      window.localStorage.removeItem(CHAT_STORAGE_KEY);
    }
    setChatMessages(DEFAULT_CHAT_MESSAGES);
  }

  async function uploadAndMatchResume(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;

    if (!authToken) {
      setResumeError("Log in before uploading a resume so it can be saved to your account.");
      return;
    }

    setResumeLoading(true);
    setResumeError("");
    setResumeStatus("Extracting resume text");
    setMatchResults([]);
    setResumeFileName(file.name);

    try {
      const extracted = await extractResume(file, authToken);
      setResumeContext(extracted.content);
      setResumeStatus(extracted.saved ? "Saved resume to your account" : "Comparing resume with listed jobs");
      await refreshSavedResumes(authToken);

      const results = await matchResume({
        content: extracted.content,
        target_role: "Seattle SDE",
        use_ai: false
      });
      setMatchResults(results);
      setResumeStatus(
        extracted.saved
          ? `Saved and compared ${results.length} jobs from ${extracted.filename}`
          : `Compared ${results.length} jobs from ${extracted.filename}`
      );
    } catch (error) {
      setResumeError(error instanceof Error ? error.message : "Resume upload failed");
      setResumeStatus("");
    } finally {
      setResumeLoading(false);
    }
  }

  async function submitChat(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await sendChatQuestion(chatQuestion);
  }

  async function sendChatQuestion(rawQuestion: string) {
    const question = rawQuestion.trim();
    if (!question) {
      return;
    }

    setChatLoading(true);
    setChatQuestion("");
    const assistantId = `assistant-${Date.now()}`;
    const userMessage: ChatMessage = {
      id: `user-${Date.now()}`,
      role: "user",
      content: question
    };
    const assistantMessage: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      status: "AI is writing"
    };
    setChatMessages((messages) => [...messages, userMessage, assistantMessage]);

    try {
      const response = await streamJobCopilot(
        {
          question,
          resume_context: resumeContext,
          conversation_id: "job-search-home",
          messages: chatMessages
            .filter((message) => message.content.trim())
            .slice(-8)
            .map((message) => ({ role: message.role, content: message.content })),
          top_k: 5,
          use_llm: true,
          token: authToken || undefined
        },
        {
          onChunk: (content) => {
            setChatMessages((messages) =>
              messages.map((message) =>
                message.id === assistantId
                  ? {
                      ...message,
                      content: `${message.content}${content}`
                    }
                  : message
              )
            );
          }
        }
      );
      setChatMessages((messages) =>
        messages.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                content: response.answer || message.content,
                response,
                status: chatResponseStatus(response)
              }
            : message
        )
      );
    } catch (error) {
      setChatMessages((messages) =>
        messages.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                content: message.content || (error instanceof Error ? error.message : "Chat request failed"),
                status: message.content ? "AI response interrupted" : "Request failed"
              }
            : message
        )
      );
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
          {currentUser ? (
            <div className="accountSummary" aria-label="Signed in account">
              <UserRound size={16} />
              <div>
                <strong>{currentUser.name}</strong>
                <span>{savedResumes.length ? `${savedResumes.length} saved resume${savedResumes.length === 1 ? "" : "s"}` : "No saved resume yet"}</span>
              </div>
              <button type="button" onClick={logout}>
                Log out
              </button>
            </div>
          ) : (
            <form className="authForm" onSubmit={submitAuth} aria-label="Account login">
              {authMode === "register" ? (
                <input
                  value={authName}
                  onChange={(event) => setAuthName(event.target.value)}
                  placeholder="Name"
                  autoComplete="name"
                />
              ) : null}
              <input
                value={authEmail}
                onChange={(event) => setAuthEmail(event.target.value)}
                placeholder="Email"
                autoComplete="email"
                type="email"
              />
              <input
                value={authPassword}
                onChange={(event) => setAuthPassword(event.target.value)}
                placeholder="Password"
                autoComplete={authMode === "register" ? "new-password" : "current-password"}
                type="password"
              />
              <button type="submit" disabled={authLoading}>
                {authLoading ? "Working" : authMode === "register" ? "Sign up" : "Log in"}
              </button>
              <button
                type="button"
                className="authSwitch"
                onClick={() => {
                  setAuthMode((mode) => (mode === "login" ? "register" : "login"));
                  setAuthError("");
                  setAuthStatus("");
                }}
              >
                {authMode === "login" ? "Create account" : "Use login"}
              </button>
            </form>
          )}
        </div>
      </header>

      {authError || authStatus ? (
        <div className={`authNotice ${authError ? "error" : ""}`}>{authError || authStatus}</div>
      ) : null}

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
                    {message.response.intent_route ? <span>Intent: {message.response.intent_route.intent}</span> : null}
                    <span>Cache: {message.response.cache_status}</span>
                    <span>Retrieval: {message.response.retrieval_source}</span>
                    <span>Template: {message.response.prompt_template}</span>
                  </div>
                ) : null}
                <div className="chatAnswer">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw, rehypeSanitize]}>
                    {message.content}
                  </ReactMarkdown>
                </div>
                {message.response?.workflow ? (
                  <GenerativeWorkflowView workflow={message.response.workflow} onSuggestReply={sendChatQuestion} />
                ) : null}
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
          {chatLoading && chatMessages[chatMessages.length - 1]?.role !== "assistant" ? (
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
            "Compare backend new-grad jobs that match Java, Spring Boot, Redis, and AWS.",
            "Build a missing-skill matrix for full-stack React and Spring Boot roles.",
            "Create a resume-tailoring checklist for cloud backend roles."
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
            <span>
              {currentUser
                ? "PDF, DOCX, TXT, or MD files are supported. Uploads are saved to your account."
                : "Log in to save resumes. PDF, DOCX, TXT, or MD files are supported."}
            </span>
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

function GenerativeWorkflowView({
  workflow,
  onSuggestReply
}: {
  workflow: CopilotWorkflow;
  onSuggestReply?: (question: string) => void;
}) {
  return (
    <section className="generativeWorkflow" aria-label={workflow.title}>
      <header className="workflowHeader">
        <span>
          <Sparkles size={14} />
          Generative UI
        </span>
        <strong>{workflow.title}</strong>
      </header>

      {workflow.tool_calls.length ? (
        <div className="toolTrace" aria-label="Tool calls">
          {workflow.tool_calls.map((tool) => (
            <span key={tool.name} title={tool.summary}>
              <CheckCircle2 size={13} />
              {tool.title}
            </span>
          ))}
        </div>
      ) : null}

      {workflow.job_cards.length ? (
        <div className="workflowCards" aria-label="Job comparison cards">
          {workflow.job_cards.map((job) => (
            <article className="workflowJobCard" key={job.job_id}>
              <div className="workflowJobTop">
                <div>
                  <span>{job.company}</span>
                  <strong>{job.title}</strong>
                  <em>{job.location} / {job.level} / {job.work_mode}</em>
                </div>
                <b>{job.score}</b>
              </div>
              <p>{job.fit_summary}</p>
              <div className="workflowChips">
                {job.matched_skills.slice(0, 4).map((skill) => (
                  <span className="chip matched" key={skill}>{skill}</span>
                ))}
                {job.missing_skills.slice(0, 3).map((skill) => (
                  <span className="chip missing" key={skill}>{skill}</span>
                ))}
              </div>
            </article>
          ))}
        </div>
      ) : null}

      {workflow.skill_matrix.length ? (
        <div className="skillMatrix" aria-label="Missing skill matrix">
          {workflow.skill_matrix.map((row) => (
            <div className="matrixRow" key={row.skill}>
              <strong>{row.skill}</strong>
              <span className={`matrixStatus ${row.status}`}>{row.status}</span>
              <p>{row.evidence}</p>
              <em>{row.jobs.join(", ")}</em>
            </div>
          ))}
        </div>
      ) : null}

      {workflow.resume_checklist.length ? (
        <div className="workflowChecklist" aria-label="Resume tailoring checklist">
          {workflow.resume_checklist.map((item) => (
            <article key={item.title}>
              <span>{item.priority}</span>
              <strong>{item.title}</strong>
              <p>{item.detail}</p>
              {item.related_skills.length ? <em>{item.related_skills.join(", ")}</em> : null}
            </article>
          ))}
        </div>
      ) : null}

      {workflow.actions.length ? (
        <div className="workflowActions" aria-label="Workflow actions">
          {workflow.actions.map((action) => (
            <WorkflowActionButton
              action={action}
              key={`${action.intent}-${action.job_id ?? action.label}`}
              onSuggestReply={onSuggestReply}
            />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function WorkflowActionButton({
  action,
  onSuggestReply
}: {
  action: WorkflowAction;
  onSuggestReply?: (question: string) => void;
}) {
  if (action.intent === "open_job" && action.job_id) {
    return (
      <Link href={`/jobs/${action.job_id}`}>
        {action.label}
        <ArrowRight size={14} />
      </Link>
    );
  }

  if (action.intent === "suggest_reply" && action.payload.prompt) {
    return (
      <button type="button" onClick={() => onSuggestReply?.(action.payload.prompt)}>
        {action.label}
      </button>
    );
  }

  return (
    <button type="button" title={Object.entries(action.payload).map(([key, value]) => `${key}: ${value}`).join(", ")}>
      {action.label}
    </button>
  );
}

function chatResponseStatus(response: ChatResponse) {
  if (response.cache_status === "skipped") {
    return "Retrieval skipped";
  }
  if (response.cache_status === "disabled") {
    return "Cache disabled";
  }
  if (response.cache_status !== "miss") {
    return `Answered from ${response.cache_status} cache`;
  }
  return response.llm_used ? "AI answer" : "Fallback answer";
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
    intent_route: toIntentRoute(value.intent_route),
    workflow: toCopilotWorkflow(value.workflow),
    warnings: Array.isArray(value.warnings) ? value.warnings.filter(isString) : []
  };
}

function toIntentRoute(value: unknown): ChatResponse["intent_route"] {
  if (!isRecord(value) || typeof value.intent !== "string") return null;
  return {
    intent: value.intent,
    confidence: typeof value.confidence === "number" ? value.confidence : 0,
    needs_retrieval: Boolean(value.needs_retrieval),
    needs_action: Boolean(value.needs_action),
    entities: isRecord(value.entities) ? value.entities : {},
    missing_fields: Array.isArray(value.missing_fields) ? value.missing_fields.filter(isString) : [],
    reason: typeof value.reason === "string" ? value.reason : "",
    source: typeof value.source === "string" ? value.source : "unknown"
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

function toCopilotWorkflow(value: unknown): CopilotWorkflow | null {
  if (!isRecord(value)) return null;
  return {
    title: typeof value.title === "string" ? value.title : "Generated workflow",
    tool_calls: Array.isArray(value.tool_calls) ? value.tool_calls.map(toToolCall).filter(isPresent) : [],
    job_cards: Array.isArray(value.job_cards) ? value.job_cards.map(toWorkflowJobCard).filter(isPresent) : [],
    skill_matrix: Array.isArray(value.skill_matrix) ? value.skill_matrix.map(toSkillMatrixRow).filter(isPresent) : [],
    resume_checklist: Array.isArray(value.resume_checklist)
      ? value.resume_checklist.map(toResumeChecklistItem).filter(isPresent)
      : [],
    actions: Array.isArray(value.actions) ? value.actions.map(toWorkflowAction).filter(isPresent) : []
  };
}

function toToolCall(value: unknown): CopilotWorkflow["tool_calls"][number] | null {
  if (!isRecord(value) || typeof value.name !== "string" || typeof value.title !== "string") return null;
  return {
    name: value.name,
    title: value.title,
    status: typeof value.status === "string" ? value.status : "completed",
    summary: typeof value.summary === "string" ? value.summary : ""
  };
}

function toWorkflowJobCard(value: unknown): CopilotWorkflow["job_cards"][number] | null {
  if (!isRecord(value)) return null;
  if (
    typeof value.job_id !== "string" ||
    typeof value.company !== "string" ||
    typeof value.title !== "string" ||
    typeof value.location !== "string" ||
    typeof value.level !== "string" ||
    typeof value.work_mode !== "string" ||
    typeof value.score !== "number" ||
    typeof value.fit_summary !== "string"
  ) {
    return null;
  }

  return {
    job_id: value.job_id,
    company: value.company,
    title: value.title,
    location: value.location,
    level: value.level,
    work_mode: value.work_mode,
    score: value.score,
    fit_summary: value.fit_summary,
    matched_skills: Array.isArray(value.matched_skills) ? value.matched_skills.filter(isString) : [],
    missing_skills: Array.isArray(value.missing_skills) ? value.missing_skills.filter(isString) : []
  };
}

function toSkillMatrixRow(value: unknown): CopilotWorkflow["skill_matrix"][number] | null {
  if (
    !isRecord(value) ||
    typeof value.skill !== "string" ||
    typeof value.status !== "string" ||
    typeof value.evidence !== "string"
  ) {
    return null;
  }

  return {
    skill: value.skill,
    status: value.status,
    evidence: value.evidence,
    jobs: Array.isArray(value.jobs) ? value.jobs.filter(isString) : []
  };
}

function toResumeChecklistItem(value: unknown): CopilotWorkflow["resume_checklist"][number] | null {
  if (
    !isRecord(value) ||
    typeof value.title !== "string" ||
    typeof value.priority !== "string" ||
    typeof value.detail !== "string"
  ) {
    return null;
  }

  return {
    title: value.title,
    priority: value.priority,
    detail: value.detail,
    related_skills: Array.isArray(value.related_skills) ? value.related_skills.filter(isString) : []
  };
}

function toWorkflowAction(value: unknown): CopilotWorkflow["actions"][number] | null {
  if (!isRecord(value) || typeof value.label !== "string" || typeof value.intent !== "string") return null;
  return {
    label: value.label,
    intent: value.intent,
    job_id: typeof value.job_id === "string" ? value.job_id : null,
    payload: toStringRecord(value.payload)
  };
}

function toStringRecord(value: unknown): Record<string, string> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.entries(value)
      .filter((entry): entry is [string, string] => typeof entry[1] === "string")
  );
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
