CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS resumes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS job_postings (
  id TEXT PRIMARY KEY,
  company TEXT NOT NULL,
  title TEXT NOT NULL,
  location TEXT NOT NULL,
  source TEXT NOT NULL,
  source_url TEXT,
  description TEXT NOT NULL,
  required_skills TEXT[] NOT NULL DEFAULT '{}',
  nice_to_have_skills TEXT[] NOT NULL DEFAULT '{}',
  level TEXT NOT NULL DEFAULT 'mid',
  work_mode TEXT NOT NULL DEFAULT 'hybrid',
  fingerprint TEXT,
  embedding VECTOR(1536),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS company_sources (
  id TEXT PRIMARY KEY,
  company TEXT NOT NULL,
  career_url TEXT NOT NULL,
  ats_type TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT true,
  board_token TEXT,
  priority INTEGER NOT NULL DEFAULT 3,
  crawl_interval_minutes INTEGER NOT NULL DEFAULT 240,
  role_keywords TEXT[] NOT NULL DEFAULT '{}',
  location_keywords TEXT[] NOT NULL DEFAULT '{}',
  last_crawled_at TIMESTAMPTZ,
  notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS companies (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  website_url TEXT NOT NULL,
  headquarters TEXT NOT NULL DEFAULT '',
  industry TEXT NOT NULL DEFAULT 'Technology',
  known_career_url TEXT,
  discovery_source TEXT NOT NULL DEFAULT 'seed',
  confidence_score NUMERIC NOT NULL DEFAULT 0.75,
  status TEXT NOT NULL DEFAULT 'candidate',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crawl_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  sources_crawled INTEGER NOT NULL DEFAULT 0,
  jobs_seen INTEGER NOT NULL DEFAULT 0,
  jobs_added INTEGER NOT NULL DEFAULT 0,
  jobs_updated INTEGER NOT NULL DEFAULT 0,
  jobs_unchanged INTEGER NOT NULL DEFAULT 0,
  needs_ai_extraction INTEGER NOT NULL DEFAULT 0,
  error_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS applications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id TEXT NOT NULL REFERENCES job_postings(id),
  stage TEXT NOT NULL CHECK (stage IN ('saved', 'applied', 'oa', 'interview', 'rejected', 'offer')),
  notes TEXT NOT NULL DEFAULT '',
  follow_up_on DATE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS job_postings_embedding_idx
  ON job_postings
  USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);
