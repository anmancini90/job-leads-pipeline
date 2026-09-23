"""Freeze candidate facts, source files, job evidence, and final leads for one run.

This command never drafts, approves, claims delivery, or sends email.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inside(root: Path, value: str | Path) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Path escapes project")
    return path


def freeze(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Frozen input changed; use a new revision directory: {path}")
        return
    with path.open("xb") as handle:
        handle.write(data)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def prepare(root: Path, local_date: str, leads_path: Path, out_dir: Path,
            evidence_paths: list[Path]) -> dict:
    root = root.resolve(strict=True)
    date.fromisoformat(local_date)
    dest = inside(root, out_dir)
    if not dest.is_relative_to(root / "outputs"):
        raise ValueError("Frozen inputs must be inside outputs/")
    profile = json.loads((root / "profile.json").read_text(encoding="utf-8"))
    candidate_bytes = (root / "candidate.json").read_bytes()
    candidate = json.loads(candidate_bytes)
    facts_bytes = (root / "sources/resume-facts.json").read_bytes()
    facts = json.loads(facts_bytes)
    pid = profile["profile_id"]
    if candidate.get("profile_id") != pid or facts.get("profile_id") != pid:
        raise ValueError("Candidate/fact profile mismatch")
    identity = facts.get("identity")
    if not isinstance(identity, dict) or not identity.get("name"):
        raise ValueError("Candidate resume facts need a confirmed identity")
    if candidate.get("display_name", "").strip().casefold() != identity["name"].strip().casefold():
        raise ValueError("Candidate name and resume fact identity conflict; reconcile before drafting")
    if candidate.get("email") and candidate["email"].strip().casefold() != str(identity.get("email", "")).strip().casefold():
        raise ValueError("Candidate email and resume fact identity conflict; reconcile before drafting")
    if not isinstance(facts.get("facts"), list) or not facts["facts"]:
        raise ValueError("Confirmed, source-backed resume facts are required")
    if not isinstance(facts.get("history"), list) or not facts["history"]:
        raise ValueError("Confirmed career history is required")
    if not isinstance(facts.get("sources"), list) or not facts["sources"]:
        raise ValueError("At least one candidate-provided source is required")
    leads_bytes = inside(root, leads_path).read_bytes()
    leads = json.loads(leads_bytes)
    if not isinstance(leads, list):
        raise ValueError("Final leads must be a list")
    seen: set[str] = set()
    for lead in leads:
        if not isinstance(lead, dict) or lead.get("profile_id") != pid or not all(lead.get(k) for k in ("role_ref", "link", "company", "role")):
            raise ValueError("Invalid final lead identity")
        if lead["role_ref"] in seen:
            raise ValueError("Duplicate role_ref")
        seen.add(lead["role_ref"])
    if leads and not evidence_paths:
        raise ValueError("Full job-description evidence is required for selected leads")
    frozen_sources = []
    source_ids = set()
    for source in facts["sources"]:
        if (not isinstance(source, dict) or not isinstance(source.get("id"), str)
                or not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", source["id"])):
            raise ValueError("Invalid resume fact source")
        if source["id"] in source_ids:
            raise ValueError("Duplicate resume fact source ID")
        source_ids.add(source["id"])
        if not source.get("path"):
            continue
        source_path = inside(root, source["path"])
        if not source_path.is_relative_to(root / "sources") or not source_path.is_file():
            raise ValueError("Resume source must be an existing file inside sources/")
        data = source_path.read_bytes()
        if source.get("sha256") and digest(data) != source["sha256"]:
            raise ValueError(f"Source changed; reconcile facts before drafting: {source['id']}")
        filename = source["id"] + source_path.suffix
        freeze(dest / "sources" / filename, data)
        frozen_sources.append({"id": source["id"], "path": (dest / "sources" / filename).relative_to(root).as_posix(), "sha256": digest(data)})
    if not frozen_sources:
        raise ValueError("At least one candidate-provided file must back resume facts")
    for index, value in enumerate(evidence_paths):
        evidence = inside(root, value)
        data = evidence.read_bytes()
        path = dest / "job-evidence" / f"{index}-{evidence.name}"
        freeze(path, data)
        frozen_sources.append({"id": f"job_evidence_{index}", "path": path.relative_to(root).as_posix(), "sha256": digest(data)})
    for filename, data in [("facts.json", facts_bytes), ("candidate.json", candidate_bytes), ("leads.json", leads_bytes)]:
        freeze(dest / filename, data)
    result = {"profile_id": pid, "local_date": local_date, "fixture_only": False,
              "facts_path": (dest / "facts.json").relative_to(root).as_posix(), "facts_sha256": digest(facts_bytes),
              "candidate_sha256": digest(candidate_bytes), "leads_sha256": digest(leads_bytes),
              "sources": frozen_sources, "drafts": [], "approval_required": True}
    freeze(dest / "input-snapshot.json", json_bytes(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--local-date", required=True)
    parser.add_argument("--leads", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    args = parser.parse_args()
    print(json.dumps(prepare(args.root, args.local_date, args.leads, args.out_dir, args.evidence), indent=2))


if __name__ == "__main__":
    main()
