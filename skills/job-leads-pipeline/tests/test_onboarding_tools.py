import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


init_profile = load_script("init_profile")
doctor = load_script("doctor")
validate_profile = load_script("validate_profile")


class OnboardingToolsTests(unittest.TestCase):
    def test_initializer_creates_private_draft_without_claims(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "candidate"
            init_profile.initialize_project(project, "sample-candidate", "Sample Candidate", "America/New_York")
            facts = json.loads((project / "sources/resume-facts.json").read_text(encoding="utf-8"))
            automation = json.loads((project / "automation.json").read_text(encoding="utf-8"))
            self.assertEqual(facts["profile_id"], "sample-candidate")
            self.assertEqual(facts["history"], [])
            self.assertEqual(facts["facts"], [])
            self.assertEqual(facts["sources"], [])
            self.assertEqual(facts["status"], "paused")
            self.assertEqual(automation["status"], "paused")
            self.assertTrue((project / "output/pdf").is_dir())
            self.assertIn("*", (project / ".gitignore").read_text(encoding="utf-8"))
            self.assertEqual(doctor.check_project(project)["status"], "ok")
            self.assertEqual(validate_profile.validate_project(project), [])

    def test_initializer_refuses_populated_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "candidate"
            project.mkdir()
            (project / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                init_profile.initialize_project(project, "sample-candidate", "Sample Candidate", "America/New_York")
            self.assertEqual((project / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_doctor_rejects_git_candidate_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "candidate"
            init_profile.initialize_project(project, "sample-candidate", "Sample Candidate", "America/New_York")
            (project / ".git").mkdir()
            self.assertEqual(doctor.check_project(project)["status"], "needs_attention")

    def test_initializer_refuses_git_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            checkout.mkdir()
            (checkout / ".git").mkdir()
            with self.assertRaisesRegex(ValueError, "outside every Git checkout"):
                init_profile.initialize_project(checkout / "candidate", "sample-candidate", "Sample Candidate", "America/New_York")

    def test_doctor_does_not_claim_gmail_or_schedule_is_ready(self):
        result = doctor.diagnose("claude", probe_web=False)
        self.assertEqual(result["checks"]["gmail"]["status"], "not_checked")
        self.assertEqual(result["checks"]["scheduler"]["status"], "not_checked")


if __name__ == "__main__":
    unittest.main()
