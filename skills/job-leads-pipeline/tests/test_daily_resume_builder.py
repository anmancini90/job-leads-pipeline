"""Run non-authoring tests first; PDF cases require the artifact marker upstream.

PDF cases opt in with DAILY_RESUME_PDF_TESTS=1 after that marker succeeds.
"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "daily_resume_builder.py"
SPEC = importlib.util.spec_from_file_location("daily_resume_builder", MODULE_PATH)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class InputValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.facts = {"profile_id": "test-candidate", "identity": {
            "name": "Test Candidate", "email": "candidate@example.com", "phone": "555-0100",
            "location": "Durham, NC", "portfolio_url": "https://example.com",
            "linkedin_url": "https://www.linkedin.com/in/test"},
            "education": {"text": "Bachelor of Science | Example University"},
            "history": [{"id": "trainer", "title": "Customer Education Specialist",
                         "company": "Example Inc.", "dates": "Jul 2018 - Aug 2021"}],
            "facts": [{"id": "training", "statement": "Delivered customer education webinars.",
                       "source_ids": ["original-resume"]}],
            "prohibited_claim_patterns": [r"\bunassisted production engineering\b"]}
        self.draft = {"profile_id": "test-candidate", "role_ref": "example-trainer-123",
                      "job_url": "https://example.com/jobs/123", "company": "Example",
                      "role": "Trainer", "drafted_by": "synthetic drafter", "headline": "CUSTOMER EDUCATION",
                      "summary": "Customer educator with webinar experience.",
                      "roles": [{"history_id": "trainer", "title": "Customer Education Specialist",
                                 "company": "Example Inc.", "dates": "Jul 2018 - Aug 2021",
                                 "bullets": ["Delivered customer education webinars."]}],
                      "skills": [["EDUCATION", "Webinars and training"]],
                      "claim_refs": {path: ["training"] for path in
                                     ("headline", "summary", "roles.0.bullets.0", "skills.0.0", "skills.0.1")}}
        self.leads = [{"role_ref": self.draft["role_ref"], "link": self.draft["job_url"],
                       "company": self.draft["company"], "role": self.draft["role"]}]
        self.write("profile.json", {"profile_id": "test-candidate"})
        self.write("candidate.json", {"profile_id": "test-candidate", "confirmed": "Candidate evidence"})
        self.persist()

    def write(self, filename, obj):
        (self.root / filename).write_text(json.dumps(obj), encoding="utf-8")

    def persist(self):
        self.write("facts.json", self.facts)
        self.write("draft.json", self.draft)
        self.write("leads.json", self.leads)
        copied_sources = []
        for source in self.facts.get("sources", []):
            if "path" in source and (self.root / source["path"]).is_file():
                copied_sources.append({"id": source["id"], "path": source["path"],
                                       "sha256": builder.sha256_bytes((self.root / source["path"]).read_bytes())})
        snapshot = {"profile_id": "test-candidate", "fixture_only": False,
                    "facts_path": "facts.json", "sources": copied_sources, "drafts": []}
        for name in ("facts", "candidate", "leads"):
            snapshot[f"{name}_sha256"] = builder.sha256_bytes((self.root / f"{name}.json").read_bytes())
        self.write("input-snapshot.json", snapshot)
        self.approval = {"profile_id": "test-candidate", "role_ref": self.draft["role_ref"],
                         "job_url": self.draft["job_url"], "verdict": "pass", "reviewer": "independent-checker",
                         "draft_sha256": builder.sha256_bytes((self.root / "draft.json").read_bytes()),
                         "facts_sha256": builder.sha256_bytes((self.root / "facts.json").read_bytes()),
                         "input_snapshot_sha256": builder.sha256_bytes((self.root / "input-snapshot.json").read_bytes()),
                         "checks": dict.fromkeys(("factual", "relevant", "consistent", "quality"), True)}
        self.write("approval.json", self.approval)

    def validate(self, **kwargs):
        options = dict(root=self.root, facts_path="facts.json", draft_path="draft.json",
                       approval_path="approval.json", leads_path="leads.json", output_dir="output/pdf/2026-09-04-test")
        options.update(kwargs)
        return builder.validate_inputs(**options)

    def rejected(self, fragment=None):
        with self.assertRaisesRegex(builder.ResumeValidationError, fragment or ".*"):
            self.validate()

    def test_valid_inputs_do_not_author_files(self):
        result = self.validate()
        self.assertEqual(result["draft"]["role_ref"], self.draft["role_ref"])
        self.assertFalse((self.root / "output").exists())

    def test_lead_envelope(self):
        self.leads = {"profile_id": "test-candidate", "leads": self.leads}
        self.persist()
        self.validate()

    def test_missing_or_unknown_claim_ref(self):
        for bad in ({}, {**self.draft["claim_refs"], "summary": ["invented"]}):
            self.draft["claim_refs"] = bad
            self.persist()
            self.rejected("reference")

    def test_fact_without_source(self):
        self.facts["facts"][0]["source_ids"] = []
        self.persist()
        self.rejected("source IDs")

    def add_source_registry(self):
        self.write("source.json", {"source": "Original evidence"})
        self.facts["sources"] = [{"id": "original-resume", "path": "source.json",
                                  "sha256": builder.sha256_bytes((self.root / "source.json").read_bytes())}]
        self.persist()

    def test_source_registry_bindings(self):
        self.add_source_registry()
        result = self.validate()
        self.assertIn({"path": "source.json", "sha256": self.facts["sources"][0]["sha256"]}, result["source_bindings"])
        self.assertEqual({row["path"] for row in result["source_bindings"]},
                         {"facts.json", "candidate.json", "leads.json", "source.json"})
        for name in ("facts", "draft", "approval", "leads"):
            self.assertEqual(result[f"{name}_path"], name + ".json")
            self.assertEqual(result[f"{name}_sha256"], builder.sha256_bytes((self.root / (name + ".json")).read_bytes()))

    def test_source_registry_unknown_reference_and_duplicate_rejected(self):
        self.add_source_registry()
        self.facts["facts"][0]["source_ids"] = ["invented-source"]
        self.persist()
        self.rejected("Unknown source ID")
        self.facts["sources"] *= 2
        self.persist()
        self.rejected("Duplicate.*source")

    def test_source_registry_changed_bytes_rejected(self):
        self.add_source_registry()
        self.write("source.json", {"source": "Changed evidence"})
        self.rejected("[Ss]ource hash mismatch")

    def test_source_registry_path_escape_rejected(self):
        self.add_source_registry()
        self.facts["sources"][0]["path"] = "../outside.json"
        self.persist()
        self.rejected("outside the project")

    def test_mutable_canonical_facts_do_not_replace_frozen_binding(self):
        (self.root / "sources").mkdir()
        self.write("sources/resume-facts.json", {"canonical": "ceiling"})
        result = self.validate()
        self.assertNotIn("sources/resume-facts.json", {row["path"] for row in result["source_bindings"]})
        self.write("sources/resume-facts.json", {"canonical": "changed later"})
        self.assertEqual(result["source_bindings"], self.validate()["source_bindings"])

    def test_frozen_candidate_change_and_stale_snapshot_approval_rejected(self):
        self.write("candidate.json", {"profile_id": "test-candidate", "confirmed": "Changed evidence"})
        self.rejected("candidate hash mismatch")
        self.persist()
        snapshot_path = self.root / "input-snapshot.json"
        snapshot_path.write_bytes(snapshot_path.read_bytes() + b" ")
        self.rejected("snapshot SHA256 mismatch")

    def test_snapshot_draft_list_binding(self):
        snapshot = json.loads((self.root / "input-snapshot.json").read_text())
        snapshot["drafts"] = [{"path": "draft.json", "role_ref": self.draft["role_ref"],
                               "sha256": builder.sha256_bytes((self.root / "draft.json").read_bytes())}]
        self.write("input-snapshot.json", snapshot)
        self.approval["input_snapshot_sha256"] = builder.sha256_bytes((self.root / "input-snapshot.json").read_bytes())
        self.write("approval.json", self.approval)
        self.validate()
        snapshot["drafts"][0]["sha256"] = "0" * 64
        self.write("input-snapshot.json", snapshot)
        self.approval["input_snapshot_sha256"] = builder.sha256_bytes((self.root / "input-snapshot.json").read_bytes())
        self.write("approval.json", self.approval)
        self.rejected("Snapshot draft hash mismatch")

    def test_prohibited_claim(self):
        self.draft["summary"] = "Experienced in unassisted production engineering."
        self.persist()
        self.rejected("Prohibited")

    def test_empty_draft(self):
        self.draft["summary"] = ""
        self.persist()
        self.rejected("empty text")

    def test_wrong_history_title_or_date(self):
        for key in ("title", "dates", "company"):
            original = self.draft["roles"][0][key]
            self.draft["roles"][0][key] = "Invented value"
            self.persist()
            self.rejected("history mismatch")
            self.draft["roles"][0][key] = original

    def test_wrong_role_or_exact_url(self):
        for key in ("role", "company", "link"):
            original = self.leads[0][key]
            self.leads[0][key] = "Wrong match"
            self.persist()
            self.rejected("differs")
            self.leads[0][key] = original

    def test_duplicate_or_removed_role(self):
        self.leads *= 2
        self.persist()
        self.rejected("exactly one")
        self.leads = []
        self.persist()
        self.rejected("exactly one")

    def test_stale_draft_or_facts_approval(self):
        for name in ("draft", "facts"):
            self.persist()
            path = self.root / f"{name}.json"
            path.write_bytes(path.read_bytes() + b" ")
            self.rejected("stale|Snapshot facts")

    def test_failed_incomplete_approval(self):
        self.approval["checks"]["factual"] = False
        self.write("approval.json", self.approval)
        self.rejected("approval is required")

    def test_profile_mismatch(self):
        self.facts["profile_id"] = "another-candidate"
        self.persist()
        self.rejected("Profile mismatch")

    def test_path_escape_and_undated_output(self):
        for options in ({"facts_path": "../facts.json"}, {"output_dir": "../elsewhere"},
                        {"output_dir": "output/pdf/tailored"}, {"output_dir": "archive/2026-09-04"}):
            with self.assertRaises(builder.ResumeValidationError):
                self.validate(**options)

    def test_dangerous_url_rejected(self):
        self.facts["identity"]["portfolio_url"] = "javascript:alert(1)"
        self.persist()
        self.rejected("HTTPS URL")

    def test_normalization_does_not_hide_content_changes(self):
        self.assertEqual(builder.normalize_text("same\n  text"), "same text")
        self.assertNotEqual(builder.normalize_text("$146K"), builder.normalize_text("$1460K"))


@unittest.skipUnless(os.environ.get("DAILY_RESUME_PDF_TESTS") == "1", "PDF artifact marker required before opt-in")
class PdfTests(unittest.TestCase):
    setUp = InputValidationTests.setUp
    write = InputValidationTests.write
    persist = InputValidationTests.persist

    def build(self):
        return builder.build_resume(self.root, "facts.json", "draft.json", "approval.json",
                                    "leads.json", "output/pdf/2026-09-04-test")

    def test_one_page_native_text_and_stable_bytes(self):
        result = self.build()
        self.assertEqual(result["status"], "needs_visual_review")
        self.assertEqual(result["validation"], {"native_text": True, "pages": 1, "text_matches": True})
        pdf = self.root / result["path"]
        self.assertEqual(result["sha256"], builder.sha256_bytes(pdf.read_bytes()))
        self.assertEqual(result, self.build())

    def test_overflow_rejected_without_output(self):
        text = "Delivered detailed customer education webinars. " * 80
        self.draft["roles"][0]["bullets"] = [text.strip()] * 10
        for i in range(10):
            self.draft["claim_refs"][f"roles.0.bullets.{i}"] = ["training"]
        self.persist()
        with self.assertRaisesRegex(builder.ResumeValidationError, "overflow"):
            self.build()
        self.assertFalse((self.root / "output").exists())

    def test_all_input_markup_escaped(self):
        self.draft["summary"] = "Training <b>not markup</b> & webinars."
        self.persist()
        result = self.build()
        text = (self.root / result["extracted_text_path"]).read_text(encoding="utf-8")
        self.assertIn("<b>not markup</b>", text)

    def test_short_contact_labels_preserve_complete_verified_hrefs(self):
        from pypdf import PdfReader
        result = self.build()
        reader = PdfReader(self.root / result["path"])
        text = reader.pages[0].extract_text()
        self.assertIn("linkedin.com/in/test", text)
        self.assertNotIn("https://", text)
        targets = {annotation.get_object()["/A"]["/URI"] for annotation in
                   reader.pages[0].get("/Annots", []) if "/A" in annotation.get_object()}
        self.assertIn(self.facts["identity"]["portfolio_url"], targets)
        self.assertIn(self.facts["identity"]["linkedin_url"], targets)
        self.assertIn("mailto:" + self.facts["identity"]["email"], targets)

    def test_never_overwrite_different_pdf(self):
        result = self.build()
        path = self.root / result["path"]
        path.write_bytes(b"historical bytes")
        with self.assertRaisesRegex(builder.ResumeValidationError, "overwrite"):
            self.build()
        self.assertEqual(path.read_bytes(), b"historical bytes")

    def test_changed_extracted_text_rejected(self):
        result = self.build()
        with self.assertRaisesRegex(builder.ResumeValidationError, "does not match"):
            builder.validate_pdf_bytes((self.root / result["path"]).read_bytes(), "Something different")


if __name__ == "__main__":
    unittest.main()
