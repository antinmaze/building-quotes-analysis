"""Offline OCR evaluation; never imports or calls an Azure backend."""
from __future__ import annotations

import argparse
import csv
import os
import re
import uuid
from decimal import Decimal
from pathlib import Path

import pdfplumber
from openpyxl import Workbook

from devis_mngt.comparison import source_key
from devis_mngt.excel import append, sheet_with_headers
from devis_mngt.models import Batch, evidence_key
from devis_mngt.storage import load


def prices(text: str) -> set[Decimal]:
    return {
        Decimal(re.sub(r"[ \u00a0\u202f]", "", value).replace(",", "."))
        for value in re.findall(
            r"(?<!\d)-?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)[,.]\d{2}(?!\d)", text,
        )
    }


def evidence(label: str, price: str | None, texts: list[str]) -> tuple[bool, float]:
    content = "\n".join(texts)
    found = price is not None and Decimal(price) in prices(content)
    tokens = {token for token in evidence_key(label).split() if len(token) > 2}
    actual = set(evidence_key(content).split())
    return found, len(tokens & actual) / len(tokens) if tokens else 0


def evaluate(diagnostic: Path, results: Path, output: Path, reference_csv: Path | None) -> dict:
    if output.exists() or output.suffix.lower() != ".xlsx":
        raise ValueError("Utiliser un nouveau fichier .xlsx pour le diagnostic")
    report = load(diagnostic)
    batch = Batch.model_validate(load(results))
    book = Workbook()
    book.remove(book.active)
    summary = sheet_with_headers(book, "À lire", ["Indicateur", "Valeur"])
    append(summary, ["Portée", "Prétraitement OCR local seulement, sans Azure ni validation métier"])
    append(summary, ["Limite", "Retrouver un PU dans une page ne prouve pas son association au bon ouvrage."])
    append(summary, ["Libellés", "Présence de mots uniquement : ne valide ni leur ordre ni une désignation complète."])
    pages_sheet = sheet_with_headers(book, "Pages", [
        "Source", "Page", "Méthode locale", "Caractères", "Tableaux acceptables localement",
        "Motif de contrôle complémentaire", "Copie OCR privée",
    ])
    source_sheet = sheet_with_headers(book, "Lignes source", [
        "Source", "Pages", "Désignation source", "PU source HT",
        "PU présent dans le texte des pages", "Part des mots du libellé présents",
    ])
    texts = {}
    total_pages = recognized = safe = 0
    for record in report["records"]:
        if record.get("status") != "ready" or "duplicate_of" in record:
            continue
        source = record["source"]
        details = record.get("diagnostic", {})
        copies = {p.get("searchable_pdf") for p in details.get("pages", []) if p.get("searchable_pdf")}
        path = Path(next(iter(copies))) if copies else Path(source)
        if details.get("error"):
            append(pages_sheet, [source, None, "Échec", None, "Non", details["error"], None])
            continue
        with pdfplumber.open(path) as pdf:
            page_texts = {str(i): p.extract_text() or "" for i, p in enumerate(pdf.pages, 1)}
        if copies:
            with pdfplumber.open(source) as pdf:
                for p in details["pages"]:
                    if p["method"] == "native":
                        page_texts[str(p["number"])] = pdf.pages[p["number"] - 1].extract_text() or ""
        texts[source_key(source)] = page_texts
        for p in details["pages"]:
            text = page_texts[str(p["number"])]
            total_pages += 1
            recognized += len(text.strip()) >= 80
            safe += p["route"] == "local"
            append(pages_sheet, [source, p["number"], p["method"], len(text),
                                "Oui" if p["route"] == "local" else "Non",
                                p["reason"], p.get("searchable_pdf")], {2, 4})
    source_found = source_count = source_priced = 0
    for document in batch.documents:
        page_texts = texts.get(source_key(document.source))
        if page_texts is None or document.extraction is None:
            continue
        for line in document.extraction.lines:
            found, recall = evidence(line.raw_designation, line.unit_price_ht,
                                     [page_texts.get(str(p), "") for p in line.source_pages])
            source_count += 1
            source_priced += line.unit_price_ht is not None
            source_found += found
            append(source_sheet, [document.source, ", ".join(map(str, line.source_pages)),
                                  line.raw_designation, line.unit_price_ht,
                                  ("Sans PU source" if line.unit_price_ht is None else
                                   "Oui" if found else "Non"), recall], {6})
    reference_found = reference_count = 0
    if reference_csv:
        reference_sheet = sheet_with_headers(book, "Témoin", [
            "Ligne témoin", "Source", "Désignation témoin", "PU témoin HT",
            "PU présent dans le document local", "Part des mots du libellé présents",
        ])
        with reference_csv.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream, delimiter=";"):
                page_texts = texts.get(source_key(row["Source référence"]), {})
                found, recall = evidence(row["Attendu — Désignation ouvrage"],
                                         row["Attendu — Prix unitaire €HT"], list(page_texts.values()))
                reference_count += 1
                reference_found += found
                append(reference_sheet, [
                    row["Ligne référence"], row["Source référence"],
                    row["Attendu — Désignation ouvrage"], row["Attendu — Prix unitaire €HT"],
                    "Oui" if found else "Non", recall,
                ], {6})
    stats = {
        "pages": total_pages, "pages_with_readable_text": recognized,
        "pages_with_safe_tables": safe, "source_occurrences": source_count,
        "source_occurrences_with_price": source_priced,
        "source_prices_found_on_pages": source_found,
        "reference_rows": reference_count, "reference_prices_found_in_document": reference_found,
    }
    for key, value in stats.items():
        append(summary, [key, str(value)])
    for sheet in book:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for col in ("A", "B", "C", "F", "G"):
            sheet.column_dimensions[col].width = 45
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.name}.{uuid.uuid4().hex}.part")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            book.save(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
        book.close()
    return stats


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-csv", type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.diagnostic, args.results, args.output, args.reference_csv)))
