"""Prépare TOUS les PDF du corpus, en conservant la numérotation physique."""
import argparse
import hashlib
import json
from pathlib import Path

from pypdf import PdfReader
try:
    import pymupdf
except ImportError:
    pymupdf = None

ROOT = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--dpi", type=int, default=150)
    a = p.parse_args()
    if a.dpi < 72:
        p.error("résolution minimale : 72 dpi")
    corpus = json.loads((ROOT / "corpus.json").read_text())
    for d in corpus["documents"]:
        source = a.source_dir / d["filename"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != d["sha256"]:
            raise SystemExit(f"PDF absent ou différent du corpus : {source}")
        out = a.out / d["id"]
        out.mkdir(parents=True, exist_ok=True)
        text_pages = PdfReader(source).pages
        records = []
        pdf = pymupdf.open(source) if pymupdf else None
        try:
            for i, text_page in enumerate(text_pages, 1):
                image, text = out / f"page_{i:02d}.png", out / f"page_{i:02d}.txt"
                if pdf is not None:
                    pdf[i - 1].get_pixmap(dpi=a.dpi).save(str(image))
                else:
                    import subprocess
                    subprocess.run(["pdftoppm", "-f", str(i), "-l", str(i), "-singlefile", "-r", str(a.dpi), "-png", str(source), str(image.with_suffix(""))], check=True)
                text.write_text(text_page.extract_text() or "", encoding="utf-8")
                records.append({"page": i, "image": image.name, "text": text.name,
                                "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                                "text_sha256": hashlib.sha256(text.read_bytes()).hexdigest()})
        finally:
            if pdf is not None:
                pdf.close()
        (out / "manifest.json").write_text(json.dumps({"document": d, "dpi": a.dpi, "renderer": "pymupdf" if pymupdf else "poppler", "pages": records}, ensure_ascii=False, indent=2))
        print(f"{d['id']}: {len(records)} pages préparées")


if __name__ == "__main__":
    main()
