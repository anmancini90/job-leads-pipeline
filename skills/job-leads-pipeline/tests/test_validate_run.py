import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_run.py"
SPEC = importlib.util.spec_from_file_location("job_leads_validate_run_test", SCRIPT)
assert SPEC and SPEC.loader
validate_run = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validate_run
SPEC.loader.exec_module(validate_run)

ATTEMPT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_attempt_state.py"
ATTEMPT_SPEC = importlib.util.spec_from_file_location(
    "job_leads_run_attempt_state_test", ATTEMPT_SCRIPT
)
assert ATTEMPT_SPEC and ATTEMPT_SPEC.loader
run_attempt_state = importlib.util.module_from_spec(ATTEMPT_SPEC)
sys.modules[ATTEMPT_SPEC.name] = run_attempt_state
ATTEMPT_SPEC.loader.exec_module(run_attempt_state)


class ValidateRunTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "state").mkdir()
        (self.root / "outputs").mkdir()
        (self.root / "state" / "run-ledger.jsonl").write_text("", encoding="utf-8")
        (self.root / "profile.json").write_text(
            json.dumps({"profile_id": "candidate", "status": "active", "timezone": "America/New_York"}),
            encoding="utf-8",
        )
        (self.root / "automation.json").write_text(
            json.dumps(
                {
                    "profile_id": "candidate",
                    "status": "active",
                    "cadence": "daily",
                    "sender": "sender@example.com",
                    "to": "candidate@example.com",
                }
            ),
            encoding="utf-8",
        )
        self.original_validator = validate_run._profile_validation_errors
        self.original_current_date = validate_run._current_profile_date
        validate_run._profile_validation_errors = lambda _root: []
        validate_run._current_profile_date = lambda _root: "2026-08-31"

    def tearDown(self):
        validate_run._profile_validation_errors = self.original_validator
        validate_run._current_profile_date = self.original_current_date
        self.temporary.cleanup()

    def manifest(self, *, run_type="daily-run", outcome="lead_email", lead_count=1):
        artifacts = {}
        for name, suffix, content in (
            ("payload", "payload.json", "{}"),
            ("html", "email.html", "<p>Outcome</p>"),
            ("text", "email.txt", "Outcome"),
        ):
            artifact_path = self.root / "outputs" / f"2026-08-31-{run_type}-{suffix}"
            artifact_path.write_text(content, encoding="utf-8")
            artifacts[name] = {
                "path": artifact_path.relative_to(self.root).as_posix(),
                "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            }
        evidence_path = self.root / "outputs" / f"2026-08-31-{run_type}-evidence.json"
        evidence_path.write_text("{}", encoding="utf-8")
        value = {
            "manifest_version": 2,
            "profile_id": "candidate",
            "local_date": "2026-08-31",
            "run_type": run_type,
            "idempotency_key": f"candidate:2026-08-31:{run_type}",
            "sender": "sender@example.com",
            "recipient": "candidate@example.com",
            "subject": "Your job leads",
            "outcome": outcome,
            "reason_code": "qualified_leads" if lead_count else "no_verified_roles",
            "lead_count": lead_count,
            "counts": {"searched": 4, "qualified": 2, "verified": lead_count, "withheld": 2 - lead_count},
            "evidence_paths": [evidence_path.relative_to(self.root).as_posix()],
            "artifacts": artifacts,
        }
        path = self.root / "outputs" / f"2026-08-31-{run_type}-manifest.json"
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return path, hashlib.sha256(path.read_bytes()).hexdigest(), value

    def acquire(self, **overrides):
        path, digest, value = self.manifest(
            run_type=overrides.get("run_type", "daily-run"),
            outcome=overrides.get("outcome", "lead_email"),
            lead_count=overrides.get("lead_count", 1),
        )
        arguments = {
            "profile_id": "candidate",
            "local_date": "2026-08-31",
            "run_type": value["run_type"],
            "manifest_path": path,
            "manifest_sha256": digest,
            "outcome": value["outcome"],
            "lead_count": value["lead_count"],
            "sender": value["sender"],
            "recipient": value["recipient"],
        }
        arguments.update(overrides)
        self.stage(path, digest, value)
        return validate_run.acquire_delivery_claim(self.root, **arguments), path, digest, value

    def stage(self, path, digest, value):
        counts = {
            "searched": value["counts"]["searched"],
            "deduplicated": value["counts"]["searched"],
            "gated": value["counts"]["qualified"],
            "scored": value["counts"]["qualified"],
            "qualified": value["counts"]["qualified"],
            "verified": value["counts"]["verified"],
            "delivered": 0,
        }
        common = {
            "profile_id": "candidate",
            "local_date": "2026-08-31",
            "run_type": value["run_type"],
        }
        run_attempt_state.transition_attempt(self.root, **common, status="started")
        run_attempt_state.transition_attempt(self.root, **common, status="searching")
        run_attempt_state.transition_attempt(
            self.root, **common, status="search_complete", funnel_counts=counts
        )
        run_attempt_state.transition_attempt(
            self.root,
            **common,
            status="staged",
            funnel_counts=counts,
            manifest_path=path,
            manifest_sha256=digest,
            candidate_pool_complete=True,
            links_validated=True,
            zero_lead_evidence_complete=value["lead_count"] == 0,
            rendering_complete=True,
        )

    def audit_legacy_receipt(self, receipt_path, *, date_role=None):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        compatibility_path = self.root / "state" / "legacy-receipt-compatibility.json"
        compatibility = (
            json.loads(compatibility_path.read_text(encoding="utf-8"))
            if compatibility_path.exists()
            else {"compatibility_version": 1, "records": {}}
        )
        record = {
            "profile_id": "candidate",
            "local_date": receipt["local_date"],
            "run_type": receipt["run_type"],
            "receipt_path": receipt_path.relative_to(self.root).as_posix(),
            "audited_at": "2026-08-31T12:00:00+00:00",
            "audited_by": "test-operator",
            "basis": "provider sent-mail record",
        }
        if date_role is not None:
            record["date_role"] = date_role
        compatibility["records"][digest] = record
        compatibility_path.write_text(json.dumps(compatibility), encoding="utf-8")

    def test_preflight_is_read_only_and_does_not_claim(self):
        result = validate_run.validate_run(
            self.root,
            profile_id="candidate",
            local_date="2026-08-31",
            run_type="daily-run",
        )
        self.assertEqual("ready", result["status"])
        self.assertFalse(result["should_send"])
        self.assertFalse((self.root / "state" / "run-claims").exists())

    def test_claim_is_bound_to_exact_manifest_and_only_one_caller_wins(self):
        path, digest, value = self.manifest()
        self.stage(path, digest, value)
        results = []
        lock = threading.Lock()

        def acquire():
            result = validate_run.acquire_delivery_claim(
                self.root,
                profile_id="candidate",
                local_date="2026-08-31",
                run_type="daily-run",
                manifest_path=path,
                manifest_sha256=digest,
                outcome=value["outcome"],
                lead_count=value["lead_count"],
                sender=value["sender"],
                recipient=value["recipient"],
            )
            with lock:
                results.append(result)

        threads = [threading.Thread(target=acquire) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, sum(bool(item["should_send"]) for item in results))
        claim = json.loads(next((self.root / "state" / "run-claims").glob("*.claim.json")).read_text())
        self.assertEqual(digest, claim["manifest_sha256"])
        self.assertEqual("candidate@example.com", claim["recipient"])

    def test_manifest_mismatch_cannot_acquire(self):
        path, digest, _value = self.manifest()
        self.stage(path, digest, _value)
        with self.assertRaisesRegex(ValueError, "manifest binding mismatch"):
            validate_run.acquire_delivery_claim(
                self.root,
                profile_id="candidate",
                local_date="2026-08-31",
                run_type="daily-run",
                manifest_path=path,
                manifest_sha256=digest,
                outcome="lead_email",
                lead_count=1,
                sender="other@example.com",
                recipient="candidate@example.com",
            )
        self.assertFalse((self.root / "state" / "run-claims").exists())

    def test_non_current_date_cannot_acquire_a_claim(self):
        path, digest, value = self.manifest()
        self.stage(path, digest, value)
        validate_run._current_profile_date = lambda _root: "2026-09-01"
        result = validate_run.acquire_delivery_claim(
            self.root,
            profile_id="candidate",
            local_date="2026-08-31",
            run_type="daily-run",
            manifest_path=path,
            manifest_sha256=digest,
            outcome=value["outcome"],
            lead_count=value["lead_count"],
            sender=value["sender"],
            recipient=value["recipient"],
        )
        self.assertEqual("blocked", result["status"])
        self.assertFalse(result["should_send"])
        self.assertFalse((self.root / "state" / "run-claims").exists())

    def test_v2_receipt_must_match_claim_and_recovers_missing_ledger(self):
        acquired, _path, digest, value = self.acquire()
        receipt = {
            "receipt_version": 2,
            "profile_id": "candidate",
            "local_date": "2026-08-31",
            "run_type": "daily-run",
            "status": "sent",
            "message_id": "gmail-message",
            "thread_id": "gmail-thread",
            "manifest_sha256": digest,
            "claim_token": acquired["claim_token"],
            "sender": value["sender"],
            "recipient": value["recipient"],
            "outcome": value["outcome"],
            "reason_code": value["reason_code"],
            "lead_count": value["lead_count"],
        }
        receipt_path = self.root / "outputs" / "2026-08-31-daily-run-send-receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        result = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual("write_state_only", result["recovery_action"])

        receipt["recipient"] = "wrong@example.com"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        result = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("inconsistent", result["status"])

    def test_legacy_receipt_still_recovers_and_blocks_same_date_send(self):
        receipt_path = self.root / "outputs" / "2026-08-31-legacy-send-receipt.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "profile_id": "candidate",
                    "local_date": "2026-08-31",
                    "run_type": "legacy",
                    "status": "sent",
                    "message_id": "old-message",
                }
            ),
            encoding="utf-8",
        )
        self.audit_legacy_receipt(receipt_path)
        result = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual("legacy", result["delivered_run_type"])
        self.assertFalse(result["should_send"])

    def test_unaudited_or_changed_legacy_receipt_is_inconsistent(self):
        receipt_path = self.root / "outputs" / "2026-08-31-legacy-send-receipt.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "profile_id": "candidate",
                    "local_date": "2026-08-31",
                    "run_type": "legacy",
                    "status": "sent",
                    "message_id": "old-message",
                }
            ),
            encoding="utf-8",
        )
        result = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("inconsistent", result["status"])
        self.audit_legacy_receipt(receipt_path)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["message_id"] = "changed-message"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        result = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("inconsistent", result["status"])
        self.assertIn("exact hash", result["errors"][0])

    def test_partial_v2_fields_can_be_grandfathered_by_exact_hash(self):
        receipt_path = self.root / "outputs" / "2026-08-31-legacy-send-receipt.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "profile_id": "candidate",
                    "local_date": "2026-08-31",
                    "run_type": "legacy",
                    "status": "sent",
                    "message_id": "old-message",
                    "sender": "sender@example.com",
                    "recipient": "candidate@example.com",
                }
            ),
            encoding="utf-8",
        )
        self.audit_legacy_receipt(receipt_path)
        result = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual(1, result["receipt_version"])

    def test_audited_historical_additional_receipt_does_not_replace_primary(self):
        primary_path = self.root / "outputs" / "2026-08-31-daily-run-send-receipt.json"
        historical_path = self.root / "outputs" / "2026-08-31-manual-hot-lead-send-receipt.json"
        for path, run_type, message_id in (
            (primary_path, "daily-run", "primary-message"),
            (historical_path, "manual-hot-lead", "historical-message"),
        ):
            path.write_text(
                json.dumps(
                    {
                        "profile_id": "candidate",
                        "local_date": "2026-08-31",
                        "run_type": run_type,
                        "status": "sent",
                        "message_id": message_id,
                    }
                ),
                encoding="utf-8",
            )
        self.audit_legacy_receipt(primary_path, date_role="primary")
        self.audit_legacy_receipt(historical_path, date_role="historical_additional")
        (self.root / "state" / "run-ledger.jsonl").write_text(
            json.dumps(
                {
                    "event": "send_completed",
                    "event_id": "primary-completion",
                    "profile_id": "candidate",
                    "local_date": "2026-08-31",
                    "run_type": "daily-run",
                    "idempotency_key": "candidate:2026-08-31:daily-run",
                    "message_id": "primary-message",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("complete", result["status"])
        self.assertEqual("outputs/2026-08-31-daily-run-send-receipt.json", result["date_delivery_receipt"])
        self.assertEqual(1, result["historical_additional_receipt_count"])

    def test_unaudited_additional_receipt_still_fails_closed(self):
        primary_path = self.root / "outputs" / "2026-08-31-daily-run-send-receipt.json"
        additional_path = self.root / "outputs" / "2026-08-31-manual-send-receipt.json"
        for path, run_type in ((primary_path, "daily-run"), (additional_path, "manual")):
            path.write_text(
                json.dumps(
                    {
                        "profile_id": "candidate",
                        "local_date": "2026-08-31",
                        "run_type": run_type,
                        "status": "sent",
                        "message_id": f"message-{run_type}",
                    }
                ),
                encoding="utf-8",
            )
        self.audit_legacy_receipt(primary_path)
        result = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("inconsistent", result["status"])
        self.assertTrue(any("exact hash" in error for error in result["errors"]))

    def test_two_audited_receipts_for_same_profile_date_are_inconsistent(self):
        for run_type in ("legacy-one", "legacy-two"):
            receipt_path = (
                self.root / "outputs" / f"2026-08-31-{run_type}-send-receipt.json"
            )
            receipt_path.write_text(
                json.dumps(
                    {
                        "profile_id": "candidate",
                        "local_date": "2026-08-31",
                        "run_type": run_type,
                        "status": "sent",
                        "message_id": f"message-{run_type}",
                    }
                ),
                encoding="utf-8",
            )
            self.audit_legacy_receipt(receipt_path)
        result = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("inconsistent", result["status"])
        self.assertTrue(
            any("multiple candidate-facing receipts" in error for error in result["errors"])
        )

    def test_alert_resolution_lookup_requires_exact_receipt_and_completion_references(self):
        receipt_path = self.root / "outputs" / "2026-08-31-legacy-send-receipt.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "profile_id": "candidate",
                    "local_date": "2026-08-31",
                    "run_type": "legacy",
                    "status": "sent",
                    "message_id": "old-message",
                }
            ),
            encoding="utf-8",
        )
        self.audit_legacy_receipt(receipt_path)
        (self.root / "state" / "missed-run-alerts.json").write_text(
            json.dumps(
                [
                    {
                        "profile_id": "candidate",
                        "local_date": "2026-08-31",
                        "reason": "missing scheduled result",
                    }
                ]
            ),
            encoding="utf-8",
        )
        completion = {
            "event": "send_completed",
            "profile_id": "candidate",
            "local_date": "2026-08-31",
            "run_type": "legacy",
            "idempotency_key": "candidate:2026-08-31:legacy",
            "message_id": "old-message",
        }
        resolution = {
            "event": "missed_run_alert_resolved",
            "event_id": "resolution-1",
            "profile_id": "candidate",
            "local_date": "2026-08-31",
            "receipt_path": "outputs/2026-08-31-legacy-send-receipt.json",
            "completion_event_id": "legacy:candidate:2026-08-31:legacy:old-message",
        }
        (self.root / "state" / "run-ledger.jsonl").write_text(
            "\n".join((json.dumps(completion), json.dumps(resolution))) + "\n",
            encoding="utf-8",
        )
        result = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("complete", result["status"])
        self.assertEqual("resolved", result["missed_run_alert_state"])
        self.assertEqual(1, result["alert_resolution_count"])

    def test_claim_requires_exact_staged_attempt_evidence(self):
        path, digest, value = self.manifest()
        result = validate_run.acquire_delivery_claim(
            self.root,
            profile_id="candidate",
            local_date="2026-08-31",
            run_type="daily-run",
            manifest_path=path,
            manifest_sha256=digest,
            outcome=value["outcome"],
            lead_count=value["lead_count"],
            sender=value["sender"],
            recipient=value["recipient"],
        )
        self.assertEqual("blocked", result["status"])
        self.assertIn("staged run attempt", result["errors"][0])
        self.assertFalse((self.root / "state" / "run-claims").exists())

    def test_explicit_rejection_can_retry_only_the_same_manifest(self):
        acquired, path, digest, value = self.acquire()
        validate_run.record_provider_result(
            self.root,
            profile_id="candidate",
            local_date="2026-08-31",
            run_type="daily-run",
            claim_token=acquired["claim_token"],
            attempt_token=acquired["attempt_token"],
            disposition="rejected_safe_to_retry",
            provider_status="rate_limited_before_acceptance",
        )
        preflight = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("retryable", preflight["status"])
        retry = validate_run.acquire_delivery_claim(
            self.root,
            profile_id="candidate",
            local_date="2026-08-31",
            run_type="daily-run",
            manifest_path=path,
            manifest_sha256=digest,
            outcome=value["outcome"],
            lead_count=value["lead_count"],
            sender=value["sender"],
            recipient=value["recipient"],
        )
        self.assertEqual("claimed_retry", retry["status"])
        self.assertTrue(retry["should_send"])
        self.assertNotEqual(acquired["attempt_token"], retry["attempt_token"])

    def test_ambiguous_provider_result_requires_manual_reconciliation(self):
        acquired, _path, _digest, _value = self.acquire()
        validate_run.record_provider_result(
            self.root,
            profile_id="candidate",
            local_date="2026-08-31",
            run_type="daily-run",
            claim_token=acquired["claim_token"],
            attempt_token=acquired["attempt_token"],
            disposition="provider_ambiguous",
        )
        result = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("manual_reconciliation_required", result["status"])
        self.assertFalse(result["should_send"])

        with self.assertRaisesRegex(ValueError, "already terminal"):
            validate_run.record_provider_result(
                self.root,
                profile_id="candidate",
                local_date="2026-08-31",
                run_type="daily-run",
                claim_token=acquired["claim_token"],
                attempt_token=acquired["attempt_token"],
                disposition="rejected_safe_to_retry",
            )

    def test_manifest_change_after_claim_requires_reconciliation(self):
        _acquired, path, _digest, _value = self.acquire()
        path.write_text("{}", encoding="utf-8")
        result = validate_run.inspect_run(
            self.root, profile_id="candidate", local_date="2026-08-31", run_type="daily-run"
        )
        self.assertEqual("inconsistent", result["status"])
        self.assertIn("changed after claim", result["errors"][0])


class CandidateDateTests(unittest.TestCase):
    def test_utc_midnight_uses_candidate_timezone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profile.json"
            instant = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
            profile.write_text(json.dumps({"timezone": "America/Los_Angeles"}), encoding="utf-8")
            self.assertEqual("2026-08-31", validate_run._current_profile_date(root, now_utc=instant))
            profile.write_text(json.dumps({"timezone": "Asia/Tokyo"}), encoding="utf-8")
            self.assertEqual("2026-09-01", validate_run._current_profile_date(root, now_utc=instant))

    def test_missing_timezone_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "profile.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "IANA timezone"):
                validate_run._current_profile_date(root)


if __name__ == "__main__":
    unittest.main()
