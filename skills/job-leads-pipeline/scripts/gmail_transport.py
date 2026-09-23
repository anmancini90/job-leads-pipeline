#!/usr/bin/env python3
"""Send a claimed, frozen daily package with a personal Gmail API account.

Credentials live in the operating system's keyring, never in the candidate
project. A send requires the shared delivery claim and an explicit CLI flag.
No exception after submitting to Gmail causes an automatic retry.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path
from typing import Any


SCOPES = ("https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly")
KEYRING_SERVICE = "job-leads-pipeline-gmail-v1"
BASE64URL = re.compile(r"[A-Za-z0-9_-]*={0,2}\Z")
SECURE_KEYRING_BACKENDS = {
    "win32": {"keyring.backends.Windows.WinVaultKeyring": "Windows Credential Manager"},
    "darwin": {"keyring.backends.macOS.Keyring": "macOS Keychain"},
    "linux": {
        "keyring.backends.SecretService.Keyring": "Freedesktop Secret Service",
        "keyring.backends.libsecret.Keyring": "Freedesktop Secret Service (libsecret)",
        "keyring.backends.kwallet.DBusKeyring": "KDE KWallet",
        "keyring.backends.kwallet.DBusKeyringKWallet4": "KDE KWallet 4",
    },
}


def _module(name: str):
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("job_leads_" + name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"missing bundled {name} script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _address(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        raise ValueError(f"{label} must be one email address")
    display, address = parseaddr(value)
    if display or address != value or value.count("@") != 1 or any(c.isspace() for c in value):
        raise ValueError(f"{label} must be one plain email address")
    return address


def _single_line(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{label} must be a nonempty single line")
    return value


def _decode64(value: Any) -> bytes:
    if not isinstance(value, str) or not BASE64URL.fullmatch(value):
        raise ValueError("attachment content must be base64url text")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64url attachment") from exc


def _expected_attachments(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = manifest.get("attachments")
    if not isinstance(entries, list):
        raise ValueError("manifest attachments must be a list")
    expected: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid attachment metadata")
        name = entry.get("filename")
        if not isinstance(name, str) or not name or name in expected:
            raise ValueError("duplicate or missing attachment filename")
        if entry.get("mime_type") != "application/pdf":
            raise ValueError("only PDF resume attachments are supported")
        if type(entry.get("size_bytes")) is not int or entry["size_bytes"] < 0:
            raise ValueError("invalid attachment size")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("invalid attachment hash")
        expected[name] = entry
    return expected


def verify_mime(raw: bytes, *, sender: str, recipient: str, subject: str,
                expected: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compare decoded PDF bytes, not merely filenames or provider metadata."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message["From"] != sender or message["To"] != recipient or message["Subject"] != subject:
        raise ValueError("serialized Gmail headers differ from the frozen request")
    found: set[str] = set()
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        if filename not in expected or filename in found:
            raise ValueError("unexpected or duplicate MIME attachment")
        found.add(filename)
        data = part.get_payload(decode=True)
        wanted = expected[filename]
        if (part.get_content_type() != wanted["mime_type"] or data is None
                or len(data) != wanted["size_bytes"] or _digest(data) != wanted["sha256"]):
            raise ValueError(f"MIME PDF bytes differ: {filename}")
    if found != set(expected):
        raise ValueError("MIME message is missing a frozen PDF attachment")
    return {"attachment_count": len(found), "attachment_sha256": {name: expected[name]["sha256"] for name in sorted(found)}}


def build_mime(request: dict[str, Any], manifest: dict[str, Any]) -> bytes:
    """Convert the frozen structured request to RFC 5322 MIME, then self-check."""
    sender = _address(request.get("from_address"), "sender")
    recipient = _address(request.get("to"), "recipient")
    subject = _single_line(request.get("subject"), "subject")
    if (sender, recipient, subject) != (manifest.get("sender"), manifest.get("recipient"), manifest.get("subject")):
        raise ValueError("Gmail request headers differ from manifest")
    payload = request.get("payload")
    if not isinstance(payload, dict) or payload.get("mime_type") != "multipart/mixed":
        raise ValueError("Gmail request must be multipart/mixed")
    parts = payload.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("Gmail request is missing parts")
    alternative = parts[0]
    if not isinstance(alternative, dict) or alternative.get("mime_type") != "multipart/alternative":
        raise ValueError("Gmail request is missing alternative text bodies")
    bodies = alternative.get("parts")
    if (not isinstance(bodies, list) or len(bodies) != 2
            or [part.get("mime_type") for part in bodies if isinstance(part, dict)] != ["text/plain", "text/html"]):
        raise ValueError("Gmail request needs plain text and HTML")
    text = bodies[0].get("body", {}).get("content")
    html = bodies[1].get("body", {}).get("content")
    if not isinstance(text, str) or not text.strip() or not isinstance(html, str) or not html.strip():
        raise ValueError("Gmail request bodies cannot be empty")
    expected = _expected_attachments(manifest)
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text, subtype="plain", charset="utf-8")
    message.add_alternative(html, subtype="html", charset="utf-8")
    observed: set[str] = set()
    for part in parts[1:]:
        if not isinstance(part, dict):
            raise ValueError("invalid Gmail PDF part")
        name = part.get("filename")
        if (name not in expected or name in observed or part.get("mime_type") != "application/pdf"
                or part.get("content_disposition") != "attachment"):
            raise ValueError("Gmail PDF part differs from manifest")
        observed.add(name)
        data = _decode64(part.get("body", {}).get("base64_url_content"))
        wanted = expected[name]
        if len(data) != wanted["size_bytes"] or _digest(data) != wanted["sha256"]:
            raise ValueError("frozen PDF bytes differ from manifest")
        message.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    if observed != set(expected):
        raise ValueError("Gmail request omits a frozen PDF")
    raw = message.as_bytes()
    verify_mime(raw, sender=sender, recipient=recipient, subject=subject, expected=expected)
    return raw


def inspect_keyring_backend(keyring_module=None, *, platform_name: str | None = None,
                            backend=None) -> dict[str, str]:
    """Identify the active storage backend; refuse file and fallback keyrings.

    The allowlist contains keyring's own OS credential-store implementations.
    Third-party wrappers are deliberately not trusted by name or priority.
    """
    platform_name = platform_name or sys.platform
    if keyring_module is None and backend is None:
        try:
            import keyring as keyring_module
        except ImportError:
            return {"status": "needs_attention", "backend": "unavailable",
                    "detail": "Install keyring and configure a supported OS credential store before connecting Gmail."}
    try:
        if backend is None:
            backend = keyring_module.get_keyring()
        identity = f"{type(backend).__module__}.{type(backend).__name__}"
    except Exception as exc:
        return {"status": "needs_attention", "backend": "unavailable",
                "detail": f"Could not inspect the active keyring backend ({type(exc).__name__}); Gmail is blocked."}
    platform_key = "linux" if platform_name.startswith("linux") else platform_name
    allowed = SECURE_KEYRING_BACKENDS.get(platform_key, {})
    if identity in allowed:
        return {"status": "ok", "backend": identity,
                "detail": f"Secure token store: {allowed[identity]}."}
    return {"status": "needs_attention", "backend": identity,
            "detail": "The active keyring is not an approved OS credential store; Gmail is blocked. "
                      "Configure Windows Credential Manager, macOS Keychain, or a Linux desktop secret service."}


def _keyring():
    """Return the checked backend instance, never a module that can switch stores."""
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("Install keyring and a supported OS credential store before connecting Gmail") from exc
    try:
        backend = keyring.get_keyring()
    except Exception as exc:
        raise RuntimeError(f"Cannot open an OS credential store ({type(exc).__name__}); Gmail is blocked") from exc
    result = inspect_keyring_backend(keyring, backend=backend)
    if result["status"] != "ok":
        raise RuntimeError(result["detail"] + f" Active backend: {result['backend']}.")
    return backend


def _google_modules():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Install the skill's Google API Python requirements") from exc
    return Request, Credentials, InstalledAppFlow, build


def connect_oauth(client_secrets: str | Path, sender: str) -> dict[str, str]:
    """Run the user's Desktop OAuth browser flow and store tokens in OS keyring."""
    sender = _address(sender, "sender")
    path = Path(client_secrets).expanduser().resolve(strict=True)
    if any((parent / ".git").exists() for parent in (path.parent, *path.parents)):
        raise ValueError("Keep the OAuth client JSON outside every Git checkout")
    client = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(client, dict) or not isinstance(client.get("installed"), dict):
        raise ValueError("Use a Google Desktop OAuth client JSON, not a web-app client")
    token_store = _keyring()  # Fail before the Google browser flow if storage is unsafe.
    _, _, InstalledAppFlow, build = _google_modules()
    flow = InstalledAppFlow.from_client_secrets_file(str(path), scopes=list(SCOPES))
    credentials = flow.run_local_server(host="localhost", port=0, open_browser=True)
    actual = build("gmail", "v1", credentials=credentials, cache_discovery=False).users().getProfile(userId="me").execute().get("emailAddress")
    if not isinstance(actual, str) or actual.casefold() != sender.casefold():
        raise ValueError("Authorized Gmail account does not match the approved sender; no token was stored")
    token_store.set_password(KEYRING_SERVICE, sender.casefold(), credentials.to_json())
    return {"status": "connected", "sender": sender, "storage": "OS keyring", "scopes": ", ".join(SCOPES)}


def gmail_service(sender: str):
    """Load and refresh one account's credentials without writing token files."""
    sender = _address(sender, "sender")
    token_store = _keyring()
    encoded = token_store.get_password(KEYRING_SERVICE, sender.casefold())
    if not encoded:
        raise ValueError("Gmail is not connected for the approved sender; run oauth-connect")
    Request, Credentials, _, build = _google_modules()
    credentials = Credentials.from_authorized_user_info(json.loads(encoded), scopes=list(SCOPES))
    if not credentials.has_scopes(SCOPES):
        raise ValueError("Saved Gmail token lacks send and read-only scopes; reconnect")
    if not credentials.valid:
        if not credentials.refresh_token:
            raise ValueError("Gmail authorization expired; reconnect")
        credentials.refresh(Request())
        token_store.set_password(KEYRING_SERVICE, sender.casefold(), credentials.to_json())
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    actual = service.users().getProfile(userId="me").execute().get("emailAddress")
    if not isinstance(actual, str) or actual.casefold() != sender.casefold():
        raise ValueError("Connected Gmail account does not match the approved sender")
    return service


def _write_new_json(path: Path, value: dict[str, Any]) -> None:
    """Publish a complete receipt without replacing another writer's evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _provider_disposition(error: Exception) -> tuple[str, str]:
    """Only an explicit non-accepting Gmail HTTP response permits a later retry."""
    status = getattr(getattr(error, "resp", None), "status", None)
    try:
        code = int(status)
    except (ValueError, TypeError):
        code = None
    if code in {400, 401, 403, 404, 413}:
        return "rejected_safe_to_retry", f"gmail_http_{code}"
    return "provider_ambiguous", f"gmail_http_{code}" if code else "gmail_result_unknown"


def verify_sent(service: Any, message_id: str, *, sender: str, recipient: str,
                subject: str, expected: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Post-send read-only check. Failure never authorizes another send."""
    observed = service.users().messages().get(userId="me", id=message_id, format="raw").execute()
    raw = _decode64(observed.get("raw"))
    details = verify_mime(raw, sender=sender, recipient=recipient, subject=subject, expected=expected)
    return {"status": "verified", **details}


def send_claimed(root: str | Path, manifest_path: str | Path, *, service: Any,
                 confirmed: bool, authorizer=None, provider_recorder=None,
                 event_appender=None) -> dict[str, Any]:
    """Validate, claim, send once, save receipt, append completion, inspect Sent.

    `service` is already authenticated and sender-verified. Injected functions
    make synthetic tests entirely offline; production uses bundled guards.
    """
    if not confirmed:
        raise ValueError("send requires --confirm-send and approved project delivery")
    root = Path(root).resolve()
    delivery = _module("daily_resume_delivery")
    validated = delivery.validate(root, manifest_path)
    path = root / validated["manifest_path"]
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    request_path = root / validated["gmail_request_path"]
    request_bytes = request_path.read_bytes()
    if _digest(request_bytes) != validated["gmail_request_sha256"]:
        raise ValueError("frozen Gmail request changed")
    request = json.loads(request_bytes.decode("utf-8-sig"))
    raw = build_mime(request, manifest)
    expected = _expected_attachments(manifest)
    # Validate the actual Gmail account immediately before crossing the claim
    # boundary. A configured From header alone cannot prove account ownership.
    actual_sender = service.users().getProfile(userId="me").execute().get("emailAddress")
    if not isinstance(actual_sender, str) or actual_sender.casefold() != manifest["sender"].casefold():
        raise ValueError("authenticated Gmail account differs from approved sender")
    claim = (authorizer or delivery.authorize_send)(root, validated["manifest_path"])
    if claim.get("should_send") is not True:
        return {"status": claim.get("status", "blocked"), "should_send": False,
                "errors": claim.get("errors", [])}
    if (claim.get("manifest_sha256") != validated["manifest_sha256"]
            or claim.get("idempotency_key") != manifest["idempotency_key"]
            or not claim.get("claim_token") or not claim.get("attempt_token")):
        raise ValueError("claim does not bind this exact frozen delivery")
    # A claim binds immutable artifacts. If another process changed a file
    # after validation, preserve the claim and stop before contacting Gmail.
    if (_digest(path.read_bytes()) != validated["manifest_sha256"]
            or _digest(request_path.read_bytes()) != validated["gmail_request_sha256"]):
        return {"status": "manual_reconciliation_required", "should_send": False,
                "retry_automatically": False,
                "message": "Frozen manifest or Gmail request changed after claim; no email was sent."}
    guard = _module("validate_run")
    recorder = provider_recorder or guard.record_provider_result
    try:
        response = service.users().messages().send(userId="me", body={
            "raw": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")}).execute()
    except Exception as exc:
        disposition, provider_status = _provider_disposition(exc)
        recorder(root, profile_id=manifest["profile_id"], local_date=manifest["local_date"],
                 run_type=manifest["run_type"], claim_token=claim["claim_token"],
                 attempt_token=claim["attempt_token"], disposition=disposition,
                 provider_status=provider_status)
        return {"status": disposition, "should_send": False, "retry_automatically": False,
                "message": "Gmail did not confirm delivery. Inspect the claim and Sent mail before any retry."}
    if not isinstance(response, dict) or not isinstance(response.get("id"), str) or not response["id"] or not isinstance(response.get("threadId"), str) or not response["threadId"]:
        recorder(root, profile_id=manifest["profile_id"], local_date=manifest["local_date"],
                 run_type=manifest["run_type"], claim_token=claim["claim_token"],
                 attempt_token=claim["attempt_token"], disposition="provider_ambiguous",
                 provider_status="gmail_response_missing_ids")
        return {"status": "provider_ambiguous", "should_send": False,
                "retry_automatically": False, "message": "Gmail response lacked message IDs; reconcile Sent mail."}
    receipt = {"receipt_version": 2, "profile_id": manifest["profile_id"],
               "local_date": manifest["local_date"], "run_type": manifest["run_type"],
               "idempotency_key": manifest["idempotency_key"], "status": "sent",
               "message_id": response["id"], "thread_id": response["threadId"],
               "manifest_sha256": validated["manifest_sha256"], "claim_token": claim["claim_token"],
               "gmail_request_sha256": validated["gmail_request_sha256"],
               "attachments": [{"filename": name, "mime_type": expected[name]["mime_type"],
                                "size_bytes": expected[name]["size_bytes"], "sha256": expected[name]["sha256"]}
                               for name in sorted(expected)],
               "sender": manifest["sender"], "recipient": manifest["recipient"],
               "outcome": manifest["outcome"], "reason_code": manifest["reason_code"],
               "lead_count": manifest["lead_count"], "sent_at": datetime.now(timezone.utc).isoformat()}
    receipt_rel = f"outputs/{manifest['local_date']}-{manifest['run_type']}-send-receipt.json"
    try:
        _write_new_json(root / receipt_rel, receipt)
    except OSError:
        return {"status": "manual_reconciliation_required", "should_send": False,
                "retry_automatically": False, "message_id": response["id"],
                "message": "Gmail accepted the message but the receipt could not be saved. Preserve the claim and reconcile."}
    event = {"event": "send_completed", "event_id": f"send:{manifest['idempotency_key']}:{response['id']}",
             "profile_id": manifest["profile_id"], "local_date": manifest["local_date"],
             "run_type": manifest["run_type"], "idempotency_key": manifest["idempotency_key"],
             "message_id": response["id"], "receipt_path": receipt_rel,
             "manifest_sha256": validated["manifest_sha256"],
             "gmail_request_sha256": validated["gmail_request_sha256"],
             "attachments": receipt["attachments"]}
    state_io = _module("state_io")
    try:
        (event_appender or state_io.append_run_event)(root / "state" / "run-ledger.jsonl", event,
                                                       expected_profile_id=manifest["profile_id"])
    except (OSError, ValueError, TimeoutError):
        return {"status": "recovery_required", "should_send": False,
                "retry_automatically": False, "message_id": response["id"],
                "receipt_path": receipt_rel,
                "message": "Gmail accepted the message and a receipt was saved. Repair only the ledger; never resend."}
    try:
        verification = verify_sent(service, response["id"], sender=manifest["sender"],
                                   recipient=manifest["recipient"], subject=manifest["subject"], expected=expected)
    except Exception:
        verification = {"status": "partial", "message": "Sent-mail byte check unavailable; receipt still blocks a resend."}
    return {"status": "sent", "should_send": False, "message_id": response["id"],
            "receipt_path": receipt_rel, "post_send_verification": verification}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    connect = commands.add_parser("oauth-connect", help="connect a personal Desktop OAuth Gmail client")
    connect.add_argument("--client-secrets", required=True, type=Path)
    connect.add_argument("--sender", required=True)
    send = commands.add_parser("send-claimed", help="send an already staged daily package once")
    send.add_argument("--root", required=True, type=Path)
    send.add_argument("--manifest", required=True, type=Path)
    send.add_argument("--confirm-send", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "oauth-connect":
            result = connect_oauth(args.client_secrets, args.sender)
        else:
            if not args.confirm_send:
                raise ValueError("--confirm-send is required")
            # Load/refresh and identity-check credentials before acquiring a claim.
            root = args.root.resolve()
            delivery = _module("daily_resume_delivery")
            validated = delivery.validate(root, args.manifest)
            manifest = json.loads((root / validated["manifest_path"]).read_text(encoding="utf-8-sig"))
            result = send_claimed(root, args.manifest, service=gmail_service(manifest["sender"]), confirmed=True)
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"status": "blocked", "should_send": False, "errors": [str(exc)]}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"connected", "sent"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
