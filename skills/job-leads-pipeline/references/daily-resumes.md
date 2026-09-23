# Daily leads with source-checked resumes

This extends the generic lead pipeline. It emails the candidate, never an employer. Read this whole reference before a daily candidate email. Use the exact private project root supplied by the candidate; never search for another person's project. Before any send, also follow [delivery-and-run-guards.md](delivery-and-run-guards.md).

## Run graph

`feedback/conflict check → [public sourcing and scoring || candidate fact preparation] → selection checker → frozen inputs → parallel role drafts → independent factual checker → parallel PDF builds and visual review → final link/index/package checker → staged attempt → guarded claim → one send → receipt → sent-message verification`

The selection checker rejects empty, unrelated, contradictory, stale, unsupported, or low-quality results. The factual reviewer must be a distinct actor from the drafter. A script can check structure and hashes, but it cannot truthfully approve the meaning of a résumé or a visual layout.

## Source-backed facts

During onboarding, the agent reads the candidate-provided résumé completely, may inspect a publicly accessible LinkedIn or portfolio page, and asks the candidate to resolve conflicts. LinkedIn is optional and read-only. Save the candidate's original file under `sources/`, and put a SHA-256-bound source registry and confirmed claims in `sources/resume-facts.json`. Each claim has a stable `id`, a plain factual `statement`, and nonempty `source_ids`. `history` holds canonical company, title, and dates. `identity` holds the candidate-confirmed name, email, and location; phone, portfolio, LinkedIn, and education are optional. Do not save passwords or OAuth tokens here. Do not use generated résumés, job descriptions, protected traits, or guessed metrics as facts.

If a source, candidate correction, title, or date changes, reconcile the facts before drafting. A role whose résumé cannot be supported is `unavailable`; the verified lead can still be delivered. A failed or ambiguous live job link, however, blocks the entire delivery.

## Freeze and draft

Run the shared preflight before sourcing. Source public employer and ATS pages, deduplicate and score them, then verify each exact final link with `validate_job_links.py`. Save final leads as a JSON list with `profile_id`, unique `role_ref`, exact `link`, `company`, and `role`. Save complete job-description evidence, not just a title or search snippet. Freeze inputs in a new revision directory:

```text
python scripts/prepare_daily_resume_inputs.py --root PRIVATE_PROJECT --local-date YYYY-MM-DD --leads outputs/YYYY-MM-DD-leads.json --evidence outputs/YYYY-MM-DD-job-evidence.json --out-dir outputs/YYYY-MM-DD-resume-work/r1
```

This creates `facts.json`, `candidate.json`, `leads.json`, source copies, and `input-snapshot.json` with exact hashes. Never change a frozen file; use `r2` for a correction. Draft a JSON file for each selected role under that revision's `drafts/`. It must contain `profile_id`, `role_ref`, exact `job_url`, `company`, `role`, a nonempty `drafted_by` actor ID, `headline`, `summary`, selected `roles` with canonical `history_id`/title/company/dates/bullets, `skills` pairs, and `claim_refs`. `claim_refs` must cover the headline, summary, every bullet, and both cells of every skill row with IDs from the frozen facts. Do not inflate or invent experience to fit a job.

An independent reviewer reads the actual frozen sources, facts, job description, and drafted prose. Only if factual, relevant, consistent, and good quality, the reviewer writes an approval file under `approvals/` containing `profile_id`, `role_ref`, `job_url`, `verdict: "pass"`, distinct `reviewer`, UTC `reviewed_at`, `checks` with those four booleans true, and exact `draft_sha256`, `facts_sha256`, and `input_snapshot_sha256`. The renderer rejects stale or self-approved drafts. A hash or reference alone is not evidence that the prose is true.

## Build and look at every PDF

Use the host's PDF authoring guidance when available. Build each passing role separately:

```text
python scripts/daily_resume_builder.py build --root PRIVATE_PROJECT --facts outputs/YYYY-MM-DD-resume-work/r1/facts.json --draft outputs/YYYY-MM-DD-resume-work/r1/drafts/ROLE.json --approval outputs/YYYY-MM-DD-resume-work/r1/approvals/ROLE.json --leads outputs/YYYY-MM-DD-resume-work/r1/leads.json --output-dir output/pdf/YYYY-MM-DD-daily-resumes/r1
```

The builder enforces one page, native selectable text, matching extracted text, source references, canonical history, exact role identity, and frozen approval hashes. It returns `needs_visual_review`, not `ready`. Render each new PDF to a PNG with `render_pdf_preview.py` and open that image. Reject clipped, overlapping, tiny, unreadable, or broken contact text. After real inspection, copy the builder's complete validation record into `outputs/` and add `pdf_sha256` plus `visual_review: {"passed": true, "reviewer": "actual reviewer", "reviewed_at": "UTC timestamp", "pdf_sha256": "exact PDF SHA-256"}`. Never let the drafter or renderer silently make this attestation. Use a new revision path for corrections; do not overwrite prior evidence.

## Package, stage, and send once

Re-run the exact final-link gate immediately before rendering/staging. Create `resume-index.json` with the run `profile_id`, `local_date`, input snapshot path/hash, and one entry per final lead in order. A `ready` entry has exact role fields, PDF `path`, display `filename`, `sha256`, `size_bytes`, `mime_type: "application/pdf"`, final `validation_path`, and its `validation_sha256`. An `unavailable` entry has exact role fields, `status: "unavailable"`, and a short candidate-safe `reason`; it has no attachment. Zero leads means an empty index.

Render the ordinary escaped email and create the shared v2 staging manifest with final payload, HTML/text, link evidence, counts, sender/recipient, and the original date's idempotency key. Then package exact attachments:

```text
python scripts/daily_resume_delivery.py prepare --root PRIVATE_PROJECT --manifest outputs/YYYY-MM-DD-staging-manifest.json --resume-index outputs/YYYY-MM-DD-resume-work/r1/resume-index.json --out-dir outputs/YYYY-MM-DD-resume-package/r1
python scripts/daily_resume_delivery.py validate --root PRIVATE_PROJECT --manifest outputs/YYYY-MM-DD-resume-package/r1/daily-resume-manifest.json
```

The package binds the source manifest, final lead cards, PDFs, reviewed facts/drafts/approvals, and a frozen structured Gmail request. Stage **that packaged manifest** through `run_attempt_state.py` with complete funnel counts, candidate-pool evidence, final links, rendering, and zero-lead evidence when applicable. Only after staging, call `daily_resume_delivery.py authorize-send` for the same manifest. The shared guard must return `should_send: true` for the exact immutable hash. The sender rechecks the frozen request bytes immediately before Gmail. A prior successful receipt blocks a second candidate email on the date.

Use [gmail.md](gmail.md) for Gmail setup and sending. Prefer the candidate's existing connector only if an actual capability check establishes exact binary attachments, a provider message ID, and read-back; otherwise use the candidate's own Gmail API OAuth credentials. Gmail authorization, a controlled test, and recurring activation each require explicit approval. Never put credentials in the project or schedule prompt. A timeout or uncertain acceptance is **not** permission to retry. Preserve the claim and reconcile Sent mail first.

After Gmail confirms acceptance, save the v2 receipt immediately, append the matching completion event, and inspect only the exact sent message. Compare attachment names, MIME types, sizes, and decoded bytes where available. If Google cannot supply original bytes, report partial verification, not full proof. Never resend to repair a missing read-back. Give the candidate the verified leads, PDF paths, unavailable reasons, and accurate send status in the host's file-link format.
