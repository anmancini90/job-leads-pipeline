#!/usr/bin/env python3
"""Validate one isolated job-leads project and its activation guards."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PIPELINE_HEADERS = [
    "profile_id", "date_found", "date_sent", "status", "company", "role",
    "link", "source", "score", "tier", "archetype", "excitement_modifier",
    "salary", "work_model", "location_details", "benefits_visible",
    "why_it_fits", "main_concern", "suggested_action", "candidate_verdict",
    "reason_codes", "candidate_notes", "operator_notes", "next_action",
    "last_checked",
]
EVALUATED_HEADERS = [
    "profile_id", "date_evaluated", "company", "role", "link", "source",
    "disposition", "band_or_reject_reason", "score", "notes",
]
FEEDBACK_HEADERS = [
    "profile_id", "date_received", "role_ref", "company",
    "candidate_verdict", "reason_codes", "quote_code", "source",
    "applied_to_model",
]

CONFIG_FILES = (
    "profile.json",
    "candidate.json",
    "scorecard.json",
    "search-plan.json",
    "automation.json",
)
CSV_SCHEMAS = {
    "pipeline.csv": PIPELINE_HEADERS,
    "evaluated-roles.csv": EVALUATED_HEADERS,
    "feedback-log.csv": FEEDBACK_HEADERS,
}
APPROVALS = (
    "sender_owner",
    "candidate_profile",
    "live_delivery",
    "automation_activation",
)
SECRET_KEY_PATTERN = re.compile(
    r"(?:secret|password|passwd|token|api[_-]?key|credential|authorization|private[_-]?key|cookie)",
    re.IGNORECASE,
)
ALLOWED_STATUSES = {"draft", "ready", "active", "paused", "revoked", "archived"}
APPROVAL_MAX_AGE = timedelta(days=30)


def _read_json(path: Path, errors: list[str]) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"{path.name}: required configuration file is missing")
    except (json.JSONDecodeError, OSError) as exc:
        errors.append(f"{path.name}: invalid JSON ({exc})")
    return None


def _walk_secret_keys(value: object, prefix: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if SECRET_KEY_PATTERN.search(key_text):
                yield path
            yield from _walk_secret_keys(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_secret_keys(item, f"{prefix}[{index}]")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _inside_project(root: Path, configured: object) -> bool:
    if not isinstance(configured, str) or not configured.strip():
        return False
    candidate = Path(configured)
    if candidate.is_absolute():
        return False
    try:
        (root / candidate).resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _is_iana_timezone(value: object) -> bool:
    if not isinstance(value, str) or not value or "/" not in value:
        return False
    try:
        ZoneInfo(value)
        return True
    except ZoneInfoNotFoundError:
        catalog = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "iana-timezones.txt"
        )
        try:
            return value in set(catalog.read_text(encoding="utf-8").splitlines())
        except OSError:
            return False
    except ValueError:
        return False


def _validate_csv(
    path: Path,
    expected_headers: list[str],
    profile_id: object,
    errors: list[str],
) -> None:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            headers = next(reader, None)
            rows = list(reader)
    except OSError as exc:
        errors.append(f"{path.name}: could not read state CSV ({exc})")
        return
    if headers != expected_headers:
        errors.append(
            f"{path.name}: header must exactly match the required schema with profile_id first"
        )
        return
    for row_number, row in enumerate(rows, start=2):
        if len(row) != len(expected_headers):
            errors.append(
                f"{path.name}: row {row_number} column count must be exactly {len(expected_headers)}"
            )
            continue
        if not row or row[0] != profile_id:
            actual = row[0] if row else ""
            errors.append(
                f"{path.name}: row {row_number} profile_id {actual!r} does not match {profile_id!r}"
            )


def _valid_tier_thresholds(value: object) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    ranges: list[tuple[float, float]] = []
    for tier, bounds in value.items():
        if not isinstance(tier, str) or not tier.strip() or not isinstance(bounds, dict):
            return False
        minimum = bounds.get("min")
        maximum = bounds.get("max")
        if (
            isinstance(minimum, bool)
            or isinstance(maximum, bool)
            or not isinstance(minimum, (int, float))
            or not isinstance(maximum, (int, float))
            or minimum < 0
            or maximum > 100
            or minimum > maximum
        ):
            return False
        ranges.append((float(minimum), float(maximum)))
    ranges.sort()
    if ranges[0][0] != 0 or ranges[-1][1] != 100:
        return False
    return all(
        current_min == previous_max + 1
        for (_, previous_max), (current_min, _) in zip(ranges, ranges[1:])
    )


def validate_project(
    project_root: str | Path,
    *,
    now: datetime | None = None,
) -> list[str]:
    root = Path(project_root).expanduser()
    errors: list[str] = []
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    configs: dict[str, object] = {}
    for filename in CONFIG_FILES:
        value = _read_json(root / filename, errors)
        if value is not None:
            if not isinstance(value, dict):
                errors.append(f"{filename}: top-level JSON must be an object")
            else:
                configs[filename] = value
                for key_path in _walk_secret_keys(value):
                    errors.append(
                        f"{filename}: secret-like field {key_path!r} is forbidden"
                    )

    profile = configs.get("profile.json", {})
    profile_id = profile.get("profile_id") if isinstance(profile, dict) else None
    if not isinstance(profile_id, str) or not profile_id:
        errors.append("profile.json: profile_id must be nonempty and immutable")

    if isinstance(profile, dict):
        status = profile.get("status")
        if status not in ALLOWED_STATUSES:
            errors.append("profile.json: status is outside the exact lifecycle")
        timezone_name = profile.get("timezone")
        if not _is_iana_timezone(timezone_name):
            errors.append("profile.json: timezone must be a valid IANA timezone")

        paths = profile.get("paths")
        if not isinstance(paths, dict):
            errors.append("profile.json: paths must be a project-relative map")
        else:
            for path_name, configured in paths.items():
                if not _inside_project(root, configured):
                    errors.append(
                        f"profile.json: {path_name} path must remain inside the project"
                    )

    for filename in CONFIG_FILES[1:]:
        config = configs.get(filename)
        if isinstance(config, dict) and config.get("profile_id") != profile_id:
            errors.append(
                f"{filename}: profile_id does not match profile.json"
            )

    profile_status = profile.get("status") if isinstance(profile, dict) else None
    candidate = configs.get("candidate.json", {})
    scorecard = configs.get("scorecard.json", {})
    dimensions = scorecard.get("dimensions") if isinstance(scorecard, dict) else None
    if not isinstance(dimensions, list) or not 5 <= len(dimensions) <= 7:
        errors.append("scorecard.json: dimension count must be between 5 and 7")
    if isinstance(dimensions, list):
        weights = [
            item.get("weight") if isinstance(item, dict) else None
            for item in dimensions
        ]
        if any(
            isinstance(weight, bool) or not isinstance(weight, int)
            for weight in weights
        ):
            errors.append("scorecard.json: every dimension weight must be an integer")
        if any(
            isinstance(weight, bool) or not isinstance(weight, (int, float))
            for weight in weights
        ) or sum(
            weight
            for weight in weights
            if not isinstance(weight, bool) and isinstance(weight, (int, float))
        ) != 100:
            errors.append("scorecard.json: dimension weights must total 100")

    if profile_status == "active" and isinstance(scorecard, dict):
        if not _valid_tier_thresholds(scorecard.get("tier_thresholds")):
            errors.append(
                "scorecard.json: active scorecard requires valid tier thresholds covering 0 through 100"
            )
        calibration = scorecard.get("calibration")
        if not isinstance(calibration, dict):
            errors.append(
                "scorecard.json: active scorecard requires calibration evidence"
            )
        else:
            if calibration.get("status") != "confirmed":
                errors.append(
                    "scorecard.json: calibration status must be confirmed"
                )
            if calibration.get("profile_id") != profile_id:
                errors.append(
                    "scorecard.json: calibration profile_id must match the active profile"
                )
            candidate_email = (
                candidate.get("email") if isinstance(candidate, dict) else None
            )
            if calibration.get("confirmed_by") != candidate_email:
                errors.append(
                    "scorecard.json: calibration confirmed_by must match the candidate email"
                )
            provenance = calibration.get("provenance")
            if not isinstance(provenance, str) or not provenance.strip():
                errors.append(
                    "scorecard.json: calibration requires nonempty candidate provenance"
                )
            role_count = calibration.get("role_count")
            if (
                isinstance(role_count, bool)
                or not isinstance(role_count, int)
                or not 8 <= role_count <= 12
            ):
                errors.append(
                    "scorecard.json: calibration role_count must be an integer between 8 and 12"
                )

    state_root = root / "state"
    for filename, schema in CSV_SCHEMAS.items():
        _validate_csv(state_root / filename, schema, profile_id, errors)

    automation = configs.get("automation.json", {})
    if isinstance(automation, dict):
        if profile_status == "active":
            if automation.get("status") != "active":
                errors.append("automation.json: active profile requires active automation status")
            for field in ("sender", "to", "cadence"):
                if not isinstance(automation.get(field), str) or not automation.get(field).strip():
                    errors.append(
                        f"automation.json: active delivery requires nonempty {field}"
                    )
            candidate_email = (
                candidate.get("email") if isinstance(candidate, dict) else None
            )
            if not isinstance(candidate_email, str) or not candidate_email.strip():
                errors.append(
                    "candidate.json: active candidate email must be nonempty"
                )
            if (
                isinstance(candidate_email, str)
                and candidate_email.strip()
                and automation.get("to") != candidate_email
            ):
                errors.append("automation.json: to must match the candidate email")

            approvals = automation.get("approvals")
            if not isinstance(approvals, dict):
                approvals = {}
            one_test_only = automation.get("cadence") == "one-test-only"
            required_approvals = APPROVALS
            if one_test_only:
                required_approvals = (
                    "sender_owner",
                    "candidate_profile",
                    "live_delivery",
                    "one_test_delivery",
                )
                test_mode = automation.get("test_mode")
                if not isinstance(test_mode, dict):
                    errors.append(
                        "automation.json: one-test-only requires bounded test_mode"
                    )
                else:
                    if test_mode.get("max_deliveries") != 1:
                        errors.append(
                            "automation.json: one-test-only max_deliveries must equal 1"
                        )
                    if test_mode.get("recurring_schedules_paused") is not True:
                        errors.append(
                            "automation.json: one-test-only requires recurring schedules paused"
                        )
                    confirmation_message_id = test_mode.get(
                        "confirmation_message_id"
                    )
                    if (
                        not isinstance(confirmation_message_id, str)
                        or not confirmation_message_id.strip()
                    ):
                        errors.append(
                            "automation.json: one-test-only requires confirmation message provenance"
                        )
                    if test_mode.get("consumed") is not False:
                        errors.append(
                            "automation.json: one-test-only approval is already consumed"
                        )
                recurring_approval = approvals.get("automation_activation")
                if (
                    isinstance(recurring_approval, dict)
                    and recurring_approval.get("approved") is True
                ):
                    errors.append(
                        "automation.json: one-test-only must not approve recurring activation"
                    )
            snapshots = {
                "sender_owner": {"sender": automation.get("sender")},
                "candidate_profile": {"profile_id": profile_id},
                "live_delivery": {
                    "sender": automation.get("sender"),
                    "to": automation.get("to"),
                    "cadence": automation.get("cadence"),
                },
                "automation_activation": {
                    "sender": automation.get("sender"),
                    "to": automation.get("to"),
                    "cadence": automation.get("cadence"),
                },
                "one_test_delivery": {
                    "sender": automation.get("sender"),
                    "to": automation.get("to"),
                    "cadence": automation.get("cadence"),
                    "max_deliveries": 1,
                },
            }
            for approval_name in required_approvals:
                approval = approvals.get(approval_name)
                if not isinstance(approval, dict):
                    errors.append(
                        f"automation.json: active profile requires {approval_name} approval"
                    )
                    continue
                if approval.get("approved") is not True:
                    errors.append(
                        f"automation.json: {approval_name} must be explicitly approved"
                    )
                if not isinstance(approval.get("approved_by"), str) or not approval.get("approved_by").strip():
                    errors.append(
                        f"automation.json: {approval_name} requires approved_by provenance"
                    )
                approved_at = _parse_timestamp(approval.get("approved_at"))
                if (
                    approved_at is None
                    or approved_at > now_utc + timedelta(minutes=5)
                    or now_utc - approved_at > APPROVAL_MAX_AGE
                ):
                    errors.append(
                        f"automation.json: {approval_name} approval is stale or has an invalid timestamp"
                    )
                for field, expected in snapshots[approval_name].items():
                    if approval.get(field) != expected:
                        errors.append(
                            f"automation.json: {approval_name} {field} snapshot does not match"
                        )
        elif automation.get("status") == "active":
            errors.append(
                "automation.json: automation must remain paused unless profile status is active"
            )

        if automation.get("include_raw_score") is False:
            template = root / "templates" / "daily-email.html"
            try:
                template_text = template.read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(f"daily-email.html: could not read template ({exc})")
            else:
                if re.search(
                    r"{{\s*(?:raw[_-]?)?(?:numeric[_-]?)?score\b[^}]*}}",
                    template_text,
                    re.IGNORECASE,
                ):
                    errors.append(
                        "daily-email.html: raw score placeholder is forbidden by the email template configuration"
                    )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    args = parser.parse_args()
    errors = validate_project(args.project_root)
    print(json.dumps({"valid": not errors, "errors": errors}, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
