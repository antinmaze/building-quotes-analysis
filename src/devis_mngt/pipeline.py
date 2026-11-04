from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from time import monotonic

from .cloud import AzureBackend, Settings
from .events import EventJournal
from .models import Batch, Document, Extraction, OCR, normalized
from .storage import digest, inventory, load, save
from .reading import Reading, Normalization, assemble, coverage, complete_reading
from .projects import resolve_projects
from .limits import LimitExceeded
from .chunks import (
    CLIENT_MERGE_POLICY, COMPANY_MERGE_POLICY, CHUNK_VERSION,
    PAGE_BATCH_SIZE, read_chunks, normalize_chunks,
)


def process(input_path: Path, cache: Path, settings: Settings, limit: int | None = None,
            backend: AzureBackend | None = None, journal: EventJournal | None = None,
            structured: bool = False, local_pdf: bool = False, pdf_only: bool = False,
            exclude_dirs: tuple[str, ...] = (), limits=None, local_ocr: bool = False,
            selected_pages: list[int] | None = None, client_hint: str | None = None) -> Batch:
    if selected_pages is not None:
        if (not input_path.is_file() or local_pdf or local_ocr or not selected_pages
                or any(type(p) is not int or p < 1 for p in selected_pages)
                or selected_pages != sorted(set(selected_pages))):
            raise ValueError("Sélection de pages : PDF unique, pages triées uniques positives, sans routage local")
    engine = None
    if local_ocr:
        from .local_ocr import engine_config
        engine = engine_config()
        local_pdf = True
    backend = backend or AzureBackend(settings, limits=limits, journal=journal)
    documents = []
    for item in inventory(input_path, limit, pdf_only=pdf_only, exclude_dirs=exclude_dirs):
        context = {"source": item["source"], "sha256": item["sha256"]}
        if journal:
            journal.emit("document_started", **context, pages=item["pages"],
                         inventory_status=item["status"])
        base = {k: item[k] for k in ("source", "sha256", "pages", "error")}
        document = Document(**base, status="error" if item["status"] == "ready" else item["status"],
                            ocr=None, extraction=None)
        if item["status"] == "ready":
            stage = "source_validation"
            started = monotonic()
            try:
                path = Path(item["source"])
                # Refuse a source changed since inventory; originals are only opened for reading.
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                        raise ValueError("Source modifiée depuis l'inventaire")
                ocr_config = settings.ocr_config()
                if selected_pages is not None:
                    if path.suffix.lower() != ".pdf" or selected_pages[-1] > item["pages"]:
                        raise ValueError("Sélection de pages hors du PDF source")
                    ocr_config = {**ocr_config, "selected_pages": selected_pages}
                existing_full_ocr = cache / "ocr" / (
                    digest({"sha256": item["sha256"], **ocr_config}) + ".json")
                if local_pdf and path.suffix.lower() == ".pdf" and not existing_full_ocr.exists():
                    from .local_pdf import LOCAL_VERSION
                    ocr_config = {**ocr_config, "local_first": LOCAL_VERSION}
                    if local_ocr:
                        ocr_config["local_ocr"] = engine
                ocr_key = digest({"sha256": item["sha256"], **ocr_config})
                ocr_path = cache / "ocr" / (ocr_key + ".json")
                stage = "ocr"
                started = monotonic()
                cached = ocr_path.exists()
                if journal:
                    journal.emit("stage_started", **context, stage=stage,
                                 cached=cached, cache_key=ocr_key)
                if cached:
                    ocr = OCR.model_validate(load(ocr_path))
                else:
                    if local_pdf and path.suffix.lower() == ".pdf":
                        from .local_pdf import local_first
                        ocr = local_first(path, backend, cache, item["sha256"], settings.ocr_config(),
                                          journal, use_ocr=local_ocr, engine=engine)
                    else:
                        ocr = (backend.ocr(path, pages=selected_pages) if selected_pages is not None
                               else backend.ocr(path))
                    expected_pages = len(selected_pages) if selected_pages is not None else item["pages"]
                    if expected_pages is not None and ocr.page_count != expected_pages:
                        raise ValueError("OCR incomplet : vérifier notamment la limite de pages du niveau F0")
                    save(ocr_path, ocr.model_dump())
                expected_pages = len(selected_pages) if selected_pages is not None else item["pages"]
                if expected_pages is not None and ocr.page_count != expected_pages:
                    raise ValueError("Nombre de pages OCR différent de l'original")
                if selected_pages is not None and set(ocr.page_texts) != {str(p) for p in selected_pages}:
                    raise ValueError("Pages OCR différentes des pages sélectionnées ou numérotation source perdue")
                document.ocr = ocr
                if journal:
                    journal.emit("stage_completed", **context, stage=stage, cached=cached,
                                 pages=ocr.page_count, duration_seconds=round(monotonic() - started, 3))
                if structured:
                    chunked = ocr.page_count > PAGE_BATCH_SIZE
                    chunk_config = {"chunking": CHUNK_VERSION} if chunked else {}
                    stage = "reading"
                    started = monotonic()
                    reading_key = digest({"ocr": ocr_key, "ocr_payload": digest(ocr.model_dump()),
                                          **settings.reading_config(), **chunk_config})
                    reading_path = cache / "reading" / (reading_key + ".json")
                    cached = reading_path.exists()
                    if journal:
                        journal.emit("stage_started", **context, stage=stage, cached=cached,
                                     cache_key=reading_key)
                    if cached:
                        reading = Reading.model_validate(load(reading_path))
                    elif chunked:
                        reading = read_chunks(backend, ocr, cache, settings, journal)
                    else:
                        reading = backend.read_quote(ocr)
                    if not cached:
                        save(reading_path, reading.model_dump())
                    reading, recovered = complete_reading(reading, ocr)
                    if journal:
                        for row_id in recovered:
                            journal.emit("table_row_recovered", **context, row_id=row_id)
                    if journal:
                        journal.emit("stage_completed", **context, stage=stage, cached=cached,
                                     extracted_lines=len(reading.lines),
                                     duration_seconds=round(monotonic() - started, 3))
                    stage = "normalization"
                    started = monotonic()
                    client_context = {"client_folder_hint": client_hint} if client_hint else {}
                    key = digest({**client_context, "reading": reading_key, "payload": digest(reading.model_dump()),
                                  "filename": path.name, **settings.normalization_config(), **chunk_config,
                                  **({"company_merge_policy": COMPANY_MERGE_POLICY,
                                      "client_merge_policy": CLIENT_MERGE_POLICY} if chunked else {})})
                    target = cache / "normalization" / (key + ".json")
                    cached = target.exists()
                    if journal:
                        journal.emit("stage_started", **context, stage=stage, cached=cached, cache_key=key)
                    if cached:
                        normalization = Normalization.model_validate(load(target))
                    elif chunked:
                        normalization = normalize_chunks(
                            backend, reading, path.name, ocr, cache, settings, journal, client_hint)
                    else:
                        normalization = backend.normalize_quote(
                            reading, path.name, ocr,
                            **({"client_hint": client_hint} if client_hint else {}))
                    extraction = assemble(reading, normalization, ocr)
                    if not cached:
                        save(target, normalization.model_dump())
                    if journal:
                        journal.emit("stage_completed", **context, stage=stage, cached=cached,
                                     duration_seconds=round(monotonic() - started, 3))
                        for missing in coverage(ocr, extraction):
                            journal.emit("coverage_gap", **context, **missing)
                    document.extraction = corroborate_filename(extraction, path.name)
                    document.status = "extracted"
                else:
                    stage = "extraction"
                    started = monotonic()
                    document.extraction = _legacy_extract(backend, settings, cache, ocr, ocr_key,
                                                          path, journal, context)
                    document.status = "extracted"
            except LimitExceeded:
                save(cache / "results.partial.json",
                     Batch(schema_version="1", documents=documents).model_dump())
                if journal:
                    journal.emit("cloud_limit_reached", **context,
                                 partial_results=str(cache / "results.partial.json"))
                raise
            except Exception as exc:
                # Do not log SDK exception messages: they can include service URLs or content.
                document.error = f"{type(exc).__name__}: traitement échoué; vérifier configuration, réponse ou cache"
                if isinstance(exc, ValueError):
                    document.error = f"Validation : {exc}"
                if journal:
                    response = getattr(exc, "response", None)
                    headers = getattr(response, "headers", {})
                    journal.emit("stage_failed", **context, stage=stage,
                                 exception_type=type(exc).__name__,
                                 status_code=getattr(exc, "status_code", None),
                                 request_id=getattr(exc, "request_id", None),
                                 retry_after=headers.get("retry-after"),
                                 duration_seconds=round(monotonic() - started, 3))
        if document.error:
            print(f"{document.source}: {document.error}", file=sys.stderr)
        documents.append(document)
        if document.extraction:
            for line in document.extraction.lines:
                line.export_forfait = False
                line.export_unit_prices = True
        if journal:
            journal.document(document)
    if structured:
        hints = {d.source: client_hint for d in documents} if client_hint else None
        for changed in resolve_projects(documents, hints):
            if journal:
                journal.emit("project_linked", source=changed.source,
                             client=changed.extraction.client, evidence=changed.extraction.project_link)
                journal.document(changed)
    return Batch(schema_version="1", documents=documents)


def _legacy_extract(backend, settings, cache, ocr, ocr_key, path, journal, context):
    extraction_key = digest({"ocr": ocr_key, "ocr_payload": digest(ocr.model_dump()),
                             **settings.extraction_config()})
    extraction_path = cache / "extraction" / (extraction_key + ".json")
    started = monotonic()
    cached = extraction_path.exists()
    if journal:
        journal.emit("stage_started", **context, stage="extraction", cached=cached,
                     cache_key=extraction_key, config=settings.extraction_config())
    if cached:
        extraction = Extraction.model_validate(load(extraction_path))
    else:
        extraction = backend.extract(ocr, "")
        save(extraction_path, extraction.model_dump())
    if journal:
        journal.emit("stage_completed", **context, stage="extraction", cached=cached,
                     duration_seconds=round(monotonic() - started, 3),
                     extracted_lines=len(extraction.lines))
    return corroborate_filename(extraction, path.name)


def corroborate_filename(extraction: Extraction, filename: str) -> Extraction:
    result = extraction.model_copy(deep=True)
    name = normalized(filename)
    # A filename is never used to exclude a document or infer missing metadata.
    conflict = ("facture" in name and result.classification == "devis") or (
        "devis" in name and result.classification == "non_devis"
    )
    result.filename_conflict = result.filename_conflict or conflict
    return result
