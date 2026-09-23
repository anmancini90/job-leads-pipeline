# Sourcing

## Allowed sources

Use public, read-only job and company sources: direct employer career pages, public applicant-tracking-system listings, and public job indexes. Prefer a canonical employer or ATS URL over an aggregator URL.

Public LinkedIn pages may be read only. Do not sign in, create an account, bypass access controls, scrape behind authentication, send connection requests, or contact anyone. When public LinkedIn access fails or the needed profile evidence is not public, request candidate-provided PDF or text and record it as candidate-provided evidence.

Do not upload a resume, submit an application, create an employer account, message a recruiter or employer, or act as the candidate.

## Search and evidence

Load the candidate, search plan, scorecard, and prior evaluated-role state from the one supplied project. Search the approved role families, locations, work models, salary constraints, sources, and exclusions. Do not broaden a hard constraint without candidate approval.

For each lead, capture source, canonical link, company, role, location, exact work model, visible compensation, date found, evidence for fit, and the main concern. Always make the candidate-facing work model explicit as fully remote, hybrid, onsite, or unclear. Always state the location. Always state the compensation amount or say that pay was not listed in the job post; never hide missing pay.

Also write a candidate-facing `day_to_day` summary. Use one or two short sentences at about a fifth-grade reading level. Translate the job post into concrete actions the person may do during a normal day so the candidate can picture the work. Use simple verbs, explain or remove jargon and acronyms, and say “likely” or “may” when the description requires an inference. Do not invent duties. Keep `why_it_fits` and `main_concern` to one plain-language sentence each.

Use `scripts/state_io.py` to normalize dedupe keys. Check both pipeline and evaluated-role history so rejected or sent roles are not rediscovered as new.

When the project defines a daily-funnel schema/helper, persist every discovered role—not only finalists—with its normalized source lane, query ID, dedupe result, hard-gate result, scoring stage, rejection reason, link status, and delivery result. The completed artifact must reconcile searched → deduplicated → gated → scored → qualified → verified → delivered counts and must be included as staged delivery evidence.

Manual hot-lead searches use the same rules but do not mutate cadence, approvals, or automation status. Present findings as leads for the candidate to review, not as endorsements from an employer.

## Mandatory live-listing verification

Search snippets, cached indexes, aggregators, and previously captured page text may help discover a role, but they never prove that it is currently open. Validate the scored and deduplicated candidate pool first with `scripts/validate_job_links.py <project-root> <candidate-pool-json> --mode candidate-pool --evidence-out <pool-evidence-json> --passing-out <passing-leads-json>`. This mode classifies every lead as `deliverable` or `withheld`, preserves a reason code and evidence for every decision, and writes a payload containing only verified leads. A mixed pool may proceed with its passing subset. When no lead passes, use `pool_outcome`: `no_deliverable_leads` means every rejection was definitive, while `validation_incomplete` means at least one malformed, blocked, failed, or ambiguous check prevented a trustworthy completed-search conclusion.

Immediately before rendering or delivery, run the validator again in its default `strict-final` mode against the exact passing payload. Strict-final mode requires at least one lead and rejects the entire payload if any lead fails. Candidate-pool mode is a filtering step, never a substitute for this final fail-closed gate.

Each delivered lead must pass all of these checks:

- the exact detail URL loads successfully over HTTPS and does not resolve to a generic careers board;
- the current employer/ATS source still lists the exact role under the expected company or board;
- an application action or active ATS application URL exists;
- the page does not say that the job is missing, closed, filled, expired, or no longer accepting applications; and
- the validation result records the check time, final URL, method, and evidence.

Use the ATS's public job-board API when it is authoritative for a client-rendered detail page. The validator dispatches recognized hosts to stricter adapters: Ashby uses its public board API; Workday uses the exact tenant, board, and job path through public CXS and requires matching job/board identifiers plus `posted: true`, `canApply: true`, and a matching same-origin external URL; Zoom requires the exact detail page and follows only its recognized same-origin lazy apply fragment, whose POST form must bind to the same job identifier. Redirects, fragments, and application actions that cross origins fail closed. Otherwise load the final page itself. If access is blocked, the page is ambiguous, the role/company cannot be matched, or the application action cannot be proved, fail closed and exclude the role. Re-run this gate after scoring and deduplication so the check covers the exact payload being sent.
