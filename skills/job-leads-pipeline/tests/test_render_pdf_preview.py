"""Synthetic PDF preview test; authoring is opt-in for local agents."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/render_pdf_preview.py"
SPEC = importlib.util.spec_from_file_location("render_pdf_preview", SCRIPT)
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)


@unittest.skipUnless(os.environ.get("DAILY_RESUME_PDF_TESTS") == "1", "PDF artifact marker required before opt-in")
class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf_path = self.root / "output/pdf/2026-10-01-test/Synthetic-Resume.pdf"
        self.pdf_path.parent.mkdir(parents=True)
        from reportlab.pdfgen import canvas
        document = canvas.Canvas(str(self.pdf_path))
        document.drawString(80, 700, "Synthetic resume preview")
        document.save()

    def test_render_and_no_overwrite(self):
        output = "outputs/previews/2026-10-01/synthetic.png"
        result = preview.render_preview(self.root, self.pdf_path, output)
        self.assertGreater(result["width"], 1000)
        self.assertTrue((self.root / output).read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(result, preview.render_preview(self.root, self.pdf_path, output))
        with self.assertRaisesRegex(ValueError, "Preview must"):
            preview.render_preview(self.root, self.pdf_path, "outside.png")


if __name__ == "__main__":
    unittest.main()
