from app.matcher import match_resume_to_job
from app.models import ResumeInput
from app.seed import load_seed_jobs


def test_matcher_scores_relevant_resume_higher_than_sparse_resume():
    jobs = load_seed_jobs()
    target = next(job for job in jobs if "AWS" in job.required_skills)

    strong_resume = ResumeInput(
        content=(
            "Seattle software engineer with Python, Java, AWS Lambda, CI/CD, "
            "distributed systems, data structures, algorithms, testing, and backend APIs."
        )
    )
    sparse_resume = ResumeInput(
        content=(
            "Designer with writing, research, social media planning, customer interviews, "
            "and brand campaign experience across consumer products."
        )
    )

    assert match_resume_to_job(strong_resume, target).score > match_resume_to_job(
        sparse_resume, target
    ).score


def test_short_skill_aliases_do_not_match_substrings():
    target = next(job for job in load_seed_jobs() if "Go" in job.required_skills)
    resume = ResumeInput(
        content=(
            "Seattle engineer with AWS, distributed systems, algorithms, data structures, "
            "Python, SQL, backend APIs, testing, and Docker experience."
        )
    )

    result = match_resume_to_job(resume, target)

    assert "Go" not in result.matched_skills
