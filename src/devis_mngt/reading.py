from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from typing import Literal
from pydantic import field_validator

from .models import ClosedModel, Extraction, Line, OCR, normalized, evidence_key, unit

READING_VERSION = "tables-1"
NORMALIZATION_VERSION = "works-2"
Trade = Literal[
    "Démolition", "Terrassement", "Maçonnerie - Gros œuvre", "Charpente", "Couverture",
    "Menuiseries extérieures", "Plomberie - Sanitaires", "Chauffage", "Ventilation",
    "Électricité", "Plâtrerie - Isolation", "Menuiseries intérieures", "Carrelage", "Peinture",
]


class ReadLine(ClosedModel):
    raw_designation: str
    raw_unit: str | None
    unit_price_ht: str | None
    quantity: str | None
    line_total_ht: str | None
    source_pages: list[int]
    source_excerpt: str | None
    table_row_ids: list[str]
    section: str | None
    option_variant: str | None
    blockers: list[str]
    warnings: list[str]

    @field_validator("unit_price_ht", "quantity", "line_total_ht")
    @classmethod
    def normalize_decimal(cls, value):
        if value is None:
            return None
        text = value.replace("\u202f", " ").replace("\xa0", " ")
        if not re.fullmatch(r"-?(?:\d{1,3}(?: \d{3})+|\d+)(?:[,.]\d{1,6})?", text):
            raise ValueError("Nombre décimal OCR ambigu")
        return text.replace(" ", "").replace(",", ".")


class Reading(ClosedModel):
    classification: Literal["devis", "non_devis", "incertain"]
    classification_reason: str
    company_raw: str | None
    project_client_raw: str | None
    billing_client_raw: str | None
    year: int | None
    company_excerpts: list[str]
    project_excerpts: list[str]
    year_excerpts: list[str]
    blockers: list[str]
    warnings: list[str]
    lines: list[ReadLine]


class NormalizedWork(ClosedModel):
    line_index: int
    designation: str
    technical_signature: str
    trade: Trade | None
    unit: str | None
    unit_basis: Literal["explicit", "interpreted", "missing"]
    blockers: list[str]
    warnings: list[str]


class Normalization(ClosedModel):
    company: str | None
    project_client: str | None
    main_trade: Trade | None
    lines: list[NormalizedWork]


def table_rows(ocr: OCR) -> list[dict]:
    result = []
    previous = None
    for table_index, table in enumerate(ocr.raw.get("tables", []), 1):
        raw_cells = table.get("cells", [])
        headers = [cell for cell in raw_cells if cell.get("kind") == "columnHeader"]
        pages = {r["pageNumber"] for cell in raw_cells for r in cell.get("boundingRegions", [])}
        if not headers and previous and pages and previous["pages"]:
            first_cells = [c for c in raw_cells if c["rowIndex"] == 0]
            def positions(cells):
                return {c["columnIndex"]: c["boundingRegions"][0]["polygon"][0]
                        for c in cells if c.get("boundingRegions")
                        and c["boundingRegions"][0].get("polygon")}
            old, new = positions(previous["headers"]), positions(first_cells)
            same_layout = (len(new) == table.get("columnCount") == len(old)
                           and new.keys() == old.keys()
                           and all(abs(new[c] - old[c]) < .15 for c in new))
            if min(pages) == max(previous["pages"]) + 1 and same_layout:
                headers = previous["headers"]
        if headers:
            previous = {"headers": headers, "pages": pages}
        else:
            previous = None
        rows = defaultdict(list)
        for cell in raw_cells:
            regions = cell.get("boundingRegions", [])
            rows[cell["rowIndex"]].append({
                "column": cell["columnIndex"], "text": cell.get("content", ""),
                "header": cell.get("kind") == "columnHeader",
                "pages": sorted({r["pageNumber"] for r in regions}),
                "regions": regions, "spans": cell.get("spans", []),
            })
        for row_index, cells in sorted(rows.items()):
            result.append({"id": f"T{table_index}R{row_index}",
                           "cells": sorted(cells, key=lambda c: c["column"]),
                           "headers": [{"column": c["columnIndex"], "text": c.get("content", "")}
                                       for c in headers]})
    return result


def row_excerpt(ocr: OCR, rows: list[dict]) -> str | None:
    spans = [s for row in rows for cell in row["cells"] for s in cell["spans"]]
    if not spans:
        return None
    start = min(s["offset"] for s in spans)
    end = max(s["offset"] + s["length"] for s in spans)
    return ocr.content[start:end]


def priced_rows(ocr: OCR, rows: list[dict] | None = None) -> list[dict]:
    """Detect explicit PU columns only; total-only tables are not silently interpreted."""
    columns = {}
    result = []
    for row in (rows if rows is not None else table_rows(ocr)):
        table = row["id"].split("R")[0]
        for cell in row["headers"]:
            header = normalized(cell["text"])
            if re.search(r"p\s*\.?\s*u\s*\.?|prix\s+unit|pv\s*h", header):
                columns[table] = cell["column"]
        if table not in columns or any(c["header"] for c in row["cells"]):
            continue
        if any(re.match(r"^(?:sous[- ]?total|total|net [àa] payer)\b", normalized(c["text"]))
               for c in row["cells"]):
            continue
        price_cell = next((c for c in row["cells"] if c["column"] == columns[table]), None)
        if not price_cell:
            continue
        text = price_cell["text"].replace("\u202f", "").replace("\xa0", "").replace(" ", "")
        if not re.fullmatch(r"-?\d+(?:[,.]\d+)?", text):
            continue
        result.append({**row, "price": str(Decimal(text.replace(",", ".")))})
    return result


def coverage(ocr: OCR, extraction: Extraction, rows: list[dict] | None = None) -> list[dict]:
    accounted = {key for line in extraction.lines for key in line.table_row_ids}
    return [{"row_id": row["id"], "price": row["price"],
             "pages": sorted({p for c in row["cells"] for p in c["pages"]}),
             "text": " | ".join(c["text"] for c in row["cells"])}
            for row in (rows if rows is not None else priced_rows(ocr))
            if row["id"] not in accounted]


def table_has_ht(ocr: OCR, row_ids: list[str], rows: list[dict] | None = None) -> bool:
    tables = {key.split("R")[0] for key in row_ids}
    return any(
        row["id"].split("R")[0] in tables and any(
            re.search(r"h\s*\.?\s*t\b|hors\s+taxes?", normalized(cell["text"]))
            for cell in row["headers"]
        )
        for row in (rows if rows is not None else table_rows(ocr))
    )


def complete_reading(reading: Reading, ocr: OCR) -> tuple[Reading, list[str]]:
    """Recover missed rows only where explicit column headers define every raw value."""
    reading = reading.model_copy(deep=True)
    accounted = {key for line in reading.lines for key in line.table_row_ids}
    columns = defaultdict(dict)
    for row in table_rows(ocr):
        table = row["id"].split("R")[0]
        for cell in row["headers"]:
            key = evidence_key(cell["text"])
            if key in {"designation", "description", "libelle"}:
                columns[table]["designation"] = cell["column"]
            elif key in {"qte", "quantite", "qt"}:
                columns[table]["quantity"] = cell["column"]
            elif key in {"unite", "un", "u"}:
                columns[table]["unit"] = cell["column"]
            elif re.match(r"^(?:total|montant)", key) and not re.search(r"ttc|tva", key):
                columns[table]["total"] = cell["column"]
    recovered = []
    for row in priced_rows(ocr):
        if row["id"] in accounted:
            continue
        mapping = columns[row["id"].split("R")[0]]
        if "designation" not in mapping:
            continue
        cells = {cell["column"]: cell["text"] for cell in row["cells"]}
        designation = cells.get(mapping["designation"], "").strip()
        if not designation:
            continue
        def value(key):
            return cells.get(mapping.get(key), "").strip() or None
        excerpt = row_excerpt(ocr, [row])
        if not excerpt or normalized(designation) not in normalized(excerpt):
            continue
        try:
            line = ReadLine(
                raw_designation=designation, raw_unit=value("unit"), unit_price_ht=row["price"],
                quantity=value("quantity"), line_total_ht=value("total"),
                source_pages=sorted({p for c in row["cells"] for p in c["pages"]}),
                source_excerpt=excerpt, table_row_ids=[row["id"]], section=None,
                option_variant=None, blockers=[],
                warnings=["Ligne omise par l'IA, récupérée depuis les colonnes explicites du tableau OCR"],
            )
        except ValueError:
            # Remains visible in coverage; do not guess ambiguous numeric cells.
            continue
        reading.lines.append(line)
        recovered.append(row["id"])
    return reading, recovered


def qualify_labour(lines: list[Line], ocr: OCR) -> int:
    """Use numbered parent headings, with a local closing subtotal as fallback."""
    rows = table_rows(ocr)
    priced = {row["id"] for row in priced_rows(ocr)}
    changed = 0
    for line in lines:
        if not re.fullmatch(r"main d['’ʼ ](?:oeuvre|œuvre)(?: plomberie sanitaire| chauffage clim| électricité)?",
                            normalized(line.raw_designation)):
            continue
        anchors = [i for i, row in enumerate(rows) if row["id"] in line.table_row_ids]
        unresolved = "Main d’œuvre : section source non déterminée, qualification à vérifier"
        if len(anchors) != 1:
            if unresolved not in line.review_reasons:
                line.review_reasons.append(unresolved)
            continue
        index = anchors[0]
        cells = rows[index]["cells"]
        if len(cells) < 2:
            if unresolved not in line.review_reasons:
                line.review_reasons.append(unresolved)
            continue
        number = cells[0]["text"].strip()
        if not re.fullmatch(r"\d+(?:\.\d+)+", number):
            if unresolved not in line.review_reasons:
                line.review_reasons.append(unresolved)
            continue
        parent = number.rsplit(".", 1)[0]
        heading = None
        for row in reversed(rows[:index]):
            previous = row["cells"]
            if len(previous) < 2 or any(c["header"] for c in previous):
                continue
            code = previous[0]["text"].strip()
            if re.fullmatch(r"\d+(?:\.\d+)*", code) and not code.startswith(parent + "."):
                if code == parent and row["id"] not in priced:
                    heading = row
                break
        closing = None
        for row in rows[index + 1:]:
            if any(c["header"] for c in row["cells"]):
                break
            descriptions = [c["text"].strip() for c in row["cells"] if c["column"] == cells[1]["column"]]
            text = descriptions[0] if descriptions else ""
            if re.match(r"^(?:sous[- ]?total|total)\s+", normalized(text)):
                closing = row
                break
            code = row["cells"][0]["text"].strip()
            if re.fullmatch(r"\d+(?:\.\d+)*", code) and not code.startswith(parent + "."):
                break
        chosen = heading or closing
        if chosen is None:
            if unresolved not in line.review_reasons:
                line.review_reasons.append(unresolved)
            continue
        text = next(c["text"] for c in chosen["cells"] if c["column"] == cells[1]["column"])
        context = re.sub(r"^(?:sous[- ]?total|total)\s+", "", text, flags=re.IGNORECASE)
        context = context.split(":", 1)[0]
        context = " ".join(context.split()).strip(" :;.")
        if not context:
            continue
        designation = line.raw_designation + " — " + context
        if line.designation != designation:
            if line.designation_before_rules is None:
                line.designation_before_rules = line.designation
            line.designation = designation
            changed += 1
        line.designation_context = context
        line.designation_context_evidence = [
            row["id"] + " : " + " | ".join(c["text"] for c in row["cells"])
            for row in (heading, closing) if row is not None
        ]
        # Context is a grouping boundary, even for otherwise identical labour.
        if not line.technical_signature:
            line.technical_signature = line.raw_designation
        suffix = " — " + context
        if not line.technical_signature.endswith(suffix):
            line.technical_signature += suffix
    return changed


def assemble(reading: Reading, normalization: Normalization, ocr: OCR) -> Extraction:
    from .business_rules import apply_surface_rule, apply_label_rule
    expected = set(range(1, len(reading.lines) + 1))
    indices = [line.line_index for line in normalization.lines]
    if set(indices) != expected or len(indices) != len(expected):
        raise ValueError("Normalisation incomplète ou indices dupliqués : aucune ligne ne peut disparaître")
    works = {line.line_index: line for line in normalization.lines}
    rows = {row["id"]: row for row in table_rows(ocr)}
    prices = {row["id"]: row for row in priced_rows(ocr)}
    result = []
    for index, raw in enumerate(reading.lines, 1):
        work = works[index]
        blockers = list(raw.blockers) + list(work.blockers)
        warnings = list(raw.warnings) + list(work.warnings)
        anchored = [rows[key] for key in raw.table_row_ids if key in rows]
        if len(anchored) != len(raw.table_row_ids):
            blockers.append("Référence de cellule OCR inconnue")
        excerpt = row_excerpt(ocr, anchored) or raw.source_excerpt
        if raw.unit_price_ht is not None and anchored:
            matching = [prices[key] for key in raw.table_row_ids if key in prices]
            if matching and not any(Decimal(r["price"]) == Decimal(raw.unit_price_ht) for r in matching):
                blockers.append("PU différent de la cellule de prix unitaire OCR")
            if len(matching) > 1:
                blockers.append("Plusieurs lignes de PU réunies dans une occurrence : vérifier la lecture")
        if raw.raw_unit and anchored:
            in_cell = any(normalized(re.sub(r":(?:unselected|selected):", "", cell["text"]))
                          == normalized(raw.raw_unit)
                          for row in anchored for cell in row["cells"])
            in_label = bool(re.search(
                r"\b(?:par|le|au)\s+" + re.escape(normalized(raw.raw_unit)) + r"(?!\w)",
                normalized(raw.raw_designation),
            ))
            if not in_cell and not in_label:
                blockers.append("Unité déclarée explicite introuvable dans les cellules source")
        canonical_unit = unit(raw.raw_unit)
        basis = work.unit_basis
        if basis == "explicit" and raw.raw_unit is None and work.unit is not None:
            basis = "interpreted"
            warnings.append("Unité proposée par la normalisation, absente des données brutes")
        if basis == "explicit" and work.unit != canonical_unit:
            blockers.append("Unité normalisée différente de l'unité source")
        if basis == "interpreted":
            warnings.append("Unité interprétée depuis la nature de l'ouvrage, non explicite")
            if raw.raw_unit is not None:
                blockers.append("Interprétation d'unité malgré une unité source : à confirmer")
        trade = work.trade
        if normalization.main_trade in {"Plâtrerie - Isolation", "Plomberie - Sanitaires", "Maçonnerie - Gros œuvre"}:
            trade = normalization.main_trade if work.trade else None
        result.append(Line(
            trade=trade, technical_trade=work.trade, designation=work.designation, raw_designation=raw.raw_designation,
            raw_unit=raw.raw_unit, unit_price_ht=raw.unit_price_ht,
            quantity=raw.quantity, line_total_ht=raw.line_total_ht,
            source_pages=raw.source_pages, source_excerpt=excerpt,
            option_variant=raw.option_variant, review_reasons=list(dict.fromkeys(blockers)),
            warnings=list(dict.fromkeys(warnings)), table_row_ids=raw.table_row_ids,
            unit_basis=basis, normalized_unit=work.unit,
            technical_signature=work.technical_signature,
        ))
        apply_label_rule(result[-1])
        apply_surface_rule(result[-1])
    qualify_labour(result, ocr)
    return Extraction(
        classification=reading.classification, classification_reason=reading.classification_reason,
        company=normalization.company, client=normalization.project_client, year=reading.year,
        company_raw=reading.company_raw, client_raw=reading.project_client_raw,
        billing_client=reading.billing_client_raw, evidence_version="2",
        document_trade=normalization.main_trade,
        metadata_evidence={"company": reading.company_excerpts, "client": reading.project_excerpts,
                           "year": reading.year_excerpts},
        metadata_excerpt="\n---\n".join(reading.company_excerpts + reading.project_excerpts + reading.year_excerpts),
        filename_conflict=False, review_reasons=reading.blockers, warnings=reading.warnings, lines=result,
    )
