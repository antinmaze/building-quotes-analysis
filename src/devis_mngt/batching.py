from __future__ import annotations

from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from .storage import inventory, save


def prepare_batches(root: Path, *, pilot_size: int = 50, batch_size: int = 100,
                    max_pages: int = 500) -> dict:
    root = root.resolve()
    if not root.is_dir() or min(pilot_size, batch_size, max_pages) < 1:
        raise ValueError("Dossier d'entrée et tailles de lots positifs requis")
    records = inventory(root, pdf_only=True, exclude_dirs=("00_ARCHIVES",))
    originals = {}
    clients = defaultdict(list)
    for record in records:
        record["client_folder"] = Path(record["source"]).relative_to(root).parts[0]
        record["batch"] = None
        record["selection"] = "bloqué : PDF illisible"
        if record["status"] != "ready":
            continue
        sha = record["sha256"]
        if sha in originals:
            record["duplicate_of"] = originals[sha]["source"]
            record["selection"] = "copie identique exclue ; contexte client conservé"
            continue
        originals[sha] = record
        record["selection"] = "à valider : devis, annexe et version finale ; aucun envoi autorisé"
        clients[record["client_folder"]].append(record)
    # Distribute the qualification lot across clients, prioritizing short documents.
    queues = {client: deque(sorted(rows, key=lambda r: (r["pages"], r["source"])))
              for client, rows in sorted(clients.items())}
    pilot = []
    pages = 0
    while len(pilot) < pilot_size:
        progressed = False
        for queue in queues.values():
            if queue and len(pilot) < pilot_size and pages + queue[0]["pages"] <= max_pages:
                row = queue.popleft()
                pilot.append(row)
                pages += row["pages"]
                progressed = True
        if not progressed:
            break
    batches = []

    def append(rows):
        if not rows:
            return
        identifier = f"lot-{len(batches) + 1:03d}"
        for row in rows:
            row["batch"] = identifier
        batches.append({
            "id": identifier, "role": "qualification" if not batches else "production_candidate",
            "status": "préparé, sélection et autorisation Azure en attente",
            "documents": len(rows), "pages": sum(r["pages"] for r in rows),
            "clients": dict(Counter(r["client_folder"] for r in rows)),
            "sha256": [r["sha256"] for r in rows],
        })

    append(pilot)
    current = []
    pages = 0
    for row in sorted((row for queue in queues.values() for row in queue),
                      key=lambda r: (r["client_folder"], r["source"])):
        if current and (len(current) >= batch_size or pages + row["pages"] > max_pages):
            append(current)
            current, pages = [], 0
        current.append(row)
        pages += row["pages"]
    append(current)
    if batches and not pilot:
        batches[0]["role"] = "qualification"
    return {
        "schema_version": "1", "created_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root), "cloud_authorized": False,
        "selection_policy": "Aucune version choisie sur la seule date ou le nom ; validation requise",
        "pilot_size": pilot_size, "batch_size": batch_size, "max_pages": max_pages,
        "page_cap_exception": "Un PDF dépassant le plafond constitue un lot isolé",
        "pdf_files": len(records), "unique_readable_pdfs": len(originals),
        "unique_pages": sum(r["pages"] for r in originals.values()),
        "duplicate_copies": sum("duplicate_of" in r for r in records),
        "inventory_errors": sum(r["status"] == "error" for r in records),
        "clients": len({r["client_folder"] for r in records}),
        "batches": batches, "records": records,
    }


def save_batch_plan(report: dict, manifest: Path, workbook: Path) -> None:
    from .reporting import write_report
    if manifest.exists() or workbook.exists():
        raise ValueError("Utiliser de nouveaux chemins pour le manifeste et le classeur de lots")
    batches = [["Lot", "Rôle", "PDF uniques", "Pages", "Clients", "État"]]
    batches += [[b["id"], b["role"], b["documents"], b["pages"],
                 ", ".join(b["clients"]), b["status"]] for b in report["batches"]]
    files = [["Source", "Client dossier", "SHA256", "Pages", "Lot", "Sélection", "Copie de", "Erreur"]]
    files += [[r["source"], r["client_folder"], r["sha256"], r["pages"], r["batch"],
               r["selection"], r.get("duplicate_of"), r["error"]] for r in report["records"]]
    summary = [["Indicateur", "Valeur"], ["Nature", "Plan provisoire, pas une base de prix"],
               ["Envoi Azure autorisé", False], ["Versions finales", "Non validées"],
               *[[k, report[k]] for k in (
                   "clients", "pdf_files", "unique_readable_pdfs", "unique_pages",
                   "duplicate_copies", "inventory_errors")],
               ["Plafond pages par lot", report["max_pages"]],
               ["Exception", report["page_cap_exception"]]]
    write_report(workbook, {"Synthèse": summary, "Lots": batches, "Fichiers": files})
    save(manifest, report)
