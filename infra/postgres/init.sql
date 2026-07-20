CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS resumes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID,
  title TEXT NOT NULL,
  filename TEXT NOT NULL DEFAULT '',
  raw_text TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE resumes ADD COLUMN IF NOT EXISTS user_id UUID;
ALTER TABLE resumes ADD COLUMN IF NOT EXISTS filename TEXT NOT NULL DEFAULT '';
ALTER TABLE resumes ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE resumes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'resumes_user_id_fkey'
  ) THEN
    ALTER TABLE resumes
      ADD CONSTRAINT resumes_user_id_fkey
      FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS resumes_user_id_idx ON resumes(user_id);

CREATE UNIQUE INDEX IF NOT EXISTS resumes_one_active_per_user_idx
  ON resumes(user_id)
  WHERE active AND user_id IS NOT NULL;

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

-- Ingestion pipeline columns (idempotent so this file stays rerunnable on existing databases).
ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'open';
ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS source_id TEXT;
ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS job_postings_status_idx ON job_postings(status);
CREATE INDEX IF NOT EXISTS job_postings_source_id_idx ON job_postings(source_id);

ALTER TABLE company_sources ADD COLUMN IF NOT EXISTS last_enqueued_at TIMESTAMPTZ;
ALTER TABLE company_sources ADD COLUMN IF NOT EXISTS last_success_at TIMESTAMPTZ;
ALTER TABLE company_sources ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE company_sources ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ;
