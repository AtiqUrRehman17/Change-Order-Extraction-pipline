"""Generates realistic messy change-order documents for testing.

Reads the hand-written samples in `sample_catalog.py` and writes:

    data/samples/<id>.txt | .pdf     the documents
    data/ground_truth/<id>.json      the correct answers
    data/samples/manifest.json       list of samples with style + expected issues

Run:  uv run python scripts/generate_samples.py
"""

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Table, TableStyle
from sample_catalog import SAMPLES, Sample

ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = ROOT / "data" / "samples"
TRUTH_DIR = ROOT / "data" / "ground_truth"

TABLE_MARKER = "{{TABLE}}"
PAGE_BREAK = "{{PAGEBREAK}}"

PAGE_WIDTH, PAGE_HEIGHT = LETTER
MARGIN = 54  # 0.75 inch
LINE_HEIGHT = 13


# --------------------------------------------------------------------------- text


def write_txt(sample: Sample, path: Path) -> None:
    path.write_text(sample.text, encoding="utf-8")


# --------------------------------------------------------------------------- text PDF


def write_text_pdf(sample: Sample, path: Path) -> None:
    """A normal PDF with a real text layer (what most software exports)."""
    canvas = Canvas(str(path), pagesize=LETTER)
    canvas.setFont("Courier", 9.5)
    y = PAGE_HEIGHT - MARGIN

    for line in sample.text.splitlines():
        if line.strip() == PAGE_BREAK:
            canvas.showPage()
            canvas.setFont("Courier", 9.5)
            y = PAGE_HEIGHT - MARGIN
        elif line.strip() == TABLE_MARKER and sample.table:
            y = draw_table(canvas, sample.table, y)
        else:
            canvas.drawString(MARGIN, y, line)
            y -= LINE_HEIGHT

    canvas.save()


def draw_table(canvas: Canvas, rows: list[list[str]], y: float) -> float:
    table = Table(rows, colWidths=[36, 200, 44, 40, 72, 80])
    table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
            ]
        )
    )
    _, height = table.wrapOn(canvas, PAGE_WIDTH - 2 * MARGIN, y)
    table.drawOn(canvas, MARGIN, y - height)
    canvas.setFont("Courier", 9.5)
    return y - height - LINE_HEIGHT


# --------------------------------------------------------------------------- scanned PDF


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("cour.ttf", "DejaVuSansMono.ttf", "LiberationMono-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render_scan(text: str, seed: int) -> Image.Image:
    """Draw the text on a 'paper' image and make it look photocopied."""
    rng = random.Random(seed)
    width, height = 1275, 1650  # US Letter at 150 DPI
    image = Image.new("L", (width, height), color=245)
    draw = ImageDraw.Draw(image)
    font = load_font(22)

    y = 90
    for line in text.splitlines():
        x = 80 + rng.randint(-2, 2)
        draw.text((x, y), line, fill=rng.randint(20, 60), font=font)
        y += 32

    # Speckles and toner dust
    for _ in range(2500):
        px, py = rng.randrange(width), rng.randrange(height)
        draw.point((px, py), fill=rng.randint(120, 200))

    image = image.rotate(0.8, resample=Image.Resampling.BICUBIC, fillcolor=245)
    return image.filter(ImageFilter.GaussianBlur(0.7))


def write_scanned_pdf(sample: Sample, path: Path) -> None:
    """A PDF that contains only an image: no text layer at all."""
    image = render_scan(sample.text, seed=sum(map(ord, sample.id)))
    canvas = Canvas(str(path), pagesize=LETTER)
    canvas.drawImage(ImageReader(image), 0, 0, width=PAGE_WIDTH, height=PAGE_HEIGHT)
    canvas.save()


# --------------------------------------------------------------------------- main

WRITERS = {
    "txt": (".txt", write_txt),
    "pdf": (".pdf", write_text_pdf),
    "pdf_scan": (".pdf", write_scanned_pdf),
}


def clean_old_files() -> None:
    for folder in (SAMPLES_DIR, TRUTH_DIR):
        for old in folder.glob("co_*"):
            old.unlink()


def main() -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    clean_old_files()

    manifest = []
    for sample in SAMPLES:
        extension, writer = WRITERS[sample.format]
        doc_path = SAMPLES_DIR / f"{sample.id}{extension}"
        truth_path = TRUTH_DIR / f"{sample.id}.json"

        writer(sample, doc_path)
        truth_path.write_text(sample.truth.model_dump_json(indent=2) + "\n", encoding="utf-8")

        manifest.append(
            {
                "id": sample.id,
                "file": doc_path.name,
                "format": sample.format,
                "style": sample.style,
                "ground_truth": truth_path.name,
                "expected_issues": sample.expected_issues,
            }
        )
        print(f"  {doc_path.name:<28} {sample.style}")

    (SAMPLES_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nWrote {len(SAMPLES)} samples to {SAMPLES_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
