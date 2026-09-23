import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_attempt_state.py"
SPEC = importlib.util.spec_from_file_location("job_leads_run_attempt_state_unit", SCRIPT)
assert SPEC and SPEC.loader
attempts = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = attempts
SPEC.loader.exec_module(attempts)


class RunAttemptStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "state").mkdir()
        (self.root / "outputs").mkdir()
        (self.root / "profile.json").write_text(
            json.dumps({"profile_id": "candidate", "status": "active"}),
            encoding="utf-8",
        )
        self.common = {
            "profile_id": "candidate",
            "local_date": "2026-09-01",
            "run_type": "daily-run",
        }
        self.counts = {
            "searched": 34,
            "deduplicated": 30,
            "gated": 8,
            "scored": 5,
            "qualified": 2,
            "verified": 1,
            "delivered": 0,
        }

    def tearDown(self):
        self.temporary.cleanup()

    def manifest(self, *, lead_count=1):
        value = {
            "manifest_version": 2,
            "profile_id": "candidate",
            "local_date": "2026-09-01",
            "run_type": "daily-run",
            "idempotency_key": "candidate:2026-09-01:daily-run",
            "lead_count": lead_count,
            "counts": {
                "searched": self.counts["searched"],
                "qualified": self.counts["qualified"],
                "verified": self.counts["verified"],
            },
        }
        path = self.root / "outputs" / "2026-09-01-daily-run-manifest.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def advance_to_search_complete(self):
        attempts.transition_attempt(self.root, **self.common, status="started")
        attempts.transition_attempt(self.root, **self.common, status="searching")
        return attempts.transition_attempt(
            self.root,
            **self.common,
            status="search_complete",
            funnel_counts=self.counts,
        )

    def test_inspection_is_read_only(self):
        result = attempts.inspect_attempt(self.root, **self.common)
        self.assertFalse(result["exists"])
        self.assertFalse((self.root / "state" / "run-attempts").exists())

    def test_heartbeat_only_refreshes_active_search(self):
        attempts.transition_attempt(self.root, **self.common, status="started")
        searching = attempts.transition_attempt(self.root, **self.common, status="searching")
        refreshed = attempts.heartbeat_attempt(self.root, **self.common)
        self.assertTrue(refreshed["heartbeat_fresh"])
        self.assertGreaterEqual(refreshed["heartbeat_at"], searching["heartbeat_at"])
        attempts.transition_attempt(
            self.root, **self.common, status="search_complete", funnel_counts=self.counts
        )
        with self.assertRaisesRegex(ValueError, "started or searching"):
            attempts.heartbeat_attempt(self.root, **self.common)

    def test_staging_requires_all_evidence_and_exact_manifest(self):
        self.advance_to_search_complete()
        path, digest = self.manifest()
        with self.assertRaisesRegex(ValueError, "link-validation"):
            attempts.transition_attempt(
                self.root,
                **self.common,
                status="staged",
                funnel_counts=self.counts,
                manifest_path=path,
                manifest_sha256=digest,
                candidate_pool_complete=True,
                rendering_complete=True,
            )
        staged = attempts.transition_attempt(
            self.root,
            **self.common,
            status="staged",
            funnel_counts=self.counts,
            manifest_path=path,
            manifest_sha256=digest,
            candidate_pool_complete=True,
            links_validated=True,
            rendering_complete=True,
        )
        self.assertEqual("staged", staged["status"])
        self.assertIsNone(
            attempts.validate_staged_attempt(
                self.root,
                **self.common,
                manifest_path=path,
                manifest_sha256=digest,
            )
        )

    def test_zero_lead_staging_requires_zero_lead_evidence(self):
        self.counts.update({"qualified": 0, "verified": 0, "delivered": 0})
        self.advance_to_search_complete()
        path, digest = self.manifest(lead_count=0)
        with self.assertRaisesRegex(ValueError, "zero-lead"):
            attempts.transition_attempt(
                self.root,
                **self.common,
                status="staged",
                funnel_counts=self.counts,
                manifest_path=path,
                manifest_sha256=digest,
                candidate_pool_complete=True,
                links_validated=True,
                rendering_complete=True,
            )

    def test_failed_attempt_can_restart_only_with_incremented_number(self):
        attempts.transition_attempt(self.root, **self.common, status="started")
        attempts.transition_attempt(
            self.root,
            **self.common,
            status="pre_claim_failed",
            failure_class="search_provider_unavailable",
        )
        with self.assertRaisesRegex(ValueError, "attempt_number must be 2"):
            attempts.transition_attempt(
                self.root, **self.common, status="started", attempt_number=1
            )
        restarted = attempts.transition_attempt(
            self.root, **self.common, status="started", attempt_number=2
        )
        self.assertEqual(2, restarted["attempt_number"])


if __name__ == "__main__":
    unittest.main()
