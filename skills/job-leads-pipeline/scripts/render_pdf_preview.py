#!/usr/bin/env python3
"""Render one private, one-page resume PDF for a real visual review."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path


def render_preview(project_root: str | Path, pdf_path: str | Path, output_path: str | Path) -> dict:
    import pypdfium2 as pdfium

    root = Path(project_root).expanduser().resolve(strict=True)
    pdf = (root / pdf_path).resolve(strict=True)
    output = (root / output_path).resolve()
    if not pdf.is_relative_to(root / "output" / "pdf") or pdf.suffix.lower() != ".pdf":
        raise ValueError("PDF must be inside this private project's output/pdf directory")
    if not output.is_relative_to(root / "outputs" / "previews") or output.suffix.lower() != ".png":
        raise ValueError("Preview must be a PNG inside this private project's outputs/previews directory")
    document = pdfium.PdfDocument(str(pdf))
    try:
        if len(document) != 1:
            raise ValueError("Only a one-page reviewed resume may be previewed")
        page = document[0]
        try:
            bitmap = page.render(scale=2)
            try:
                image = bitmap.to_pil().copy()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    data = stream.getvalue()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        if output.read_bytes() != data:
            raise ValueError("Preview changed; use a new revision path")
    return {"pdf": pdf.relative_to(root).as_posix(), "preview": output.relative_to(root).as_posix(),
            "width": image.width, "height": image.height}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(render_preview(args.root, args.pdf, args.out), indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Preview rejected: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
