from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re

from .models import Document, Line, normalized, reasons

ACCEPTANCE_THRESHOLD = 0.90
SCORE_VERSION = "rules-7"


@dataclass(frozen=True)
class Quality:
    score: float
    price_score: float
    accepted: bool
    issues: list[str]
    unit_evidence: str | None = None


def corroborated_unit(document: Document, line: Line,
                      evidence_cache: dict | None = None) -> str | None:
    if not document.ocr or not line.table_row_ids:
        return None
    if evidence_cache and "priced_rows_by_id" in evidence_cache:
        rows = evidence_cache["priced_rows_by_id"]
    else:
        from .reading import priced_rows
        rows = {row["id"]: row for row in priced_rows(document.ocr)}
    if len(line.table_row_ids) != 1:
        return None
    row = rows.get(line.table_row_ids[0])
    if not row or line.unit_price_ht is None or Decimal(row["price"]) != Decimal(line.unit_price_ht):
        return None
    if line.quantity is None or line.line_total_ht is None:
        return None
    if abs(Decimal(line.quantity) * Decimal(line.unit_price_ht) - Decimal(line.line_total_ht)) > Decimal(".02"):
        return None
    text = normalized(line.raw_designation)
    proposed = line.normalized_unit
    from .business_rules import surface_basis
    surface = surface_basis(line)
    if surface and surface[0] == proposed:
        return ("Surface explicite et quantité facturée corroborées : "
                + ("forfait pour la surface indiquée" if proposed == "Forfait"
                   else "quantité égale à la surface, tarif au m²"))
    if proposed == "Forfait" and "forfait" in text:
        return "Libellé forfait explicite, PU de tableau et quantité × PU corroborés"
    if proposed == "ml" and re.search(r"\ble\s+ml\b", text):
        return "Libellé « Le Ml », PU de tableau et quantité × PU corroborés"
    if proposed == "U" and re.search(
        r"\b(?:prise|point lumineux|spot|detecteur|détecteur|tableau électrique|coffret|"
        r"alimentation|liaison|vanne|robinet|radiateur|collecteur|bulbe|coude|kit|bouche|pipe|"
        r"sortie cloison|seche serviette|sèche.serviette|bloc.porte|trappe|porte|baie)\b", text,
    ) and Decimal(line.quantity) == Decimal(line.quantity).to_integral_value():
        return "Ouvrage dénombrable, quantité entière, PU de tableau et total corroborés"
    return None


def assessment_context(document: Document) -> dict:
    if not document.ocr:
        return {}
    from .reading import priced_rows, table_rows
    rows = table_rows(document.ocr)
    priced = priced_rows(document.ocr, rows)
    return {
        "normalized_content": normalized(document.ocr.content),
        "normalized_pages": {page: normalized(text)
                             for page, text in document.ocr.page_texts.items()},
        "table_rows": rows,
        "priced_rows": priced,
        "priced_rows_by_id": {row["id"]: row for row in priced},
    }


def assess(document: Document, line: Line, evidence_cache: dict | None = None) -> Quality:
    """Explainable rule score, not a calibrated probability of correctness."""
    blockers = reasons(document, line, evidence_cache)
    from .business_rules import waste_package, waste_package_price, approved_duct
    user_waste_rule = line.export_unit_prices and waste_package(line)
    user_duct_rule = line.export_unit_prices and approved_duct(line) is not None
    if user_waste_rule and waste_package_price(line) is None:
        blockers.append("Prix du forfait déchets indéterminé : total HT ou quantité et PU requis")
    if line.export_unit_prices:
        from .business_rules import unit_price_values
        conversion = unit_price_values(line)[3]
        if conversion.startswith("Conversion du forfait à vérifier"):
            blockers.append(conversion)
    if line.export_forfait:
        from .business_rules import forfait_values
        if forfait_values(line)[1] is None:
            blockers.append("Prix forfait impossible à déterminer : quantité et total HT absents")
    price_problem = any(
        any(token in normalized(reason) for token in (
            "prix", "pu ", "puht", "ht absent", "ttc", "quantité ×", "extrait", "pages justificatives",
        ))
        for reason in blockers
    )
    price_score = 0.05 if price_problem else 1.0
    score = min(price_score, 0.60 if blockers else 1.0)
    issues = list(blockers)
    evidence = (corroborated_unit(document, line, evidence_cache)
                if line.unit_basis == "interpreted" else None)
    if user_duct_rule:
        evidence = "Conditionnement U validé par l'utilisateur pour cette gaine VMC et sa longueur"
    elif user_waste_rule:
        evidence = "Unité Forfait validée par l'utilisateur pour la gestion des déchets ; montant total de ligne"
    elif line.unit_basis == "interpreted":
        score = min(score, 0.95 if evidence else 0.85)
        if not evidence:
            issues.append("Unité interprétée, non explicitement justifiée : score plafonné à 0,85")
    accepted = not blockers and score >= ACCEPTANCE_THRESHOLD
    if not accepted:
        issues.append(f"Score {score:.2f} inférieur au seuil {ACCEPTANCE_THRESHOLD:.2f}")
    return Quality(score, price_score, accepted, list(dict.fromkeys(issues)), evidence)
