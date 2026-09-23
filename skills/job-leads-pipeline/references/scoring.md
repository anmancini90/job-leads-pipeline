# Scoring and calibration

## Scorecard

Use five to seven job-relevant dimensions whose weights are integers totaling 100. Each dimension needs observable anchors for strong, partial, and absent evidence. Score only evidence available in the job listing, approved company sources, and the candidate-confirmed profile.

Never infer or score protected traits, health, family status, age, ethnicity, religion, disability, sexuality, political affiliation, or proxies for them. Do not treat missing public data as negative evidence about the candidate.

Apply hard constraints first and record a specific rejection reason. For eligible roles, preserve dimension evidence and compute the configured internal score. Map the result to candidate-facing bands or tiers. Unless `include_raw_score` is explicitly true, candidate delivery may show the tier and rationale but not the raw numeric score.

## Calibration

Before recurring live delivery, evaluate 8–12 representative roles: several likely fits, several borderline cases, and several clear rejects. Check that the ordering, thresholds, and rationales match candidate intent. Record candidate verdicts and reason codes, then revise only the dimensions or anchors supported by that evidence.

An active scorecard must contain explicit, valid tier thresholds covering the scoring range without gaps or overlaps. Its calibration record must have `status: confirmed`, the exact `profile_id`, `confirmed_by` equal to the candidate email, nonempty candidate provenance, and an integer `role_count` from 8 through 12. Draft projects may omit these activation-only fields.

The conflict checker runs before a scorecard change: compare new feedback with confirmed constraints, other recent feedback, and the evidence in the referenced role. Contradictory, relayed, ambiguous, or identity-mismatched feedback is held for clarification rather than learned automatically.

Weekly calibration summarizes false positives, false negatives, repeated reason codes, and source quality. Suggest changes with their affected examples. Do not silently change hard constraints, weights, tier boundaries, delivery settings, or approvals.
