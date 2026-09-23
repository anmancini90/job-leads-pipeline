#!/usr/bin/env python3
"""Read-only setup checks for a private job-leads project."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


MODULES = {
    "pdf_creation": "reportlab",
    "pdf_text_check": "pypdf",
    "pdf_visual_check": "pypdfium2",
    "gmail_api": "googleapiclient",
    "gmail_oauth": "google_auth_oauthlib",
}


def check_token_store() -> dict[str, str]:
    """Report the actual keyring backend, not only an installed package."""
    path = Path(__file__).resolve().with_name("gmail_transport.py")
    spec = importlib.util.spec_from_file_location("job_leads_gmail_transport_doctor", path)
    if spec is None or spec.loader is None:
        return {"status": "needs_attention", "backend": "unavailable",
                "detail": "Gmail transport is missing; token storage cannot be checked."}
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        return module.inspect_keyring_backend()
    except Exception as exc:
        return {"status": "needs_attention", "backend": "unavailable",
                "detail": f"Could not inspect token storage ({type(exc).__name__}); Gmail is blocked."}


def check_web() -> dict[str, str]:
    """Check only that HTTPS to a public, stable site is available."""
    try:
        request = Request("https://example.com/", method="HEAD")
        with urlopen(request, timeout=5) as response:
            if 200 <= response.status < 400:
                return {"status": "ok", "detail": "Public HTTPS works."}
            return {"status": "needs_attention", "detail": f"HTTPS returned {response.status}."}
    except (OSError, URLError) as exc:
        return {"status": "needs_attention", "detail": f"Public HTTPS unavailable: {type(exc).__name__}."}


def check_project(project_root: Path | None) -> dict[str, str]:
    if project_root is None:
        return {"status": "not_checked", "detail": "Pass --project-root after onboarding."}
    root = project_root.expanduser().resolve()
    if not (root / "profile.json").is_file():
        return {"status": "needs_attention", "detail": "No initialized profile.json at that exact project root."}
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        return {"status": "needs_attention", "detail": "Candidate project must not be a Git repository."}
    try:
        profile = json.loads((root / "profile.json").read_text(encoding="utf-8"))
        automation = json.loads((root / "automation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "needs_attention", "detail": "Project configuration could not be read."}
    if not isinstance(profile, dict) or not isinstance(automation, dict):
        return {"status": "needs_attention", "detail": "Project configuration is malformed."}
    if profile.get("profile_id") != automation.get("profile_id"):
        return {"status": "needs_attention", "detail": "Project and automation IDs do not match."}
    if not (root / "sources" / "resume-facts.json").is_file():
        return {"status": "needs_attention", "detail": "Resume facts draft is missing."}
    return {"status": "ok", "detail": f"Project found; delivery is {automation.get('status', 'unknown')}."}


def diagnose(host: str, project_root: Path | None = None, *, probe_web: bool = True) -> dict:
    checks: dict[str, dict[str, str]] = {}
    checks["python"] = {
        "status": "ok" if sys.version_info >= (3, 10) else "needs_attention",
        "detail": f"Python {sys.version_info.major}.{sys.version_info.minor}; 3.10 or newer is required.",
    }
    for purpose, module in MODULES.items():
        present = importlib.util.find_spec(module) is not None
        checks[purpose] = {
            "status": "ok" if present else "needs_attention",
            "detail": f"{module} {'is installed' if present else 'is not installed'}.",
        }
    checks["secure_token_store"] = check_token_store()
    checks["public_web"] = check_web() if probe_web else {
        "status": "not_checked", "detail": "Network probe skipped."
    }
    checks["project"] = check_project(project_root)
    checks["host_cli"] = {
        "status": "ok" if shutil.which("codex" if host == "codex" else "claude") else "not_checked",
        "detail": f"{host} command {'found' if shutil.which('codex' if host == 'codex' else 'claude') else 'not on PATH; desktop app may still work'}.",
    }
    checks["gmail"] = {
        "status": "not_checked",
        "detail": "A Python check cannot prove Gmail permission or exact PDF attachment support. Use the guided connector probe or Gmail API OAuth test.",
    }
    checks["scheduler"] = {
        "status": "not_checked",
        "detail": "Confirm a paused daily schedule in Codex or Claude Code Desktop, then run one controlled test before activation.",
    }
    return {"host": host, "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, choices=("codex", "claude"))
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--skip-web", action="store_true")
    args = parser.parse_args()
    print(json.dumps(diagnose(args.host, args.project_root, probe_web=not args.skip_web), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
