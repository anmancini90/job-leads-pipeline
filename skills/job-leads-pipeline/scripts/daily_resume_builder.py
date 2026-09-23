"""Build one source-checked, native-text resume; never approve its own claims.

The separate semantic reviewer signs exact draft and fact bytes. This module checks
that signature's freshness, source references, role identity, and rendered text.
A successful build still requires independent visual review before delivery.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import unicodedata
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class ResumeValidationError(ValueError):
    """A resume must not be rendered or delivered in its current state."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _contained(root: Path, value: str | Path, *, must_exist: bool = True) -> Path:
    value = Path(value)
    path = (value if value.is_absolute() else root / value).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ResumeValidationError(f"Path is outside the project: {value}")
    if must_exist and not path.is_file():
        raise ResumeValidationError(f"Input is not a file: {value}")
    return path


def _json(path: Path) -> tuple[Any, str]:
    data = path.read_bytes()
    try:
        return json.loads(data.decode("utf-8-sig")), sha256_bytes(data)
    except (ValueError, UnicodeError) as exc:
        raise ResumeValidationError(f"Invalid JSON: {path.name}") from exc


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResumeValidationError(f"Missing or empty text: {name}")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ResumeValidationError(f"Text must be trimmed and single-line: {name}")
    if any(char in value for char in "\u2010\u2011\u2012\u2013\u2014\u2212"):
        raise ResumeValidationError(f"Use ASCII hyphens: {name}")
    try:
        value.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise ResumeValidationError(f"Unsupported standard-font glyph in {name}") from exc
    return value


def _url(value: Any, name: str) -> str:
    value = _text(value, name)
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ResumeValidationError(f"Expected a public HTTPS URL: {name}")
    return value


def _mapping(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ResumeValidationError(f"Expected object: {name}")
    return value


def normalize_text(text: str) -> str:
    """Ignore wrapping/whitespace only, not punctuation, words, or numbers."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def validate_inputs(root: str | Path, facts_path: str | Path,
                    draft_path: str | Path, approval_path: str | Path,
                    leads_path: str | Path, output_dir: str | Path,
                    input_snapshot_path: str | Path | None = None) -> dict:
    """Read-only validation. Does not generate artifacts or create directories."""
    root = Path(root).resolve(strict=True)
    inputs = {name: _contained(root, path) for name, path in {
        "facts": facts_path, "draft": draft_path, "approval": approval_path,
        "leads": leads_path, "profile": "profile.json",
    }.items()}
    output = _contained(root, output_dir, must_exist=False)
    relative = output.relative_to(root)
    if (len(relative.parts) < 3 or relative.parts[:2] != ("output", "pdf")
            or not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[-_])", relative.parts[2])):
        raise ResumeValidationError("Output directory must be inside output/pdf/<dated-run>/")
    data, hashes = {}, {}
    for name, path in inputs.items():
        data[name], hashes[name] = _json(path)
    facts = _mapping(data["facts"], "facts")
    draft = _mapping(data["draft"], "draft")
    approval = _mapping(data["approval"], "approval")
    snapshot_path = _contained(root, input_snapshot_path or inputs["facts"].parent / "input-snapshot.json")
    snapshot, snapshot_hash = _json(snapshot_path)
    snapshot = _mapping(snapshot, "input snapshot")
    if type(snapshot.get("fixture_only")) is not bool:
        raise ResumeValidationError("Snapshot must explicitly declare fixture_only true or false")
    if (snapshot.get("facts_sha256") != hashes["facts"]
            or _contained(root, _text(snapshot.get("facts_path"), "snapshot.facts_path")) != inputs["facts"]
            or inputs["facts"].parent != snapshot_path.parent):
        raise ResumeValidationError("Snapshot facts path/hash mismatch")
    if inputs["leads"] != snapshot_path.parent / "leads.json" or snapshot.get("leads_sha256") != hashes["leads"]:
        raise ResumeValidationError("Snapshot final leads path/hash mismatch")
    candidate_path = _contained(root, snapshot_path.parent / "candidate.json")
    candidate, candidate_hash = _json(candidate_path)
    if snapshot.get("candidate_sha256") != candidate_hash:
        raise ResumeValidationError("Snapshot candidate hash mismatch")
    if approval.get("input_snapshot_sha256") != snapshot_hash:
        raise ResumeValidationError("Approval is stale: input snapshot SHA256 mismatch")
    source_ids = None
    source_bindings = {inputs["facts"].relative_to(root).as_posix(): hashes["facts"],
                       inputs["leads"].relative_to(root).as_posix(): hashes["leads"],
                       candidate_path.relative_to(root).as_posix(): candidate_hash}
    snapshot_sources = snapshot.get("sources")
    if not isinstance(snapshot_sources, list):
        raise ResumeValidationError("Snapshot sources must be a list")
    copied_sources = {}
    for source in snapshot_sources:
        source = _mapping(source, "snapshot source")
        source_id = _text(source.get("id"), "snapshot source.id")
        if source_id in copied_sources:
            raise ResumeValidationError("Duplicate snapshot source ID")
        source_path = _contained(root, _text(source.get("path"), "snapshot source.path"))
        if not source_path.is_relative_to(snapshot_path.parent):
            raise ResumeValidationError("Snapshot source is not a frozen copy under the input directory")
        source_hash = sha256_bytes(source_path.read_bytes())
        if source.get("sha256") != source_hash:
            raise ResumeValidationError(f"Snapshot source hash mismatch: {source_id}")
        copied_sources[source_id] = source_hash
        source_bindings[source_path.relative_to(root).as_posix()] = source_hash
    if "sources" in facts:
        if not isinstance(facts["sources"], list) or not facts["sources"]:
            raise ResumeValidationError("Sources registry must be a nonempty list when present")
        source_ids = set()
        for source in facts["sources"]:
            source = _mapping(source, "source")
            source_id = _text(source.get("id"), "source.id")
            if source_id in source_ids:
                raise ResumeValidationError("Duplicate source ID")
            source_ids.add(source_id)
            if "path" in source:
                # Resolve the declared source path only for project containment;
                # validation binds its immutable snapshot copy, not today's file.
                _contained(root, _text(source["path"], "source.path"), must_exist=False)
                if source_id not in copied_sources:
                    raise ResumeValidationError(f"Source lacks a frozen snapshot copy: {source_id}")
                if "sha256" in source and source["sha256"] != copied_sources[source_id]:
                    raise ResumeValidationError(f"Source hash mismatch: {source_id}")

    def check_source_refs(value: Any, name: str) -> None:
        if not isinstance(value, list) or not value:
            raise ResumeValidationError(f"Missing source IDs: {name}")
        for source_id in value:
            _text(source_id, f"{name}.source_id")
            if source_ids is not None and source_id not in source_ids:
                raise ResumeValidationError(f"Unknown source ID in {name}: {source_id}")

    profile_id = _text(_mapping(data["profile"], "profile").get("profile_id"), "profile_id")
    if snapshot.get("profile_id") != profile_id or _mapping(candidate, "snapshot candidate").get("profile_id") != profile_id:
        raise ResumeValidationError("Profile mismatch: snapshot or frozen candidate")
    for name in ("facts", "draft", "approval"):
        if data[name].get("profile_id") != profile_id:
            raise ResumeValidationError(f"Profile mismatch: {name}")
    for field in ("role_ref", "company", "role"):
        _text(draft.get(field), f"draft.{field}")
    _url(draft.get("job_url"), "draft.job_url")
    snapshot_drafts = snapshot.get("drafts")
    if not isinstance(snapshot_drafts, list):
        raise ResumeValidationError("Snapshot drafts must be a list")
    if snapshot_drafts:
        matches = []
        draft_roles = set()
        for entry in snapshot_drafts:
            entry = _mapping(entry, "snapshot draft")
            draft_ref = _text(entry.get("role_ref"), "snapshot draft.role_ref")
            if draft_ref in draft_roles:
                raise ResumeValidationError("Duplicate snapshot draft role")
            draft_roles.add(draft_ref)
            frozen_draft_path = _contained(root, _text(entry.get("path"), "snapshot draft.path"))
            if not frozen_draft_path.is_relative_to(snapshot_path.parent):
                raise ResumeValidationError("Snapshot draft is outside the input directory")
            if entry.get("sha256") != sha256_bytes(frozen_draft_path.read_bytes()):
                raise ResumeValidationError("Snapshot draft hash mismatch")
            if draft_ref == draft["role_ref"]:
                matches.append(frozen_draft_path)
        if matches != [inputs["draft"]]:
            raise ResumeValidationError("Draft does not match its frozen snapshot entry")
    leads = data["leads"]
    if isinstance(leads, dict):
        if leads.get("profile_id") != profile_id:
            raise ResumeValidationError("Profile mismatch: leads")
        leads = leads.get("leads")
    if not isinstance(leads, list):
        raise ResumeValidationError("Leads must be a list or profile_id/leads object")
    matches = [lead for lead in leads if isinstance(lead, dict)
               and lead.get("role_ref") == draft["role_ref"]]
    if len(matches) != 1:
        raise ResumeValidationError("Draft must match exactly one final lead")
    lead = matches[0]
    if (lead.get("link", lead.get("job_url")) != draft["job_url"]
            or any(lead.get(key) != draft[key] for key in ("company", "role"))):
        raise ResumeValidationError("Draft company, role, or exact job URL differs from final lead")
    for field in ("role_ref", "job_url"):
        if approval.get(field) != draft[field]:
            raise ResumeValidationError(f"Approval role mismatch: {field}")
    if (approval.get("draft_sha256") != hashes["draft"]
            or approval.get("facts_sha256") != hashes["facts"]):
        raise ResumeValidationError("Approval is stale: exact draft/facts SHA256 mismatch")
    checks = approval.get("checks", {})
    if (approval.get("verdict") != "pass" or not isinstance(checks, dict)
            or any(checks.get(key) is not True for key in ("factual", "relevant", "consistent", "quality"))):
        raise ResumeValidationError("Independent factual, relevance, consistency, and quality approval is required")
    reviewer = _text(approval.get("reviewer"), "approval.reviewer")
    drafter = _text(draft.get("drafted_by"), "draft.drafted_by")
    if reviewer.casefold() == drafter.casefold():
        raise ResumeValidationError("A draft cannot approve itself; use an independent reviewer")
    identity = _mapping(facts.get("identity"), "facts.identity")
    for field in ("name", "email", "location"):
        _text(identity.get(field), f"identity.{field}")
    if identity.get("phone"):
        _text(identity["phone"], "identity.phone")
    if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", identity["email"]):
        raise ResumeValidationError("Invalid identity email")
    for field in ("portfolio_url", "linkedin_url"):
        if identity.get(field):
            _url(identity[field], f"identity.{field}")
    if facts.get("education") is not None:
        education = _mapping(facts["education"], "education")
        _text(education.get("text"), "education.text")
        if "source_ids" in education:
            check_source_refs(education["source_ids"], "education")
    fact_list = facts.get("facts")
    if not isinstance(fact_list, list) or not fact_list:
        raise ResumeValidationError("Facts list must not be empty")
    fact_ids = set()
    for fact in fact_list:
        fact = _mapping(fact, "fact")
        fact_id = _text(fact.get("id"), "fact.id")
        if fact_id in fact_ids:
            raise ResumeValidationError("Duplicate fact ID")
        fact_ids.add(fact_id)
        _text(fact.get("statement"), f"fact.{fact_id}.statement")
        check_source_refs(fact.get("source_ids"), f"fact.{fact_id}")
    histories = facts.get("history")
    if not isinstance(histories, list) or not histories:
        raise ResumeValidationError("Canonical history must not be empty")
    by_history = {}
    for history in histories:
        history = _mapping(history, "history")
        for field in ("id", "title", "company", "dates"):
            _text(history.get(field), f"history.{field}")
        if history["id"] in by_history:
            raise ResumeValidationError("Duplicate history ID")
        if "source_ids" in history:
            check_source_refs(history["source_ids"], f"history.{history['id']}")
        by_history[history["id"]] = history
    claims = {field: _text(draft.get(field), f"draft.{field}") for field in ("headline", "summary")}
    roles = draft.get("roles")
    if not isinstance(roles, list) or not roles:
        raise ResumeValidationError("Draft experience must not be empty")
    seen_history = set()
    for i, role in enumerate(roles):
        role = _mapping(role, f"roles.{i}")
        history_id = _text(role.get("history_id"), f"roles.{i}.history_id")
        if history_id not in by_history or history_id in seen_history:
            raise ResumeValidationError("Unknown or repeated canonical history ID")
        seen_history.add(history_id)
        for field in ("title", "company", "dates"):
            if role.get(field) != by_history[history_id][field]:
                raise ResumeValidationError(f"Canonical history mismatch: roles.{i}.{field}")
        bullets = role.get("bullets")
        if not isinstance(bullets, list) or not bullets:
            raise ResumeValidationError(f"Role has no bullets: {i}")
        for j, bullet in enumerate(bullets):
            claims[f"roles.{i}.bullets.{j}"] = _text(bullet, f"roles.{i}.bullets.{j}")
    skills = draft.get("skills")
    if not isinstance(skills, list) or not skills:
        raise ResumeValidationError("Draft skills must not be empty")
    for i, skill in enumerate(skills):
        if not isinstance(skill, list) or len(skill) != 2:
            raise ResumeValidationError(f"Skill must be [label, value]: {i}")
        for j, text in enumerate(skill):
            claims[f"skills.{i}.{j}"] = _text(text, f"skills.{i}.{j}")
    refs = _mapping(draft.get("claim_refs"), "draft.claim_refs")
    if set(refs) != set(claims):
        raise ResumeValidationError("Claim reference paths must exactly cover headline, summary, every bullet, and both skill cells")
    for path, references in refs.items():
        if (not isinstance(references, list) or not references
                or any(not isinstance(ref, str) or ref not in fact_ids for ref in references)):
            raise ResumeValidationError(f"Missing or invalid fact references: {path}")
    patterns = facts.get("prohibited_claim_patterns", [])
    if not isinstance(patterns, list):
        raise ResumeValidationError("prohibited_claim_patterns must be a list")
    patterns = [r"\b(?:TODO|TBD|INSERT HERE|PLACEHOLDER)\b", *patterns]
    for pattern in patterns:
        try:
            expression = re.compile(_text(pattern, "prohibited pattern"), re.IGNORECASE)
        except re.error as exc:
            raise ResumeValidationError("Invalid prohibited claim pattern") from exc
        for path, text in claims.items():
            if expression.search(text):
                raise ResumeValidationError(f"Prohibited claim pattern in {path}")
    result = {"root": root, "output_dir": output, "facts": facts, "draft": draft,
              "input_snapshot_path": snapshot_path.relative_to(root).as_posix(),
              "input_snapshot_sha256": snapshot_hash, "fixture_only": snapshot["fixture_only"],
              "approval": approval, "source_bindings": [{"path": path, "sha256": digest}
                                                         for path, digest in sorted(source_bindings.items())]}
    for name in ("facts", "draft", "approval", "leads"):
        result[f"{name}_path"] = inputs[name].relative_to(root).as_posix()
        result[f"{name}_sha256"] = hashes[name]
    return result


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:64] or "resume"


def _render(facts: dict, draft: dict) -> tuple[bytes, str]:
    # Lazy imports keep read-only input validation independent of PDF dependencies.
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Flowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.platypus.doctemplate import LayoutError

    green, black, gray = (colors.HexColor(code) for code in ("#008B4A", "#14181F", "#5C6370"))
    width = letter[0] - 1.1 * inch
    expected = []

    def paragraph(text, style, markup=None):
        expected.append(text)
        return Paragraph(escape(text) if markup is None else markup, style)

    class SectionRule(Flowable):
        def __init__(self, text):
            super().__init__()
            self.text, self.width, self.height = text.upper(), width, 23.0
            expected.append(self.text)

        def draw(self):
            self.canv.setFont("Helvetica-Bold", 10.4)
            self.canv.setFillColor(green)
            self.canv.drawString(0, 11.2, self.text)
            self.canv.setStrokeColor(green)
            self.canv.setLineWidth(1.2)
            self.canv.line(0, 4.0, self.width, 4.0)

    name = ParagraphStyle("Name", fontName="Helvetica-Bold", fontSize=28, leading=30, textColor=black, spaceAfter=1.5)
    headline = ParagraphStyle("Headline", fontName="Helvetica-Bold", fontSize=10.4, leading=12.8, textColor=green, spaceAfter=3)
    contact = ParagraphStyle("Contact", fontName="Helvetica", fontSize=9.6, leading=12, textColor=gray, spaceAfter=8)
    summary = ParagraphStyle("Summary", fontName="Helvetica", fontSize=9.9, leading=13.3, textColor=black, spaceAfter=5)
    skill_style = ParagraphStyle("Skill", fontName="Helvetica", fontSize=9.1, leading=11.3, textColor=black, spaceAfter=1.2)
    title_style = ParagraphStyle("RoleTitle", fontName="Helvetica", fontSize=9.95, leading=12.5, textColor=black)
    date_style = ParagraphStyle("RoleDate", fontName="Helvetica", fontSize=9.25, leading=12.5, textColor=gray, alignment=TA_RIGHT)
    bullet_style = ParagraphStyle("Bullet", fontName="Helvetica", fontSize=9.35, leading=12.35, textColor=black, leftIndent=12, firstLineIndent=-7, spaceAfter=2.1)
    education_style = ParagraphStyle("Education", fontName="Helvetica", fontSize=9.7, leading=12.4, textColor=black)
    identity = facts["identity"]
    # Keep the complete verified hrefs, while readable labels avoid adding a
    # second contact line to the retained one-page geometry.
    contact_keys = [key for key in ("email", "phone", "location", "portfolio_url", "linkedin_url") if identity.get(key)]
    contact_labels = {key: (identity[key].removeprefix("https://").removeprefix("www.")
                            if key in ("portfolio_url", "linkedin_url") else identity[key])
                      for key in contact_keys}
    contact_text = " | ".join(contact_labels.values())
    contact_markup = " | ".join(
        f'<link href="{escape(("mailto:" if key == "email" else "") + identity[key], quote=True)}">{escape(contact_labels[key])}</link>'
        if key in ("email", "portfolio_url", "linkedin_url") else escape(identity[key])
        for key in contact_keys)
    story = [paragraph(identity["name"].upper(), name), paragraph(draft["headline"], headline),
             paragraph(contact_text, contact, contact_markup), paragraph(draft["summary"], summary),
             Spacer(1, 3), SectionRule("Experience")]
    for role in draft["roles"]:
        role_text = role["title"].upper() + " | " + role["company"]
        role_markup = f'<b>{escape(role["title"].upper())}</b> <font color="#008B4A"><b>| {escape(role["company"])}</b></font>'
        table = Table([[paragraph(role_text, title_style, role_markup), paragraph(role["dates"], date_style)]],
                      colWidths=[width - 1.48 * inch, 1.48 * inch], hAlign="LEFT")
        table.setStyle(TableStyle([(side, (0, 0), (-1, -1), 0) for side in
                                   ("LEFTPADDING", "RIGHTPADDING", "TOPPADDING", "BOTTOMPADDING")]
                                  + [("VALIGN", (0, 0), (-1, -1), "TOP")]))
        block = [table, Spacer(1, 3)]
        for text in role["bullets"]:
            # The retained layout places the marker outside the text rather
            # than spending line width on a literal hyphen in the paragraph.
            expected.append("- " + text)
            block.append(Paragraph(escape(text), bullet_style, bulletText="-"))
        block.append(Spacer(1, 5.5))
        story.append(KeepTogether(block))
    story.append(SectionRule("Core Skills"))
    rows = [[paragraph(label + ": " + value, skill_style,
                       f'<b><font color="#008B4A">{escape(label)}:</font></b> {escape(value)}')]
            for label, value in draft["skills"]]
    commands = [("BACKGROUND", (0, i), (-1, i), colors.HexColor("#E4F4EB")) for i in range(0, len(rows), 2)]
    commands += [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 7),
                 ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 3.7),
                 ("BOTTOMPADDING", (0, 0), (-1, -1), 3.7)]
    story += [Table(rows, colWidths=[width], style=TableStyle(commands))]
    if facts.get("education"):
        story += [Spacer(1, 5), SectionRule("Education"),
                  paragraph(facts["education"]["text"], education_style)]
    stream = io.BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=letter, leftMargin=.55 * inch, rightMargin=.55 * inch,
                            topMargin=.46 * inch, bottomMargin=.48 * inch,
                            title=f'{identity["name"]} - {draft["company"]} - {draft["role"]}',
                            author=identity["name"], subject="Source-checked tailored resume", invariant=1)
    try:
        doc.build(story)
    except LayoutError as exc:
        raise ResumeValidationError("Resume overflows the supported one-page layout") from exc
    return stream.getvalue(), "\n".join(expected) + "\n"


def validate_pdf_bytes(data: bytes, expected: str) -> tuple[dict, str]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    if len(reader.pages) != 1:
        raise ResumeValidationError(f"Resume overflow: expected one page, got {len(reader.pages)}")
    page = reader.pages[0]
    if len(list(page.images)):
        raise ResumeValidationError("Resume contains raster images")
    extracted = page.extract_text(extraction_mode="plain") or ""
    if not extracted.strip() or not page.get("/Resources", {}).get("/Font"):
        raise ResumeValidationError("Resume does not have native selectable text")
    if normalize_text(extracted) != normalize_text(expected):
        raise ResumeValidationError("Full extracted PDF text does not match the approved draft")
    return {"native_text": True, "pages": 1, "text_matches": True}, extracted


def _write_once(path: Path, data: bytes) -> None:
    """Never overwrite historical artifacts, including a different validation report."""
    if path.exists():
        if path.read_bytes() != data:
            raise ResumeValidationError(f"Refusing to overwrite different bytes: {path.name}")
        return
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ResumeValidationError(f"Concurrent artifact conflict: {path.name}")


def build_resume(root: str | Path, facts_path: str | Path, draft_path: str | Path,
                 approval_path: str | Path, leads_path: str | Path, output_dir: str | Path,
                 input_snapshot_path: str | Path | None = None) -> dict:
    checked = validate_inputs(root, facts_path, draft_path, approval_path, leads_path, output_dir, input_snapshot_path)
    root, output = checked["root"], checked["output_dir"]
    facts, draft = checked["facts"], checked["draft"]
    data, expected = _render(facts, draft)
    validation, extracted = validate_pdf_bytes(data, expected)
    # Candidate-facing filenames must not expose internal requisition IDs or hashes.
    # Existing immutable-write guards reject collisions; use a new revision directory.
    role_key = _slug(draft["role"])
    display_name = facts["identity"]["name"]
    if display_name.isupper():
        display_name = display_name.title()
    filename = f'{_slug(display_name)}-{_slug(draft["company"])}-{role_key}-Resume.pdf'
    pdf = output / filename
    expected_path = pdf.with_suffix(".expected.txt")
    extracted_path = pdf.with_suffix(".extracted.txt")
    report_path = pdf.with_suffix(".validation.json")
    result = {key: draft[key] for key in ("profile_id", "role_ref", "job_url", "company", "role")}
    result.update({"status": "needs_visual_review", "path": pdf.relative_to(root).as_posix(),
                   "filename": filename, "sha256": sha256_bytes(data), "size_bytes": len(data),
                   "mime_type": "application/pdf", "draft_sha256": checked["draft_sha256"],
                   "facts_sha256": checked["facts_sha256"], "validation": validation,
                   "expected_text_path": expected_path.relative_to(root).as_posix(),
                   "extracted_text_path": extracted_path.relative_to(root).as_posix(),
                   "validation_report_path": report_path.relative_to(root).as_posix()})
    for name in ("facts", "draft", "approval", "leads"):
        result[f"{name}_path"] = checked[f"{name}_path"]
        result[f"{name}_sha256"] = checked[f"{name}_sha256"]
    result["source_bindings"] = checked["source_bindings"]
    for field in ("input_snapshot_path", "input_snapshot_sha256", "fixture_only"):
        result[field] = checked[field]
    files = {pdf: data, expected_path: expected.encode("utf-8"), extracted_path: extracted.encode("utf-8"),
             report_path: (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8")}
    # Check all targets before writing any; a later correction must choose a new run directory.
    for path, content in files.items():
        _contained(root, path, must_exist=False)
        if path.exists() and path.read_bytes() != content:
            raise ResumeValidationError(f"Refusing to overwrite different bytes: {path.name}")
    output.mkdir(parents=True, exist_ok=True)
    for path, content in files.items():
        _write_once(path, content)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    build = subcommands.add_parser("build")
    for option in ("root", "facts", "draft", "approval", "leads", "output-dir"):
        build.add_argument("--" + option, required=True)
    build.add_argument("--snapshot", help="Default: input-snapshot.json beside frozen facts")
    args = parser.parse_args()
    try:
        result = build_resume(args.root, args.facts, args.draft, args.approval, args.leads, args.output_dir, args.snapshot)
    except ResumeValidationError as exc:
        parser.exit(2, f"Resume rejected: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
