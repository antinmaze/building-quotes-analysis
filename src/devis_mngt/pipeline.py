from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from .cloud import AzureBackend, Settings
from .models import Batch, Document, Extraction, OCR, normalized
from .storage import digest, inventory, load, save


def process(input_path: Path, cache: Path, settings: Settings, limit: int | None = None,
            backend: AzureBackend | None = None) -> Batch:
    backend = backend or AzureBackend(settings)
    documents = []
    for item in inventory(input_path, limit):
        base = {k: item[k] for k in ("source", "sha256", "pages", "error")}
        document = Document(**base, status="error" if item["status"] == "ready" else item["status"],
                            ocr=None, extraction=None)
        if item["status"] == "ready":
            try:
                path = Path(item["source"])
                # Refuse a source changed since inventory; originals are only opened for reading.
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                        raise ValueError("Source modifiée depuis l'inventaire")
                ocr_key = digest({"sha256": item["sha256"], **settings.ocr_config()})
                ocr_path = cache / "ocr" / (ocr_key + ".json")
                if ocr_path.exists():
                    ocr = OCR.model_validate(load(ocr_path))
                else:
                    ocr = backend.ocr(path)
                    if item["pages"] is not None and ocr.page_count != item["pages"]:
                        raise ValueError("OCR incomplet : vérifier notamment la limite de pages du niveau F0")
                    save(ocr_path, ocr.model_dump())
                if item["pages"] is not None and ocr.page_count != item["pages"]:
                    raise ValueError("Nombre de pages OCR différent de l'original")
                document.ocr = ocr
                extraction_key = digest({"ocr": ocr_key, "ocr_payload": digest(ocr.model_dump()),
                                         **settings.extraction_config()})
                extraction_path = cache / "extraction" / (extraction_key + ".json")
                if extraction_path.exists():
                    extraction = Extraction.model_validate(load(extraction_path))
                else:
                    # Content-only extraction means duplicate bytes are charged only once.
                    extraction = backend.extract(ocr, "")
                    save(extraction_path, extraction.model_dump())
                document.extraction = corroborate_filename(extraction, path.name)
                document.status = "extracted"
            except Exception as exc:
                # Do not log SDK exception messages: they can include service URLs or content.
                document.error = f"{type(exc).__name__}: traitement échoué; vérifier configuration, réponse ou cache"
                if isinstance(exc, ValueError):
                    document.error = f"Validation : {exc}"
        if document.error:
            print(f"{document.source}: {document.error}", file=sys.stderr)
        documents.append(document)
    return Batch(schema_version="1", documents=documents)


def corroborate_filename(extraction: Extraction, filename: str) -> Extraction:
    result = extraction.model_copy(deep=True)
    name = normalized(filename)
    # A filename is never used to exclude a document or infer missing metadata.
    conflict = ("facture" in name and result.classification == "devis") or (
        "devis" in name and result.classification == "non_devis"
    )
    result.filename_conflict = result.filename_conflict or conflict
    return result
