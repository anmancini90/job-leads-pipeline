#!/usr/bin/env python3
"""Atomically record non-authoritative job-leads run attempt progress.

Delivery receipts, claims, and the append-only run ledger remain authoritative.
This file only exposes enough fresh progress and staging evidence for recovery
watchdogs and the final claim gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUN_TYPE_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ATTEMPT_STATUSES = {
    "started",
    "searching",
    "search_complete",
    "staged",
    "sent",
    "pre_claim_failed",
    "blocked",
}
ACTIVE_HEARTBEAT_STATUSES = {"started", "searching"}
FUNNEL_FIELDS = (
    "searched",
    "deduplicated",
    "gated",
    "scored",
    "qualified",
    "verified",
    "delivered",
)
LOCK_ATTEMPTS = 400
LOCK_RETRY_SECONDS = 0.005


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_calendar_date(value: object) -> str:
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise ValueError("local date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("local date must be a valid calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError("local date must use canonical YYYY-MM-DD format")
    return value


def _validate_identity(profile_id: str, local_date: str, run_type: str) -> None:
    if not isinstance(profile_id, str) or not profile_id or any(
        character in profile_id for character in "/\\:"
    ):
        raise ValueError("invalid profile_id")
    _valid_calendar_date(local_date)
    if not isinstance(run_type, str) or not RUN_TYPE_PATTERN.fullmatch(run_type):
        raise ValueError("invalid run_type")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(50):
            try:
                os.replace(temporary_path, path)
                temporary_path = None
                return
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(LOCK_RETRY_SECONDS)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


@contextmanager
def _bounded_file_lock(lock_path: Path):
    token = uuid.uuid4().hex
    thread_ident = threading.get_ident()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(LOCK_ATTEMPTS):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except (FileExistsError, PermissionError):
            if attempt == LOCK_ATTEMPTS - 1:
                raise TimeoutError(
                    "manual_reconciliation_required: existing attempt-state lock was preserved: "
                    f"{lock_path}"
                )
            time.sleep(LOCK_RETRY_SECONDS)
            continue
        try:
            os.write(
                descriptor,
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "thread": thread_ident,
                        "token": token,
                        "acquired_at": _utc_now(),
                    }
                ).encode("utf-8"),
            )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        break
    else:
        raise TimeoutError("manual_reconciliation_required: attempt-state lock unavailable")
    try:
        yield
    finally:
        try:
            current = _read_json(lock_path)
            owns_lock = isinstance(current, dict) and current.get("token") == token
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            owns_lock = False
        if owns_lock:
            for attempt in range(50):
                try:
                    lock_path.unlink()
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    if attempt == 49:
                        raise
                    time.sleep(LOCK_RETRY_SECONDS)


def _project_root(project_root: str | Path, profile_id: str) -> Path:
    root = Path(project_root).expanduser().resolve()
    profile = _read_json(root / "profile.json")
    if not isinstance(profile, dict) or profile.get("profile_id") != profile_id:
        raise ValueError("profile mismatch for run attempt state")
    if not (root / "state").is_dir():
        raise ValueError("project state directory is missing")
    return root


def _attempt_path(root: Path, local_date: str, run_type: str) -> Path:
    return root / "state" / "run-attempts" / f"{local_date}-{run_type}.attempt.json"


def _path_inside_project(root: Path, value: str | Path, *, label: str) -> tuple[Path, str]:
    candidate = Path(value)
    resolved = candidate.resolve(strict=False) if candidate.is_absolute() else (root / candidate).resolve(strict=False)
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"{label} must remain inside the project root") from exc
    return resolved, relative


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_funnel_counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or any(field not in value for field in FUNNEL_FIELDS):
        raise ValueError("funnel_counts must include " + ", ".join(FUNNEL_FIELDS))
    counts: dict[str, int] = {}
    for field in FUNNEL_FIELDS:
        count = value[field]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("funnel counts must be nonnegative integers")
        counts[field] = count
    if any(counts[left] < counts[right] for left, right in zip(FUNNEL_FIELDS, FUNNEL_FIELDS[1:])):
        raise ValueError("funnel counts must be non-increasing through delivered")
    return counts


def inspect_attempt(
    project_root: str | Path,
    *,
    profile_id: str,
    local_date: str,
    run_type: str,
    fresh_for_seconds: float = 30.0 * 60.0,
) -> dict[str, object]:
    """Read attempt state without creating or changing project files."""
    _validate_identity(profile_id, local_date, run_type)
    root = _project_root(project_root, profile_id)
    path = _attempt_path(root, local_date, run_type)
    base = {
        "profile_id": profile_id,
        "local_date": local_date,
        "run_type": run_type,
        "idempotency_key": f"{profile_id}:{local_date}:{run_type}",
        "attempt_path": path.relative_to(root).as_posix(),
        "exists": path.is_file(),
    }
    if not path.is_file():
        return base
    value = _read_json(path)
    if not isinstance(value, dict):
        raise ValueError("run attempt state must be an object")
    expected = {
        "attempt_version": 1,
        "profile_id": profile_id,
        "local_date": local_date,
        "run_type": run_type,
        "idempotency_key": base["idempotency_key"],
    }
    mismatches = [name for name, expected_value in expected.items() if value.get(name) != expected_value]
    if mismatches:
        raise ValueError("run attempt state identity mismatch for: " + ", ".join(sorted(mismatches)))
    if value.get("status") not in ATTEMPT_STATUSES:
        raise ValueError("run attempt state has an invalid status")
    attempt_number = value.get("attempt_number")
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool) or attempt_number < 1:
        raise ValueError("run attempt number must be a positive integer")
    heartbeat_age_seconds = None
    try:
        heartbeat_at = datetime.fromisoformat(str(value["heartbeat_at"]))
        if heartbeat_at.tzinfo is None:
            raise ValueError
        heartbeat_age_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - heartbeat_at.astimezone(timezone.utc)).total_seconds(),
        )
    except (KeyError, TypeError, ValueError):
        pass
    return {
        **base,
        **value,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "heartbeat_fresh": (
            value.get("status") in ACTIVE_HEARTBEAT_STATUSES
            and heartbeat_age_seconds is not None
            and heartbeat_age_seconds <= fresh_for_seconds
        ),
    }


def transition_attempt(
    project_root: str | Path,
    *,
    profile_id: str,
    local_date: str,
    run_type: str,
    status: str,
    attempt_number: int | None = None,
    funnel_counts: dict[str, int] | None = None,
    failure_class: str | None = None,
    manifest_path: str | Path | None = None,
    manifest_sha256: str | None = None,
    candidate_pool_complete: bool = False,
    links_validated: bool = False,
    zero_lead_evidence_complete: bool = False,
    rendering_complete: bool = False,
    terminal_receipt_path: str | Path | None = None,
) -> dict[str, object]:
    """Atomically advance one non-authoritative attempt through a guarded state machine."""
    _validate_identity(profile_id, local_date, run_type)
    if status not in ATTEMPT_STATUSES:
        raise ValueError("invalid attempt status")
    root = _project_root(project_root, profile_id)
    path = _attempt_path(root, local_date, run_type)
    lock_path = path.with_name(f"{path.name}.lock")
    with _bounded_file_lock(lock_path):
        current = _read_json(path) if path.exists() else None
        if current is not None and not isinstance(current, dict):
            raise ValueError("run attempt state must be an object")
        current_status = current.get("status") if isinstance(current, dict) else None
        allowed = {
            None: {"started"},
            "started": {"started", "searching", "pre_claim_failed", "blocked"},
            "searching": {"searching", "search_complete", "pre_claim_failed", "blocked"},
            "search_complete": {"search_complete", "staged", "pre_claim_failed", "blocked"},
            "staged": {"staged", "sent", "pre_claim_failed", "blocked"},
            "pre_claim_failed": {"started"},
            "blocked": {"started"},
            "sent": set(),
        }
        if status not in allowed[current_status]:
            raise ValueError(f"invalid attempt transition: {current_status or 'missing'} -> {status}")
        previous_number = current.get("attempt_number") if isinstance(current, dict) else None
        if status == "started" and current_status in {"pre_claim_failed", "blocked"}:
            expected_number = int(previous_number) + 1
        elif previous_number is not None:
            expected_number = int(previous_number)
        else:
            expected_number = 1
        if attempt_number is not None and attempt_number != expected_number:
            raise ValueError(f"attempt_number must be {expected_number} for this transition")
        attempt_number = expected_number
        now = _utc_now()
        if status == "started" and current_status in {None, "pre_claim_failed", "blocked"}:
            value: dict[str, object] = {
                "attempt_version": 1,
                "profile_id": profile_id,
                "local_date": local_date,
                "run_type": run_type,
                "idempotency_key": f"{profile_id}:{local_date}:{run_type}",
                "status": status,
                "attempt_number": attempt_number,
                "started_at": now,
                "updated_at": now,
                "heartbeat_at": now,
                "funnel_counts": {field: 0 for field in FUNNEL_FIELDS},
            }
        else:
            value = dict(current or {})
            value.update({"status": status, "attempt_number": attempt_number, "updated_at": now})
            if status in ACTIVE_HEARTBEAT_STATUSES:
                value["heartbeat_at"] = now
        if funnel_counts is not None:
            value["funnel_counts"] = _validated_funnel_counts(funnel_counts)
        elif status in {"search_complete", "staged", "sent"}:
            value["funnel_counts"] = _validated_funnel_counts(value.get("funnel_counts"))
        if status == "search_complete":
            value["search_completed_at"] = value.get("search_completed_at", now)
        if status == "staged":
            if not all((candidate_pool_complete, links_validated, rendering_complete)):
                raise ValueError(
                    "staging requires candidate-pool, link-validation, and rendering completion evidence"
                )
            if manifest_path is None or manifest_sha256 is None:
                raise ValueError("staging requires an exact manifest path and SHA-256")
            manifest_file, manifest_relative = _path_inside_project(root, manifest_path, label="manifest path")
            if not manifest_relative.startswith("outputs/") or not manifest_file.is_file():
                raise ValueError("staged manifest must exist inside project outputs/")
            if not SHA256_PATTERN.fullmatch(manifest_sha256) or _file_sha256(manifest_file) != manifest_sha256:
                raise ValueError("staged manifest SHA-256 does not match")
            manifest = _read_json(manifest_file)
            if not isinstance(manifest, dict):
                raise ValueError("staged manifest must be an object")
            expected_manifest = {
                "profile_id": profile_id,
                "local_date": local_date,
                "run_type": run_type,
                "idempotency_key": f"{profile_id}:{local_date}:{run_type}",
            }
            if any(manifest.get(name) != expected for name, expected in expected_manifest.items()):
                raise ValueError("staged manifest identity does not match the attempt")
            lead_count = manifest.get("lead_count")
            if not isinstance(lead_count, int) or isinstance(lead_count, bool) or lead_count < 0:
                raise ValueError("staged manifest lead_count is invalid")
            manifest_counts = manifest.get("counts")
            if not isinstance(manifest_counts, dict) or any(
                not isinstance(manifest_counts.get(field), int)
                or isinstance(manifest_counts.get(field), bool)
                or manifest_counts[field] < 0
                for field in ("searched", "qualified", "verified")
            ):
                raise ValueError("staged manifest counts are invalid")
            staged_counts = _validated_funnel_counts(value.get("funnel_counts"))
            for field in ("searched", "qualified", "verified"):
                if staged_counts[field] != manifest_counts[field]:
                    raise ValueError(f"attempt funnel {field} does not match the staged manifest")
            if staged_counts["delivered"] != 0:
                raise ValueError("a staged pre-claim attempt must have delivered count 0")
            if lead_count == 0 and not zero_lead_evidence_complete:
                raise ValueError("a zero-lead staged outcome requires complete zero-lead evidence")
            value["manifest_reference"] = {
                "path": manifest_relative,
                "sha256": manifest_sha256,
            }
            value["stage_evidence"] = {
                "search_complete": bool(value.get("search_completed_at")),
                "candidate_pool_complete": True,
                "links_validated": True,
                "zero_lead_evidence_complete": bool(zero_lead_evidence_complete),
                "rendering_complete": True,
                "manifest_complete": True,
            }
            value["staged_at"] = value.get("staged_at", now)
        if status in {"pre_claim_failed", "blocked"}:
            if not isinstance(failure_class, str) or not failure_class.strip():
                raise ValueError(f"{status} requires a nonempty failure_class")
            value["failure_class"] = failure_class.strip()
        if status == "sent":
            if terminal_receipt_path is None:
                raise ValueError("sent attempt state requires a terminal receipt reference")
            receipt_file, receipt_relative = _path_inside_project(
                root, terminal_receipt_path, label="terminal receipt path"
            )
            if not receipt_relative.startswith("outputs/") or not receipt_file.is_file():
                raise ValueError("terminal receipt must exist inside project outputs/")
            value["terminal_receipt_reference"] = receipt_relative
            value["sent_at"] = now
        _atomic_write_json(path, value)
    return inspect_attempt(
        root,
        profile_id=profile_id,
        local_date=local_date,
        run_type=run_type,
    )


def heartbeat_attempt(
    project_root: str | Path,
    *,
    profile_id: str,
    local_date: str,
    run_type: str,
) -> dict[str, object]:
    """Refresh only an actively searching attempt; never create an attempt."""
    _validate_identity(profile_id, local_date, run_type)
    root = _project_root(project_root, profile_id)
    path = _attempt_path(root, local_date, run_type)
    lock_path = path.with_name(f"{path.name}.lock")
    with _bounded_file_lock(lock_path):
        if not path.exists():
            raise ValueError("cannot heartbeat a missing run attempt")
        value = _read_json(path)
        if not isinstance(value, dict) or value.get("status") not in ACTIVE_HEARTBEAT_STATUSES:
            raise ValueError("heartbeat is allowed only while an attempt is started or searching")
        now = _utc_now()
        value["heartbeat_at"] = now
        value["updated_at"] = now
        _atomic_write_json(path, value)
    return inspect_attempt(root, profile_id=profile_id, local_date=local_date, run_type=run_type)


def validate_staged_attempt(
    project_root: str | Path,
    *,
    profile_id: str,
    local_date: str,
    run_type: str,
    manifest_path: str | Path,
    manifest_sha256: str,
) -> str | None:
    """Return a claim-blocking error unless exact staged evidence exists."""
    try:
        state = inspect_attempt(
            project_root,
            profile_id=profile_id,
            local_date=local_date,
            run_type=run_type,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return f"run attempt state is invalid: {exc}"
    if not state.get("exists"):
        return "delivery claim requires a staged run attempt"
    if state.get("status") != "staged":
        return "delivery claim requires run attempt status staged"
    if not state.get("search_completed_at") or not state.get("staged_at"):
        return "delivery claim requires search-complete and staged timestamps"
    evidence = state.get("stage_evidence")
    required_evidence = {
        "search_complete",
        "candidate_pool_complete",
        "links_validated",
        "rendering_complete",
        "manifest_complete",
    }
    if not isinstance(evidence, dict) or any(evidence.get(name) is not True for name in required_evidence):
        return "delivery claim requires complete pre-claim staging evidence"
    try:
        root = Path(project_root).expanduser().resolve()
        _manifest_file, manifest_relative = _path_inside_project(root, manifest_path, label="manifest path")
    except ValueError as exc:
        return str(exc)
    reference = state.get("manifest_reference")
    if not isinstance(reference, dict) or reference != {
        "path": manifest_relative,
        "sha256": manifest_sha256,
    }:
        return "staged attempt does not match the exact delivery manifest"
    counts = state.get("funnel_counts")
    try:
        staged_counts = _validated_funnel_counts(counts)
        manifest_file, _manifest_relative = _path_inside_project(
            root, manifest_path, label="manifest path"
        )
        manifest = _read_json(manifest_file)
        if not isinstance(manifest, dict):
            raise ValueError("staged manifest must be an object")
        manifest_counts = manifest.get("counts")
        if not isinstance(manifest_counts, dict):
            raise ValueError("staged manifest counts are invalid")
        for field in ("searched", "qualified", "verified"):
            if staged_counts[field] != manifest_counts.get(field):
                raise ValueError(f"attempt funnel {field} does not match the staged manifest")
        if staged_counts["delivered"] != 0:
            raise ValueError("a staged pre-claim attempt must have delivered count 0")
        if manifest.get("lead_count") == 0 and evidence.get("zero_lead_evidence_complete") is not True:
            raise ValueError("a zero-lead staged outcome requires complete zero-lead evidence")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return str(exc)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    parser.add_argument("profile_id")
    parser.add_argument("local_date")
    parser.add_argument("run_type")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--heartbeat", action="store_true")
    action.add_argument("--transition", choices=sorted(ATTEMPT_STATUSES))
    parser.add_argument("--attempt-number", type=int)
    parser.add_argument("--funnel-counts", help="JSON object containing all funnel counts")
    parser.add_argument("--failure-class")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--candidate-pool-complete", action="store_true")
    parser.add_argument("--links-validated", action="store_true")
    parser.add_argument("--zero-lead-evidence-complete", action="store_true")
    parser.add_argument("--rendering-complete", action="store_true")
    parser.add_argument("--terminal-receipt", type=Path)
    args = parser.parse_args()
    common = {
        "profile_id": args.profile_id,
        "local_date": args.local_date,
        "run_type": args.run_type,
    }
    if args.inspect:
        result = inspect_attempt(args.project_root, **common)
    elif args.heartbeat:
        result = heartbeat_attempt(args.project_root, **common)
    else:
        try:
            funnel_counts = json.loads(args.funnel_counts) if args.funnel_counts else None
        except json.JSONDecodeError as exc:
            parser.error(f"--funnel-counts must be valid JSON: {exc}")
        result = transition_attempt(
            args.project_root,
            **common,
            status=args.transition,
            attempt_number=args.attempt_number,
            funnel_counts=funnel_counts,
            failure_class=args.failure_class,
            manifest_path=args.manifest,
            manifest_sha256=args.manifest_sha256,
            candidate_pool_complete=args.candidate_pool_complete,
            links_validated=args.links_validated,
            zero_lead_evidence_complete=args.zero_lead_evidence_complete,
            rendering_complete=args.rendering_complete,
            terminal_receipt_path=args.terminal_receipt,
        )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
