from __future__ import annotations

import os
import re
import uuid
from copy import copy
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from .models import Batch, HEADERS, TRADE_ORDER, normalized, line_unit, technical_features
from .reading import coverage, priced_rows
from .quality import assess, assessment_context, ACCEPTANCE_THRESHOLD
from .business_rules import unit_price_values

BASE_HEADERS = HEADERS + ["Score fiabilité", "Score prix", "Seuil d'acceptation"]

TRACE_HEADERS = HEADERS + [
    "Fichier source", "SHA256", "Pages", "Désignation brute", "Unité brute",
    "Extrait source", "Quantité", "Total ligne HT", "Option / variante",
    "Classification", "Motif classification", "Justificatif métadonnées",
    "Statut", "Motifs de vérification", "Ligne Base de prix",
    "Avertissements", "Origine unité", "Entreprise brute", "Client projet brut",
    "Client facturation", "Cellules OCR", "Signature technique",
    "Rapprochement projet",
    "Lot principal du devis",
    "Nature technique",
    "Score fiabilité", "Score prix", "Seuil d'acceptation", "Destination",
    "Justification score unité",
    "Libellé avant règles métier",
    "Contexte du libellé", "Preuve du contexte du libellé",
    "PU HT source", "Unité source normalisée", "Règle de présentation du prix",
]
REVIEW_HEADERS = TRACE_HEADERS
IGNORED_HEADERS = ["Fichier source", "SHA256", "Statut", "Motif"]
TRACE_NUMERIC = {TRACE_HEADERS.index(header) + 1 for header in (
    "Prix unitaire €HT", "Année du devis", "Quantité", "Total ligne HT",
    "Ligne Base de prix",
    "Score fiabilité", "Score prix", "Seuil d'acceptation",
    "PU HT source",
)}


def text_cell(cell, value) -> None:
    # openpyxl infers "=" as a formula unless data_type is forced AFTER assignment.
    value = "" if value is None else str(value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "\ufffd", value)
    if len(value) > 32767:
        value = value[:32680] + " […] Extrait tronqué pour Excel; texte intégral dans le cache local."
    cell.value = value
    cell.data_type = "s"


def append(sheet, values: list, numeric: set[int] | None = None) -> int:
    if not hasattr(sheet, "_export_append_row"):
        sheet._export_append_row = sheet.max_row
    row = sheet._export_append_row + 1
    sheet._export_append_row = row
    for index, value in enumerate(values, 1):
        cell = sheet.cell(row, index)
        if numeric and index in numeric and value is not None:
            cell.value = value
        else:
            text_cell(cell, value)
    return row


def sheet_with_headers(workbook, name: str, headers: list[str]):
    sheet = workbook.create_sheet(name)
    for col, header in enumerate(headers, 1):
        text_cell(sheet.cell(1, col), header)
        sheet.cell(1, col).font = Font(bold=True, color="FFFFFF")
        sheet.cell(1, col).fill = PatternFill("solid", fgColor="24476A")
    return sheet


def base_values(extraction, line) -> list:
    designation = line.designation
    price = Decimal(line.unit_price_ht) if line.unit_price_ht is not None else None
    display_unit = line_unit(line)
    if line.export_forfait:
        from .business_rules import forfait_values
        designation, price, _ = forfait_values(line)
        display_unit = "Forfait"
    if line.export_unit_prices:
        designation, price, display_unit, _ = unit_price_values(line)
    if line.option_variant and normalized(line.option_variant) not in normalized(designation):
        designation += " — " + line.option_variant
    return [line.trade, designation, display_unit,
            float(price) if price is not None else None,
            extraction.company, extraction.client, extraction.year]


def identity(extraction, line) -> tuple:
    # Never round prices or discard attribution/option semantics when deduplicating.
    values = unit_price_values(line) if line.export_unit_prices else None
    return (line.trade, normalized(line.technical_signature or line.designation), normalized(line.option_variant),
            values[2] if values else line_unit(line), values[1] if values else Decimal(line.unit_price_ht),
            normalized(extraction.company), normalized(extraction.client), extraction.year,
            tuple(sorted(technical_features(line.raw_designation))) if line.technical_signature else (),
            normalized(line.designation_context),
            (line.quantity, line.line_total_ht, normalized(base_values(extraction, line)[1]))
            if line.export_forfait else ())


def group_keys(entries) -> dict:
    parent = {}
    raw_aliases = {}
    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key
    for extraction, line in entries:
        key = identity(extraction, line)
        parent.setdefault(key, key)
        raw_key = (key[0], normalized(line.raw_designation), *key[2:])
        if raw_key in raw_aliases:
            parent[root(key)] = root(raw_aliases[raw_key])
        raw_aliases[raw_key] = key
    return {key: root(key) for key in parent}


def validate_standalone_output(output: Path) -> None:
    for path in {output, output.resolve()}:
        if (path.with_suffix(".campaign.json").exists()
                or path.with_suffix(".xlsx.lock").exists()):
            raise ValueError(
                "Classeur de campagne protégé : utiliser la publication de campagne "
                "pour le cumulatif, ou une autre sortie pour un export indépendant"
            )


def validate_destination(output: Path, template: Path | None = None) -> None:
    if output.suffix.lower() != ".xlsx":
        raise ValueError("La sortie doit avoir l'extension .xlsx")
    if template:
        if output.resolve() == template.resolve():
            raise ValueError("La sortie ne peut pas remplacer le modèle")
        workbook = load_workbook(template)
        try:
            if not any([s.cell(1, i).value for i in range(1, 8)] == HEADERS for s in workbook.worksheets):
                raise ValueError("Le modèle doit contenir les sept en-têtes exacts en A1:G1")
        finally:
            workbook.close()


def export(batch: Batch, output: Path, template: Path | None = None) -> dict:
    validate_standalone_output(output)
    validate_destination(output, template)
    protected = [Path(d.source).resolve() for d in batch.documents]
    if output.resolve() in protected or (template and output.resolve() == template.resolve()):
        raise ValueError("La sortie ne peut pas remplacer une source ou le modèle")
    if template:
        workbook = load_workbook(template)
        candidates = [s for s in workbook.worksheets if [s.cell(1, i).value for i in range(1, 8)] == HEADERS]
        if not candidates:
            raise ValueError("Le modèle doit contenir les sept en-têtes exacts en A1:G1")
        base = candidates[0]
        # Only the seven-column input sheet is reused; no unrelated template data.
        for sheet in list(workbook.worksheets):
            if sheet != base:
                workbook.remove(sheet)
        base.title = "Base de prix"
        styles = [copy(base.cell(2, c)._style) for c in range(1, 8)]
        if base.max_row > 1:
            base.delete_rows(2, base.max_row - 1)
        if base.max_column > 7:
            base.delete_cols(8, base.max_column - 7)
        for name in list(base.tables):
            del base.tables[name]
    else:
        workbook = Workbook()
        workbook.remove(workbook.active)
        base = sheet_with_headers(workbook, "Base de prix", HEADERS)
        styles = [None] * 7
    for col, header in enumerate(BASE_HEADERS, 1):
        text_cell(base.cell(1, col), header)
    base.sheet_state = "visible"
    base.sheet_view.topLeftCell = "A1"
    for dimension in base.row_dimensions.values():
        dimension.hidden = False
    for dimension in base.column_dimensions.values():
        dimension.hidden = False
    workbook.active = workbook.index(base)
    trace = sheet_with_headers(workbook, "Traçabilité", TRACE_HEADERS)
    raw_sheet = sheet_with_headers(workbook, "Données brutes", [
        "Fichier source", "Pages", "Désignation complète source", "Unité brute",
        "PU HT source", "Quantité source", "Total ligne HT source",
        "Section de contexte", "Cellules OCR", "Extrait source",
    ])
    review = sheet_with_headers(workbook, "À vérifier", REVIEW_HEADERS)
    ignored = sheet_with_headers(workbook, "Documents ignorés", IGNORED_HEADERS)
    gaps = sheet_with_headers(workbook, "Couverture", [
        "Fichier source", "Cellule OCR", "Pages", "PU source", "Ligne OCR non rapprochée", "Motif",
    ])
    documents_sheet = sheet_with_headers(workbook, "Documents", [
        "Fichier source", "SHA256", "Pages", "Statut", "Classification",
        "Lignes extraites", "Occurrences acceptées", "Occurrences à vérifier",
        "Lignes OCR avec PU détectées", "Lignes OCR non rapprochées",
        "Entreprise", "Client projet", "Client facturation", "Avertissements", "Erreur",
    ])
    accepted = {}
    audited = []
    quality_cache = {}
    errors = 0
    for document in batch.documents:
        extraction = document.extraction
        if document.status == "extracted" and extraction:
            evidence_context = assessment_context(document)
        elif document.ocr:
            evidence_context = {"priced_rows": priced_rows(document.ocr)}
        else:
            evidence_context = {}
        document_qualities = {
            id(line): assess(document, line, evidence_context)
            for line in extraction.lines
        } if document.status == "extracted" and extraction else {}
        quality_cache.update(document_qualities)
        source_gaps = (
            coverage(document.ocr, extraction, evidence_context.get("priced_rows"))
            if document.ocr and extraction and extraction.evidence_version == "2" else []
        )
        line_count = len(extraction.lines) if extraction else 0
        accepted_count = sum(quality.accepted for quality in document_qualities.values())
        append(documents_sheet, [
            document.source, document.sha256, document.pages, document.status,
            extraction.classification if extraction else None,
            line_count, accepted_count, line_count - accepted_count,
            len(evidence_context.get("priced_rows", [])) if document.ocr
            else None, len(source_gaps),
            extraction.company if extraction else None, extraction.client if extraction else None,
            extraction.billing_client if extraction else None,
            "; ".join(extraction.warnings) if extraction else None, document.error,
        ], {3, 6, 7, 8, 9, 10})
        if document.status != "extracted" or extraction is None:
            errors += int(document.status == "error")
            append(ignored, [document.source, document.sha256, document.status, document.error])
            audited.append((document, None, [], None))
            continue
        if extraction.classification == "non_devis":
            append(ignored, [document.source, document.sha256, "non_devis", extraction.classification_reason])
        if document.ocr and extraction.evidence_version == "2":
            for missing in source_gaps:
                append(gaps, [document.source, missing["row_id"], str(missing["pages"]),
                              missing["price"], missing["text"],
                              "Ligne avec PU détectée sans correspondance : vérifier titre, omission ou OCR"])
        if not extraction.lines:
            audited.append((document, None, [], None))
        for line in extraction.lines:
            append(raw_sheet, [
                document.source, ", ".join(map(str, line.source_pages)), line.raw_designation,
                line.raw_unit,
                float(Decimal(line.unit_price_ht)) if line.unit_price_ht is not None else None,
                float(Decimal(line.quantity)) if line.quantity is not None else None,
                float(Decimal(line.line_total_ht)) if line.line_total_ht is not None else None,
                line.designation_context, ", ".join(line.table_row_ids), line.source_excerpt,
            ], {5, 6, 7})
            quality = quality_cache[id(line)]
            flags = [] if quality.accepted else quality.issues
            key = identity(extraction, line) if quality.accepted else None
            if key is not None:
                accepted.setdefault(key, (extraction, line))
            audited.append((document, line, flags, key))
    mapping = {}
    canonical = group_keys((document.extraction, line) for document, line, flags, key in audited if key is not None)
    grouped = {}
    for key, value in accepted.items():
        grouped.setdefault(canonical[key], value)
    accepted = grouped
    audited = [(document, line, flags, canonical[key] if key is not None else None)
               for document, line, flags, key in audited]
    group_quality = {}
    for document, line, _, key in audited:
        if key is None:
            continue
        quality = quality_cache[id(line)]
        score, price_score = group_quality.get(key, (1.0, 1.0))
        group_quality[key] = (min(score, quality.score), min(price_score, quality.price_score))
    sorted_items = sorted(accepted.items(), key=lambda item: (
        TRADE_ORDER.index(item[0][0]), item[0][1], item[0][2], item[0][3],
        item[0][4], item[0][5], item[0][6], item[0][7],
    ))
    for key, (extraction, line) in sorted_items:
        score, price_score = group_quality[key]
        row = append(base, base_values(extraction, line) + [score, price_score, ACCEPTANCE_THRESHOLD],
                     {4, 7, 8, 9, 10})
        mapping[key] = row
        for col, style in enumerate(styles, 1):
            if style is not None:
                base.cell(row, col)._style = copy(style)
        base.cell(row, 4).number_format = '#,##0.00" €"'
        base.cell(row, 7).number_format = "0"
    for document, line, flags, key in audited:
        extraction = document.extraction
        if line is None:
            values = {
                "Fichier source": document.source,
                "SHA256": document.sha256,
                "Statut": document.status if extraction is None else "sans ligne",
                "Motifs de vérification": document.error if extraction is None else (
                    extraction.classification_reason or "Aucune ligne extraite"
                ),
            }
            append(review, [values.get(header) for header in REVIEW_HEADERS])
            continue
        quality = quality_cache[id(line)]
        values = base_values(extraction, line) + [
            document.source, document.sha256, ", ".join(map(str, line.source_pages)),
            line.raw_designation, line.raw_unit, line.source_excerpt,
            float(Decimal(line.quantity)) if line.quantity is not None else None,
            float(Decimal(line.line_total_ht)) if line.line_total_ht is not None else None,
            line.option_variant, extraction.classification, extraction.classification_reason,
            extraction.metadata_excerpt, "À vérifier" if flags else "Accepté",
            "; ".join(flags), mapping.get(key),
            "; ".join(extraction.warnings + line.warnings), line.unit_basis,
            extraction.company_raw, extraction.client_raw, extraction.billing_client,
            ", ".join(line.table_row_ids), line.technical_signature,
            str(extraction.project_link) if extraction.project_link else None,
            extraction.document_trade,
            line.technical_trade,
            quality.score, quality.price_score, ACCEPTANCE_THRESHOLD,
            "Base de prix" if quality.accepted else "À vérifier",
            quality.unit_evidence,
            line.designation_before_rules,
            line.designation_context, "\n".join(line.designation_context_evidence),
            float(Decimal(line.unit_price_ht)) if line.unit_price_ht is not None else None,
            line_unit(line),
            (unit_price_values(line)[3] if line.export_unit_prices else (
                "Forfait = total HT de ligne ; à défaut quantité × PU HT ; "
                "à défaut PU déjà forfaitaire" if line.export_forfait else "PU source")),
        ]
        append(trace, values, TRACE_NUMERIC)
        if flags:
            append(review, values, TRACE_NUMERIC)
    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for index in range(1, sheet.max_column + 1):
            letter = sheet.cell(1, index).column_letter
            if not template or sheet != base or sheet.column_dimensions[letter].width == 13:
                sheet.column_dimensions[letter].width = 55 if index in {2, 8, 13, 18, 20} else 24
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                alignment = copy(cell.alignment)
                alignment.wrap_text = True
                cell.alignment = alignment
        if sheet in (trace, review):
            for row in range(2, sheet.max_row + 1):
                for col in (4, 15):
                    sheet.cell(row, col).number_format = '#,##0.00" €"'
                sheet.cell(row, 7).number_format = "0"
        for col in range(1, sheet.max_column + 1):
            if sheet.cell(1, col).value in {"Score fiabilité", "Score prix", "Seuil d'acceptation"}:
                for row in range(2, sheet.max_row + 1):
                    sheet.cell(row, col).number_format = "0.00"
    workbook.calculation.fullCalcOnLoad = True
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.xlsx")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        workbook.save(temporary)
        os.replace(temporary, output)
    finally:
        workbook.close()
        temporary.unlink(missing_ok=True)
    return {"accepted_rows": len(accepted), "extracted_lines": sum(line is not None for _, line, _, _ in audited),
            "review_rows": review.max_row - 1, "errors": errors,
            "coverage_gaps": gaps.max_row - 1,
            "status": "ok" if accepted else "aucune donnée acceptée; classeur d'audit uniquement"}
