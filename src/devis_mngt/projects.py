from __future__ import annotations

import re
from pathlib import Path

from .models import Document, alias_supported, evidence_key, normalized


def project_hint(source: str) -> str | None:
    parts = Path(source).stem.removeprefix("OCR ").split(" - ")
    return parts[1] if len(parts) >= 4 else None


def addresses(text: str) -> set[str]:
    text = evidence_key(text)
    return set(re.findall(
        r"\b\d{1,4}\s+(?:bis\s+|ter\s+)?(?:rue|avenue|boulevard|impasse|chemin|route|allee)"
        r"\s+(?:[a-z]+\s+){1,10}\d{5}\s+[a-z]+", text,
    ))


def resolve_projects(documents: list[Document], client_hints: dict[str, str] | None = None) -> list[Document]:
    changed = []
    for target in documents:
        ext = target.extraction
        parts = Path(target.source).stem.removeprefix("OCR ").split(" - ")
        if ext and target.ocr and ext.evidence_version == "2" and len(parts) >= 4:
            company_hint = parts[2].strip()
            fragments = [f for f in ext.metadata_evidence.get("company", [])
                         if normalized(f) in normalized(target.ocr.content)]
            if (alias_supported(company_hint, ext.company_raw)
                    and alias_supported(company_hint, " ".join(fragments))
                    and ext.company != company_hint):
                previous = ext.company
                ext.company = company_hint
                ext.warnings.append(
                    f"Entreprise harmonisée avec le nom de fichier et le contenu ; ancienne valeur : {previous}"
                )
                changed.append(target)
            elif not alias_supported(company_hint, ext.company_raw):
                warning = "Entreprise du nom de fichier non corroborée : " + company_hint
                if warning not in ext.warnings:
                    ext.warnings.append(warning)
        hint = (client_hints or {}).get(target.source) or project_hint(target.source)
        if not ext or not target.ocr or not hint or ext.evidence_version != "2" or ext.project_link:
            continue
        supporting_pages = [text for text in target.ocr.page_texts.values()
                            if alias_supported(hint, text)]
        if supporting_pages and evidence_key(ext.client) != evidence_key(hint):
            previous = ext.client
            ext.client = hint
            ext.metadata_evidence["client"] = supporting_pages
            ext.warnings.append(
                f"Client projet harmonisé avec le nom de fichier et le contenu ; ancienne valeur : {previous}"
            )
            changed.append(target)
        valid = [f for f in ext.metadata_evidence.get("client", [])
                 if normalized(f) in normalized(target.ocr.content)]
        if (ext.client and evidence_key(ext.client) == evidence_key(hint)
                and alias_supported(ext.client, " ".join(valid))):
            continue
        target_addresses = addresses(target.ocr.content)
        candidates = []
        for donor in documents:
            other = donor.extraction
            if donor is target or not donor.ocr or not other or other.project_link:
                continue
            if not other.client or not alias_supported(other.client, other.client_raw):
                continue
            if evidence_key(hint) != evidence_key(other.client):
                continue
            fragments = [f for f in other.metadata_evidence.get("client", [])
                         if normalized(f) in normalized(donor.ocr.content)]
            if not alias_supported(other.client_raw, " ".join(fragments)):
                continue
            # Restrict matching to the beneficiary's evidence, not a shared billing address.
            beneficiary_addresses = addresses(" ".join(fragments))
            shared = target_addresses & beneficiary_addresses
            if shared and donor.sha256:
                candidates.append((donor, sorted(shared)[0]))
        if not candidates:
            if evidence_key(ext.client) != evidence_key(hint):
                warning = "Client du nom de fichier non corroboré : " + hint
                if warning not in ext.review_reasons:
                    ext.review_reasons.append(warning)
            continue
        identities = {evidence_key(d.extraction.client) for d, _ in candidates}
        if len(identities) != 1:
            ext.warnings.append("Plusieurs identités possibles pour la même adresse : rapprochement refusé")
            continue
        donor, address = candidates[0]
        ext.client = donor.extraction.client
        ext.review_reasons = [r for r in ext.review_reasons
                              if not r.startswith("Client du nom de fichier non corroboré")]
        ext.project_link = {
            "source": donor.source, "sha256": donor.sha256, "address": address,
            "client_raw": donor.extraction.client_raw,
        }
        ext.warnings.append("Projet corroboré par adresse identique dans un autre devis et nom de fichier")
        changed.append(target)
    return changed
