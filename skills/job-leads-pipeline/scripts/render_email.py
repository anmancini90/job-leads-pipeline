#!/usr/bin/env python3
"""Render a candidate email while escaping all untrusted lead data."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from urllib.parse import urlsplit


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _safe_text(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _safe_link(value: object) -> str:
    raw = str(value or "").strip()
    parts = urlsplit(raw)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    return html.escape(raw, quote=True)


def _role_card(lead: dict[str, object], *, include_raw_score: bool) -> str:
    role = _safe_text(lead.get("role") or "Untitled role")
    company = _safe_text(lead.get("company") or "Company not listed")
    tier = _safe_text(lead.get("tier") or "Review")
    work_model = _safe_text(lead.get("work_model") or "Not clear in the job post")
    location = _safe_text(lead.get("location_details") or "Not listed")
    salary_value = str(lead.get("salary") or "").strip()
    salary = _safe_text(salary_value or "Not listed in the job post")
    day_to_day = _safe_text(
        lead.get("day_to_day")
        or "The job post does not give enough detail to explain a normal day."
    )
    why = _safe_text(lead.get("why_it_fits") or "The fit is not clear yet.")
    concern = _safe_text(lead.get("main_concern") or "No clear concern was found.")
    link = _safe_link(lead.get("link"))
    if link:
        action = (
            f'<a href="{link}" rel="noopener noreferrer" '
            'style="color:#175cd3;font-weight:700">Open job</a>'
        )
    else:
        action = "<span>Job link is not available</span>"
    score_line = ""
    if include_raw_score and lead.get("score") not in (None, ""):
        score_line = (
            '<p style="margin:8px 0"><strong>Score:</strong> '
            f'{_safe_text(lead.get("score"))}/100</p>'
        )
    return (
        '<article style="background:#fff;border:1px solid #dfe4eb;'
        'border-radius:10px;padding:18px;margin:0 0 16px">'
        f'<p style="margin:0 0 6px;color:#566176">Match: {tier}</p>'
        f'<h2 style="font-size:19px;margin:0 0 4px">{role}</h2>'
        f'<p style="margin:0 0 12px">{company}</p>'
        f'{score_line}'
        '<div style="background:#f6f8fa;border-radius:8px;padding:12px 14px;'
        'margin:0 0 14px;font-size:14px;line-height:1.55">'
        f'<p style="margin:0 0 4px"><strong>Work setup:</strong> {work_model}</p>'
        f'<p style="margin:0 0 4px"><strong>Location:</strong> {location}</p>'
        f'<p style="margin:0"><strong>Pay:</strong> {salary}</p>'
        '</div>'
        f'<p style="margin:8px 0;line-height:1.55"><strong>What your day may look like:</strong> {day_to_day}</p>'
        f'<p style="margin:8px 0;line-height:1.55"><strong>Why it may fit:</strong> {why}</p>'
        f'<p style="margin:8px 0;line-height:1.55"><strong>What to watch:</strong> {concern}</p>'
        f'<p style="margin:12px 0 0">{action}</p>'
        '</article>'
    )


def render_email(
    project_root: str | Path,
    leads: list[dict[str, object]],
) -> str:
    root = Path(project_root).expanduser()
    profile = _read_json(root / "profile.json")
    candidate = _read_json(root / "candidate.json")
    automation = _read_json(root / "automation.json")
    profile_id = profile.get("profile_id")
    if candidate.get("profile_id") != profile_id:
        raise ValueError("candidate profile mismatch")
    for index, lead in enumerate(leads):
        if not isinstance(lead, dict):
            raise ValueError(f"lead {index} must be an object")
        if lead.get("profile_id") != profile_id:
            raise ValueError(f"lead {index} profile mismatch")

    display_name = str(candidate.get("display_name") or profile.get("display_name") or "")
    preferred_name = str(candidate.get("preferred_name") or "").strip()
    if not preferred_name:
        preferred_name = display_name.strip().split()[0] if display_name.strip() else "there"
    include_raw_score = automation.get("include_raw_score") is True
    cards = "".join(
        _role_card(lead, include_raw_score=include_raw_score) for lead in leads
    )
    if not cards:
        cards = "<p>No new roles met the current criteria.</p>"

    template = (root / "templates" / "daily-email.html").read_text(
        encoding="utf-8"
    )
    return template.replace(
        "{{preferred_name}}", _safe_text(preferred_name)
    ).replace("{{role_cards}}", cards)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    parser.add_argument("leads_json", type=Path)
    args = parser.parse_args()
    leads = json.loads(args.leads_json.read_text(encoding="utf-8"))
    if not isinstance(leads, list):
        raise ValueError("leads JSON must be a list")
    print(render_email(args.project_root, leads))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
