from __future__ import annotations

import math
from pathlib import Path

from .local_pdf import LOCAL_VERSION, prepare_pdf
from .storage import digest, inventory, load, save


def plan(root: Path, cache: Path, limit: int | None = None, batch_size: int = 50,
         *, local_ocr: bool = False) -> dict:
    if batch_size < 1:
        raise ValueError("Taille de lot positive requise")
    records = inventory(root, limit, pdf_only=True, exclude_dirs=("00_ARCHIVES",))
    engine = None
    if local_ocr:
        from .local_ocr import engine_config
        engine = engine_config()
    originals = {}
    for record in records:
        if record["status"] != "ready":
            continue
        source_sha = record["sha256"]
        if source_sha in originals:
            record["duplicate_of"] = originals[source_sha]
            continue
        originals[source_sha] = record["source"]
        key = digest({"sha256": source_sha, "local_version": LOCAL_VERSION, "ocr": engine})
        path = cache / "local-diagnostic" / (key + ".json")
        if path.exists():
            details = load(path)
        else:
            try:
                pages = prepare_pdf(Path(record["source"]), cache, use_ocr=local_ocr, config=engine)
                details = {"pages": [
                    {"number": p["number"], "route": "azure" if p["reason"] else "local",
                     "reason": p["reason"], "method": p["method"],
                     "text_characters": len(p["text"]),
                     "searchable_pdf": p.get("searchable_pdf")} for p in pages
                ]}
                if len(pages) != record["pages"]:
                    raise ValueError("Nombre de pages locales différent de l'inventaire")
            except (ValueError, OSError) as exc:
                details = {"error": f"{type(exc).__name__} : diagnostic natif impossible",
                           "pages": []}
            save(path, details)
        record["diagnostic"] = details
        record["selection"] = "À confirmer : devis/version finale, aucun envoi autorisé par ce rapport"
    unique = [r for r in records if r["status"] == "ready" and "duplicate_of" not in r]
    local = sum(p["route"] == "local" for r in unique for p in r["diagnostic"]["pages"])
    # Unanalysable native documents are conservatively charged as full Azure fallback.
    azure = sum(sum(p["route"] == "azure" for p in r["diagnostic"]["pages"])
                if not r["diagnostic"].get("error") else r["pages"] for r in unique)
    return {
        "scope": "Diagnostic local seulement ; classification et versions finales non validées",
        "pdf_files": len(records), "unique_readable_pdfs": len(unique),
        "duplicate_copies": sum("duplicate_of" in r for r in records),
        "inventory_errors": sum(r["status"] == "error" for r in records),
        "native_errors": sum(bool(r["diagnostic"].get("error")) for r in unique),
        "local_pages": local, "azure_fallback_pages": azure,
        "local_ocr_enabled": local_ocr,
        "batch_size": batch_size, "max_batches": math.ceil(len(unique) / batch_size),
        "records": records,
    }
