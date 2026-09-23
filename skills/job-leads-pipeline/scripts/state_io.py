#!/usr/bin/env python3
"""Safe state helpers for isolated job-leads projects."""

from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "referrer",
    "source",
}
_CSV_PATH_KEYS = {
    "pipeline.csv": "pipeline",
    "evaluated-roles.csv": "evaluated_roles",
    "feedback-log.csv": "feedback_log",
}
_RUN_TYPE_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
LOCK_ATTEMPTS = 600
LOCK_RETRY_SECONDS = 0.005
LOCK_STALE_SECONDS = 60.0


def _normalize_words(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _normalize_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = parts.scheme.casefold()
    hostname = (parts.hostname or "").casefold()
    if not scheme or not hostname:
        return raw.casefold().rstrip("/")
    port = parts.port
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        normalized_key = key.casefold()
        if normalized_key.startswith("utm_") or normalized_key in _TRACKING_KEYS:
            continue
        query.append((key, item))
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def canonical_dedupe_key(row: dict[str, object]) -> str:
    """Return a profile-scoped key, preferring a canonical URL."""
    profile_id = _normalize_words(row.get("profile_id"))
    link = _normalize_url(row.get("link"))
    if link:
        return f"{profile_id}|url|{link}"
    company = _normalize_words(row.get("company"))
    role = _normalize_words(row.get("role"))
    return f"{profile_id}|text|{company}|{role}"


def _require_profile(value: object, expected_profile_id: str) -> None:
    if value != expected_profile_id:
        raise ValueError(
            f"profile mismatch: expected {expected_profile_id!r}, got {value!r}"
        )


@contextmanager
def _bounded_file_lock(lock_path: Path):
    token = uuid.uuid4().hex
    thread_ident = threading.get_ident()
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
                    f"manual_reconciliation_required: existing ledger lock was preserved: {lock_path}"
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
            f"manual_reconciliation_required: could not acquire preserved ledger lock: {lock_path}"
        )
    try:
        yield
    finally:
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8"))
            owns_current_lock = (
                isinstance(current, dict) and current.get("token") == token
            )
        except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
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


def _validated_date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("local date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("local date must be a valid calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError("local date must use canonical YYYY-MM-DD format")
    return value


def _validated_run_type(value: object) -> str:
    if not isinstance(value, str) or not _RUN_TYPE_PATTERN.fullmatch(value):
        raise ValueError("run_type must be lowercase letters, digits, hyphens, or underscores")
    return value


def _validate_state_path(
    path: str | Path,
    *,
    expected_profile_id: str,
    profile_path_key: str,
) -> Path:
    """Derive and verify the initialized project boundary from a state path."""
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=False)
        state_root = resolved.parent
        project_root = state_root.parent
    except OSError as exc:
        raise ValueError(f"invalid project state path: {exc}") from exc
    if state_root.name != "state" or state_root != (project_root / "state").resolve():
        raise ValueError("state path must be inside the initialized project state directory")

    profile_path = project_root / "profile.json"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"project profile configuration is unavailable: {exc}") from exc
    if not isinstance(profile, dict):
        raise ValueError("project profile configuration must be an object")
    _require_profile(profile.get("profile_id"), expected_profile_id)
    paths = profile.get("paths")
    configured = paths.get(profile_path_key) if isinstance(paths, dict) else None
    if not isinstance(configured, str) or not configured:
        raise ValueError(f"project profile is missing the {profile_path_key} state path")
    configured_path = Path(configured)
    if configured_path.is_absolute():
        raise ValueError("project state path configuration must be relative")
    try:
        configured_resolved = (project_root / configured_path).resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"invalid configured project state path: {exc}") from exc
    if configured_resolved != resolved or configured_resolved.parent != state_root:
        raise ValueError("state path does not match the initialized project profile")
    return resolved


def append_csv_row(
    path: str | Path,
    row: dict[str, object],
    *,
    expected_profile_id: str,
) -> bool:
    """Append one row only when it belongs to the expected profile."""
    _require_profile(row.get("profile_id"), expected_profile_id)
    candidate = Path(path)
    profile_path_key = _CSV_PATH_KEYS.get(candidate.name)
    if profile_path_key is None:
        raise ValueError("CSV path is not a configured project state file")
    csv_path = _validate_state_path(
        candidate,
        expected_profile_id=expected_profile_id,
        profile_path_key=profile_path_key,
    )
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        headers = next(reader, None)
        for row_number, existing_row in enumerate(reader, start=2):
            if len(existing_row) != len(headers or []):
                raise ValueError(
                    f"CSV state row {row_number} has the wrong column count"
                )
    if not headers or headers[0] != "profile_id":
        raise ValueError("CSV must have a profile_id-first header")
    if set(row) != set(headers):
        raise ValueError("row fields must exactly match the CSV schema")
    with csv_path.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=headers).writerow(row)
    return True


def make_idempotency_key(
    profile_id: str, local_date: str, run_type: str
) -> str:
    if not isinstance(profile_id, str) or not profile_id or ":" in profile_id:
        raise ValueError("profile_id is invalid for an idempotency key")
    valid_date = _validated_date(local_date)
    valid_run_type = _validated_run_type(run_type)
    return f"{profile_id}:{valid_date}:{valid_run_type}"


def append_run_event(
    path: str | Path,
    event: dict[str, object],
    *,
    expected_profile_id: str,
) -> bool:
    """Append a ledger event, suppressing exact idempotent-event retries."""
    _require_profile(event.get("profile_id"), expected_profile_id)
    ledger_path = _validate_state_path(
        path,
        expected_profile_id=expected_profile_id,
        profile_path_key="run_ledger",
    )
    if not ledger_path.is_file():
        raise ValueError("initialized project run ledger state file is missing")

    if "local_date" in event:
        _validated_date(event.get("local_date"))
    if "run_type" in event:
        _validated_run_type(event.get("run_type"))

    candidate_key = event.get("idempotency_key")
    candidate_event = event.get("event")
    if candidate_key is not None:
        if not all(field in event for field in ("profile_id", "local_date", "run_type")):
            raise ValueError("idempotency key requires profile_id, local_date, and run_type")
        expected_key = make_idempotency_key(
            str(event["profile_id"]), str(event["local_date"]), str(event["run_type"])
        )
        if candidate_key != expected_key:
            raise ValueError("idempotency key does not match the event payload")
    lock_path = ledger_path.with_name(f"{ledger_path.name}.lock")
    with _bounded_file_lock(lock_path):
        if candidate_key:
            for line_number, line in enumerate(
                ledger_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid run ledger JSON at line {line_number}"
                    ) from exc
                if not isinstance(existing, dict):
                    raise ValueError(f"invalid run ledger event at line {line_number}")
                if (
                    existing.get("idempotency_key") == candidate_key
                    and existing.get("event") == candidate_event
                ):
                    if existing == event:
                        return False
                    raise ValueError(
                        "idempotency conflict: existing event payload differs"
                    )

        with ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
