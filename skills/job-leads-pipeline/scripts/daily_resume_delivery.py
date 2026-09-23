#!/usr/bin/env python3
"""Freeze verified daily resumes into the Gmail connector's structured MIME request.

prepare and validate never acquire a claim or send email. authorize-send delegates
all activation, staging, current-date, duplicate, and ambiguous-delivery decisions
to the existing shared validator. A caller must send the frozen request unchanged.
Validation reports are supplied attestations; this module never invents PDF or
visual-review results. verify-observed compares a saved provider observation only.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import html
import importlib.util
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SHARED_GUARD = Path(__file__).resolve().with_name("validate_run.py")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SAFE_ID = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")
SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,175}\.pdf\Z")
BASE64URL = re.compile(r"[A-Za-z0-9_-]*={0,2}\Z")
OUTCOMES = {"lead_email", "no_deliverable_leads_email", "service_status_email"}
SUMMARY_MARKER = '<section data-daily-resume-summary="1">'


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _line(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{label} must be a nonempty single line without control characters")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _inside(root: Path, value: Any, prefix: str = "outputs", *, must_exist: bool = True) -> Path:
    raw = _line(str(value) if isinstance(value, Path) else value, "artifact path")
    if '"' in raw or "\x00" in raw:
        raise ValueError("unsafe artifact path")
    candidate = Path(raw)
    if not candidate.is_absolute() and ":" in raw:
        raise ValueError("drive-relative artifact paths are unsafe")
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    allowed = (root / prefix).resolve()
    if not allowed.is_relative_to(root) or not resolved.is_relative_to(allowed) or resolved == allowed:
        raise ValueError(f"artifact path must stay inside {prefix}/")
    if must_exist and not resolved.is_file():
        raise ValueError(f"artifact file missing: {resolved}")
    return resolved


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _project_file(root: Path, value: Any) -> Path:
    raw = _line(value, "bound input path")
    candidate = Path(raw)
    if candidate.is_absolute() or ":" in raw or '"' in raw:
        raise ValueError("bound input paths must be safe and project-relative")
    resolved = (root / candidate).resolve()
    if resolved == root or not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("bound input file is missing or outside the project")
    return resolved


def _load_builder():
    path = Path(__file__).resolve().with_name("daily_resume_builder.py")
    spec = importlib.util.spec_from_file_location("daily_resume_builder_delivery_validation", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load independent resume input validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot_binding(root: Path, value: dict[str, Any], manifest: dict[str, Any], *, required: bool = False) -> dict[str, Any] | None:
    keys = ("input_snapshot_path", "input_snapshot_sha256")
    present = [key in value for key in keys]
    if not any(present) and not required:
        return None
    if not all(present):
        raise ValueError("input snapshot path and hash must be supplied together")
    path = _project_file(root, value[keys[0]])
    digest = _hash(value[keys[1]], "input snapshot hash")
    if _digest(path.read_bytes()) != digest:
        raise ValueError("input snapshot hash mismatch")
    snapshot = _object(_read(path), "input snapshot")
    if any(snapshot.get(key) != manifest[key] for key in ("profile_id", "local_date")):
        raise ValueError("input snapshot profile or local date mismatch")
    if type(snapshot.get("fixture_only")) is not bool:
        raise ValueError("input snapshot fixture_only must be a boolean")
    # Validate the immutable copies, never their mutable canonical originals.
    references = [(snapshot.get("facts_path"), snapshot.get("facts_sha256")),
                  (_relative(root, path.parent / "candidate.json"), snapshot.get("candidate_sha256")),
                  (_relative(root, path.parent / "leads.json"), snapshot.get("leads_sha256"))]
    sources = snapshot.get("sources")
    drafts = snapshot.get("drafts")
    if not isinstance(sources, list) or not isinstance(drafts, list):
        raise ValueError("input snapshot sources and drafts must be lists")
    source_ids = set()
    for source in sources:
        source = _object(source, "snapshot source")
        source_id = _line(source.get("id"), "snapshot source ID")
        if source_id in source_ids:
            raise ValueError("duplicate snapshot source ID")
        source_ids.add(source_id)
    references += [(item.get("path"), item.get("sha256")) for item in sources]
    references += [(item.get("path"), item.get("sha256")) for item in drafts if isinstance(item, dict)]
    if any(not isinstance(item, dict) for item in drafts):
        raise ValueError("snapshot draft must be an object")
    seen = set()
    for raw, expected in references:
        bound = _project_file(root, raw)
        if not bound.is_relative_to(path.parent):
            raise ValueError("snapshot copies must remain inside its frozen input directory")
        if bound in seen:
            raise ValueError("duplicate input snapshot file binding")
        seen.add(bound)
        if _digest(bound.read_bytes()) != _hash(expected, "snapshot source hash"):
            raise ValueError("input snapshot source hash mismatch")
    return {"input_snapshot_path": _relative(root, path), "input_snapshot_sha256": digest,
            "fixture_only": snapshot["fixture_only"]}


def _report_input_bindings(root: Path, report: dict[str, Any], pdf_path: Path,
                           manifest: dict[str, Any], snapshot: dict[str, Any]) -> None:
    keys = ("facts", "draft", "approval", "leads")
    inputs = {}
    for name in keys:
        path = _project_file(root, report.get(name + "_path"))
        if _digest(path.read_bytes()) != _hash(report.get(name + "_sha256"), name + " hash"):
            raise ValueError(f"resume report {name} file hash mismatch")
        inputs[name] = path
    for key in ("input_snapshot_path", "input_snapshot_sha256", "fixture_only"):
        if report.get(key) != snapshot[key]:
            raise ValueError(f"resume report {key} differs from the index snapshot")
    checked = _load_builder().validate_inputs(
        root, inputs["facts"], inputs["draft"], inputs["approval"], inputs["leads"], pdf_path.parent,
        input_snapshot_path=_project_file(root, report["input_snapshot_path"]))
    for key in ("profile_id", "role_ref", "job_url", "company", "role"):
        if checked["draft"].get(key) != report.get(key):
            raise ValueError(f"resume report {key} differs from its independently approved draft")
    for name in keys:
        for suffix in ("_path", "_sha256"):
            key = name + suffix
            if report.get(key) != checked.get(key):
                raise ValueError(f"resume report {key} differs from fresh source validation")
    for key in ("input_snapshot_path", "input_snapshot_sha256", "fixture_only"):
        if report.get(key) != checked.get(key):
            raise ValueError(f"resume report {key} differs from fresh snapshot validation")
    bindings = report.get("source_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise ValueError("resume report requires nonempty source bindings")
    seen = set()
    for binding in bindings:
        binding = _object(binding, "source binding")
        path = _project_file(root, binding.get("path"))
        if path in seen:
            raise ValueError("duplicate resume source binding")
        seen.add(path)
        if _digest(path.read_bytes()) != _hash(binding.get("sha256"), "source binding hash"):
            raise ValueError("resume source binding hash mismatch")
    if bindings != checked.get("source_bindings"):
        raise ValueError("resume source bindings differ from the complete frozen source set")


def _artifact(root: Path, item: Any, label: str) -> tuple[Path, bytes]:
    item = _object(item, label)
    if not isinstance(item.get("path"), str) or Path(item["path"]).is_absolute():
        raise ValueError(f"{label} path must be project-relative")
    path = _inside(root, item["path"])
    data = path.read_bytes()
    if _hash(item.get("sha256"), label) != _digest(data):
        raise ValueError(f"{label} hash mismatch")
    return path, data


def _url(value: Any) -> str:
    value = _line(value, "job URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("job URL must be an absolute HTTPS URL without credentials")
    return value


def _load_manifest(root: Path, value: str | Path) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    path = _inside(root, value)
    manifest = _object(_read(path), "manifest")
    if manifest.get("manifest_version") != 2:
        raise ValueError("daily delivery requires manifest_version 2")
    profile = _line(manifest.get("profile_id"), "profile_id")
    if not SAFE_ID.fullmatch(profile):
        raise ValueError("invalid profile_id")
    local_date = _line(manifest.get("local_date"), "local_date")
    if date.fromisoformat(local_date).isoformat() != local_date:
        raise ValueError("local_date must be canonical YYYY-MM-DD")
    if manifest.get("run_type") != "daily":
        raise ValueError("resume delivery is restricted to the existing daily run")
    if manifest.get("idempotency_key") != f"{profile}:{local_date}:daily":
        raise ValueError("manifest idempotency identity mismatch")
    if _object(_read(root / "profile.json"), "project profile").get("profile_id") != profile:
        raise ValueError("manifest profile mismatch")
    automation = _object(_read(root / "automation.json"), "automation")
    resume_preparation = _object(automation.get("resume_preparation"), "resume_preparation")
    if resume_preparation.get("enabled") is not True:
        raise ValueError("resume preparation feature is disabled")
    approval = _object(resume_preparation.get("approval"), "resume preparation approval")
    if approval.get("approved") is not True:
        raise ValueError("resume preparation approval is missing or revoked")
    for approved_key, configured_key in (("sender", "sender"), ("recipient", "to"), ("cadence", "cadence")):
        configured = _line(automation.get(configured_key), f"automation {configured_key}")
        if approval.get(approved_key) != configured:
            raise ValueError(f"resume preparation approval {approved_key} does not match automation.json")
    for field, configured in (("sender", "sender"), ("recipient", "to")):
        if _line(manifest.get(field), field) != automation.get(configured):
            raise ValueError(f"manifest {field} must match automation.json")
    _line(manifest.get("subject"), "subject")
    _line(manifest.get("reason_code"), "reason_code")
    count = manifest.get("lead_count")
    if type(count) is not int or count < 0 or manifest.get("outcome") not in OUTCOMES:
        raise ValueError("invalid lead count or outcome")
    if (manifest["outcome"] == "lead_email") != (count > 0):
        raise ValueError("outcome and lead count disagree")
    counts = _object(manifest.get("counts"), "counts")
    if any(type(counts.get(k)) is not int or counts[k] < 0 for k in ("searched", "qualified", "verified", "withheld")):
        raise ValueError("counts must be nonnegative integers")
    if not (counts["searched"] >= counts["qualified"] >= counts["verified"] == count
            and counts["withheld"] == counts["qualified"] - count):
        raise ValueError("manifest counts disagree")
    evidence = manifest.get("evidence_paths")
    if not isinstance(evidence, list):
        raise ValueError("evidence_paths must be a list")
    for item in evidence:
        if not isinstance(item, str) or Path(item).is_absolute():
            raise ValueError("evidence paths must be project-relative")
        _inside(root, item)
    artifacts = _object(manifest.get("artifacts"), "artifacts")
    bodies = {k: _artifact(root, artifacts.get(k), k)[1] for k in ("payload", "html", "text")}
    for kind in ("html", "text"):
        if not bodies[kind].decode("utf-8-sig").strip():
            raise ValueError(f"rendered {kind} body is empty")
    payload = json.loads(bodies["payload"].decode("utf-8-sig"))
    leads = payload.get("leads") if isinstance(payload, dict) else payload
    if not isinstance(leads, list) or len(leads) != count:
        raise ValueError("final leads payload does not match manifest lead_count")
    seen: set[str] = set()
    for lead in leads:
        lead = _object(lead, "lead")
        ref = _line(lead.get("role_ref"), "role_ref")
        if ref in seen:
            raise ValueError("duplicate final role_ref")
        seen.add(ref)
        for key in ("company", "role"):
            _line(lead.get(key), key)
        _url(lead.get("link"))
        if lead.get("profile_id", profile) != profile:
            raise ValueError("lead profile mismatch")
    return path, manifest, leads


def _resume_entries(root: Path, manifest: dict[str, Any], leads: list[dict[str, Any]], index: Any) -> list[dict[str, Any]]:
    index = _object(index, "resume index")
    for key in ("profile_id", "local_date"):
        if index.get(key) != manifest[key]:
            raise ValueError(f"resume index {key} mismatch")
    entries = index.get("entries")
    if not isinstance(entries, list):
        raise ValueError("resume index entries must be a list")
    snapshot = _snapshot_binding(root, index, manifest, required=any(isinstance(item, dict) and item.get("status") == "ready" for item in entries))
    by_ref: dict[str, Any] = {}
    filenames: set[str] = set()
    pdf_paths: set[Path] = set()
    for item in entries:
        item = _object(item, "resume entry")
        ref = _line(item.get("role_ref"), "resume role_ref")
        if ref in by_ref:
            raise ValueError("duplicate resume role_ref")
        by_ref[ref] = item
    if set(by_ref) != {lead["role_ref"] for lead in leads}:
        raise ValueError("resume entries must exactly match final roles: missing or extra entries")
    ordered = []
    for lead in leads:
        item = by_ref[lead["role_ref"]]
        expected = {"role_ref": lead["role_ref"], "job_url": lead["link"], "company": lead["company"], "role": lead["role"]}
        if any(item.get(k) != v for k, v in expected.items()):
            raise ValueError("resume role identity or job URL mismatch")
        status = item.get("status")
        normalized = {**expected, "status": status}
        if status == "unavailable":
            normalized["reason"] = _line(item.get("reason"), "unavailable reason")
            if any(item.get(k) is not None for k in ("path", "filename", "sha256", "size_bytes", "mime_type", "validation_path", "validation_sha256")):
                raise ValueError("unavailable resume must not advertise an attachment")
        elif status == "ready":
            filename = _line(item.get("filename"), "attachment filename")
            if not SAFE_FILENAME.fullmatch(filename) or ".." in filename or filename.casefold() in filenames:
                raise ValueError("unsafe or duplicate attachment filename")
            filenames.add(filename.casefold())
            if item.get("mime_type") != "application/pdf":
                raise ValueError("resume MIME must be application/pdf")
            path = _inside(root, item.get("path"), "output/pdf")
            if filename != path.name:
                raise ValueError("attachment filename must exactly match the PDF path filename")
            if path in pdf_paths or path.suffix.lower() != ".pdf":
                raise ValueError("duplicate or non-PDF resume path")
            pdf_paths.add(path)
            data = path.read_bytes()
            if not data.startswith(b"%PDF-"):
                raise ValueError("resume bytes are not PDF")
            if type(item.get("size_bytes")) is not int or item["size_bytes"] != len(data):
                raise ValueError("resume size mismatch")
            pdf_hash = _hash(item.get("sha256"), "resume hash")
            if pdf_hash != _digest(data):
                raise ValueError("resume hash mismatch")
            report_path = _inside(root, item.get("validation_path"))
            report_bytes = report_path.read_bytes()
            if _hash(item.get("validation_sha256"), "validation hash") != _digest(report_bytes):
                raise ValueError("resume validation report hash mismatch")
            report = _object(json.loads(report_bytes.decode("utf-8-sig")), "validation report")
            for k, v in {"profile_id": manifest["profile_id"], "role_ref": lead["role_ref"], "job_url": lead["link"],
                         "company": lead["company"], "role": lead["role"], "pdf_sha256": pdf_hash}.items():
                if report.get(k) != v:
                    raise ValueError(f"validation report {k} mismatch")
            for key in ("draft_sha256", "facts_sha256"):
                _hash(report.get(key), key)
            _report_input_bindings(root, report, path, manifest, snapshot)
            checks = _object(report.get("validation"), "PDF validation")
            if checks.get("native_text") is not True or checks.get("text_matches") is not True or type(checks.get("pages")) is not int or checks["pages"] != 1:
                raise ValueError("PDF validation must attest native text, matching text, and one page")
            review = _object(report.get("visual_review"), "visual review")
            if review.get("passed") is not True or review.get("pdf_sha256") != pdf_hash:
                raise ValueError("visual review is missing, failed, or bound to different PDF bytes")
            _line(review.get("reviewer"), "visual reviewer")
            reviewed_at = datetime.fromisoformat(_line(review.get("reviewed_at"), "reviewed_at").replace("Z", "+00:00"))
            if reviewed_at.tzinfo is None:
                raise ValueError("visual reviewed_at must include a timezone")
            normalized.update({"path": _relative(root, path), "filename": filename, "sha256": pdf_hash,
                               "size_bytes": len(data), "mime_type": "application/pdf",
                               "validation_path": _relative(root, report_path), "validation_sha256": _digest(report_bytes)})
        else:
            raise ValueError("resume status must be ready or unavailable")
        ordered.append(normalized)
    return ordered


def _statuses(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: e[k] for k in ("role_ref", "job_url", "company", "role", "status")}
            | ({"filename": e["filename"]} if e["status"] == "ready" else {"reason": e["reason"]}) for e in entries]


def _attachments(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in e.items() if k != "status"} for e in entries if e["status"] == "ready"]


def _summary(entries: list[dict[str, Any]]) -> tuple[str, str]:
    lines = ["Tailored resumes"]
    markup = [SUMMARY_MARKER, "<h2>Tailored resumes</h2>", "<ul>"]
    for entry in entries:
        label = f"{entry['company']} — {entry['role']}"
        status = f"Attached: {entry['filename']}" if entry["status"] == "ready" else f"Unavailable: {entry['reason']}"
        lines.append(f"- {label}: {status}")
        markup.append(f"<li><strong>{html.escape(label)}</strong>: {html.escape(status)}</li>")
    if not entries:
        lines.append("No final leads; no resume attachments.")
        markup.append("<li>No final leads; no resume attachments.</li>")
    markup += ["</ul>", "</section>"]
    return "\n".join(lines) + "\n", "\n".join(markup)


def _augmented_bodies(source_html: str, source_text: str, entries: list[dict[str, Any]]) -> tuple[str, str]:
    if SUMMARY_MARKER in source_html:
        raise ValueError("source body already contains a resume summary")
    cards = list(re.finditer(r"<article\b[^>]*>.*?</article\s*>", source_html, re.IGNORECASE | re.DOTALL))
    if len(cards) != len(entries):
        raise ValueError("rendered job card count does not match final roles")
    # The shared renderer emits one article in final-lead order. Reject an
    # unfamiliar/mismatched template instead of attaching a filename to a guess.
    for card, entry in reversed(list(zip(cards, entries))):
        links = [html.unescape(m.group(2)) for m in re.finditer(r'href\s*=\s*([\"\'])(.*?)\1', card.group(), re.IGNORECASE | re.DOTALL)]
        if links.count(entry["job_url"]) != 1:
            raise ValueError("rendered job card URL/order does not match final role")
        status = f"Attached: {entry['filename']}" if entry["status"] == "ready" else f"Unavailable: {entry['reason']}"
        note = f'<p data-resume-role="{html.escape(entry["role_ref"], quote=True)}"><strong>Tailored resume:</strong> {html.escape(status)}</p>'
        closing = re.search(r"</article\s*>\Z", card.group(), re.IGNORECASE)
        insertion = card.start() + closing.start()
        source_html = source_html[:insertion] + note + source_html[insertion:]
    text_summary, html_summary = _summary(entries)
    closing = re.search(r"</body\s*>", source_html, re.IGNORECASE)
    if closing:
        rendered_html = source_html[:closing.start()] + html_summary + "\n" + source_html[closing.start():]
    else:
        rendered_html = source_html.rstrip() + "\n" + html_summary + "\n"
    return rendered_html, source_text.rstrip() + "\n\n" + text_summary


def _request(root: Path, manifest: dict[str, Any], rendered_html: str, rendered_text: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    parts = [{"mime_type": "multipart/alternative", "parts": [
        {"mime_type": "text/plain", "body": {"content": rendered_text}},
        {"mime_type": "text/html", "body": {"content": rendered_html}},
    ]}]
    for entry in entries:
        if entry["status"] == "ready":
            data = _inside(root, entry["path"], "output/pdf").read_bytes()
            if _digest(data) != entry["sha256"]:
                raise ValueError("resume changed while packaging")
            parts.append({"mime_type": "application/pdf", "filename": entry["filename"],
                          "content_disposition": "attachment", "body": {
                              "base64_url_content": base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")}})
    return {"from_address": manifest["sender"], "to": manifest["recipient"], "subject": manifest["subject"],
            "payload": {"mime_type": "multipart/mixed", "parts": parts}}


def _chat(root: Path, entries: list[dict[str, Any]]) -> str:
    lines = ["Tailored daily resumes", ""]
    for entry in entries:
        if entry["status"] == "ready":
            absolute = _inside(root, entry["path"], "output/pdf").as_posix()
            lines += [f"{_chat_text(entry['company'])} - {_chat_text(entry['role'])}",
                      f"PDF: {absolute}", ""]
        else:
            lines += [f"{_chat_text(entry['company'])} - {_chat_text(entry['role'])}: unavailable - {_chat_text(entry['reason'])}", ""]
    if not entries:
        lines.append("No final leads; no resume attachments.")
    return "\n".join(lines).rstrip() + "\n"


def _chat_text(value: str) -> str:
    escaped = re.sub(r"([\\`*_\[\]#!|])", r"\\\1", value)
    return html.escape(escaped).replace(":", "&#58;").replace("{", "&#123;").replace("}", "&#125;")


def _freeze(files: dict[Path, bytes]) -> None:
    # Inspect the whole plan before creating any artifact. Existing different bytes
    # are preserved; concurrent creators also cannot be overwritten.
    for path, data in files.items():
        if path.exists() and (not path.is_file() or path.read_bytes() != data):
            raise ValueError(f"frozen artifact conflict; existing bytes preserved: {path}")
    for path, data in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError(f"frozen artifact changed concurrently: {path}")


def prepare(root: str | Path, manifest_path: str | Path, resume_index_path: str | Path, out_dir: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    source_path, manifest, leads = _load_manifest(root, manifest_path)
    if "resume_delivery" in manifest:
        raise ValueError("prepare requires the original rendered manifest; validate the frozen package instead")
    index_path = _inside(root, resume_index_path)
    input_index = _object(_read(index_path), "resume index")
    entries = _resume_entries(root, manifest, leads, input_index)
    snapshot = _snapshot_binding(root, input_index, manifest)
    snapshot_reference = {k: snapshot[k] for k in ("input_snapshot_path", "input_snapshot_sha256")} if snapshot else {}
    # Resolve a file child to validate an otherwise possibly nonexistent directory.
    destination = _inside(root, Path(out_dir) / "manifest.json", must_exist=False).parent
    source_html = _artifact(root, manifest["artifacts"]["html"], "html")[1].decode("utf-8-sig")
    source_text = _artifact(root, manifest["artifacts"]["text"], "text")[1].decode("utf-8-sig")
    rendered_html, rendered_text = _augmented_bodies(source_html, source_text, entries)
    values = {
        "resume_index": ("resume-index.json", _json_bytes({"profile_id": manifest["profile_id"], "local_date": manifest["local_date"], "entries": entries, **snapshot_reference})),
        "html": ("daily-email-with-resumes.html", rendered_html.encode("utf-8")),
        "text": ("daily-email-with-resumes.txt", rendered_text.encode("utf-8")),
        "gmail_request": ("gmail-request.json", _json_bytes(_request(root, manifest, rendered_html, rendered_text, entries))),
        "chat_report": ("chat-report.md", _chat(root, entries).encode("utf-8")),
    }
    frozen = copy.deepcopy(manifest)
    frozen["resume_delivery"] = {"version": 1, "source_manifest": {"path": _relative(root, source_path), "sha256": _digest(source_path.read_bytes())}, **snapshot_reference}
    frozen["attachments"] = _attachments(entries)
    frozen["resume_statuses"] = _statuses(entries)
    files = {}
    for key, (name, data) in values.items():
        target = _inside(root, destination / name, must_exist=False)
        files[target] = data
        frozen["artifacts"][key] = {"path": _relative(root, target), "sha256": _digest(data)}
    output_manifest = destination / "daily-resume-manifest.json"
    files[output_manifest] = _json_bytes(frozen)
    if source_path in files or index_path in files:
        raise ValueError("frozen outputs must not replace an input artifact")
    _freeze(files)
    return validate(root, output_manifest)


def validate(root: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    path, manifest, leads = _load_manifest(root, manifest_path)
    delivery = _object(manifest.get("resume_delivery"), "resume_delivery")
    if delivery.get("version") != 1:
        raise ValueError("unsupported resume delivery version")
    source_path, _ = _artifact(root, delivery.get("source_manifest"), "source manifest")
    if source_path == path:
        raise ValueError("source manifest cannot refer to itself")
    _, source, source_leads = _load_manifest(root, source_path)
    if "resume_delivery" in source:
        raise ValueError("source manifest must be an original rendered manifest")
    # Preserve all original required and extension fields, except the deliberately
    # replaced artifacts and the new resume metadata.
    for key, value in source.items():
        if key != "artifacts" and manifest.get(key) != value:
            raise ValueError(f"frozen manifest changed original field {key}")
    if source_leads != leads:
        raise ValueError("frozen final lead payload changed")
    for key, artifact in source["artifacts"].items():
        if key not in {"html", "text"} and manifest["artifacts"].get(key) != artifact:
            raise ValueError(f"frozen manifest changed source artifact {key}")
    _, index_bytes = _artifact(root, manifest["artifacts"].get("resume_index"), "resume index")
    index = _object(json.loads(index_bytes.decode("utf-8-sig")), "resume index")
    entries = _resume_entries(root, manifest, leads, index)
    snapshot = _snapshot_binding(root, index, manifest)
    for key in ("input_snapshot_path", "input_snapshot_sha256"):
        if delivery.get(key) != index.get(key):
            raise ValueError("manifest and resume index snapshot references differ")
    if manifest.get("attachments") != _attachments(entries) or manifest.get("resume_statuses") != _statuses(entries):
        raise ValueError("manifest attachment metadata or resume statuses mismatch")
    source_html = _artifact(root, source["artifacts"]["html"], "source html")[1].decode("utf-8-sig")
    source_text = _artifact(root, source["artifacts"]["text"], "source text")[1].decode("utf-8-sig")
    expected_html, expected_text = _augmented_bodies(source_html, source_text, entries)
    for key, expected in (("html", expected_html), ("text", expected_text), ("chat_report", _chat(root, entries))):
        if _artifact(root, manifest["artifacts"].get(key), key)[1] != expected.encode("utf-8"):
            raise ValueError(f"{key} content does not match frozen resume entries")
    request_path, request_bytes = _artifact(root, manifest["artifacts"].get("gmail_request"), "Gmail request")
    request = json.loads(request_bytes.decode("utf-8-sig"))
    if request != _request(root, manifest, expected_html, expected_text, entries):
        raise ValueError("frozen Gmail request does not match headers, bodies, and PDF bytes")
    # Explicitly decode each connector attachment and compare its original bytes.
    for part, entry in zip(request["payload"]["parts"][1:], _attachments(entries)):
        if _decode64(part["body"]["base64_url_content"]) != _inside(root, entry["path"], "output/pdf").read_bytes():
            raise ValueError("Gmail attachment decoded bytes mismatch")
    return {"status": "validated", "should_send": False, "claim_acquired": False,
            "fixture_only": snapshot["fixture_only"] if snapshot else False,
            "profile_id": manifest["profile_id"], "local_date": manifest["local_date"], "run_type": manifest["run_type"],
            "manifest_path": _relative(root, path), "manifest_sha256": _digest(path.read_bytes()),
            "gmail_request_path": _relative(root, request_path), "gmail_request_sha256": _digest(request_bytes),
            "ready_count": len(manifest["attachments"]), "unavailable_count": len(entries) - len(manifest["attachments"])}


def authorize_send(root: str | Path, manifest_path: str | Path, *, runner=None) -> dict[str, Any]:
    """Validate locally, then ask the shared guard to acquire its normal claim."""
    root = Path(root).resolve()
    validated = validate(root, manifest_path)
    if validated["fixture_only"]:
        return {"status": "blocked", "should_send": False, "errors": ["fixture-only resume snapshots cannot authorize email delivery"]}
    path = _inside(root, validated["manifest_path"])
    manifest = _read(path)
    command = [sys.executable, str(SHARED_GUARD), str(root), manifest["profile_id"], manifest["local_date"], manifest["run_type"],
               "--acquire-claim", "--manifest", validated["manifest_path"], "--manifest-sha256", validated["manifest_sha256"],
               "--outcome", manifest["outcome"], "--lead-count", str(manifest["lead_count"]),
               "--sender", manifest["sender"], "--recipient", manifest["recipient"]]
    completed = (runner or subprocess.run)(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return {"status": "blocked", "should_send": False, "errors": ["shared delivery guard failed", completed.stderr.strip()]}
    result = _object(json.loads(completed.stdout), "shared guard result")
    if result.get("should_send") is True:
        if result.get("status") not in {"claimed", "claimed_retry"} or not result.get("claim_token") or not result.get("attempt_token"):
            raise ValueError("shared guard did not return a complete claimed result")
        if any(result.get(k) != manifest[k] for k in ("profile_id", "local_date", "run_type", "idempotency_key")):
            raise ValueError("shared guard response identity mismatch")
        if result.get("manifest_sha256") != validated["manifest_sha256"]:
            raise ValueError("shared guard response manifest hash mismatch")
    # Preserve duplicate and ambiguous results verbatim; never retry here.
    return result


def _decode64(value: Any) -> bytes:
    if not isinstance(value, str) or not BASE64URL.fullmatch(value):
        raise ValueError("invalid base64url attachment bytes")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64url attachment bytes") from exc


def verify_observed(root: str | Path, manifest_path: str | Path, observed_path: str | Path) -> dict[str, Any]:
    """Read-only comparison; missing provider evidence is partial, never a retry.

    Fixture: {attachments_complete: bool, attachments: [{filename, mime_type?,
    size_bytes?, sha256?, base64_url_content?}]}. A saved Gmail MIME payload may be
    supplied instead of attachments; body.size/base64_url_content are understood.
    Only supplied decoded bytes can establish full attachment-byte verification.
    """
    root = Path(root).resolve()
    validate(root, manifest_path)
    manifest = _read(_inside(root, manifest_path))
    observed = _object(_read(_inside(root, observed_path)), "provider observation")
    attachments = observed.get("attachments")
    if attachments is None and isinstance(observed.get("payload"), dict):
        attachments = []
        def collect(part):
            if not isinstance(part, dict):
                raise ValueError("observed MIME part must be an object")
            if part.get("filename"):
                attachments.append(part)
            for child in part.get("parts") or []:
                collect(child)
        collect(observed["payload"])
    if attachments is None:
        attachments = []
    if not isinstance(attachments, list):
        raise ValueError("observed attachments must be a list")
    expected = {item["filename"]: item for item in manifest["attachments"]}
    seen = set()
    matched = []
    issues = []
    for item in attachments:
        item = _object(item, "observed attachment")
        filename = _line(item.get("filename"), "observed filename")
        if filename in seen:
            issues.append(f"duplicate observed filename: {filename}")
            continue
        seen.add(filename)
        if filename not in expected:
            issues.append(f"unexpected attachment: {filename}")
            continue
        wanted = expected[filename]
        body = item.get("body") or {}
        fields = {"mime_type": item.get("mime_type"), "size_bytes": item.get("size_bytes", body.get("size")), "sha256": item.get("sha256")}
        for key, value in fields.items():
            if value is not None and (type(value) is not int if key == "size_bytes" else False):
                issues.append(f"{filename}: invalid {key}")
            elif value is not None and value != wanted[key]:
                issues.append(f"{filename}: {key} mismatch")
        encoded = item.get("base64_url_content", body.get("base64_url_content"))
        byte_verified = False
        if encoded is not None:
            try:
                data = _decode64(encoded)
                byte_verified = _digest(data) == wanted["sha256"] and len(data) == wanted["size_bytes"]
                if not byte_verified:
                    issues.append(f"{filename}: observed bytes mismatch")
            except ValueError:
                issues.append(f"{filename}: invalid observed byte encoding")
        matched.append({"filename": filename, "bytes_verified": byte_verified,
                        "metadata_fields_compared": [k for k, v in fields.items() if v is not None]})
    complete = observed.get("attachments_complete") is True
    missing = sorted(set(expected) - seen)
    if complete and missing:
        issues.append("provider observation is missing expected attachments: " + ", ".join(missing))
    full = complete and not missing and all(row["bytes_verified"] and "mime_type" in row["metadata_fields_compared"] for row in matched)
    return {"status": "mismatch" if issues else "verified" if full else "partial", "retry": False,
            "observed_complete": complete, "attachments": matched, "unobserved_filenames": missing, "issues": issues,
            "limitation": None if full and not issues else "Only observed fields were compared; no delivery retry is authorized."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "validate", "authorize-send", "verify-observed"):
        sub = commands.add_parser(name)
        sub.add_argument("--root", required=True, type=Path)
        sub.add_argument("--manifest", required=True, type=Path)
        if name == "prepare":
            sub.add_argument("--resume-index", required=True, type=Path)
            sub.add_argument("--out-dir", required=True, type=Path)
        if name == "verify-observed":
            sub.add_argument("--observed", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.root, args.manifest, args.resume_index, args.out_dir)
        elif args.command == "validate":
            result = validate(args.root, args.manifest)
        elif args.command == "authorize-send":
            result = authorize_send(args.root, args.manifest)
        else:
            result = verify_observed(args.root, args.manifest, args.observed)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "should_send": False, "errors": [str(exc)]}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 1 if result.get("status") in {"blocked", "mismatch", "manual_reconciliation_required"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
