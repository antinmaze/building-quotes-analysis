from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

SCHEMA_VERSION = "1"
PROMPT_VERSION = "1"
PRICE_PATTERN = re.compile(r"^-?\d+(?:\.\d{1,6})?$")
HEADERS = [
    "Corps d'état", "Désignation ouvrage", "Unité", "Prix unitaire €HT",
    "Nom de l'entreprise", "Nom du client", "Année du devis",
]
TRADES = [
    "Démolition", "Terrassement", "Maçonnerie", "Charpente", "Couverture",
    "Menuiseries extérieures", "Plomberie", "Électricité", "Isolation",
    "Plâtrerie / cloisons", "Menuiseries intérieures", "Carrelage", "Peinture",
]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Line(ClosedModel):
    trade: str | None
    designation: str
    raw_designation: str
    raw_unit: str | None
    unit_price_ht: str | None
    quantity: str | None
    line_total_ht: str | None
    source_pages: list[int]
    source_excerpt: str | None
    option_variant: str | None
    review_reasons: list[str]

    @field_validator("unit_price_ht", "quantity", "line_total_ht")
    @classmethod
    def decimal_string(cls, value: str | None) -> str | None:
        if value is not None:
            if len(value) > 18 or not PRICE_PATTERN.fullmatch(value):
                raise ValueError("Expected a finite decimal string, e.g. 1234.56")
            try:
                if not Decimal(value).is_finite():
                    raise ValueError("Non-finite decimal")
            except InvalidOperation as exc:
                raise ValueError("Invalid decimal") from exc
        return value

    @field_validator("source_pages")
    @classmethod
    def positive_pages(cls, pages: list[int]) -> list[int]:
        if any(p < 1 for p in pages):
            raise ValueError("Page numbers start at one")
        return pages


class Extraction(ClosedModel):
    classification: Literal["devis", "non_devis", "incertain"]
    classification_reason: str
    company: str | None
    client: str | None
    year: int | None
    metadata_excerpt: str | None
    filename_conflict: bool
    review_reasons: list[str]
    lines: list[Line]

    @field_validator("year")
    @classmethod
    def valid_year(cls, year: int | None) -> int | None:
        if year is not None and not 1900 <= year <= 2200:
            raise ValueError("Year outside supported range")
        return year


class OCR(ClosedModel):
    content: str
    page_texts: dict[str, str]
    page_count: int
    raw: dict


class Document(ClosedModel):
    source: str
    sha256: str | None
    pages: int | None
    status: Literal["extracted", "error", "unsupported"]
    error: str | None
    ocr: OCR | None
    extraction: Extraction | None


class Batch(ClosedModel):
    schema_version: Literal["1"]
    documents: list[Document]


def normalized(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def unit(raw: str | None) -> str | None:
    key = normalized(raw).rstrip(".")
    return {
        "m²": "m²", "m2": "m²", "m^2": "m²",
        "m³": "m³", "m3": "m³", "m^3": "m³",
        "ml": "ml", "m.l": "ml", "m": "ml",
        "u": "U", "unité": "U", "unite": "U", "un": "U",
        "forfait": "Forfait", "ft": "Forfait",
    }.get(key)


def reasons(document: Document, line: Line) -> list[str]:
    extraction, ocr = document.extraction, document.ocr
    if extraction is None or ocr is None:
        return ["Extraction ou OCR absent"]
    flags = list(extraction.review_reasons) + list(line.review_reasons)
    if extraction.classification != "devis":
        flags.append("Nature du document à confirmer : " + extraction.classification)
    if extraction.filename_conflict:
        flags.append("Conflit entre nom du fichier et contenu")
    if line.trade not in TRADES:
        flags.append("Corps d'état absent ou ambigu")
    if not line.designation.strip() or not line.raw_designation.strip():
        flags.append("Désignation manquante")
    if unit(line.raw_unit) is None:
        flags.append("Unité absente ou non reconnue")
    if line.unit_price_ht is None:
        flags.append("Prix unitaire HT absent (aucune déduction depuis TTC/total)")
    elif Decimal(line.unit_price_ht) <= 0:
        flags.append("Prix nul ou négatif à confirmer")
    elif Decimal(line.unit_price_ht) > Decimal("999999999999.99"):
        flags.append("Prix hors précision monétaire sûre pour Excel")
    elif Decimal(line.unit_price_ht) != Decimal(line.unit_price_ht).quantize(Decimal(".01")):
        flags.append("Prix comportant plus de deux décimales à confirmer")
    excerpt = normalized(line.source_excerpt)
    if not excerpt or excerpt not in normalized(ocr.content):
        flags.append("Extrait justificatif absent du texte OCR")
    if not line.source_pages or any(str(p) not in ocr.page_texts for p in line.source_pages):
        flags.append("Pages justificatives absentes ou invalides")
    elif excerpt and excerpt not in normalized(
        " ".join(ocr.page_texts[str(p)] for p in sorted(set(line.source_pages)))
    ):
        flags.append("Extrait absent des pages indiquées")
    # A verbatim excerpt alone does not prove that an emitted price occurs in it.
    if line.unit_price_ht is not None and not price_in_excerpt(line.unit_price_ht, line.source_excerpt):
        flags.append("Prix unitaire absent de l'extrait justificatif")
    if "ttc" in normalized(line.source_excerpt) and "ht" not in normalized(line.source_excerpt):
        flags.append("Extrait TTC sans confirmation HT")
    evidence_pages = " ".join(ocr.page_texts.get(str(p), "") for p in line.source_pages)
    if not re.search(r"\bh\s*\.?\s*t\b|hors\s+taxes?", normalized(evidence_pages)):
        flags.append("Mention HT absente des pages justificatives")
    if normalized(line.raw_designation) not in normalized(line.source_excerpt):
        flags.append("Désignation brute absente de l'extrait justificatif")
    metadata = normalized(extraction.metadata_excerpt)
    if not metadata or metadata not in normalized(ocr.content):
        flags.append("Justificatif des métadonnées absent du texte OCR")
    for label, value in [
        ("Entreprise", extraction.company), ("Client", extraction.client),
        ("Année", str(extraction.year) if extraction.year is not None else None),
    ]:
        if value is None or not normalized(value):
            flags.append(label + " inconnu")
        elif normalized(value) not in metadata:
            flags.append(label + " non corroboré par le justificatif")
    if line.quantity is not None and line.line_total_ht is not None and line.unit_price_ht is not None:
        with localcontext() as context:
            context.prec = 50
            expected = Decimal(line.quantity) * Decimal(line.unit_price_ht)
            if abs(expected - Decimal(line.line_total_ht)) > Decimal(".02"):
                flags.append("Quantité × PU différent du total HT (tolérance 0,02 €)")
    if line.quantity is not None and Decimal(line.quantity) <= 0:
        flags.append("Quantité nulle ou négative à confirmer")
    return list(dict.fromkeys(flags))


def price_in_excerpt(price: str, excerpt: str | None) -> bool:
    # Accept French grouped numbers without matching 25 inside 125.
    text = (excerpt or "").replace("\u202f", " ").replace("\xa0", " ")
    tokens = re.findall(r"(?<![\d.,])-?\d+(?: \d{3})*(?:[,.]\d+)?(?![\d.,])", text)
    return any(Decimal(t.replace(" ", "").replace(",", ".")) == Decimal(price) for t in tokens)
