# Delivery, lifecycle, and run guards

## Lifecycle

The exact lifecycle is `draft → ready → active → paused/revoked → archived`.

- `draft`: intake or calibration incomplete; automation is paused.
- `ready`: configuration is validated, but recurring delivery remains paused pending exact approvals.
- `active`: recurring delivery is allowed only under the stored approval snapshots.
- `paused`: reversible stop; no sends. Resume revalidates configuration and all approvals.
- `revoked`: approval has been withdrawn; no sends. Activation requires new approval provenance, never reuse of revoked approval.
- `archived`: terminal, read-only history; no sourcing, sync, scheduling, or delivery.

Only follow these edges. `active` may move to `paused` or `revoked`; either may move to `archived`. A paused project may return to `active` after revalidation. A revoked project must obtain a new candidate-specific approval set before returning to `active`. Do not unarchive by inference.

## Approval provenance and activation

Create recurring tasks paused. Before activation, validate the project and store four approvals with `approved: true`, a fresh ISO-8601 `approved_at`, and nonempty `approved_by`:

- `sender_owner`: exact `sender` snapshot and authorization to use it;
- `candidate_profile`: exact immutable `profile_id` and candidate confirmation of the profile/scorecard;
- `live_delivery`: exact `sender`, candidate `to` address, and `cadence` snapshot;
- `automation_activation`: exact `sender`, candidate `to` address, and `cadence` snapshot approving recurring activation.

Approvals are candidate-specific and cannot be copied across projects. A snapshot mismatch, stale or future timestamp, recipient mismatch, missing field, profile refresh affecting approved facts, pause caused by configuration change, or revocation blocks activation. Candidate-specific approval must be obtained before recurring live delivery. Sender authorization does not substitute for candidate consent.

### One-test-only delivery

When the candidate explicitly approves one test but explicitly denies recurring activation, do not create a false `automation_activation` approval. Use cadence `one-test-only`, keep every recurring scheduler record paused, and store a bounded `test_mode` with `max_deliveries: 1`, `recurring_schedules_paused: true`, the direct candidate confirmation message ID, and `consumed: false`. Require `sender_owner`, `candidate_profile`, and `live_delivery` plus a `one_test_delivery` snapshot containing the exact sender, recipient, cadence, and `max_deliveries: 1`.

Only run type `one-test-only` is eligible under that configuration. After provider confirmation, immediately write the receipt and completion event, mark the test approval consumed, and transition the profile and automation to `paused`. Never activate a recurring scheduler record as part of a one-test run.

Scheduled prompts must be minimal: the exact project root or project identifier, the mode, the intended local date, and an instruction to load `$job-leads-pipeline`. Do not embed candidate details, email bodies, credentials, or scoring logic in the prompt.

## Idempotency and recovery

Use `profile_id:local_date:run_type` as the run idempotency key, with a real calendar date. Recovery retries use the original daily key; they do not create a recovery-specific delivery identity. Append run events; never rewrite prior ledger events. An exact retry of the same keyed event is a no-op; the same event identity with a different payload is a conflict that stops the run.

Run `scripts/validate_run.py <root> <profile-id> <local-date> <run-type>` before sourcing. This default preflight is read-only: it checks activation, all receipts for the local date, ledger completions, claims, and retry state, and it never creates a claim. `should_send` is always false during preflight. Only `status: ready` permits sourcing and staging. A valid candidate-facing receipt for any run type on the same local date blocks another send, enforcing one candidate-facing email per date.

For a ready run, record non-authoritative progress with `scripts/run_attempt_state.py`. Transition `started → searching → search_complete → staged`; refresh `--heartbeat` only while the run is actively started/searching. The `search_complete` transition carries all seven funnel counts: searched, deduplicated, gated, scored, qualified, verified, and delivered. After rendering and manifest creation, transition to `staged` with the exact manifest path/SHA plus `--candidate-pool-complete`, `--links-validated`, and `--rendering-complete`; also pass `--zero-lead-evidence-complete` for a zero-lead outcome. Use `pre_claim_failed` with a failure class for safe pre-claim failures and `blocked` for ambiguity. Attempt state never authorizes delivery and never substitutes for a claim, receipt, or ledger event.

- Valid sent receipt plus matching completion event: complete; do nothing.
- Valid sent receipt without completion state: never resend; recover by writing state only.
- Completion state without a valid receipt: never resend automatically; investigate and reconcile the receipt.
- No receipt or completion state: the run may proceed only if lifecycle and approvals pass.
- Ambiguous, malformed, or mismatched state: stop for manual reconciliation.

After sourcing, link validation, rendering, and manifest creation all succeed, acquire the final claim with `validate_run.py --acquire-claim` and the exact manifest path, SHA-256, outcome, lead count, sender, and recipient. The claim API recomputes the manifest hash and requires every binding value to match the manifest. Only this explicit operation can return `should_send: true`, and only when `local_date` equals today's date in the candidate's approved IANA timezone from `profile.json`. The computer's own timezone does not decide the claim date; historical and future candidate-local dates are inspection-only.

Claim acquisition also verifies the exact per-run staged attempt. Missing search completion, candidate-pool evidence, final-link evidence, zero-lead evidence when applicable, rendering completion, or manifest binding fails before any claim is created.

Only the claim winner may deliver. Concurrent callers report `in_progress` and wait while a recent attempt is active. Claims record the immutable manifest binding, process ID, thread identity, base claim token, and attempt token. Runtime code never deletes or reclaims a claim based on age or cached liveness. An old claim with no provable result becomes `manual_reconciliation_required`; it never creates a second winner.

An explicit provider rejection is retryable only when the provider proves the request was rejected before acceptance. Record `rejected_safe_to_retry` against the exact claim and attempt tokens; a retry must reacquire an attempt token for the identical manifest hash and binding. A timeout, connection loss after submission, missing provider response, or any uncertain acceptance is `provider_ambiguous`; preserve the claim and reconcile manually. Never rebuild or alter the manifest during a provider retry.

Write the v2 send receipt immediately after provider confirmation, including the manifest hash, base claim token, exact sender and recipient, outcome, reason code, lead count, and Gmail message/thread IDs, then append completion state. On a retry, a receipt always wins over an incomplete ledger and blocks a duplicate send. Legacy receipts with the original identity/status/message fields remain valid for state-only recovery; any receipt containing a v2 field must satisfy the complete v2 schema and match its claim.

Record at most one missed-run alert for each `profile_id` and valid local calendar date. Alert updates use a bounded local lock and a unique temporary file replaced atomically so concurrent observers do not lose updates. Locks record process, thread, and token; Windows access-denied errors are contention. Runtime code never deletes or reclaims a pre-existing alert or ledger lock, even when it is old or its owner appears dead. Bounded timeout raises `manual_reconciliation_required` and preserves the exact marker and token. The acquiring thread may release only the lock carrying its own token. Ledger idempotency checks and appends use the same rule. A checker only diagnoses and alerts; it does not perform an unapproved catch-up send.

Resolve an alert by appending a `missed_run_alert_resolved` ledger event with the exact receipt path and completion-event identity; never delete the original alert. Legacy receipt compatibility is project-local, exact-hash, audited, and read-only. It may classify one receipt as the historical primary and preserved extras as historical additions, but it never rewrites receipt bytes or weakens duplicate blocking for new deliveries.

## Manual reconciliation

Treat every preserved claim or lock as evidence that an operation may have crossed an irreversible boundary. Do not delete it merely to unblock the next run.

For a delivery claim, compare the exact profile, sender, candidate recipient, local date, and run type against the provider's Sent-mail record and this project's receipt and ledger. If Sent mail proves delivery occurred, write or validate the send receipt and append only the missing completion state; never resend. If delivery is inconclusive, keep the claim and stop. Only after an authorized operator proves no delivery occurred may they preserve a copy of the marker, verify its exact token has not changed, manually remove that exact claim, and rerun validation.

For an alert or ledger lock, verify no writer is active, preserve the marker and affected state for audit, validate the JSON/JSONL or CSV boundary, and confirm the token has not changed immediately before an authorized manual removal. If the token changes or state is ambiguous, stop. Manual recovery never broadens delivery approval or permits an automatic catch-up send.

## Delivery

Validate the entire project immediately before staging and again during claim acquisition. A claim operation returns `should_send: true` only when both profile and automation are active, candidate email is nonempty and exactly matches the recipient, scorecard calibration is confirmed, all four current approval snapshots exactly match sender/recipient/cadence/profile, the date has no prior delivery, and idempotency state permits the bound manifest.

After scoring and deduplication, run `scripts/validate_job_links.py <project-root> <final-leads-json> --evidence-out <project-relative-evidence-json>`. This is a fail-closed pre-send gate: every lead in the exact final payload must pass. A dead page, generic-board redirect, missing role, missing application action, blocked request, unsupported ambiguous page, or validation error rejects the entire set. Do not render, send, or claim that a job is live when this gate fails. Discovery indexes and cached search text cannot satisfy the gate.

Render untrusted job data with `scripts/render_email.py`; links and all text must be escaped. Keep raw numeric scores hidden unless explicitly enabled. Store provider receipt identifiers for constrained feedback sync. Store the passing link-evidence artifact beside the run outputs and reference it from the send receipt.

Every completed eligible search produces exactly one staged outcome: `lead_email`, `no_deliverable_leads_email`, or `service_status_email`. A completed search whose candidates were all rejected or withheld is a normal `no_deliverable_leads_email`; its candidate-safe reason must state the searched, qualified, verified, and withheld counts without claiming the search found no jobs. An incomplete search never uses that outcome. Retry incomplete pre-send work hourly through the configured Eastern cutoff; at the cutoff, stage `service_status_email` if recovery delivery is candidate-approved, otherwise alert the operator without sending.
