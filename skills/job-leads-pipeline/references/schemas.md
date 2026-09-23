# Project schemas

All paths are relative to one supplied project root and must resolve inside it. State helpers derive the initialized project from the supplied path, require the exact `state/` directory and matching `profile.json` path entry, and reject outside or cross-profile writes. All JSON configuration files and every state row carry the same immutable `profile_id`.

## Configuration

- `profile.json`: `profile_id`, `display_name`, IANA `timezone`, lifecycle `status`, and the relative `paths` map.
- `candidate.json`: `profile_id`, candidate-confirmed display/preferred name and delivery address, plus approved job-relevant preferences.
- `scorecard.json`: `profile_id`, five to seven integer-weighted dimensions totaling 100, anchors, explicit tier thresholds, and confirmed candidate calibration provenance with an 8–12 `role_count` before activation.
- `search-plan.json`: `profile_id`, approved public sources, queries, constraints, and exclusions.
- `automation.json`: `profile_id`, paused/active automation status, exact nonempty sender, candidate recipient, cadence, `include_raw_score`, and approval snapshots. An active candidate email is nonempty and exactly equals `automation.to`. A candidate-approved one-time test uses cadence `one-test-only`, a one-delivery `test_mode`, and `one_test_delivery` approval instead of falsely recording recurring activation.
- `sources/resume-facts.json`: starts as a draft with the matching `profile_id`, empty `identity`, `education`, `history`, `facts`, and `sources`. Onboarding fills these only from candidate-confirmed or checked candidate-provided material. Drafting is blocked while required facts or source proof are missing.

Never store passwords, tokens, API keys, authorization headers, private keys, cookies, or other credentials in any configuration, state, prompt, or receipt.

## State

`state/pipeline.csv` exact header order:

```text
profile_id,date_found,date_sent,status,company,role,link,source,score,tier,archetype,excitement_modifier,salary,work_model,location_details,benefits_visible,why_it_fits,main_concern,suggested_action,candidate_verdict,reason_codes,candidate_notes,operator_notes,next_action,last_checked
```

`state/evaluated-roles.csv` exact header order:

```text
profile_id,date_evaluated,company,role,link,source,disposition,band_or_reject_reason,score,notes
```

`state/feedback-log.csv` exact header order:

```text
profile_id,date_received,role_ref,company,candidate_verdict,reason_codes,quote_code,source,applied_to_model
```

`state/run-ledger.jsonl` is append-only, one JSON event per line. `state/missed-run-alerts.json` is a list deduplicated by profile and local date. `outputs/` holds dated render artifacts, manifests, link evidence, and send receipts; `output/pdf/` holds role-specific resume PDFs and their checks; `archive/` holds retained terminal history. `templates/daily-email.html` is copied from the reusable generic asset at initialization.

Each v2 delivery manifest is an immutable JSON object with `manifest_version: 2`, `profile_id`, `local_date`, `run_type`, `idempotency_key`, exact `sender` and `recipient`, nonempty `subject`, one of `lead_email`, `no_deliverable_leads_email`, or `service_status_email` as `outcome`, nonempty `reason_code`, nonnegative integer `lead_count`, and a `counts` object containing nonnegative integer `searched`, `qualified`, `verified`, and `withheld` values. `evidence_paths` is a list of existing project-relative files inside `outputs/`. `artifacts` contains `payload`, `html`, and `text` objects, each with an existing project-relative `path` inside `outputs/` and the exact lowercase `sha256` of that file. A lead outcome has at least one lead; both non-lead outcomes have zero leads.

A v2 send receipt has `receipt_version: 2` plus the run identity and sent status, Gmail `message_id` and `thread_id`, `manifest_sha256`, `claim_token`, exact `sender` and `recipient`, `outcome`, `reason_code`, and nonnegative integer `lead_count`. Its binding fields exactly match the preserved v2 claim. Legacy receipts containing only the original identity, status, and message ID remain readable for recovery; partial v2 receipts are invalid.

V2 claim files contain the idempotency key, immutable manifest path and SHA-256, sender, recipient, outcome, lead count, claim token, current attempt token, process/thread identity, and timestamp. A sibling provider-state file may record only `rejected_safe_to_retry`, `retry_in_progress`, or `provider_ambiguous`, bound to the same claim, manifest, and attempt tokens. Provider errors and statuses must not contain credentials or response bodies with secrets.

Every CSV data row has exactly the header's column count. `state/run-claims/` contains atomic per-run delivery claims and provider-attempt state and is runtime state, not configuration. Dates in keys, ledger events, manifests, claims, receipts, and alerts use calendar-valid `YYYY-MM-DD` values. At most one valid candidate-facing receipt may exist for a profile and local date.
