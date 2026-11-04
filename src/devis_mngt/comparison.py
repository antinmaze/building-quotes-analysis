from __future__ import annotations

import csv
import os
import uuid
from collections import Counter
from difflib import SequenceMatcher
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from .excel import base_values, group_keys
from .models import Batch, HEADERS, evidence_key, normalized
from .quality import assess


def source_key(source: str) -> str:
    return normalized(Path(source).stem)


def compare(batch: Batch, reference: Path, output: Path) -> dict:
    """Source/price/description candidates are not a claim of semantic equivalence."""
    if output.suffix.lower() != ".csv":
        raise ValueError("Le rapport de comparaison doit être un CSV")
    protected = {reference.resolve(), *(Path(d.source).resolve() for d in batch.documents)}
    if output.resolve() in protected:
        raise ValueError("La comparaison ne peut remplacer une source")
    book = load_workbook(reference, data_only=True, read_only=True)
    try:
        base = list(book["Base de prix"].values)
        if list(base[0][:7]) != HEADERS:
            raise ValueError("En-têtes de la référence incompatibles")
        trace = list(book["Traçabilité"].values)
        indexes = {header: i for i, header in enumerate(trace[0])}
        expected = []
        for row in trace[1:]:
            if not row[indexes["Désignation ouvrage"]]:
                continue
            expected.append((row[indexes["Source"]], [row[indexes[h]] for h in HEADERS]))
        if Counter(tuple(values) for _, values in expected) != Counter(
            tuple(row[:7]) for row in base[1:] if row[1]
        ):
            raise ValueError("La traçabilité de référence ne correspond pas à sa Base de prix")
    finally:
        book.close()
    documents = {source_key(d.source): d for d in batch.documents}
    reference_sources = {source_key(source) for source, _ in expected}
    rows = []
    counts = Counter()
    destinations = Counter()
    for reference_index, (source, values) in enumerate(expected, 2):
        document = documents.get(source_key(source))
        candidates = []
        if document and document.extraction:
            for index, line in enumerate(document.extraction.lines, 1):
                if line.unit_price_ht is not None and Decimal(str(values[3])) == Decimal(line.unit_price_ht):
                    score = max(
                        SequenceMatcher(None, evidence_key(values[1]), evidence_key(line.raw_designation)).ratio(),
                        SequenceMatcher(None, evidence_key(values[1]), evidence_key(line.designation)).ratio(),
                    )
                    candidates.append((score, index, line))
        if candidates:
            score, index, line = max(candidates, key=lambda candidate: candidate[0])
            quality = assess(document, line)
            flags = [] if quality.accepted else quality.issues
            status = ("libellé et prix identiques" if normalized(values[1]) in {
                normalized(line.designation), normalized(line.raw_designation)
            } else "candidat à confirmer")
            actual = base_values(document.extraction, line)
            if line.export_forfait:
                status = "candidat source au même PU ; sortie convertie en forfait"
            if line.export_unit_prices and actual[3] != float(values[3]):
                status = "candidat source au même PU ; forfait converti en prix unitaire"
            detail = [
                index, round(score, 3), "À vérifier" if flags else "Base de prix",
                "; ".join(flags), "; ".join(document.extraction.warnings + line.warnings),
                " | ".join(str(p) for p in line.source_pages), line.raw_designation,
            ]
            destinations[detail[2]] += 1
        else:
            status = "source absente" if document is None else "aucun PU identique"
            actual, detail = [None] * 7, [None] * 7
        counts[status] += 1
        rows.append([reference_index, source, status, *values, *actual, *detail])
    headers = ["Ligne référence", "Source référence", "Rapprochement"] + [
        "Attendu — " + header for header in HEADERS
    ] + ["Produit — " + header for header in HEADERS] + [
        "Index extraction", "Similarité indicative", "Destination produite",
        "Blocages", "Avertissements", "Pages", "Désignation source",
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.part")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(headers)
            for row in rows:
                escaped = []
                for value in row:
                    text = "" if value is None else str(value).replace("\r", "\\r").replace("\n", "\\n")
                    escaped.append("'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text)
                writer.writerow(escaped)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    entries = [(d.extraction, line)
               for d in batch.documents if source_key(d.source) in reference_sources and d.extraction
               for line in d.extraction.lines if assess(d, line).accepted]
    accepted = set(group_keys(entries).values())
    return {
        "reference_rows": len(expected), "reference_sources": len(reference_sources),
        "produced_base_rows_on_reference_sources": len(accepted),
        "matches": dict(counts),
        "candidate_destinations": dict(destinations),
        "other_sources": [d.source for d in batch.documents if source_key(d.source) not in reference_sources],
        "note": "Rapprochement sur le PU SOURCE : candidat seulement, pas preuve d'équivalence technique. "
                "Le prix produit peut différer après conversion explicite "
                "(forfait / métré ou quantité × PU selon la règle de présentation). "
                "Un candidat peut correspondre à plusieurs lignes de référence. Aucun taux de précision déduit.",
    }
