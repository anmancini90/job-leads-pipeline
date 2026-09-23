#!/usr/bin/env python3
"""Create one isolated, generic job-leads project."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PIPELINE_HEADERS = [
    "profile_id",
    "date_found",
    "date_sent",
    "status",
    "company",
    "role",
    "link",
    "source",
    "score",
    "tier",
    "archetype",
    "excitement_modifier",
    "salary",
    "work_model",
    "location_details",
    "benefits_visible",
    "why_it_fits",
    "main_concern",
    "suggested_action",
    "candidate_verdict",
    "reason_codes",
    "candidate_notes",
    "operator_notes",
    "next_action",
    "last_checked",
]

EVALUATED_HEADERS = [
    "profile_id",
    "date_evaluated",
    "company",
    "role",
    "link",
    "source",
    "disposition",
    "band_or_reject_reason",
    "score",
    "notes",
]

FEEDBACK_HEADERS = [
    "profile_id",
    "date_received",
    "role_ref",
    "company",
    "candidate_verdict",
    "reason_codes",
    "quote_code",
    "source",
    "applied_to_model",
]

PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _is_known_iana_timezone(value: str) -> bool:
    if "/" not in value:
        return False
    try:
        ZoneInfo(value)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        catalog = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "iana-timezones.txt"
        )
        try:
            return value in set(catalog.read_text(encoding="utf-8").splitlines())
        except OSError:
            return False


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _write_csv_header(path: Path, headers: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(headers)


def _inside_git_checkout(root: Path) -> bool:
    resolved = root.resolve()
    return any((parent / ".git").exists() for parent in (resolved, *resolved.parents))


def initialize_project(
    project_root: str | Path,
    profile_id: str,
    display_name: str,
    timezone: str,
) -> Path:
    """Initialize an empty project without candidate-specific defaults."""
    root = Path(project_root).expanduser().resolve()
    if not PROFILE_ID_PATTERN.fullmatch(profile_id):
        raise ValueError("profile_id must be lowercase kebab-case")
    if not display_name.strip():
        raise ValueError("display_name must not be empty")
    timezone_name = timezone.strip()
    if not timezone_name or not _is_known_iana_timezone(timezone_name):
        raise ValueError("timezone must be an IANA timezone name")
    if _inside_git_checkout(root):
        raise ValueError("candidate project must be outside every Git checkout")
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"project root is not empty: {root}")

    root.mkdir(parents=True, exist_ok=True)
    for relative in ("sources", "state", "outputs", "output/pdf", "archive", "templates"):
        (root / relative).mkdir(parents=True)

    paths = {
        "candidate": "candidate.json",
        "scorecard": "scorecard.json",
        "search_plan": "search-plan.json",
        "automation": "automation.json",
        "pipeline": "state/pipeline.csv",
        "evaluated_roles": "state/evaluated-roles.csv",
        "feedback_log": "state/feedback-log.csv",
        "run_ledger": "state/run-ledger.jsonl",
        "missed_run_alerts": "state/missed-run-alerts.json",
        "email_template": "templates/daily-email.html",
        "resume_facts": "sources/resume-facts.json",
        "resume_pdf": "output/pdf",
    }
    clean_name = display_name.strip()
    preferred_name = clean_name.split()[0]
    _write_json(
        root / "profile.json",
        {
            "profile_id": profile_id,
            "display_name": clean_name,
            "timezone": timezone_name,
            "status": "draft",
            "paths": paths,
        },
    )
    _write_json(
        root / "candidate.json",
        {
            "profile_id": profile_id,
            "display_name": clean_name,
            "preferred_name": preferred_name,
            "email": "",
            "preferences": {},
            "evidence": [],
        },
    )
    _write_json(
        root / "scorecard.json",
        {
            "profile_id": profile_id,
            "dimensions": [
                {"id": "role_scope", "weight": 20},
                {"id": "responsibility_fit", "weight": 20},
                {"id": "domain_fit", "weight": 20},
                {"id": "compensation", "weight": 20},
                {"id": "work_model", "weight": 20},
            ],
            "calibration": {"required_roles_min": 8, "required_roles_max": 12},
        },
    )
    _write_json(
        root / "search-plan.json",
        {
            "profile_id": profile_id,
            "sources": ["direct-careers", "greenhouse", "lever"],
            "queries": [],
            "constraints": {},
            "exclusions": [],
        },
    )
    _write_json(
        root / "automation.json",
        {
            "profile_id": profile_id,
            "status": "paused",
            "sender": "",
            "to": "",
            "cadence": "",
            "include_raw_score": False,
            "approvals": {},
        },
    )
    _write_json(
        root / "sources" / "resume-facts.json",
        {
            "profile_id": profile_id,
            "status": "paused",
            "identity": {},
            "education": {},
            "history": [],
            "facts": [],
            "sources": [],
            "prohibited_claim_patterns": [],
        },
    )

    _write_csv_header(root / "state" / "pipeline.csv", PIPELINE_HEADERS)
    _write_csv_header(
        root / "state" / "evaluated-roles.csv", EVALUATED_HEADERS
    )
    _write_csv_header(root / "state" / "feedback-log.csv", FEEDBACK_HEADERS)
    (root / "state" / "run-ledger.jsonl").write_text("", encoding="utf-8")
    _write_json(root / "state" / "missed-run-alerts.json", [])
    (root / ".gitignore").write_text(
        "# This private candidate folder must not become a Git repository.\n"
        "*\n!.gitignore\n",
        encoding="utf-8",
    )

    asset = Path(__file__).resolve().parents[1] / "assets" / "daily-email.html"
    if not asset.is_file():
        raise FileNotFoundError(f"generic email asset is missing: {asset}")
    shutil.copyfile(asset, root / "templates" / "daily-email.html")
    return root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    parser.add_argument("profile_id")
    parser.add_argument("display_name")
    parser.add_argument("timezone")
    args = parser.parse_args()
    initialize_project(
        args.project_root, args.profile_id, args.display_name, args.timezone
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
