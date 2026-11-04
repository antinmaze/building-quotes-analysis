from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .cloud import AzureBackend, Settings
from .events import EventJournal
from .excel import export
from .limits import CloudLimits, LimitExceeded
from .models import Batch, Document
from .pipeline import process
from .storage import digest, inventory, load, save


def _document_error_can_be_deferred(error: str | None) -> bool:
    return bool(error)


def workbook_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def campaign_batch(state: dict) -> Batch:
    return Batch(schema_version="1", documents=[
        document for wave in state["waves"].values()
        for document in Batch.model_validate(wave["batch"]).documents
    ])


def publish_campaign(state: dict, path: Path, output: Path) -> dict:
    # The journal precedes replacement so an interrupted publication is recoverable.
    pending = state.get("pending")
    allowed = {pending["before"], pending["after"]} if pending else {state["workbook_sha256"]}
    before = workbook_hash(output)
    if before not in allowed:
        raise ValueError("Classeur modifié pendant le traitement ; publication refusée")
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        staged = Path(directory) / "compet.xlsx"
        summary = export(campaign_batch(state), staged)
        state["pending"] = {"before": before, "after": workbook_hash(staged)}
        save(path, state)
        if workbook_hash(output) != before:
            raise ValueError("Classeur modifié pendant le traitement ; publication refusée")
        os.replace(staged, output)
    state["workbook_sha256"] = state.pop("pending")["after"]
    save(path, state)
    return summary


@contextmanager
def campaign(output: Path, root: Path, settings: Settings):
    path = output.with_suffix(".campaign.json")
    lock = output.with_suffix(".xlsx.lock")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("Campagne déjà verrouillée ; vérifier le traitement avant toute reprise") from exc
    try:
        os.close(fd)
        identity = {"root": str(root.resolve()), "output": str(output.resolve()),
                    "normalization": settings.normalization_config()}
        if path.exists():
            state = load(path)
            if state["identity"] != identity:
                raise ValueError("Classeur lié à une autre campagne ou configuration")
            actual = workbook_hash(output)
            pending = state.get("pending")
            allowed = {pending["before"], pending["after"]} if pending else {state["workbook_sha256"]}
            if actual not in allowed:
                raise ValueError("Classeur de campagne modifié ou absent ; reprise refusée")
            campaign_batch(state)
            if pending:
                publish_campaign(state, path, output)
        else:
            if output.exists():
                raise ValueError("Utiliser un nouveau classeur : sortie existante sans registre de campagne")
            state = {"identity": identity, "waves": {}, "workbook_sha256": None}
        yield state, path
    finally:
        lock.unlink(missing_ok=True)


class SelectedPagesBackend(AzureBackend):
    def __init__(self, settings, cache, **kwargs):
        super().__init__(settings, **kwargs)
        self.cache = cache

    def ocr(self, path: Path, pages: list[int] | None = None):
        if not pages:
            raise ValueError("Pages explicitement sélectionnées requises pour une vague")
        from pypdf import PdfReader, PdfWriter
        self.cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Only selected pages leave the machine, not the complete composite document.
        with tempfile.TemporaryDirectory(dir=self.cache) as directory:
            subset = Path(directory) / "selected.pdf"
            writer = PdfWriter()
            try:
                reader = PdfReader(path, strict=True)
                for number in pages:
                    writer.add_page(reader.pages[number - 1])
                with subset.open("xb") as stream:
                    import os
                    os.fchmod(stream.fileno(), 0o600)
                    writer.write(stream)
            finally:
                writer.close()
            result = super().ocr(subset)
        if set(result.page_texts) != {str(i) for i in range(1, len(pages) + 1)}:
            raise ValueError("Réponse OCR du sous-document incomplète")
        mapping = {i: number for i, number in enumerate(pages, 1)}
        result.page_texts = {str(mapping[int(i)]): text for i, text in result.page_texts.items()}

        def remap(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"pageNumber", "page_number"}:
                        if type(item) is not int or item not in mapping:
                            raise ValueError("Numéro de page OCR inconnu")
                        value[key] = mapping[item]
                    else:
                        remap(item)
            elif isinstance(value, list):
                for item in value:
                    remap(item)
        remap(result.raw)
        result.raw["source_page_selection"] = pages
        return result


def preflight(manifest: Path, root: Path) -> dict:
    report = load(manifest)
    approval = report.get("approval", {})
    if not approval.get("selection_approved") or approval.get("selection_digest") != digest(report["records"]):
        raise ValueError("Sélection non approuvée ou différente de la sélection approuvée")
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Dossier source absent")
    sources = set()
    selected_hashes = set()
    selected = []
    for record in report["records"]:
        path = Path(record["source"]).resolve()
        if root not in path.parents or path.suffix.lower() != ".pdf" or path in sources:
            raise ValueError("Source dupliquée ou hors périmètre")
        sources.add(path)
        if record["decision"] != "retenu":
            continue
        if path.is_symlink() or any(part == "00_ARCHIVES" for part in path.parts):
            raise ValueError("Source exclue")
        item = inventory(path, pdf_only=True)[0]
        if (item["status"] != "ready" or item["sha256"] != record["sha256"]
                or item["pages"] != record["source_pages"]):
            raise ValueError("PDF absent, illisible ou modifié depuis la sélection")
        if path.relative_to(root).parts[0] != record["client"]:
            raise ValueError("Client du dossier différent du client sélectionné")
        if item["sha256"] in selected_hashes:
            raise ValueError("PDF sélectionné en double dans la vague")
        selected_hashes.add(item["sha256"])
        pages = record["selected_pages"]
        if (not pages or any(type(p) is not int or p < 1 or p > item["pages"] for p in pages)
                or pages != sorted(set(pages))):
            raise ValueError("Plage de pages invalide")
        selected.append(record)
    if not selected or sum(len(r["selected_pages"]) for r in selected) != report["selected_pages"]:
        raise ValueError("Comptage de pages incohérent")
    return {"wave": report["wave"], "documents": len(selected),
            "pages": report["selected_pages"], "max_llm_calls": len(selected) * 2,
            "records": selected}


def run_wave(manifest: Path, root: Path, cache: Path, output: Path, settings: Settings,
             *, max_pages: int | None, max_calls: int | None, defer_sources: tuple[str, ...] = (),
             continue_on_document_error: bool = False) -> dict:
    plan = preflight(manifest, root)
    if settings.normalization_config()["normalization"] != "works-2":
        raise ValueError("La vague exige la normalisation validée works-2")
    if ((max_pages is not None and max_pages < 1)
            or (max_calls is not None and max_calls < 1)):
        raise ValueError("Les plafonds configurés doivent être positifs")
    if cache.resolve() == Path(".devis-cache").resolve():
        raise ValueError("Cache de vague dédié requis ; ne pas remplacer le pilote")
    for target in (cache.resolve(), output.resolve()):
        if target == root.resolve() or root.resolve() in target.parents or target in root.resolve().parents:
            raise ValueError("Cache et sortie hors des sources requis")
    from .excel import validate_destination
    validate_destination(output)
    with campaign(output, root, settings) as (state, state_path):
        return _run_wave(manifest, root, cache, output, settings, plan, state, state_path,
                         max_pages=max_pages, max_calls=max_calls, defer_sources=defer_sources,
                         continue_on_document_error=continue_on_document_error)


def _run_wave(manifest: Path, root: Path, cache: Path, output: Path, settings: Settings,
              plan: dict, state: dict, state_path: Path, *,
              max_pages: int | None, max_calls: int | None,
              defer_sources: tuple[str, ...], continue_on_document_error: bool) -> dict:
    marker = cache / "wave-selection.json"
    identity = {"manifest_digest": digest(load(manifest)), "root": str(root.resolve()),
                "normalization": settings.normalization_config()}
    previous = state["waves"].get(plan["wave"])
    if previous and previous["identity"] != identity:
        raise ValueError("Vague déjà enregistrée avec une autre sélection")
    deferral_path = cache / "deferred-sources.json"
    deferred = dict(previous.get("deferred_sources", {})) if previous else {}
    if deferral_path.exists():
        saved = load(deferral_path)
        if saved["identity"] != identity:
            raise ValueError("Reports liés à une autre sélection")
        deferred.update(saved["sources"])
    previous_documents = Batch.model_validate(previous["batch"]).documents if previous else []
    failures = {d.source: d.error for d in previous_documents if d.status == "error"}
    if continue_on_document_error:
        for source, error in failures.items():
            if _document_error_can_be_deferred(error):
                deferred[source] = "Report automatique autorisé ; erreur précédente : " + error
    for source in defer_sources:
        if source not in failures and source not in deferred:
            raise ValueError("Seul un PDF déjà en erreur peut être reporté explicitement")
        if source in failures:
            deferred[source] = "Mis en attente sur demande explicite ; erreur précédente : " + str(failures[source])
    if not set(deferred).issubset(r["source"] for r in plan["records"]):
        raise ValueError("Report hors de la sélection approuvée")
    report = load(manifest)
    sources = {r["source"] for r in report["records"]}
    hashes = {r["sha256"] for r in plan["records"]}
    for name, wave in state["waves"].items():
        if name == plan["wave"]:
            continue
        if (sources.intersection(d["source"] for d in wave["batch"]["documents"])
                or hashes.intersection(wave["selected_hashes"])):
            raise ValueError("Document déjà rattaché à une autre vague de la campagne")
    if marker.exists():
        if load(marker) != identity:
            raise ValueError("Cache déjà lié à une autre sélection ou configuration")
    elif cache.exists() and any(cache.iterdir()):
        raise ValueError("Cache de vague non identifié ; utiliser un cache dédié")
    else:
        save(marker, identity)
    if deferred:
        save(deferral_path, {"identity": identity, "sources": deferred})
    journal = EventJournal(cache / "events.jsonl")
    for source, reason in deferred.items():
        journal.emit("document_deferred", source=source, reason=reason)
    limits = CloudLimits(max_pages, max_calls)
    backend = SelectedPagesBackend(settings, cache / "upload-subsets", limits=limits, journal=journal)
    documents = []
    paused = False
    completed = {
        d.source: d for d in previous_documents
        if d.status == "extracted" and not d.error
    }
    for record in plan["records"]:
        if record["source"] in deferred:
            continue
        if record["source"] in completed:
            documents.append(completed[record["source"]])
            continue
        try:
            result = process(Path(record["source"]), cache, settings, backend=backend,
                             journal=journal, structured=True, pdf_only=True, limits=limits,
                             selected_pages=record["selected_pages"], client_hint=record["client"])
        except LimitExceeded:
            save(cache / "results.partial.json",
                 Batch(schema_version="1", documents=documents).model_dump())
            journal.emit("wave_paused", reason="consumption_limit")
            paused = True
            break
        failed = [d for d in result.documents if d.error]
        if (continue_on_document_error and failed
                and all(_document_error_can_be_deferred(d.error) for d in failed)):
            for document in failed:
                deferred[document.source] = "Report automatique autorisé ; " + document.error
                journal.emit("document_deferred", source=document.source,
                             reason=deferred[document.source])
            save(deferral_path, {"identity": identity, "sources": deferred})
            documents.extend(d for d in result.documents if not d.error)
        else:
            documents.extend(result.documents)
        batch = Batch(schema_version="1", documents=documents)
        save(cache / "results.partial.json", batch.model_dump())
        if failed and not all(d.source in deferred for d in failed):
            journal.emit("wave_paused", reason="document_error")
            paused = True
            break
    from .projects import resolve_projects
    hints = {r["source"]: r["client"] for r in plan["records"]}
    resolve_projects(documents, hints)
    processed = {d.source for d in documents}
    # Preserve ignored and deferred files in the global workbook's document audit.
    for record in report["records"]:
        if record["source"] not in processed:
            documents.append(Document(
                source=record["source"], sha256=record["sha256"],
                pages=record["source_pages"], status="unsupported",
                error=(deferred[record["source"]] if record["source"] in deferred
                       else f"{record['decision']} : {record['reason']}" if record["decision"] != "retenu"
                       else "Non traité : vague interrompue avant ce document"),
                ocr=None, extraction=None,
            ))
    batch = Batch(schema_version="1", documents=documents)
    save(cache / "results.json", batch.model_dump())
    state["waves"][plan["wave"]] = {
        "identity": identity, "selected_hashes": sorted(hashes), "batch": batch.model_dump(),
        "status": "paused" if paused else "completed",
        "deferred_sources": deferred,
    }
    summary = publish_campaign(state, state_path, output)
    journal.emit("wave_completed_or_paused", **summary, ocr_pages=limits.ocr_pages,
                 llm_calls=limits.llm_calls)
    return {**summary, "wave_status": "paused" if paused else "completed",
            "campaign_waves": len(state["waves"]),
            "deferred_documents": len(deferred),
            "new_ocr_pages": limits.ocr_pages, "new_llm_calls": limits.llm_calls}
