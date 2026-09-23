# Onboarding and refresh

## Start the conversation

Begin with: “I can help find roles that fit you. First, tell me what kind of work you want. We will review everything together before I send or schedule anything.” Ask in small groups rather than sending a long form. Collect only job-related details:

1. A resume PDF or text, the name they want on it, and the email address where their daily note should arrive. Offer a public LinkedIn or portfolio URL as optional context; if the page is unavailable, ask for a candidate-provided export or text. A URL is not a source of verified private facts by itself.
2. Target role titles and level, industries of interest, where they can work, remote/hybrid/on-site preference, travel limits, pay floor or range, and clear dealbreakers. Ask about search timing and what an especially exciting opportunity would look like.
3. Whether they want tailored one-page resume PDFs attached to the daily note. Explain that claims are drawn only from checked source material and each role draft receives a separate factual review.

Show a short summary of the resume facts and preferences, mark each fact with its source, and ask the candidate to correct or confirm it. When information conflicts, show both versions and pause that decision. Never infer or request protected traits. Do not use job postings or prior AI-generated resumes as evidence of the candidate's experience.

After the candidate confirms the search, review 8–12 example openings spanning good fits, borderline roles, and clear rejects. Show the reasons in everyday language and tune the scorecard from the candidate's job-related feedback.

Only then ask the candidate to choose an exact **private project folder outside any Git repository**. Explain that this holds their resume and search history, while the public skill code stays separate. Confirm the immutable `profile_id` and time zone. Use `scripts/init_profile.py`, copy the supplied resume into `sources/` if authorized, and fill the empty `sources/resume-facts.json` from verified material. Do not create fake examples as personal facts.

Run `scripts/doctor.py` and walk through any missing tools. Gmail connection and scheduling are separate setup steps. Keep both paused until a controlled test and exact sender, recipient, local delivery time, and recurring-delivery approvals are complete. A test email requires its own explicit approval; it is not implied by onboarding.

## Isolation and identity

Create exactly one separate project per person. The project must not be a Git repository and must not be nested in a shared candidate project. Do not create a registry, index, cross-project lookup, or discovery mechanism. The caller must supply the exact project root on every operation.

Create `profile_id` once from an approved stable identifier. It is immutable: never rename, merge, recycle, or silently repair it. Every configuration document and state row carries the same `profile_id`; a mismatch stops the workflow.

Use `scripts/init_profile.py` to create a new project and copy the generic email asset. It also creates an empty resume-facts draft and PDF output folder. Refuse to overwrite an existing populated directory. Use `scripts/validate_profile.py` after every material refresh.

## Evidence and conflict checker

Classify each intake fact as one of:

- candidate-confirmed;
- candidate-provided artifact;
- public-source observation;
- relayed and provisional.

Relayed facts remain provisional until confirmed directly by the candidate. Before combining sources or updating the candidate profile, compare identity, desired roles, location/work model, compensation, constraints, exclusions, and timeline. If sources conflict, preserve both claims with provenance, stop the affected decision, and ask the candidate to resolve it. Do not choose the more convenient claim.

Never collect or infer protected traits. Exclude protected characteristics and proxies from sourcing, scoring, filtering, feedback learning, and delivery.

## Calibration and readiness

Draft a scorecard with five to seven job-relevant dimensions totaling 100 weight points. Run an 8–12-role calibration set spanning obvious fits, borderline roles, and clear rejects. Show the role list, evidence, band, and key reason to the candidate. Adjust only from job-relevant feedback, then obtain candidate confirmation of the profile and scorecard.

Keep all recurring tasks paused while onboarding. A project can become `ready` only after configuration and conflict checks pass. It can become `active` only after exact, candidate-specific approvals are stored as described in [delivery-and-run-guards.md](delivery-and-run-guards.md).

## Refresh

Refresh only the named project. Preserve `profile_id`, append history, re-run conflicts, recalibrate when preferences materially change, and invalidate approval snapshots whose sender, recipient, cadence, profile, or approved content no longer matches. A refresh never activates or sends by implication.
