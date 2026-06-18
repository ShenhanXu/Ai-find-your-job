import type { JobPosting } from "./types";

const companies = [
  "Microsoft",
  "Amazon",
  "Google",
  "Meta",
  "Apple",
  "Salesforce",
  "Snowflake",
  "Databricks",
  "Stripe",
  "DoorDash",
  "Uber",
  "Expedia",
  "Zillow",
  "Redfin",
  "F5",
  "Smartsheet",
  "Qualtrics",
  "Remitly",
  "Convoy Labs",
  "Outreach",
  "Highspot",
  "Tableau",
  "MongoDB",
  "ServiceNow",
  "Oracle",
  "Adobe",
  "Nvidia",
  "Palantir",
  "Robinhood",
  "Block"
];

const titles = [
  "Software Development Engineer",
  "Backend Software Engineer",
  "Full Stack Engineer",
  "Frontend Software Engineer",
  "Platform Engineer",
  "Infrastructure Software Engineer",
  "Cloud Services Engineer",
  "Data Platform Engineer",
  "Product Software Engineer",
  "Machine Learning Platform Engineer"
];

const locations = [
  "Seattle, WA",
  "Bellevue, WA",
  "Redmond, WA",
  "Kirkland, WA",
  "Seattle, WA / Remote US",
  "Bellevue, WA / Hybrid"
];

const levels = ["intern", "new-grad", "entry", "mid", "senior"];
const workModes = ["onsite", "hybrid", "remote"];

const skillGroups = [
  ["Java", "Spring Boot", "REST API", "SQL", "Distributed Systems", "AWS"],
  ["Python", "FastAPI", "PostgreSQL", "Redis", "Docker", "System Design"],
  ["TypeScript", "React", "Next.js", "Node.js", "GraphQL", "Testing"],
  ["Go", "Kubernetes", "gRPC", "Linux", "Observability", "Terraform"],
  ["C#", ".NET", "Azure", "Microservices", "CI/CD", "Event Streaming"],
  ["Python", "Spark", "Airflow", "Data Modeling", "ETL", "Databases"],
  ["JavaScript", "Accessibility", "Design Systems", "React", "Performance", "Testing"],
  ["Rust", "C++", "Networking", "Concurrency", "Low-Level Systems", "Linux"],
  ["Machine Learning", "Python", "Feature Stores", "Model Serving", "Kubernetes", "MLOps"],
  ["Security", "IAM", "Cloud", "Threat Modeling", "APIs", "Automation"]
];

const niceToHavePool = [
  "Kafka",
  "DynamoDB",
  "Lambda",
  "Elasticsearch",
  "Snowflake",
  "Datadog",
  "Prometheus",
  "Playwright",
  "Tailwind CSS",
  "Prisma",
  "SQS",
  "Flink",
  "Pandas",
  "Kotlin",
  "Swift",
  "OpenTelemetry",
  "GitHub Actions",
  "Feature Flags",
  "A/B Testing",
  "Cost Optimization"
];

function slugify(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
}

function sourceUrl(company: string) {
  return `https://www.google.com/search?q=${encodeURIComponent(`${company} careers software engineer Seattle`)}`;
}

export const jobDatabase: JobPosting[] = Array.from({ length: 100 }, (_, index) => {
  const company = companies[index % companies.length];
  const titleBase = titles[index % titles.length];
  const skillGroup = skillGroups[index % skillGroups.length];
  const location = locations[index % locations.length];
  const level = levels[index % levels.length];
  const workMode = workModes[index % workModes.length];
  const focus = skillGroup.slice(0, 3).join(", ");
  const title = `${titleBase}${level === "intern" ? " Intern" : level === "new-grad" ? ", New Grad" : ""}`;

  return {
    id: `${slugify(company)}-${slugify(titleBase)}-${index + 1}`,
    company,
    title,
    location,
    source: "local-db",
    sourceUrl: sourceUrl(company),
    level,
    workMode,
    description:
      `Work on ${titleBase.toLowerCase()} projects for customer-facing and internal platforms. ` +
      `This role emphasizes ${focus}, production ownership, clear code reviews, and practical collaboration with product, design, and operations teams.`,
    requiredSkills: skillGroup,
    niceToHaveSkills: [
      niceToHavePool[index % niceToHavePool.length],
      niceToHavePool[(index + 5) % niceToHavePool.length],
      niceToHavePool[(index + 11) % niceToHavePool.length],
      niceToHavePool[(index + 17) % niceToHavePool.length]
    ]
  };
});

