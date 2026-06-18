"use client";

import { ArrowLeft, Building2, ExternalLink, MapPin } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { fetchJobById } from "@/lib/api";
import type { JobPosting } from "@/lib/types";

export default function JobDetailPage() {
  const params = useParams<{ id: string }>();
  const [job, setJob] = useState<JobPosting | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");

    fetchJobById(params.id)
      .then((item) => {
        if (!cancelled) setJob(item);
      })
      .catch((fetchError) => {
        if (!cancelled) setError(fetchError instanceof Error ? fetchError.message : "Listing not found.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [params.id]);

  if (loading || error || !job) {
    return (
      <main className="detailShell">
        <Link className="backLink" href="/">
          <ArrowLeft size={17} />
          Back to jobs
        </Link>
        <div className="emptyState">{loading ? "Loading listing." : error || "Listing not found."}</div>
      </main>
    );
  }

  return (
    <main className="detailShell">
      <header className="detailTopbar">
        <Link className="backLink" href="/">
          <ArrowLeft size={17} />
          Back to jobs
        </Link>
        <span className="statusCluster compactStatus">
          <span className="status online" />
          Postgres database
        </span>
      </header>

      <section className="listingDetail singleColumn">
        <article className="listingMain">
          <span className="company">
            <Building2 size={16} />
            {job.company}
          </span>
          <h1>{job.title}</h1>
          <p>
            <MapPin size={15} />
            {job.location} / {job.workMode} / {job.level}
          </p>
          <p className="detailDescription">{job.description}</p>

          <div className="detailSkills">
            <section>
              <h2>Required</h2>
              {job.requiredSkills.map((skill) => (
                <span className="chip matched" key={skill}>{skill}</span>
              ))}
            </section>
            <section>
              <h2>Nice to have</h2>
              {job.niceToHaveSkills.map((skill) => (
                <span className="chip neutral" key={skill}>{skill}</span>
              ))}
            </section>
          </div>

          <div className="detailMetaGrid" aria-label="Job metadata">
            <div>
              <span>Source</span>
              <strong>{job.source}</strong>
            </div>
            <div>
              <span>Work mode</span>
              <strong>{job.workMode}</strong>
            </div>
            <div>
              <span>Level</span>
              <strong>{job.level}</strong>
            </div>
          </div>

          {job.sourceUrl ? (
            <a className="applyPrimary" href={job.sourceUrl} target="_blank" rel="noreferrer">
              Search application source
              <ExternalLink size={18} />
            </a>
          ) : null}
        </article>
      </section>
    </main>
  );
}
