"""Synthetic source-freezing tests. No real candidate data is used."""

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_daily_resume_inputs.py"
spec = importlib.util.spec_from_file_location("resume_inputs", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class InputSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "sources").mkdir()
        self.write("profile.json", {"profile_id": "candidate"})
        self.candidate = {"profile_id": "candidate", "display_name": "Test Candidate", "email": "candidate@example.com"}
        self.write("candidate.json", self.candidate)
        data = b"Synthetic source: led customer webinars."
        (self.root / "sources/original.txt").write_bytes(data)
        self.facts = {
            "profile_id": "candidate",
            "identity": {"name": "Test Candidate", "email": "candidate@example.com"},
            "sources": [{"id": "resume", "path": "sources/original.txt", "sha256": hashlib.sha256(data).hexdigest()}],
            "facts": [{"id": "webinars", "statement": "Led customer webinars.", "source_ids": ["resume"]}],
            "history": [{"id": "educator", "title": "Customer Educator", "company": "Example Co", "dates": "2021 - 2024"}],
        }
        self.write("sources/resume-facts.json", self.facts)
        self.lead = {"profile_id": "candidate", "role_ref": "example-educator", "company": "Example Co", "role": "Educator", "link": "https://example.com/job"}
        self.write("leads.json", [self.lead])
        self.write("job-evidence.txt", "Synthetic full job description")

    def write(self, path, value):
        dest = self.root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")

    def run_prepare(self, **kwargs):
        params = dict(root=self.root, local_date="2026-10-01", leads_path=Path("leads.json"),
                      out_dir=Path("outputs/work/r1"), evidence_paths=[Path("job-evidence.txt")])
        params.update(kwargs)
        return module.prepare(**params)

    def test_snapshot_is_repeatable_and_binds_source(self):
        result = self.run_prepare()
        self.assertEqual(result, self.run_prepare())
        self.assertFalse(result["fixture_only"])
        self.assertEqual(result["drafts"], [])
        self.assertEqual(len(result["sources"]), 2)

    def test_changed_source_or_candidate_requires_reconciliation(self):
        self.run_prepare()
        (self.root / "sources/original.txt").write_text("Changed source")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            self.run_prepare(out_dir=Path("outputs/work/r2"))
        (self.root / "sources/original.txt").write_text("Synthetic source: led customer webinars.")
        self.candidate["email"] = "different@example.com"
        self.write("candidate.json", self.candidate)
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            self.run_prepare(out_dir=Path("outputs/work/r2"))

    def test_frozen_inputs_cannot_be_overwritten(self):
        self.run_prepare()
        self.facts["facts"][0]["statement"] = "Changed claim"
        self.write("sources/resume-facts.json", self.facts)
        with self.assertRaisesRegex(ValueError, "Frozen input changed"):
            self.run_prepare()

    def test_duplicate_lead_or_missing_job_evidence_rejected(self):
        self.write("leads.json", [self.lead, self.lead])
        with self.assertRaisesRegex(ValueError, "Duplicate role_ref"):
            self.run_prepare()
        self.write("leads.json", [self.lead])
        with self.assertRaisesRegex(ValueError, "job-description evidence"):
            self.run_prepare(evidence_paths=[])

    def test_zero_leads_and_path_escape(self):
        self.write("leads.json", [])
        self.assertEqual(self.run_prepare(evidence_paths=[])["drafts"], [])
        with self.assertRaises(ValueError):
            self.run_prepare(out_dir=Path("../escape"), evidence_paths=[])

    def test_source_id_cannot_escape_frozen_directory(self):
        self.facts["sources"][0]["id"] = "../../outside"
        self.write("sources/resume-facts.json", self.facts)
        with self.assertRaisesRegex(ValueError, "Invalid resume fact source"):
            self.run_prepare()
        self.assertFalse((self.root / "outside.txt").exists())


if __name__ == "__main__":
    unittest.main()
