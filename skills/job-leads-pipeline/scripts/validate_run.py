#!/usr/bin/env python3
"""Check send idempotency, recovery state, and missed-run alert dedupe."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUN_TYPE_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
LOCK_ATTEMPTS = 400
LOCK_RETRY_SECONDS = 0.005
LOCK_STALE_SECONDS = 60.0
CLAIM_ATTEMPTS = 8
CLAIM_STALE_SECONDS = 30.0 * 60.0
OUTCOMES = {
    "lead_email",
    "no_deliverable_leads_email",
    "service_status_email",
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VALIDATOR_LOCK = threading.Lock()
_VALIDATE_PROJECT = None
_VALIDATE_STAGED_ATTEMPT = None


def _current_profile_date(root: Path, *, now_utc: datetime | None = None) -> str:
    """Return today's date in the candidate's configured IANA time zone."""
    profile = _read_json(root / "profile.json")
    if not isinstance(profile, dict) or not isinstance(profile.get("timezone"), str):
        raise ValueError("candidate profile has no IANA timezone")
    current = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    return current.astimezone(ZoneInfo(profile["timezone"])).date().isoformat()


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
            json.dump(value, handle, indent=2)
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
    if not isinstance(profile_id, str) or not profile_id or any(char in profile_id for char in "/\\:"):
        raise ValueError("invalid profile_id")
    _valid_calendar_date(local_date)
    if not isinstance(run_type, str) or not RUN_TYPE_PATTERN.fullmatch(run_type):
        raise ValueError("invalid run_type")


def _profile_validation_errors(root: Path) -> list[str]:
    """Load the sibling validator so send gating has one source of truth."""
    global _VALIDATE_PROJECT
    path = Path(__file__).resolve().with_name("validate_profile.py")
    with _VALIDATOR_LOCK:
        if _VALIDATE_PROJECT is None:
            module_name = f"job_leads_validate_profile_{abs(hash(path))}"
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                return ["could not load activation profile validator"]
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            _VALIDATE_PROJECT = module.validate_project
        validate_project = _VALIDATE_PROJECT
    return list(validate_project(root))


def _staged_attempt_error(
    root: Path,
    *,
    profile_id: str,
    local_date: str,
    run_type: str,
    manifest_path: str | Path,
    manifest_sha256: str,
) -> str | None:
    """Load the attempt-state helper without making preflight stateful."""
    global _VALIDATE_STAGED_ATTEMPT
    path = Path(__file__).resolve().with_name("run_attempt_state.py")
    with _VALIDATOR_LOCK:
        if _VALIDATE_STAGED_ATTEMPT is None:
            module_name = f"job_leads_run_attempt_state_{abs(hash(path))}"
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                return "could not load staged run-attempt validator"
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            _VALIDATE_STAGED_ATTEMPT = module.validate_staged_attempt
        validate_staged_attempt = _VALIDATE_STAGED_ATTEMPT
    return validate_staged_attempt(
        root,
        profile_id=profile_id,
        local_date=local_date,
        run_type=run_type,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
    )


@contextmanager
def _bounded_file_lock(lock_path: Path):
    """Cross-process lock with bounded waits and ownership-safe stale recovery."""
    token = uuid.uuid4().hex
    thread_ident = threading.get_ident()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for attempt in range(LOCK_ATTEMPTS):
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except (FileExistsError, PermissionError):
            if attempt == LOCK_ATTEMPTS - 1:
                raise TimeoutError(
                    f"manual_reconciliation_required: existing local state lock was preserved: {lock_path}"
                )
            time.sleep(LOCK_RETRY_SECONDS)
            continue
        try:
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "thread": thread_ident,
                    "token": token,
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                }
            ).encode("utf-8")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        acquired = True
        break
    if not acquired:
        raise TimeoutError(
            f"manual_reconciliation_required: could not acquire preserved local state lock: {lock_path}"
        )
    try:
        yield
    finally:
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8"))
            owns_current_lock = (
                isinstance(current, dict) and current.get("token") == token
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            owns_current_lock = False
        if owns_current_lock:
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


def _claim_paths(root: Path, local_date: str, run_type: str) -> tuple[Path, Path]:
    claims_root = root / "state" / "run-claims"
    claim_path = claims_root / f"{local_date}-{run_type}.claim.json"
    provider_path = claims_root / f"{local_date}-{run_type}.provider.json"
    return claim_path, provider_path


def _path_inside_project(root: Path, value: str | Path, *, label: str) -> tuple[Path, str]:
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
    else:
        resolved = (root / candidate).resolve(strict=False)
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


def _validate_manifest_binding(
    root: Path,
    *,
    profile_id: str,
    local_date: str,
    run_type: str,
    manifest_path: str | Path,
    manifest_sha256: str,
    outcome: str,
    lead_count: int,
    sender: str,
    recipient: str,
) -> tuple[str, dict[str, object]]:
    path, relative = _path_inside_project(root, manifest_path, label="manifest path")
    if not relative.startswith("outputs/") or not path.is_file():
        raise ValueError("manifest must be an existing file inside project outputs/")
    if not isinstance(manifest_sha256, str) or not SHA256_PATTERN.fullmatch(manifest_sha256):
        raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
    actual_hash = _file_sha256(path)
    if actual_hash != manifest_sha256:
        raise ValueError("manifest_sha256 does not match the manifest file")
    try:
        manifest = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest is invalid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    key = f"{profile_id}:{local_date}:{run_type}"
    expected = {
        "manifest_version": 2,
        "profile_id": profile_id,
        "local_date": local_date,
        "run_type": run_type,
        "idempotency_key": key,
        "outcome": outcome,
        "lead_count": lead_count,
        "sender": sender,
        "recipient": recipient,
    }
    mismatches = [name for name, value in expected.items() if manifest.get(name) != value]
    if mismatches:
        raise ValueError(
            "manifest binding mismatch for: " + ", ".join(sorted(mismatches))
        )
    if outcome not in OUTCOMES:
        raise ValueError("unsupported candidate email outcome")
    if not isinstance(lead_count, int) or isinstance(lead_count, bool) or lead_count < 0:
        raise ValueError("lead_count must be a nonnegative integer")
    if outcome == "lead_email" and lead_count < 1:
        raise ValueError("lead_email requires at least one lead")
    if outcome != "lead_email" and lead_count != 0:
        raise ValueError("non-lead outcomes require lead_count 0")
    if not isinstance(sender, str) or not sender.strip():
        raise ValueError("sender is required")
    if not isinstance(recipient, str) or not recipient.strip():
        raise ValueError("recipient is required")
    try:
        automation = _read_json(root / "automation.json")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"automation configuration is unavailable: {exc}") from exc
    if not isinstance(automation, dict):
        raise ValueError("automation configuration must be an object")
    if automation.get("sender") != sender or automation.get("to") != recipient:
        raise ValueError("manifest sender and recipient must exactly match automation.json")
    if not isinstance(manifest.get("subject"), str) or not manifest["subject"].strip():
        raise ValueError("manifest subject is required")
    if not isinstance(manifest.get("reason_code"), str) or not manifest["reason_code"].strip():
        raise ValueError("manifest reason_code is required")
    counts = manifest.get("counts")
    required_counts = {"searched", "qualified", "verified", "withheld"}
    if not isinstance(counts, dict) or not required_counts.issubset(counts):
        raise ValueError("manifest counts must include searched, qualified, verified, and withheld")
    if any(
        not isinstance(counts[name], int)
        or isinstance(counts[name], bool)
        or counts[name] < 0
        for name in required_counts
    ):
        raise ValueError("manifest counts must be nonnegative integers")
    if not (
        counts["searched"] >= counts["qualified"] >= counts["verified"]
        and counts["verified"] == lead_count
        and counts["withheld"] == counts["qualified"] - counts["verified"]
    ):
        raise ValueError("manifest counts are internally inconsistent")
    evidence_paths = manifest.get("evidence_paths")
    if not isinstance(evidence_paths, list) or any(
        not isinstance(value, str) or not value for value in evidence_paths
    ):
        raise ValueError("manifest evidence_paths must be a list of project-relative paths")
    for value in evidence_paths:
        if Path(value).is_absolute():
            raise ValueError("manifest evidence paths must be project-relative")
        evidence_path, evidence_relative = _path_inside_project(root, value, label="evidence path")
        if not evidence_relative.startswith("outputs/") or not evidence_path.is_file():
            raise ValueError("every manifest evidence path must exist inside project outputs/")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not {"payload", "html", "text"}.issubset(artifacts):
        raise ValueError("manifest artifacts must include payload, html, and text")
    for artifact_name in ("payload", "html", "text"):
        artifact = artifacts[artifact_name]
        if not isinstance(artifact, dict):
            raise ValueError(f"manifest {artifact_name} artifact must be an object")
        artifact_path_value = artifact.get("path")
        artifact_sha256 = artifact.get("sha256")
        if not isinstance(artifact_path_value, str) or not artifact_path_value:
            raise ValueError(f"manifest {artifact_name} artifact path is required")
        if Path(artifact_path_value).is_absolute():
            raise ValueError(f"manifest {artifact_name} artifact path must be project-relative")
        if not isinstance(artifact_sha256, str) or not SHA256_PATTERN.fullmatch(artifact_sha256):
            raise ValueError(f"manifest {artifact_name} artifact SHA-256 is invalid")
        artifact_path, artifact_relative = _path_inside_project(
            root, artifact_path_value, label=f"{artifact_name} artifact path"
        )
        if not artifact_relative.startswith("outputs/") or not artifact_path.is_file():
            raise ValueError(f"manifest {artifact_name} artifact must exist inside project outputs/")
        if _file_sha256(artifact_path) != artifact_sha256:
            raise ValueError(f"manifest {artifact_name} artifact SHA-256 does not match")
    return relative, manifest


def _claim_delivery(
    root: Path,
    key: str,
    local_date: str,
    run_type: str,
    *,
    manifest_path: str,
    manifest_sha256: str,
    outcome: str,
    lead_count: int,
    sender: str,
    recipient: str,
) -> tuple[bool, str, str | None]:
    claim_path, _provider_path = _claim_paths(root, local_date, run_type)
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    relative = claim_path.relative_to(root).as_posix()
    token = uuid.uuid4().hex
    thread_ident = threading.get_ident()
    for _attempt in range(CLAIM_ATTEMPTS):
        try:
            descriptor = os.open(
                claim_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            return False, relative, None
        except PermissionError:
            time.sleep(LOCK_RETRY_SECONDS)
            continue
        try:
            payload = json.dumps(
                {
                    "claim_version": 2,
                    "idempotency_key": key,
                    "claimed_at": datetime.now(timezone.utc).isoformat(),
                    "pid": os.getpid(),
                    "thread": thread_ident,
                    "token": token,
                    "attempt_token": token,
                    "manifest_path": manifest_path,
                    "manifest_sha256": manifest_sha256,
                    "outcome": outcome,
                    "lead_count": lead_count,
                    "sender": sender,
                    "recipient": recipient,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            try:
                claim_path.unlink()
            except FileNotFoundError:
                pass
            raise
        else:
            os.close(descriptor)
            return True, relative, token
    return False, relative, None


def _read_claim(path: Path, *, key: str) -> tuple[dict[str, object] | None, str | None]:
    if not path.exists():
        return None, None
    try:
        claim = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"invalid delivery claim: {exc}"
    if not isinstance(claim, dict) or claim.get("idempotency_key") != key:
        return None, "delivery claim does not match this run"
    return claim, None


def _validate_claim_integrity(root: Path, claim: dict[str, object]) -> str | None:
    if claim.get("claim_version") != 2:
        return None
    required = (
        "token",
        "attempt_token",
        "manifest_path",
        "manifest_sha256",
        "outcome",
        "sender",
        "recipient",
    )
    if any(not isinstance(claim.get(name), str) or not claim[name].strip() for name in required):
        return "v2 delivery claim is missing required binding fields"
    if not SHA256_PATTERN.fullmatch(str(claim["manifest_sha256"])):
        return "v2 delivery claim manifest_sha256 is invalid"
    lead_count = claim.get("lead_count")
    if not isinstance(lead_count, int) or isinstance(lead_count, bool) or lead_count < 0:
        return "v2 delivery claim lead_count is invalid"
    try:
        manifest_path, relative = _path_inside_project(
            root, str(claim["manifest_path"]), label="claim manifest path"
        )
    except ValueError as exc:
        return str(exc)
    if not relative.startswith("outputs/") or not manifest_path.is_file():
        return "claim manifest is missing from project outputs/"
    if _file_sha256(manifest_path) != claim["manifest_sha256"]:
        return "claim manifest changed after claim acquisition"
    return None


def _validate_receipt(
    receipt: object,
    *,
    profile_id: str,
    local_date: str,
    claim: dict[str, object] | None,
) -> tuple[bool, str | None, bool]:
    if not isinstance(receipt, dict):
        return False, "send receipt must be an object", False
    run_type = receipt.get("run_type")
    core_valid = (
        receipt.get("profile_id") == profile_id
        and receipt.get("local_date") == local_date
        and isinstance(run_type, str)
        and RUN_TYPE_PATTERN.fullmatch(run_type) is not None
        and receipt.get("status") == "sent"
        and isinstance(receipt.get("message_id"), str)
        and bool(receipt.get("message_id").strip())
    )
    if not core_valid:
        return False, "send receipt fields do not match this delivery date", False
    v2_names = {
        "manifest_sha256",
        "claim_token",
        "sender",
        "recipient",
        "outcome",
        "reason_code",
        "lead_count",
        "thread_id",
    }
    is_v2 = receipt.get("receipt_version") == 2 or any(name in receipt for name in v2_names)
    if not is_v2:
        return True, None, True
    required_strings = (
        "manifest_sha256",
        "claim_token",
        "sender",
        "recipient",
        "outcome",
        "reason_code",
        "thread_id",
    )
    if receipt.get("receipt_version") != 2:
        return False, "v2 receipt fields require receipt_version 2", False
    if any(not isinstance(receipt.get(name), str) or not receipt[name].strip() for name in required_strings):
        return False, "v2 send receipt is missing required fields", False
    if not SHA256_PATTERN.fullmatch(str(receipt["manifest_sha256"])):
        return False, "v2 receipt manifest_sha256 is invalid", False
    lead_count = receipt.get("lead_count")
    if not isinstance(lead_count, int) or isinstance(lead_count, bool) or lead_count < 0:
        return False, "v2 receipt lead_count is invalid", False
    if receipt.get("outcome") not in OUTCOMES:
        return False, "v2 receipt outcome is invalid", False
    if (receipt.get("outcome") == "lead_email" and lead_count < 1) or (
        receipt.get("outcome") != "lead_email" and lead_count != 0
    ):
        return False, "v2 receipt outcome and lead_count are inconsistent", False
    if claim is None or claim.get("claim_version") != 2:
        return False, "v2 receipt has no matching v2 delivery claim", False
    comparisons = {
        "manifest_sha256": "manifest_sha256",
        "claim_token": "token",
        "sender": "sender",
        "recipient": "recipient",
        "outcome": "outcome",
        "lead_count": "lead_count",
    }
    mismatches = [
        receipt_name
        for receipt_name, claim_name in comparisons.items()
        if receipt.get(receipt_name) != claim.get(claim_name)
    ]
    if mismatches:
        return False, "v2 receipt/claim mismatch for: " + ", ".join(mismatches), False
    return True, None, False


def _legacy_receipt_compatibility_error(
    root: Path,
    receipt_path: Path,
    receipt: dict[str, object],
    *,
    profile_id: str,
    local_date: str,
) -> tuple[str | None, str | None]:
    """Require an audited project-local record for the exact legacy bytes."""
    compatibility_path = root / "state" / "legacy-receipt-compatibility.json"
    if not compatibility_path.is_file():
        return "legacy receipt is not covered by an audited exact-hash compatibility record", None
    try:
        compatibility = _read_json(compatibility_path)
    except (OSError, json.JSONDecodeError) as exc:
        return f"legacy receipt compatibility state is invalid: {exc}", None
    if not isinstance(compatibility, dict) or compatibility.get("compatibility_version") != 1:
        return "legacy receipt compatibility state must use compatibility_version 1", None
    records = compatibility.get("records")
    if not isinstance(records, dict):
        return "legacy receipt compatibility records must be keyed by receipt SHA-256", None
    receipt_sha256 = _file_sha256(receipt_path)
    record = records.get(receipt_sha256)
    if not isinstance(record, dict):
        return "legacy receipt exact hash is not present in compatibility records", None
    try:
        receipt_relative = receipt_path.resolve().relative_to(root).as_posix()
    except ValueError:
        return "legacy receipt must remain inside the project root", None
    expected = {
        "profile_id": profile_id,
        "local_date": local_date,
        "run_type": receipt.get("run_type"),
        "receipt_path": receipt_relative,
    }
    mismatches = [name for name, value in expected.items() if record.get(name) != value]
    if mismatches:
        return "legacy compatibility record mismatch for: " + ", ".join(sorted(mismatches)), None
    for field in ("audited_at", "audited_by", "basis"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            return f"legacy compatibility record requires nonempty {field}", None
    try:
        audited_at = datetime.fromisoformat(str(record["audited_at"]))
        if audited_at.tzinfo is None:
            raise ValueError
    except ValueError:
        return "legacy compatibility audited_at must be timezone-aware ISO-8601", None
    date_role = record.get("date_role", "primary")
    if date_role not in {"primary", "historical_additional"}:
        return "legacy compatibility date_role must be primary or historical_additional", None
    return None, str(date_role)


def _timestamp_age_seconds(value: dict[str, object], field: str) -> float | None:
    try:
        timestamp = datetime.fromisoformat(str(value[field]))
        if timestamp.tzinfo is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds())
    except (KeyError, TypeError, ValueError):
        return None


def _completion_event_identity(event: dict[str, object]) -> str | None:
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id.strip():
        return event_id
    idempotency_key = event.get("idempotency_key")
    message_id = event.get("message_id")
    if (
        isinstance(idempotency_key, str)
        and idempotency_key.strip()
        and isinstance(message_id, str)
        and message_id.strip()
    ):
        return f"legacy:{idempotency_key}:{message_id}"
    return None


def inspect_run(
    project_root: str | Path,
    *,
    profile_id: str,
    local_date: str,
    run_type: str,
) -> dict[str, object]:
    """Inspect eligibility and delivery state without creating files or claims."""
    root = Path(project_root).expanduser().resolve()
    _validate_identity(profile_id, local_date, run_type)
    profile = _read_json(root / "profile.json")
    if not isinstance(profile, dict) or profile.get("profile_id") != profile_id:
        raise ValueError("profile mismatch for run validation")
    key = f"{profile_id}:{local_date}:{run_type}"
    receipt_relative = f"outputs/{local_date}-{run_type}-send-receipt.json"
    base = {
        "profile_id": profile_id,
        "local_date": local_date,
        "run_type": run_type,
        "idempotency_key": key,
        "receipt": receipt_relative,
        "phase": "preflight",
        "should_send": False,
    }

    receipt_records: list[tuple[Path, dict[str, object], bool, str]] = []
    receipt_errors: list[str] = []
    outputs = root / "outputs"
    # Scan every receipt in this isolated project. Filtering only by filename can
    # miss a second receipt whose filename is wrong but whose body claims the
    # same profile/date delivery identity.
    for receipt_path in sorted(outputs.glob("*-send-receipt.json")) if outputs.is_dir() else []:
        try:
            receipt = _read_json(receipt_path)
        except (OSError, json.JSONDecodeError) as exc:
            if receipt_path.name.startswith(f"{local_date}-"):
                receipt_errors.append(f"invalid send receipt {receipt_path.name}: {exc}")
            continue
        body_matches_date = (
            isinstance(receipt, dict)
            and receipt.get("profile_id") == profile_id
            and receipt.get("local_date") == local_date
        )
        filename_matches_date = receipt_path.name.startswith(f"{local_date}-")
        if not body_matches_date and not filename_matches_date:
            continue
        receipt_run_type = receipt.get("run_type") if isinstance(receipt, dict) else None
        receipt_claim = None
        if isinstance(receipt_run_type, str) and RUN_TYPE_PATTERN.fullmatch(receipt_run_type):
            receipt_key = f"{profile_id}:{local_date}:{receipt_run_type}"
            receipt_claim_path, _ = _claim_paths(root, local_date, receipt_run_type)
            receipt_claim, claim_error = _read_claim(receipt_claim_path, key=receipt_key)
            if receipt_claim is not None and claim_error is None:
                claim_error = _validate_claim_integrity(root, receipt_claim)
            if claim_error:
                receipt_errors.append(f"{receipt_path.name}: {claim_error}")
        valid, error, legacy = _validate_receipt(
            receipt,
            profile_id=profile_id,
            local_date=local_date,
            claim=receipt_claim,
        )
        compatibility_date_role = "primary"
        compatibility_checked = False
        if (
            not valid
            and error == "v2 receipt fields require receipt_version 2"
            and isinstance(receipt, dict)
        ):
            # Grandfather only the exact preserved bytes; without a matching
            # audit record, retain the original partial-v2 validation error.
            compatibility_error, compatibility_date_role = (
                _legacy_receipt_compatibility_error(
                    root,
                    receipt_path,
                    receipt,
                    profile_id=profile_id,
                    local_date=local_date,
                )
            )
            compatibility_checked = True
            if compatibility_error is None:
                valid, error, legacy = True, None, True
        if not valid:
            receipt_errors.append(f"{receipt_path.name}: {error}")
        elif isinstance(receipt, dict):
            expected_name = f"{local_date}-{receipt['run_type']}-send-receipt.json"
            if receipt_path.name != expected_name:
                receipt_errors.append(f"{receipt_path.name}: receipt filename does not match run identity")
            else:
                compatibility_error, compatibility_date_role = (
                    (None, compatibility_date_role)
                    if compatibility_checked
                    else _legacy_receipt_compatibility_error(
                        root,
                        receipt_path,
                        receipt,
                        profile_id=profile_id,
                        local_date=local_date,
                    )
                    if legacy
                    else (None, "primary")
                )
                if compatibility_error:
                    receipt_errors.append(f"{receipt_path.name}: {compatibility_error}")
                else:
                    receipt_records.append(
                        (receipt_path, receipt, legacy, str(compatibility_date_role))
                    )

    ledger_completions: list[dict[str, object]] = []
    alert_resolutions: list[dict[str, object]] = []
    ledger_error = None
    ledger_path = root / "state" / "run-ledger.jsonl"
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError(f"line {line_number} is not an object")
            if (
                event.get("profile_id") == profile_id
                and event.get("local_date") == local_date
                and event.get("event") in {"completed", "send_completed"}
            ):
                ledger_completions.append(event)
            if (
                event.get("profile_id") == profile_id
                and event.get("local_date") == local_date
                and event.get("event") == "missed_run_alert_resolved"
            ):
                alert_resolutions.append(event)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        ledger_error = f"invalid run ledger: {exc}"

    alert_exists = False
    alert_lookup_error = None
    alerts_path = root / "state" / "missed-run-alerts.json"
    if alerts_path.exists():
        try:
            alerts = _read_json(alerts_path)
            if not isinstance(alerts, list):
                raise ValueError("missed-run-alerts.json must contain a list")
            alert_exists = any(
                isinstance(alert, dict)
                and alert.get("profile_id") == profile_id
                and alert.get("local_date") == local_date
                for alert in alerts
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            alert_lookup_error = str(exc)
    primary_receipts = [record for record in receipt_records if record[3] == "primary"]
    historical_additional_receipts = [
        record for record in receipt_records if record[3] == "historical_additional"
    ]
    valid_receipt_paths = {
        receipt_path.relative_to(root).as_posix()
        for receipt_path, _receipt, _legacy, _date_role in receipt_records
    }
    completion_event_ids = {
        identity
        for event in ledger_completions
        if (identity := _completion_event_identity(event)) is not None
    }
    valid_alert_resolutions = [
        event
        for event in alert_resolutions
        if event.get("receipt_path") in valid_receipt_paths
        and event.get("completion_event_id") in completion_event_ids
    ]
    base["missed_run_alert_state"] = (
        "resolved" if alert_exists and valid_alert_resolutions else "unresolved" if alert_exists else "not_recorded"
    )
    if valid_alert_resolutions:
        base["alert_resolution_count"] = len(valid_alert_resolutions)
    if alert_lookup_error:
        base["alert_lookup_error"] = alert_lookup_error

    invalid_date_roles = len(primary_receipts) > 1 or (
        bool(historical_additional_receipts) and not primary_receipts
    )
    if receipt_errors or ledger_error or invalid_date_roles:
        errors = [*receipt_errors]
        if ledger_error:
            errors.append(ledger_error)
        if len(primary_receipts) > 1:
            errors.append("multiple candidate-facing receipts exist for one local date")
        elif historical_additional_receipts and not primary_receipts:
            errors.append("historical additional receipts require one audited primary receipt")
        return {
            **base,
            "status": "inconsistent",
            "recovery_action": "manual_reconciliation",
            "errors": errors,
        }
    if primary_receipts:
        receipt_path, receipt, legacy, _date_role = primary_receipts[0]
        receipt_run_type = receipt["run_type"]
        allowed_completion_run_types = {
            record_receipt["run_type"]
            for _record_path, record_receipt, _record_legacy, _record_role in receipt_records
        }
        matching_completion = any(
            event.get("run_type") == receipt_run_type
            and event.get("idempotency_key") in (
                None,
                f"{profile_id}:{local_date}:{receipt_run_type}",
            )
            for event in ledger_completions
        )
        mismatched_completions = [
            event
            for event in ledger_completions
            if event.get("run_type") not in allowed_completion_run_types
        ]
        if mismatched_completions:
            return {
                **base,
                "status": "inconsistent",
                "recovery_action": "manual_reconciliation",
                "errors": ["completion state conflicts with the one delivery receipt for this date"],
            }
        result = {
            **base,
            "status": "complete" if matching_completion else "recovery_required",
            "recovery_action": "none" if matching_completion else "write_state_only",
            "date_delivery_receipt": receipt_path.relative_to(root).as_posix(),
            "delivered_run_type": receipt_run_type,
            "receipt_version": 1 if legacy else 2,
        }
        if historical_additional_receipts:
            result["historical_additional_receipt_count"] = len(
                historical_additional_receipts
            )
        return result
    if ledger_completions:
        return {
            **base,
            "status": "recovery_required",
            "recovery_action": "reconcile_receipt",
            "errors": ["completion state exists without a valid send receipt"],
        }

    try:
        activation_errors = _profile_validation_errors(root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        activation_errors = [f"activation validation failed: {exc}"]
    try:
        automation = _read_json(root / "automation.json")
    except (OSError, json.JSONDecodeError) as exc:
        automation = None
        activation_errors.append(f"automation activation state is invalid: {exc}")
    active_state = (
        profile.get("status") == "active"
        and isinstance(automation, dict)
        and automation.get("profile_id") == profile_id
        and automation.get("status") == "active"
    )
    if not active_state:
        activation_errors.append("profile and automation must both be active")
    if isinstance(automation, dict):
        one_test_only = automation.get("cadence") == "one-test-only"
        if one_test_only and run_type != "one-test-only":
            activation_errors.append("one-test-only configuration permits only the one-test-only run type")
        if run_type == "one-test-only" and not one_test_only:
            activation_errors.append("one-test-only run type requires one-test-only configuration")
    claim_files = sorted((root / "state" / "run-claims").glob(f"{local_date}-*.claim.json")) if (root / "state" / "run-claims").is_dir() else []
    if claim_files:
        current_claim_path, provider_path = _claim_paths(root, local_date, run_type)
        if len(claim_files) != 1 or current_claim_path not in claim_files:
            return {
                **base,
                "status": "manual_reconciliation_required",
                "recovery_action": "manual_reconciliation",
                "errors": ["another candidate-facing delivery claim exists for this local date"],
            }
        claim, claim_error = _read_claim(current_claim_path, key=key)
        if claim is not None and claim_error is None:
            claim_error = _validate_claim_integrity(root, claim)
        if claim_error or claim is None:
            return {
                **base,
                "status": "inconsistent",
                "recovery_action": "manual_reconciliation",
                "errors": [claim_error or "delivery claim is unavailable"],
            }
        provider_state = None
        if provider_path.exists():
            try:
                provider_state = _read_json(provider_path)
            except (OSError, json.JSONDecodeError) as exc:
                return {
                    **base,
                    "status": "inconsistent",
                    "recovery_action": "manual_reconciliation",
                    "errors": [f"invalid provider state: {exc}"],
                }
        if isinstance(provider_state, dict):
            allowed_provider_states = {
                "rejected_safe_to_retry",
                "retry_in_progress",
                "provider_ambiguous",
            }
            if (
                provider_state.get("state") not in allowed_provider_states
                or provider_state.get("claim_token") != claim.get("token")
                or provider_state.get("manifest_sha256") != claim.get("manifest_sha256")
                or not isinstance(provider_state.get("attempt_token"), str)
                or not provider_state.get("attempt_token")
            ):
                return {
                    **base,
                    "status": "inconsistent",
                    "recovery_action": "manual_reconciliation",
                    "errors": ["provider state does not match the preserved delivery claim"],
                }
        claim_relative = current_claim_path.relative_to(root).as_posix()
        if isinstance(provider_state, dict) and provider_state.get("state") == "rejected_safe_to_retry":
            if activation_errors:
                return {
                    **base,
                    "status": "blocked",
                    "recovery_action": "none",
                    "claim": claim_relative,
                    "errors": activation_errors,
                }
            return {
                **base,
                "status": "retryable",
                "recovery_action": "acquire_same_manifest_retry",
                "claim": claim_relative,
                "manifest_sha256": claim.get("manifest_sha256"),
            }
        if isinstance(provider_state, dict) and provider_state.get("state") == "provider_ambiguous":
            return {
                **base,
                "status": "manual_reconciliation_required",
                "recovery_action": "manual_reconciliation",
                "claim": claim_relative,
                "errors": ["provider outcome is ambiguous; preserve the claim"],
            }
        age_source = provider_state if isinstance(provider_state, dict) else claim
        age_field = "updated_at" if isinstance(provider_state, dict) else "claimed_at"
        age = _timestamp_age_seconds(age_source, age_field)
        if age is None or age > CLAIM_STALE_SECONDS:
            return {
                **base,
                "status": "manual_reconciliation_required",
                "recovery_action": "manual_reconciliation",
                "claim": claim_relative,
                "errors": ["existing claim delivery status cannot be proven"],
            }
        return {
            **base,
            "status": "in_progress",
            "recovery_action": "wait",
            "claim": claim_relative,
        }

    if activation_errors:
        return {
            **base,
            "status": "blocked",
            "recovery_action": "none",
            "errors": activation_errors,
        }

    return {
        **base,
        "status": "ready",
        "recovery_action": "none",
    }


def validate_run(
    project_root: str | Path,
    *,
    profile_id: str,
    local_date: str,
    run_type: str,
) -> dict[str, object]:
    """Backward-compatible name for the now read-only preflight inspection."""
    return inspect_run(
        project_root,
        profile_id=profile_id,
        local_date=local_date,
        run_type=run_type,
    )


def _claim_matches_binding(claim: dict[str, object], binding: dict[str, object]) -> bool:
    return all(claim.get(name) == value for name, value in binding.items())


def acquire_delivery_claim(
    project_root: str | Path,
    *,
    profile_id: str,
    local_date: str,
    run_type: str,
    manifest_path: str | Path,
    manifest_sha256: str,
    outcome: str,
    lead_count: int,
    sender: str,
    recipient: str,
) -> dict[str, object]:
    """Acquire the final atomic claim for an already rendered, immutable manifest."""
    root = Path(project_root).expanduser().resolve()
    _validate_identity(profile_id, local_date, run_type)
    if local_date != _current_profile_date(root):
        return {
            "profile_id": profile_id,
            "local_date": local_date,
            "run_type": run_type,
            "idempotency_key": f"{profile_id}:{local_date}:{run_type}",
            "phase": "claim",
            "status": "blocked",
            "should_send": False,
            "recovery_action": "none",
            "errors": ["delivery claims may be acquired only for the candidate's current local date"],
        }
    relative_manifest, _manifest = _validate_manifest_binding(
        root,
        profile_id=profile_id,
        local_date=local_date,
        run_type=run_type,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        outcome=outcome,
        lead_count=lead_count,
        sender=sender,
        recipient=recipient,
    )
    preflight = inspect_run(
        root,
        profile_id=profile_id,
        local_date=local_date,
        run_type=run_type,
    )
    if preflight["status"] not in {"ready", "retryable"}:
        return {**preflight, "phase": "claim"}
    attempt_error = _staged_attempt_error(
        root,
        profile_id=profile_id,
        local_date=local_date,
        run_type=run_type,
        manifest_path=relative_manifest,
        manifest_sha256=manifest_sha256,
    )
    if attempt_error:
        return {
            **preflight,
            "phase": "claim",
            "status": "blocked",
            "should_send": False,
            "recovery_action": "none",
            "errors": [attempt_error],
        }
    binding = {
        "manifest_path": relative_manifest,
        "manifest_sha256": manifest_sha256,
        "outcome": outcome,
        "lead_count": lead_count,
        "sender": sender,
        "recipient": recipient,
    }
    if preflight["status"] == "ready":
        claimed, claim_relative, token = _claim_delivery(
            root,
            str(preflight["idempotency_key"]),
            local_date,
            run_type,
            **binding,
        )
        if claimed:
            return {
                **preflight,
                "phase": "claim",
                "status": "claimed",
                "should_send": True,
                "claim": claim_relative,
                "claim_token": token,
                "attempt_token": token,
                "manifest_sha256": manifest_sha256,
            }
        return inspect_run(
            root,
            profile_id=profile_id,
            local_date=local_date,
            run_type=run_type,
        )
    claim_path, provider_path = _claim_paths(root, local_date, run_type)
    claim, claim_error = _read_claim(claim_path, key=str(preflight["idempotency_key"]))
    if claim_error or claim is None or not _claim_matches_binding(claim, binding):
        return {
            **preflight,
            "phase": "claim",
            "status": "manual_reconciliation_required",
            "recovery_action": "manual_reconciliation",
            "errors": [claim_error or "retry manifest does not exactly match the preserved claim"],
        }
    lock_path = provider_path.with_name(f"{provider_path.name}.lock")
    try:
        with _bounded_file_lock(lock_path):
            provider_state = _read_json(provider_path)
            if not isinstance(provider_state, dict) or provider_state.get("state") != "rejected_safe_to_retry":
                return {**inspect_run(root, profile_id=profile_id, local_date=local_date, run_type=run_type), "phase": "claim"}
            attempt_token = uuid.uuid4().hex
            _atomic_write_json(
                provider_path,
                {
                    "claim_token": claim.get("token"),
                    "manifest_sha256": manifest_sha256,
                    "state": "retry_in_progress",
                    "attempt_token": attempt_token,
                    "attempt_number": int(provider_state.get("attempt_number", 1)) + 1,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
    except (OSError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        return {
            **preflight,
            "phase": "claim",
            "status": "manual_reconciliation_required",
            "recovery_action": "manual_reconciliation",
            "errors": [str(exc)],
        }
    return {
        **preflight,
        "phase": "claim",
        "status": "claimed_retry",
        "should_send": True,
        "claim_token": claim.get("token"),
        "attempt_token": attempt_token,
        "manifest_sha256": manifest_sha256,
    }


def record_provider_result(
    project_root: str | Path,
    *,
    profile_id: str,
    local_date: str,
    run_type: str,
    claim_token: str,
    attempt_token: str,
    disposition: str,
    provider_status: str = "",
) -> dict[str, object]:
    """Record a failed provider attempt without weakening the delivery claim."""
    if disposition not in {"rejected_safe_to_retry", "provider_ambiguous"}:
        raise ValueError("disposition must be rejected_safe_to_retry or provider_ambiguous")
    if not isinstance(provider_status, str) or len(provider_status) > 256 or "\n" in provider_status or "\r" in provider_status:
        raise ValueError("provider_status must be a single line of at most 256 characters")
    root = Path(project_root).expanduser().resolve()
    _validate_identity(profile_id, local_date, run_type)
    key = f"{profile_id}:{local_date}:{run_type}"
    claim_path, provider_path = _claim_paths(root, local_date, run_type)
    claim, claim_error = _read_claim(claim_path, key=key)
    if claim_error or claim is None:
        raise ValueError(claim_error or "delivery claim is missing")
    if claim.get("token") != claim_token:
        raise ValueError("claim token mismatch")
    lock_path = provider_path.with_name(f"{provider_path.name}.lock")
    with _bounded_file_lock(lock_path):
        current = _read_json(provider_path) if provider_path.exists() else None
        if current is not None and (
            not isinstance(current, dict) or current.get("state") != "retry_in_progress"
        ):
            raise ValueError("provider result is already terminal and cannot be replaced")
        current_attempt = current.get("attempt_token") if isinstance(current, dict) else claim.get("attempt_token")
        if current_attempt != attempt_token:
            raise ValueError("attempt token mismatch")
        attempt_number = int(current.get("attempt_number", 1)) if isinstance(current, dict) else 1
        value = {
            "claim_token": claim_token,
            "manifest_sha256": claim.get("manifest_sha256"),
            "state": disposition,
            "attempt_token": attempt_token,
            "attempt_number": attempt_number,
            "provider_status": str(provider_status),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(provider_path, value)
    return value


def record_missed_alert(
    project_root: str | Path,
    *,
    profile_id: str,
    local_date: str,
    reason: str,
) -> bool:
    root = Path(project_root).expanduser().resolve()
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("profile_id is required")
    _valid_calendar_date(local_date)
    project_profile = _read_json(root / "profile.json")
    if (
        not isinstance(project_profile, dict)
        or project_profile.get("profile_id") != profile_id
    ):
        raise ValueError("profile mismatch for missed-run alert")
    paths = project_profile.get("paths")
    configured = paths.get("missed_run_alerts") if isinstance(paths, dict) else None
    if not isinstance(configured, str) or not configured:
        raise ValueError("project profile is missing the missed alert state path")
    configured_path = Path(configured)
    if configured_path.is_absolute():
        raise ValueError("missed alert state path must be project-relative")
    path = (root / configured_path).resolve()
    expected_path = (root / "state" / "missed-run-alerts.json").resolve()
    if path != expected_path or path.parent != (root / "state").resolve():
        raise ValueError("missed alert state path must remain in the exact project state directory")

    lock_path = path.with_name(f"{path.name}.lock")
    with _bounded_file_lock(lock_path):
        if path.exists():
            alerts = _read_json(path)
        else:
            alerts = []
        if not isinstance(alerts, list):
            raise ValueError("missed-run-alerts.json must contain a list")
        if any(
            isinstance(alert, dict)
            and alert.get("profile_id") == profile_id
            and alert.get("local_date") == local_date
            for alert in alerts
        ):
            return False
        alerts.append(
            {
                "profile_id": profile_id,
                "local_date": local_date,
                "reason": str(reason),
            }
        )
        _atomic_write_json(path, alerts)
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    parser.add_argument("profile_id")
    parser.add_argument("local_date")
    parser.add_argument("run_type")
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--acquire-claim",
        action="store_true",
        help="acquire the final delivery claim after a successful read-only preflight",
    )
    action.add_argument(
        "--record-provider-result",
        action="store_true",
        help="record a rejected or ambiguous provider attempt for an existing claim",
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--outcome", choices=sorted(OUTCOMES))
    parser.add_argument("--lead-count", type=int)
    parser.add_argument("--sender")
    parser.add_argument("--recipient")
    parser.add_argument("--claim-token")
    parser.add_argument("--attempt-token")
    parser.add_argument(
        "--disposition",
        choices=("rejected_safe_to_retry", "provider_ambiguous"),
    )
    parser.add_argument("--provider-status", default="")
    args = parser.parse_args()
    if args.acquire_claim:
        missing = [
            name
            for name, value in (
                ("--manifest", args.manifest),
                ("--manifest-sha256", args.manifest_sha256),
                ("--outcome", args.outcome),
                ("--lead-count", args.lead_count),
                ("--sender", args.sender),
                ("--recipient", args.recipient),
            )
            if value is None
        ]
        if missing:
            parser.error("--acquire-claim requires " + ", ".join(missing))
        result = acquire_delivery_claim(
            args.project_root,
            profile_id=args.profile_id,
            local_date=args.local_date,
            run_type=args.run_type,
            manifest_path=args.manifest,
            manifest_sha256=args.manifest_sha256,
            outcome=args.outcome,
            lead_count=args.lead_count,
            sender=args.sender,
            recipient=args.recipient,
        )
    elif args.record_provider_result:
        missing = [
            name
            for name, value in (
                ("--claim-token", args.claim_token),
                ("--attempt-token", args.attempt_token),
                ("--disposition", args.disposition),
            )
            if value is None
        ]
        if missing:
            parser.error("--record-provider-result requires " + ", ".join(missing))
        result = record_provider_result(
            args.project_root,
            profile_id=args.profile_id,
            local_date=args.local_date,
            run_type=args.run_type,
            claim_token=args.claim_token,
            attempt_token=args.attempt_token,
            disposition=args.disposition,
            provider_status=args.provider_status,
        )
    else:
        result = validate_run(
            args.project_root,
            profile_id=args.profile_id,
            local_date=args.local_date,
            run_type=args.run_type,
        )
    print(
        json.dumps(
            result,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
