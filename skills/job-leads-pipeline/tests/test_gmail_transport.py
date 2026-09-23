"""Offline Gmail transport tests: no OAuth browser, network, or live send."""

import base64
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gmail_transport.py"
SPEC = importlib.util.spec_from_file_location("gmail_transport", SCRIPT)
gmail = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gmail)
DOCTOR_SPEC = importlib.util.spec_from_file_location("job_leads_doctor_gmail_test", SCRIPT.with_name("doctor.py"))
doctor = importlib.util.module_from_spec(DOCTOR_SPEC)
DOCTOR_SPEC.loader.exec_module(doctor)


class FakeService:
    def __init__(self, response=None, error=None):
        self.response = response or {"id": "gmail-message-1", "threadId": "gmail-thread-1"}
        self.error = error
        self.calls = 0
        self.raw = None

    def users(self):
        return self

    def getProfile(self, *, userId):
        self.pending = "profile"
        return self

    def messages(self):
        return self

    def send(self, *, userId, body):
        self.pending = "send"
        self.calls += 1
        self.raw = gmail._decode64(body["raw"])
        return self

    def get(self, *, userId, id, format):
        self.pending = "get"
        self.get_id = id
        return self

    def execute(self):
        if self.pending == "profile":
            return {"emailAddress": "sender@example.com"}
        if self.pending == "get":
            return {"raw": base64.urlsafe_b64encode(self.raw).decode("ascii").rstrip("=")}
        if self.error:
            raise self.error
        return self.response


class GmailTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gmail-transport-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "outputs").mkdir()
        (self.root / "state").mkdir()
        (self.root / "state" / "run-ledger.jsonl").write_text("", encoding="utf-8")
        self.pdf = b"%PDF-1.7\nSYNTHETIC TEST ONLY\n%%EOF\n"
        self.manifest = {
            "profile_id": "candidate", "local_date": "2026-09-23", "run_type": "daily",
            "idempotency_key": "candidate:2026-09-23:daily", "sender": "sender@example.com",
            "recipient": "candidate@example.com", "subject": "Synthetic daily leads",
            "outcome": "lead_email", "reason_code": "verified_leads", "lead_count": 1,
            "attachments": [{"filename": "Candidate-Resume.pdf", "mime_type": "application/pdf",
                             "sha256": gmail._digest(self.pdf), "size_bytes": len(self.pdf)}],
        }
        self.request = {
            "from_address": self.manifest["sender"], "to": self.manifest["recipient"],
            "subject": self.manifest["subject"],
            "payload": {"mime_type": "multipart/mixed", "parts": [
                {"mime_type": "multipart/alternative", "parts": [
                    {"mime_type": "text/plain", "body": {"content": "One synthetic lead."}},
                    {"mime_type": "text/html", "body": {"content": "<p>One synthetic lead.</p>"}},
                ]},
                {"mime_type": "application/pdf", "filename": "Candidate-Resume.pdf",
                 "content_disposition": "attachment", "body": {
                     "base64_url_content": base64.urlsafe_b64encode(self.pdf).decode("ascii").rstrip("=")}},
            ]},
        }
        self.manifest_path = self.root / "outputs" / "manifest.json"
        self.request_path = self.root / "outputs" / "gmail-request.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.request_path.write_text(json.dumps(self.request), encoding="utf-8")
        self.validated = {
            "manifest_path": "outputs/manifest.json", "manifest_sha256": gmail._digest(self.manifest_path.read_bytes()),
            "gmail_request_path": "outputs/gmail-request.json", "gmail_request_sha256": gmail._digest(self.request_path.read_bytes()),
        }
        self.claim = {"status": "claimed", "should_send": True, "manifest_sha256": self.validated["manifest_sha256"],
                      "idempotency_key": self.manifest["idempotency_key"], "claim_token": "claim-token",
                      "attempt_token": "attempt-token"}

    def _modules(self, recorder=None, appender=None):
        delivery = SimpleNamespace(validate=lambda *_: self.validated,
                                   authorize_send=lambda *_: self.claim)
        guard = SimpleNamespace(record_provider_result=recorder or Mock())
        state = SimpleNamespace(append_run_event=appender or Mock())
        return {"daily_resume_delivery": delivery, "validate_run": guard, "state_io": state}

    def test_build_mime_decodes_exact_pdf_bytes(self):
        raw = gmail.build_mime(self.request, self.manifest)
        checked = gmail.verify_mime(raw, sender="sender@example.com", recipient="candidate@example.com",
                                    subject="Synthetic daily leads", expected=gmail._expected_attachments(self.manifest))
        self.assertEqual(1, checked["attachment_count"])
        self.assertEqual(gmail._digest(self.pdf), checked["attachment_sha256"]["Candidate-Resume.pdf"])

    def test_mismatched_attachment_rejected_before_claim(self):
        self.request["payload"]["parts"][1]["body"]["base64_url_content"] = base64.urlsafe_b64encode(b"%PDF-OTHER").decode()
        with self.assertRaisesRegex(ValueError, "frozen PDF bytes differ"):
            gmail.build_mime(self.request, self.manifest)

    def test_header_injection_rejected(self):
        self.request["subject"] = "Hi\r\nBcc: someone@example.com"
        with self.assertRaises(ValueError):
            gmail.build_mime(self.request, self.manifest)

    def test_only_native_secure_backend_is_allowed(self):
        cases = [
            ("win32", "keyring.backends.Windows", "WinVaultKeyring", "ok"),
            ("darwin", "keyring.backends.macOS", "Keyring", "ok"),
            ("linux", "keyring.backends.SecretService", "Keyring", "ok"),
            ("linux", "keyring.backends.libsecret", "Keyring", "ok"),
            ("linux", "keyring.backends.kwallet", "DBusKeyring", "ok"),
            ("win32", "keyrings.alt.file", "PlaintextKeyring", "needs_attention"),
            ("linux", "keyring.backends.chainer", "ChainerBackend", "needs_attention"),
            ("darwin", "keyring.backends.fail", "Keyring", "needs_attention"),
        ]
        for platform, module, name, expected in cases:
            with self.subTest(platform=platform, backend=f"{module}.{name}"):
                backend = type(name, (), {"__module__": module})()
                result = gmail.inspect_keyring_backend(backend=backend, platform_name=platform)
                self.assertEqual(expected, result["status"])
                self.assertEqual(f"{module}.{name}", result["backend"])

    def test_keyring_rejects_plaintext_before_token_access(self):
        backend = type("PlaintextKeyring", (), {"__module__": "keyrings.alt.file"})()
        backend.get_password = Mock()
        backend.set_password = Mock()
        fake_module = SimpleNamespace(get_keyring=lambda: backend)
        with patch.dict(sys.modules, {"keyring": fake_module}):
            with self.assertRaisesRegex(RuntimeError, "not an approved OS credential store"):
                gmail._keyring()
        backend.get_password.assert_not_called()
        backend.set_password.assert_not_called()

    def test_doctor_reports_actual_backend_safety(self):
        backend = type("PlaintextKeyring", (), {"__module__": "keyrings.alt.file"})()
        with patch.dict(sys.modules, {"keyring": SimpleNamespace(get_keyring=lambda: backend)}):
            result = doctor.check_token_store()
        self.assertEqual("needs_attention", result["status"])
        self.assertEqual("keyrings.alt.file.PlaintextKeyring", result["backend"])
        self.assertIn("Gmail is blocked", result["detail"])
        native = type("WinVaultKeyring", (), {"__module__": "keyring.backends.Windows"})()
        with patch.dict(sys.modules, {"keyring": SimpleNamespace(get_keyring=lambda: native)}):
            with patch.object(sys, "platform", "win32"):
                result = doctor.check_token_store()
        self.assertEqual("ok", result["status"])
        self.assertEqual("keyring.backends.Windows.WinVaultKeyring", result["backend"])

    def test_oauth_checks_secure_backend_before_browser(self):
        client = self.root / "oauth-client.json"
        client.write_text(json.dumps({"installed": {}}), encoding="utf-8")
        with patch.object(gmail, "_keyring", side_effect=RuntimeError("unsafe keyring")), \
             patch.object(gmail, "_google_modules") as google:
            with self.assertRaisesRegex(RuntimeError, "unsafe keyring"):
                gmail.connect_oauth(client, "sender@example.com")
        google.assert_not_called()

    def test_no_confirmation_or_claim_never_sends(self):
        service = FakeService()
        modules = self._modules()
        with patch.object(gmail, "_module", side_effect=modules.get):
            with self.assertRaisesRegex(ValueError, "confirm-send"):
                gmail.send_claimed(self.root, self.manifest_path, service=service, confirmed=False)
            self.claim = {"status": "complete", "should_send": False}
            result = gmail.send_claimed(self.root, self.manifest_path, service=service, confirmed=True)
        self.assertEqual("complete", result["status"])
        self.assertEqual(0, service.calls)

    def test_wrong_authenticated_account_never_acquires_claim(self):
        service = FakeService()
        service.getProfile = lambda *, userId: SimpleNamespace(execute=lambda: {"emailAddress": "other@example.com"})
        authorizer = Mock(return_value=self.claim)
        with patch.object(gmail, "_module", side_effect=self._modules().get):
            with self.assertRaisesRegex(ValueError, "authenticated Gmail account"):
                gmail.send_claimed(self.root, self.manifest_path, service=service, confirmed=True,
                                   authorizer=authorizer)
        authorizer.assert_not_called()
        self.assertEqual(0, service.calls)

    def test_success_writes_v2_receipt_then_ledger_and_verifies_sent_bytes(self):
        service = FakeService()
        order = []
        def append(path, event, *, expected_profile_id):
            receipt = self.root / "outputs" / "2026-09-23-daily-send-receipt.json"
            self.assertTrue(receipt.is_file())
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(saved["attachments"], event["attachments"])
            self.assertEqual(saved["gmail_request_sha256"], event["gmail_request_sha256"])
            order.append("ledger")
            self.assertEqual("send_completed", event["event"])
        modules = self._modules(appender=append)
        with patch.object(gmail, "_module", side_effect=modules.get):
            result = gmail.send_claimed(self.root, self.manifest_path, service=service, confirmed=True)
        self.assertEqual("sent", result["status"])
        self.assertEqual("verified", result["post_send_verification"]["status"])
        self.assertEqual(["ledger"], order)
        self.assertEqual(1, service.calls)
        receipt = json.loads((self.root / result["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(2, receipt["receipt_version"])
        self.assertEqual(self.validated["manifest_sha256"], receipt["manifest_sha256"])
        self.assertEqual(self.validated["gmail_request_sha256"], receipt["gmail_request_sha256"])
        self.assertEqual([{"filename": "Candidate-Resume.pdf", "mime_type": "application/pdf",
                           "sha256": gmail._digest(self.pdf), "size_bytes": len(self.pdf)}], receipt["attachments"])
        self.assertNotIn("base64_url_content", json.dumps(receipt))
        self.assertEqual("claim-token", receipt["claim_token"])

    def test_uncertain_error_preserves_claim_and_records_ambiguity(self):
        service = FakeService(error=TimeoutError("socket closed after submission"))
        recorder = Mock()
        modules = self._modules(recorder=recorder)
        with patch.object(gmail, "_module", side_effect=modules.get):
            result = gmail.send_claimed(self.root, self.manifest_path, service=service, confirmed=True)
        self.assertEqual("provider_ambiguous", result["status"])
        self.assertFalse(result["retry_automatically"])
        self.assertFalse((self.root / "outputs" / "2026-09-23-daily-send-receipt.json").exists())
        self.assertEqual("provider_ambiguous", recorder.call_args.kwargs["disposition"])

    def test_explicit_rejection_marks_safe_but_does_not_retry(self):
        error = Exception("Gmail rejected request")
        error.resp = SimpleNamespace(status=400)
        service = FakeService(error=error)
        recorder = Mock()
        with patch.object(gmail, "_module", side_effect=self._modules(recorder=recorder).get):
            result = gmail.send_claimed(self.root, self.manifest_path, service=service, confirmed=True)
        self.assertEqual("rejected_safe_to_retry", result["status"])
        self.assertFalse(result["retry_automatically"])
        self.assertEqual(1, service.calls)

    def test_missing_provider_ids_is_ambiguous(self):
        service = FakeService(response={"labelIds": ["SENT"]})
        recorder = Mock()
        with patch.object(gmail, "_module", side_effect=self._modules(recorder=recorder).get):
            result = gmail.send_claimed(self.root, self.manifest_path, service=service, confirmed=True)
        self.assertEqual("provider_ambiguous", result["status"])
        self.assertEqual("provider_ambiguous", recorder.call_args.kwargs["disposition"])

    def test_receipt_collision_is_not_overwritten(self):
        target = self.root / "outputs" / "receipt.json"
        target.write_text("old evidence", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            gmail._write_new_json(target, {"new": True})
        self.assertEqual("old evidence", target.read_text(encoding="utf-8"))

    def test_ledger_failure_keeps_receipt_and_forbids_resend(self):
        service = FakeService()
        def fail_append(*args, **kwargs):
            raise OSError("ledger not writable")
        with patch.object(gmail, "_module", side_effect=self._modules(appender=fail_append).get):
            result = gmail.send_claimed(self.root, self.manifest_path, service=service, confirmed=True)
        self.assertEqual("recovery_required", result["status"])
        self.assertFalse(result["retry_automatically"])
        self.assertTrue((self.root / result["receipt_path"]).exists())
        self.assertEqual(1, service.calls)

    def test_frozen_request_change_after_claim_stops_before_gmail(self):
        service = FakeService()
        def claim_and_mutate(*_):
            self.request_path.write_bytes(self.request_path.read_bytes() + b" ")
            return self.claim
        with patch.object(gmail, "_module", side_effect=self._modules().get):
            result = gmail.send_claimed(self.root, self.manifest_path, service=service,
                                        confirmed=True, authorizer=claim_and_mutate)
        self.assertEqual("manual_reconciliation_required", result["status"])
        self.assertFalse(result["retry_automatically"])
        self.assertEqual(0, service.calls)

    def test_oauth_client_must_not_live_in_git_checkout(self):
        (self.root / ".git").mkdir()
        client = self.root / "client.json"
        client.write_text('{"installed": {}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside every Git checkout"):
            gmail.connect_oauth(client, "sender@example.com")


if __name__ == "__main__":
    unittest.main()
