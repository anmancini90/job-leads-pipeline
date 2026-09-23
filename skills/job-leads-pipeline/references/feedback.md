# Feedback synchronization

Accept feedback only when all of these match the named project:

1. The sender is the candidate address stored in `candidate.json`.
2. The conversation or message ID is one already stored by this project in a send receipt or delivery state.
3. The referenced role resolves unambiguously to this project's pipeline or evaluated-role state.

Do not search a mailbox broadly, ingest other threads, infer identity from display name, or accept forwarded and relayed comments as confirmed candidate feedback. Relayed facts remain provisional.

Append accepted feedback to `state/feedback-log.csv` with `profile_id`, date, role reference, candidate verdict, controlled reason codes, source, and whether it was applied. Preserve the original delivery/message reference without copying unnecessary mailbox content. Do not store access tokens or credentials.

Before applying feedback, run the conflict checker from [scoring.md](scoring.md). A clear per-role verdict can update disposition. Changes to durable preferences or scoring require evidence across examples or explicit candidate confirmation. Feedback synchronization never sends mail or activates automation as a side effect.
