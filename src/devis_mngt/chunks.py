from __future__ import annotations

from pathlib import Path

from .models import OCR, normalized
from .reading import Reading, Normalization, table_rows
from .storage import digest, load, save

PAGE_BATCH_SIZE = 2
CHUNK_VERSION = "pages-2-v1"
COMPANY_MERGE_POLICY = "first-block-company-1"
CLIENT_MERGE_POLICY = "first-block-client-1"


def page_batches(ocr: OCR) -> list[list[int]]:
    pages = sorted(int(p) for p in ocr.page_texts)
    return [pages[i:i + PAGE_BATCH_SIZE] for i in range(0, len(pages), PAGE_BATCH_SIZE)]


def cached_call(cache: Path, identity: dict, schema, call, journal, stage: str, pages: list[int]):
    target = cache / "chunks" / (digest(identity) + ".json")
    cached = target.exists()
    if journal:
        journal.emit("chunk_started", stage=stage, pages=pages, cached=cached,
                     cache_key=target.stem)
    result = schema.model_validate(load(target)) if cached else call()
    if not cached:
        save(target, result.model_dump())
    if journal:
        journal.emit("chunk_completed", stage=stage, pages=pages, cached=cached)
    return result


def read_chunks(backend, ocr: OCR, cache: Path, settings, journal=None) -> Reading:
    merged = None
    batches = page_batches(ocr)
    page_owner = {p: i for i, pages in enumerate(batches) for p in pages}
    boundary_rows = {
        row["id"] for row in table_rows(ocr)
        if len({page_owner[p] for cell in row["cells"] for p in cell["pages"]}) > 1
    }
    for pages in batches:
        context = merged.model_dump(exclude={"lines"}) if merged is not None else {}
        part = cached_call(
            cache, {"version": CHUNK_VERSION, "stage": "reading", "pages": pages,
                    "ocr": digest(ocr.model_dump()), "context": context,
                    "config": settings.reading_config()},
            Reading, lambda: backend.read_quote(ocr, pages=pages, document_context=context),
            journal, "reading", pages,
        )
        for line in part.lines:
            if not line.source_pages or not set(line.source_pages).issubset(pages):
                raise ValueError("Lecture par blocs : pages de ligne hors du bloc")
            if boundary_rows.intersection(line.table_row_ids):
                line.blockers.append("Ligne de tableau traversant une frontière de bloc : vérifier la continuité")
        if merged is None:
            merged = part.model_copy(deep=True)
            continue
        if part.classification != merged.classification:
            merged.classification = "incertain"
            merged.blockers.append("Classification différente entre les blocs du devis")
        for field, evidence in (
            ("company_raw", "company_excerpts"),
            ("project_client_raw", "project_excerpts"),
            ("billing_client_raw", None),
            ("year", "year_excerpts"),
        ):
            old, new = getattr(merged, field), getattr(part, field)
            if old is None:
                setattr(merged, field, new)
            elif new is not None and normalized(str(old)) != normalized(str(new)):
                merged.blockers.append(f"Métadonnée contradictoire entre blocs : {field}")
            if evidence:
                setattr(merged, evidence, list(dict.fromkeys(
                    getattr(merged, evidence) + getattr(part, evidence))))
        merged.lines.extend(part.lines)
        merged.blockers = list(dict.fromkeys(merged.blockers + part.blockers))
        merged.warnings = list(dict.fromkeys(merged.warnings + part.warnings))
    if merged is None:
        raise ValueError("OCR sans pages : découpage impossible")
    return merged


def normalize_chunks(backend, reading: Reading, filename: str, ocr: OCR,
                     cache: Path, settings, journal=None, client_hint=None) -> Normalization:
    batches = page_batches(ocr)
    owner = {p: i for i, pages in enumerate(batches) for p in pages}
    groups: list[list[int]] = [[] for _ in batches]
    for index, line in enumerate(reading.lines):
        if not line.source_pages or not set(line.source_pages).issubset(owner):
            raise ValueError("Normalisation par blocs : pages sources invalides")
        if len({owner[p] for p in line.source_pages}) != 1:
            raise ValueError("Ligne récupérée traversant plusieurs blocs : continuité à vérifier")
        groups[owner[min(line.source_pages)]].append(index)
    merged = None
    for pages, indices in zip(batches, groups):
        if not indices and reading.lines:
            continue
        part_reading = reading.model_copy(deep=True)
        part_reading.lines = [reading.lines[i] for i in indices]
        # The header is taken from this block, not an extra page of the document.
        part_ocr = ocr.model_copy(update={
            "page_texts": {str(p): ocr.page_texts[str(p)] for p in pages},
        })
        hint = {"client_hint": client_hint} if client_hint else {}
        part = cached_call(
            cache, {"version": CHUNK_VERSION, "stage": "normalization", "pages": pages,
                    "reading": part_reading.model_dump(), "filename": filename,
                    "header": next(iter(part_ocr.page_texts.values()))[:3500],
                    **hint, "config": settings.normalization_config()},
            Normalization,
            lambda: backend.normalize_quote(part_reading, filename, part_ocr, **hint),
            journal, "normalization", pages,
        )
        if sorted(line.line_index for line in part.lines) != list(range(1, len(indices) + 1)):
            raise ValueError("Normalisation par blocs : lignes manquantes ou dupliquées")
        part = part.model_copy(deep=True)
        for line in part.lines:
            line.line_index = indices[line.line_index - 1] + 1
        if merged is None:
            merged = part
        else:
            if (part.company is not None
                    and normalized(part.company) != normalized(merged.company or "")):
                warning = (
                    f"Entreprise du bloc {pages} : {part.company!r} ; "
                    f"premier nom conservé sur consigne utilisateur : {merged.company!r}"
                )
                for line in part.lines:
                    line.warnings.append(warning)
            if (part.project_client is not None
                    and normalized(part.project_client) != normalized(merged.project_client or "")):
                warning = (
                    f"Client du bloc {pages} : {part.project_client!r} ; "
                    f"premier nom conservé sur consigne utilisateur : {merged.project_client!r}"
                )
                for line in part.lines:
                    line.warnings.append(warning)
            if merged.main_trade != part.main_trade:
                merged.main_trade = None
            merged.lines.extend(part.lines)
    if merged is None:
        raise ValueError("Normalisation par blocs sans résultat")
    return merged
