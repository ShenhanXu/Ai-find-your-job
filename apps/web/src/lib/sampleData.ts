import type { ApplicationRecord, JobPosting, MatchResult } from "./types";
import { jobDatabase } from "./jobDatabase";

export const sampleResume = `Seattle-based software engineer focused on backend and full-stack products.

Experience
- Built Python and Java services with REST APIs, SQL, Docker, AWS Lambda, and CI/CD pipelines.
- Improved API latency by 32% by adding caching, query tuning, and production telemetry.
- Created a React and TypeScript dashboard backed by PostgreSQL and automated tests.
- Practiced data structures, algorithms, system design, and distributed systems through production projects and interview prep.

Projects
- Job match platform using Next.js, FastAPI, PostgreSQL, Redis, embeddings, and AWS deployment patterns.`;

export const fallbackJobs: JobPosting[] = jobDatabase;

export const fallbackMatches: MatchResult[] = fallbackJobs.map((job, index) => ({
  job,
  score: [84, 91, 79][index] ?? 75,
  evaluation_source: "local",
  matched_skills: job.requiredSkills.slice(0, Math.max(3, job.requiredSkills.length - 2)),
  missing_skills: job.requiredSkills.slice(-2),
  risks: index === 0 ? ["C# is not visible in the current resume."] : [],
  bullet_suggestions: [
    `Lead one bullet with measurable impact, then name ${job.requiredSkills[0]} and ${job.requiredSkills[1]}.`,
    "Add a production ownership signal: monitoring, on-call, launch quality, or reliability improvement."
  ],
  ai_summary: null,
  ai_strengths: [],
  interview_focus: [],
  breakdown: {
    semantic_fit: [88, 93, 80][index] ?? 75,
    required_skills: [71, 86, 67][index] ?? 70,
    nice_to_have_skills: [25, 50, 50][index] ?? 40,
    level_fit: 82,
    location_fit: 100
  }
}));

export const initialApplications: ApplicationRecord[] = [
  {
    id: "app-1",
    jobId: "amazon-seattle-sde-aws",
    stage: "applied",
    notes: "Resume version tailored for AWS tools role."
  },
  {
    id: "app-2",
    jobId: "stripe-seattle-fullstack",
    stage: "saved",
    notes: "Need stronger product metrics before applying."
  }
];
