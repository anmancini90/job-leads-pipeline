---
name: job-leads-pipeline
description: Set up and run a private, candidate-approved job search that finds verified leads, prepares fact-checked tailored resume PDFs, and emails the candidate once per day. Use for onboarding, manual or daily searches, feedback, status, and pause or resume requests; never use to apply to jobs or contact employers.
---

# Job Leads Pipeline

When a new user invokes this skill, begin a short, friendly recruiter-style interview. Ask one small group of questions at a time. Read [onboarding](references/onboarding.md), [project schemas](references/schemas.md), and [setup](references/setup.md). Explain the next action in plain language. The first run creates a **private, non-Git project outside this skill** and leaves delivery paused. A resume, LinkedIn URL, or connected account is never permission to email or activate a schedule.

Operate exactly one project supplied by the user. Never scan for other candidates, maintain a registry, or share state across projects. Keep `profile_id` immutable. Never put candidate data, credentials, search output, or receipts in the skill or Git checkout. Never infer protected traits. The daily pipeline sends only to the candidate; it does not apply to jobs, upload resumes, create employer accounts, or contact employers.

## Choose the task

- **Onboard or refresh:** Use [onboarding](references/onboarding.md), [schemas](references/schemas.md), and [delivery guards](references/delivery-and-run-guards.md). Run `scripts/doctor.py --host codex|claude [--project-root PATH]`. For a new user, call `scripts/init_profile.py` only after confirming a private project path, then gather and confirm source-backed facts and search preferences. Run `scripts/validate_profile.py` before saying the setup is ready.
- **Manual or daily search:** Use [sourcing](references/sourcing.md), [scoring](references/scoring.md), [daily resumes](references/daily-resumes.md), and [delivery guards](references/delivery-and-run-guards.md). Run the exact preflight, live-link, factual-review, attachment, claim, and receipt gates in those references. A manual search does not activate recurring delivery.
- **Gmail setup or recovery:** Use [Gmail](references/gmail.md), [setup](references/setup.md), and [delivery guards](references/delivery-and-run-guards.md). Probe the installed connector for exact attachment bytes and Sent-mail verification. If it cannot prove both, guide the user through personal Gmail API OAuth. Never store a token in the project or repo.
- **Feedback or calibration:** Use [feedback](references/feedback.md) and [scoring](references/scoring.md). Preserve all historical evaluated-role and receipt records.
- **Status, pause, resume, revoke, or archive:** Use [delivery guards](references/delivery-and-run-guards.md). Status is read-only. Require explicit permission for a lifecycle change and revalidate every approval before activation or resume.

## Every run

1. Confirm the exact private project root and matching `profile_id`; stop on mismatch or path escape.
2. Keep unconfirmed facts provisional. Before combining facts, reject empty, contradictory, off-topic, or weak evidence. Ask the candidate to resolve conflicts; do not choose for them.
3. Source only public openings and record each evaluated role. Verify the exact final job URLs live immediately before preparing delivery; one ambiguous or dead link blocks the lead set.
4. Never invent or embellish resume claims. Freeze the candidate facts and job evidence, draft role-specific PDFs, require an independent factual approval tied to exact input bytes, then check text and page appearance. Do not reuse historical generated variants as factual sources.
5. Keep scheduled prompts minimal: project root, mode, intended local date, and this skill name. Do not put personal details, email text, credentials, or scoring logic in them.
6. Before any candidate email, verify lifecycle approvals, exact recipient, local date, no prior receipt, exact attachment bytes, and final claim. An uncertain provider response stops automatic retry until Sent mail is reconciled. A success receipt always blocks another email that date.
