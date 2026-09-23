#!/usr/bin/env python3
"""Reject obvious private files and secrets before this repository is shared.

This is a safety net, not a substitute for human review of every release.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_SUFFIXES = {".pdf", ".doc", ".docx", ".eml", ".msg", ".zip", ".pyc", ".p12", ".pem", ".key"}
PRIVATE_NAMES = {"token.json", "credentials.json", "id_rsa", "id_ed25519"}
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
HOME_PATH = re.compile(r"(?:[A-Za-z]:[\\/](?:Users|Documents)[\\/]|/(?:Users|home)/[A-Za-z0-9._-]+/)", re.I)
SECRET = re.compile(r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|AIza[0-9A-Za-z_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
TOKEN_JSON = re.compile(r'"(?:access_token|refresh_token|client_secret)"\s*:\s*"[^"\s]{12,}"', re.I)


def candidate_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, check=True,
    )
    return sorted({ROOT / Path(raw.decode("utf-8")) for raw in result.stdout.split(b"\0") if raw})


def audit_data(relative: str, contents: bytes) -> list[str]:
    problems: list[str] = []
    path = Path(relative)
    if path.suffix.lower() in PRIVATE_SUFFIXES or path.name.lower() in PRIVATE_NAMES or "__pycache__" in path.parts:
        problems.append(f"{relative}: private or generated file type")
        return problems
    try:
        data = contents.decode("utf-8")
    except UnicodeDecodeError:
        problems.append(f"{relative}: non-text content needs explicit review")
        return problems
    if HOME_PATH.search(data):
        problems.append(f"{relative}: machine-specific home path")
    if SECRET.search(data) or TOKEN_JSON.search(data):
        problems.append(f"{relative}: possible credential material")
    for address in EMAIL.findall(data):
        if not address.lower().endswith("@example.com") and address != "YOU@gmail.com":
            problems.append(f"{relative}: non-synthetic email address")
            break
    return problems


def audit() -> list[str]:
    problems: list[str] = []
    for path in candidate_paths():
        relative = path.relative_to(ROOT).as_posix()
        if path.is_symlink() or not path.is_file():
            problems.append(f"{relative}: only regular files may ship")
            continue
        problems.extend(audit_data(relative, path.read_bytes()))
    return problems


def audit_history() -> tuple[list[str], int]:
    """Inspect every tracked file in every reachable commit, not only HEAD."""
    commits = subprocess.run(
        ["git", "rev-list", "--all"], cwd=ROOT, capture_output=True, check=True, text=True
    ).stdout.splitlines()
    problems: list[str] = []
    inspected: set[tuple[str, str]] = set()
    for commit in commits:
        tree = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "--full-tree", commit],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.decode("ascii").split()
            relative = name.decode("utf-8")
            if mode not in {"100644", "100755"} or kind != "blob":
                problems.append(f"{commit[:12]}:{relative}: only regular files may ship")
                continue
            if (relative, object_id) in inspected:
                continue
            inspected.add((relative, object_id))
            blob = subprocess.run(
                ["git", "cat-file", "blob", object_id],
                cwd=ROOT, capture_output=True, check=True,
            ).stdout
            problems.extend(f"{commit[:12]}:{item}" for item in audit_data(relative, blob))
    return problems, len(commits)


def main() -> int:
    problems = audit()
    history_problems, commit_count = audit_history()
    problems.extend(history_problems)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print(f"Public tree audit passed: {len(candidate_paths())} files and {commit_count} commits inspected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
