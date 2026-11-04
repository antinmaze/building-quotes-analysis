from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1"
PROMPT_VERSION = "2"
PRICE_PATTERN = re.compile(r"^-?\d+(?:\.\d{1,6})?$")
HEADERS = [
    "Corps d'état", "Désignation ouvrage", "Unité", "Prix unitaire €HT",
    "Nom de l'entreprise", "Nom du client", "Année du devis",
]
TRADES = [
    "Démolition", "Terrassement", "Maçonnerie", "Charpente", "Couverture",
    "Menuiseries extérieures", "Plomberie", "Électricité", "Isolation",
    "Plâtrerie / cloisons", "Menuiseries intérieures", "Carrelage", "Peinture",
    "Maçonnerie - Gros œuvre", "Plâtrerie - Isolation", "Plomberie - Sanitaires",
    "Chauffage", "Ventilation",
]
TRADE_ORDER = [
    "Démolition", "Terrassement", "Maçonnerie", "Maçonnerie - Gros œuvre",
    "Charpente", "Couverture", "Menuiseries extérieures",
    "Plomberie", "Plomberie - Sanitaires", "Chauffage", "Ventilation", "Électricité",
    "Isolation", "Plâtrerie / cloisons", "Plâtrerie - Isolation",
    "Menuiseries intérieures", "Carrelage", "Peinture",
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
    warnings: list[str] = Field(default_factory=list)
    table_row_ids: list[str] = Field(default_factory=list)
    unit_basis: Literal["explicit", "interpreted", "missing"] = "explicit"
    normalized_unit: str | None = None
    technical_signature: str | None = None
    technical_trade: str | None = None
    designation_before_rules: str | None = None
    designation_context: str | None = None
    designation_context_evidence: list[str] = Field(default_factory=list)
    export_forfait: bool = False
    export_unit_prices: bool = False

    @field_validator("quantity", mode="before")
    @classmethod
    def french_quantity(cls, value):
        if isinstance(value, str) and re.fullmatch(r"-?\d+,\d{1,6}", value):
            return value.replace(",", ".")
        return value

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
    evidence_version: str = "legacy"
    company_raw: str | None = None
    client_raw: str | None = None
    billing_client: str | None = None
    metadata_evidence: dict[str, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    project_link: dict[str, str] = Field(default_factory=dict)
    document_trade: str | None = None

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

    @model_validator(mode="after")
    def verify_project_links(self):
        sources = {d.source: d for d in self.documents}
        for document in self.documents:
            extraction = document.extraction
            if not extraction or not extraction.project_link:
                continue
            link = extraction.project_link
            donor = sources.get(link.get("source"))
            if (not donor or donor is document or not donor.ocr or not donor.extraction
                    or donor.extraction.project_link or not document.ocr
                    or not donor.sha256 or donor.sha256 != link.get("sha256")):
                raise ValueError("Source de rapprochement du projet absente ou invalide")
            address = evidence_key(link.get("address"))
            donor_raw = donor.extraction.client_raw
            fragments = donor.extraction.metadata_evidence.get("client", [])
            if (not address or address not in evidence_key(donor.ocr.content)
                    or address not in evidence_key(document.ocr.content)
                    or link.get("client_raw") != donor_raw
                    or not alias_supported(extraction.client, donor_raw)
                    or not alias_supported(donor_raw, " ".join(
                        f for f in fragments if normalized(f) in normalized(donor.ocr.content)
                    ))):
                raise ValueError("Rapprochement du projet non corroboré par les deux documents")
        return self


def normalized(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def unit(raw: str | None) -> str | None:
    key = normalized(raw).rstrip(".")
    return {
        "m²": "m²", "m2": "m²", "m^2": "m²",
        "m³": "m³", "m3": "m³", "m^3": "m³",
        "ml": "ml", "m.l": "ml", "m": "ml",
        "u": "U", "unité": "U", "unite": "U", "un": "U",
        "forfait": "Forfait", "ft": "Forfait", "for": "Forfait",
        "f": "Forfait", "ens": "Ensemble", "ensemble": "Ensemble",
        "h": "h", "heure": "h", "kg": "kg", "t": "t",
    }.get(key)


def line_unit(line: Line) -> str | None:
    return line.normalized_unit if line.unit_basis == "interpreted" else unit(line.raw_unit)


def technical_features(text: str) -> set[str]:
    text = normalized(text).replace(",", ".")
    text = re.sub(r"\bsurface\s*:?\s*\d+(?:\.\d+)?\s*m[²2]\b", "", text)
    patterns = (
        r"\d+(?:\.\d+)?\s*(?:mm|cm|kw|w|db)\b",
        r"\d+(?:\.\d+)?(?:\s*[x×/]\s*\d+(?:\.\d+)?){1,2}",
        r"\br\s*[=:]?\s*\d+(?:\.\d+)?",
        r"\b(?:rei|ei|cf)\s*\d+\b",
    )
    features = set()
    for match in re.finditer(
        r"\b[hlp]\s*(\d+(?:\.\d+)?)\s*(?:mm)?\s*[x×]\s*[hlp]?\s*(\d+(?:\.\d+)?)\s*mm\b", text,
    ):
        features.update(format(Decimal(value).normalize(), "f") + "mm" for value in match.groups())
    for pattern in patterns:
        for match in re.findall(pattern, text):
            compact = re.sub(r"\s+|[=:]", "", match).replace("×", "x")
            compact = re.sub(r"\d+(?:\.\d+)?", lambda m: format(Decimal(m[0]).normalize(), "f"), compact)
            features.add(compact)
    return features


def evidence_key(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", normalized(value))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", "".join(
        c for c in text if not unicodedata.combining(c))).split())


def alias_supported(value: str | None, raw: str | None) -> bool:
    tokens = evidence_key(value).split()
    return bool(tokens) and all(token in evidence_key(raw).split() for token in tokens)


def reasons(document: Document, line: Line, evidence_cache: dict | None = None) -> list[str]:
    extraction, ocr = document.extraction, document.ocr
    if extraction is None or ocr is None:
        return ["Extraction ou OCR absent"]
    evidence_cache = evidence_cache or {}
    normalized_content = evidence_cache.get("normalized_content")
    if normalized_content is None:
        normalized_content = normalized(ocr.content)
    normalized_pages = evidence_cache.get("normalized_pages")
    if normalized_pages is None:
        normalized_pages = {page: normalized(text) for page, text in ocr.page_texts.items()}
    flags = list(extraction.review_reasons) + list(line.review_reasons)
    if extraction.classification != "devis":
        flags.append("Nature du document à confirmer : " + extraction.classification)
    if extraction.filename_conflict:
        flags.append("Conflit entre nom du fichier et contenu")
    if line.trade not in TRADES:
        flags.append("Corps d'état absent ou ambigu")
    if not line.designation.strip() or not line.raw_designation.strip():
        flags.append("Désignation manquante")
    if extraction.evidence_version == "2":
        missing = technical_features(line.raw_designation) - technical_features(
            line.designation + " " + (line.technical_signature or "")
        )
        if missing:
            flags.append("Caractéristiques techniques perdues à la normalisation : " + ", ".join(sorted(missing)))
        if line.unit_basis == "interpreted" and line.normalized_unit not in {
            "m²", "m³", "ml", "U", "Forfait", "Ensemble", "h", "kg", "t",
        }:
            flags.append("Unité interprétée non reconnue")
    if line_unit(line) is None:
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
    if not excerpt or excerpt not in normalized_content:
        flags.append("Extrait justificatif absent du texte OCR")
    if not line.source_pages or any(str(p) not in ocr.page_texts for p in line.source_pages):
        flags.append("Pages justificatives absentes ou invalides")
    elif excerpt and excerpt not in " ".join(
        normalized_pages[str(p)] for p in sorted(set(line.source_pages))
    ):
        flags.append("Extrait absent des pages indiquées")
    # A verbatim excerpt alone does not prove that an emitted price occurs in it.
    if line.unit_price_ht is not None and not price_in_excerpt(line.unit_price_ht, line.source_excerpt):
        flags.append("Prix unitaire absent de l'extrait justificatif")
    if "ttc" in normalized(line.source_excerpt) and "ht" not in normalized(line.source_excerpt):
        flags.append("Extrait TTC sans confirmation HT")
    evidence_pages = " ".join(ocr.page_texts.get(str(p), "") for p in line.source_pages)
    if not re.search(r"\bh\s*\.?\s*t\b|hors\s+taxes?", normalized(evidence_pages)):
        from .reading import table_has_ht
        if extraction.evidence_version != "2" or not table_has_ht(
            ocr, line.table_row_ids, evidence_cache.get("table_rows")
        ):
            flags.append("Mention HT absente des pages justificatives")
    if normalized(line.raw_designation) not in normalized(line.source_excerpt):
        flags.append("Désignation brute absente de l'extrait justificatif")
    if extraction.evidence_version == "2":
        for key, label, value, raw in [
            ("company", "Entreprise", extraction.company, extraction.company_raw),
            ("client", "Client", extraction.client, extraction.client_raw),
            ("year", "Année", str(extraction.year) if extraction.year else None, None),
        ]:
            fragments = extraction.metadata_evidence.get(key, [])
            valid = [f for f in fragments if normalized(f) and normalized(f) in normalized_content]
            if not value:
                flags.append(label + " inconnu")
            elif key == "year":
                if not any(year_in_excerpt(extraction.year, normalized(f)) for f in valid):
                    flags.append("Année non corroborée par les extraits")
            elif key == "client" and extraction.project_link:
                link = extraction.project_link
                if not (alias_supported(value, link.get("client_raw"))
                        and evidence_key(link.get("address")) in evidence_key(ocr.content)):
                    flags.append("Client lié non corroboré par l'adresse du chantier")
            elif not alias_supported(value, " ".join(valid)):
                flags.append(label + " non corroboré par les extraits")
    else:
        metadata = normalized(extraction.metadata_excerpt)
        if not metadata or metadata not in normalized(ocr.content):
            flags.append("Justificatif des métadonnées absent du texte OCR")
        for label, value in [
            ("Entreprise", extraction.company), ("Client", extraction.client),
            ("Année", str(extraction.year) if extraction.year is not None else None),
        ]:
            if value is None or not normalized(value):
                flags.append(label + " inconnu")
            elif not (year_in_excerpt(extraction.year, metadata) if label == "Année"
                      else normalized(value) in metadata):
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


def year_in_excerpt(year: int, excerpt: str) -> bool:
    if re.search(rf"(?<!\d){year}(?!\d)", excerpt):
        return True
    for match in re.finditer(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{2})(?!\d)", excerpt):
        day, month, short_year = map(int, match.groups())
        if year % 100 != short_year:
            continue
        try:
            date(year, month, day)
        except ValueError:
            continue
        return True
    return False


def price_in_excerpt(price: str, excerpt: str | None) -> bool:
    # Accept French grouped numbers without matching 25 inside 125.
    text = (excerpt or "").replace("\u202f", " ").replace("\xa0", " ")
    tokens = re.findall(r"(?<![\d.,])-?\d+(?: \d{3})*(?:[,.]\d+)?(?![\d.,])", text)
    return any(Decimal(t.replace(" ", "").replace(",", ".")) == Decimal(price) for t in tokens)
