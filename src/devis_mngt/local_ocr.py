from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader

from .storage import digest, load, save

OCR_VERSION = "ocrmypdf-fra-1"


def engine_config() -> dict:
    try:
        version = subprocess.run(
            [sys.executable, "-m", "ocrmypdf", "--version"],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        tess = subprocess.run(
            ["tesseract", "--version"], check=True, capture_output=True, text=True, timeout=30,
        ).stdout.splitlines()[0]
        langs = subprocess.run(
            ["tesseract", "--list-langs"], check=True, capture_output=True, text=True, timeout=30,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("OCR local indisponible : installer OCRmyPDF, Tesseract et Ghostscript") from exc
    if "fra" not in langs:
        raise ValueError("OCR local : données françaises Tesseract (fra) manquantes")
    return {"adapter": OCR_VERSION, "ocrmypdf": version, "tesseract": tess, "language": "fra",
            "dpi": 300, "rotate": True, "deskew": True}


def searchable_copy(source: Path, cache: Path, pages: list[int], config: dict) -> Path:
    pages = sorted(set(pages))
    count = len(PdfReader(source).pages)
    if not pages or pages[0] < 1 or pages[-1] > count:
        raise ValueError("Sélection de pages OCR locale invalide")
    with source.open("rb") as stream:
        source_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    key = digest({"source": source_sha, "pages": pages, **config})
    folder = cache / "local-ocr"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target, manifest = folder / (key + ".pdf"), folder / (key + ".json")
    if target.exists() and manifest.exists():
        metadata = load(manifest)
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != metadata["sha256"]:
                raise ValueError("Copie OCR locale modifiée : contrôle d'intégrité échoué")
        return target
    # All intermediate documents stay in the private cache, not a shared temp folder.
    with tempfile.TemporaryDirectory(prefix="ocr-work-", dir=folder) as work:
        work_path = Path(work)
        output = work_path / "searchable.pdf"
        env = {**os.environ, "TMPDIR": str(work_path.absolute())}
        command = [
            sys.executable, "-m", "ocrmypdf", "--language", "fra",
            "--force-ocr", "--pages", ",".join(map(str, pages)),
            "--output-type", "pdf", "--optimize", "0", "--jobs", "2",
            "--oversample", "300", "--rotate-pages", "--deskew",
            "--tesseract-timeout", "180", "--quiet",
            str(source.absolute()), str(output.absolute()),
        ]
        try:
            response = subprocess.run(command, env=env, capture_output=True, text=True,
                                      timeout=max(300, len(pages) * 240))
        except subprocess.TimeoutExpired as exc:
            raise ValueError("OCR local interrompu : délai maximal dépassé") from exc
        # Preserve diagnostics privately, never treat an engine failure as successful OCR.
        log = folder / (key + ".log")
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(response.stderr)
        if response.returncode != 0 or not output.exists():
            raise ValueError(f"OCR local échoué (code {response.returncode}) ; diagnostic : {log}")
        if len(PdfReader(output).pages) != count:
            raise ValueError("Copie OCR locale incomplète : nombre de pages différent")
        output.chmod(0o600)
        os.replace(output, target)
        with target.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        save(manifest, {"sha256": checksum, "source_sha256": source_sha,
                        "pages": pages, "config": config})
    return target
