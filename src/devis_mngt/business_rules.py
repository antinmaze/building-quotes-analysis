from __future__ import annotations

import re
from decimal import Decimal

from .models import Line, normalized, unit, technical_features, line_unit, evidence_key

SURFACE_RULE = "surface-quantity-1"
LABEL_RULE = "labels-1"


def approved_duct(line: Line) -> tuple[str, str] | None:
    text = evidence_key(line.raw_designation)
    match = re.fullmatch(
        r"gaine vmc resist r d(82|127|160) iso 25 pvc intisse (10|6)ml renforcee re2020",
        text,
    )
    if not match:
        return None
    diameter, length = match.groups()
    if (diameter, length) not in {("82", "10"), ("127", "6"), ("160", "6")}:
        return None
    return diameter, length


def waste_package(line: Line) -> bool:
    return evidence_key(line.raw_designation).startswith(
        "tri gestion evacuation et valorisation ou elimination des dechets"
    )


def waste_package_price(line: Line) -> Decimal | None:
    if line.line_total_ht is not None:
        return Decimal(line.line_total_ht)
    if line.quantity is not None and line.unit_price_ht is not None:
        return Decimal(line.quantity) * Decimal(line.unit_price_ht)
    return None


def unit_price_values(line: Line) -> tuple[str, Decimal | None, str | None, str]:
    designation = line.raw_designation
    if line.designation_context:
        designation += " — " + line.designation_context
    duct = approved_duct(line)
    if duct:
        diameter, length = duct
        return (
            f"Gaine VMC Resist’r RE2020 D{diameter} — isolant 25 mm, PVC/intissé — longueur de {length} m",
            Decimal(line.unit_price_ht) if line.unit_price_ht is not None else None,
            "U", "Conditionnement validé par l'utilisateur : U = une longueur de " + length + " m ; PU source inchangé",
        )
    if waste_package(line):
        return (designation, waste_package_price(line), "Forfait",
                "Règle utilisateur déchets : forfait = total HT source, sinon quantité × PU HT source")
    price = Decimal(line.unit_price_ht) if line.unit_price_ht is not None else None
    source_unit = line_unit(line)
    measures = {
        (Decimal(value.replace(",", ".")), "m²" if suffix.lower() in {"m2", "m²"} else "ml")
        for value, suffix in re.findall(
            r"\b(?:surface|longueur)\s*:?\s*(\d+(?:[,.]\d+)?)\s*(m²|m2|ml|m)\b",
            line.raw_designation, re.IGNORECASE,
        )
    }
    if source_unit == "Forfait" and len(measures) == 1 and line.quantity is not None:
        measure, target_unit = next(iter(measures))
        if measure > 0 and Decimal(line.quantity) == 1 and price is not None:
            return (designation, price / measure, target_unit,
                    f"PU forfait source {price} € HT / métré explicite {measure} {target_unit}")
    if source_unit == "Forfait" and measures:
        return designation, price, source_unit, "Conversion du forfait à vérifier : métré ou quantité ambigu"
    return designation, price, source_unit, "PU et unité source normalisée, sans conversion"


def forfait_values(line: Line) -> tuple[str, Decimal | None, str]:
    """Project a source occurrence as a priced package, without changing source data."""
    designation = line.designation
    quantity = Decimal(line.quantity) if line.quantity is not None else None
    source_unit = line_unit(line)
    if quantity is not None:
        value = format(quantity.normalize(), "f").replace(".", ",")
        if source_unit in {"m²", "m³", "ml", "h", "kg", "t"}:
            scope = f"pour {value} {source_unit}"
        else:
            scope = f"quantité facturée : {value}" + (f" {source_unit}" if source_unit else "")
        if normalized(scope) not in normalized(designation):
            designation += " — " + scope
    else:
        scope = "quantité non renseignée"
    if line.line_total_ht is not None:
        price = Decimal(line.line_total_ht)
    elif quantity is not None and line.unit_price_ht is not None:
        price = quantity * Decimal(line.unit_price_ht)
    elif source_unit == "Forfait" and line.unit_price_ht is not None:
        price = Decimal(line.unit_price_ht)
    else:
        price = None
    return designation, price, scope


def apply_label_rule(line: Line) -> bool:
    original = line.designation
    raw = " ".join(line.raw_designation.split())
    # Short complete source labels are preferable to model-added prose.
    candidate = raw if len(raw) <= 160 else original
    candidate = re.sub(r"^(?:Salon|Pièce de vie|Chambre(?:\s+\d+)?|Cuisine)\s*:\s*", "",
                       candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s+(?:Chambre\s+\d+(?:\s*\([^)]*\))?|Salle de bain(?:\s+étage)?|"
                       r"Dégagement|WC|Cloison|Plafond)\s*:\s*$", "", candidate, flags=re.IGNORECASE)
    # Remove only additions absent from the source, never source supply/installation scope.
    for annotation in (
        "forfait", "unité", "m²", "linéaire", "fourniture", "fourniture et pose",
        "forfait fourniture et pose", "unité fourniture et pose", "pose d'huisserie simple",
    ):
        if normalized(annotation) not in normalized(raw):
            candidate = re.sub(r"\s*\(" + re.escape(annotation) + r"\)", "",
                               candidate, flags=re.IGNORECASE)
    if "isolation laine de bois" not in normalized(raw):
        candidate = re.sub(r"\bisolation laine de bois\b", "laine de bois", candidate,
                           flags=re.IGNORECASE)
    candidate = re.sub(r"\bFoumiture\b", "Fourniture", candidate)
    candidate = re.sub(r"\b(?:mm|cm)\b", lambda m: m[0].lower(), candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"(?<=\d)\s*(?:[xX×])\s*(?=\d)", "x", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" .;")
    if line.designation_context:
        candidate = raw + " — " + line.designation_context
    surface = surface_basis(line)
    if surface and surface[0] == "Forfait":
        candidate = re.sub(r"\bsurface\s*:?\s*\d+(?:[,.]\d+)?\s*m(?:²|2)\.?\s*",
                           "", candidate, flags=re.IGNORECASE).strip(" .;")
        label = format(surface[1].normalize(), "f").replace(".", ",")
        suffix = f"pour {label} m²"
        if normalized(suffix) not in normalized(candidate):
            candidate += " — " + suffix
    # Do not imitate truncated reference labels or lose numeric technical distinctions.
    if not candidate or technical_features(original) - technical_features(candidate):
        return False
    if candidate == original:
        return False
    if line.designation_before_rules is None:
        line.designation_before_rules = original
    line.designation = candidate
    warning = f"Règle métier {LABEL_RULE} : libellé rapproché de la rédaction source, sans modification du PU"
    if warning not in line.warnings:
        line.warnings.append(warning)
    apply_surface_rule(line)
    return True


def surface_basis(line: Line) -> tuple[str, Decimal] | None:
    matches = re.findall(
        r"\bsurface\s*:?\s*(\d+(?:[,.]\d+)?)\s*m(?:²|2)\b",
        line.raw_designation, re.IGNORECASE,
    )
    surfaces = {Decimal(value.replace(",", ".")) for value in matches}
    if len(surfaces) != 1 or line.quantity is None:
        return None
    surface = surfaces.pop()
    quantity = Decimal(line.quantity)
    if surface <= 0:
        return None
    explicit = unit(line.raw_unit)
    if quantity == 1 and surface != 1 and explicit in {None, "Forfait"}:
        return "Forfait", surface
    if quantity == surface and explicit in {None, "m²"}:
        return "m²", surface
    return None


def apply_surface_rule(line: Line) -> bool:
    basis = surface_basis(line)
    if basis is None:
        return False
    chosen, surface = basis
    line.normalized_unit = chosen
    line.unit_basis = "explicit" if unit(line.raw_unit) == chosen else "interpreted"
    if chosen == "Forfait":
        label = format(surface.normalize(), "f").replace(".", ",")
        suffix = f"pour {label} m²"
        if normalized(suffix) not in normalized(line.designation):
            line.designation += " — " + suffix
        if line.technical_signature and normalized(suffix) not in normalized(line.technical_signature):
            line.technical_signature += " — " + suffix
    warning = (
        f"Règle métier {SURFACE_RULE} : surface {format(surface, 'f')} m², "
        f"quantité {line.quantity}, unité retenue {chosen} ; PU inchangé"
    )
    if warning not in line.warnings:
        line.warnings.append(warning)
    return True
