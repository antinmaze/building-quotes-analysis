from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import load_workbook

from .cloud import Settings
from .events import EventJournal
from .excel import export
from .limits import CloudLimits, LimitExceeded
from .models import Batch, Document
from .pipeline import process
from .reporting import write_report
from .storage import digest, inventory, load, save
from .waves import workbook_hash


def azure(arguments: list[str], *, timeout: int = 120) -> dict:
    response = subprocess.run(
        ["az", *arguments, "--only-show-errors", "--output", "json"],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if response.returncode:
        raise ValueError(
            "Commande Azure échouée : " + " ".join(arguments[:3])
            + " ; vérifier az login, droits, quota et journaux de déploiement Azure"
        )
    value = json.loads(response.stdout)
    if not isinstance(value, dict):
        raise ValueError("Réponse Azure inattendue")
    return value


def deploy_infrastructure(parameters: Path, template: Path, settings_path: Path,
                          *, apply: bool, create_resource_group: bool) -> dict:
    """Reuse the existing subscription template; deployment is never implicit."""
    if not template.is_file() or not parameters.is_file():
        raise ValueError("Fichiers Bicep et paramètres existants requis")
    if settings_path.exists() or settings_path.is_symlink():
        raise ValueError("Choisir un nouveau fichier de configuration, sans écrasement")
    values = json.loads(parameters.read_text(encoding="utf-8"))["parameters"]
    subscription = str(uuid.UUID(values["expectedSubscriptionId"]["value"]))
    group = values["resourceGroupName"]["value"]
    location = values["location"]["value"]
    if not isinstance(group, str) or not group.strip() or not isinstance(location, str) or not location.strip():
        raise ValueError("Groupe de ressources et région requis")
    account = azure(["account", "show", "--subscription", subscription])
    if str(account.get("id", "")).lower() != subscription:
        raise ValueError("Souscription Azure différente de la souscription demandée")
    groups = azure(["group", "list", "--subscription", subscription,
                    "--query", "{groups: [].{name:name, location:location}}"])
    matches = [item for item in groups["groups"] if item["name"].casefold() == group.casefold()]
    if matches and matches[0]["location"].casefold() != location.casefold():
        raise ValueError("Région du groupe existant différente des paramètres")
    if not matches:
        if not create_resource_group:
            raise ValueError("Groupe absent : ajouter --create-resource-group pour préparer sa création")
        if not apply:
            return {
                "status": "resource_group_required", "resource_group": group,
                "location": location,
                "notice": "Groupe absent ; création prévue avec --apply. What-if des ressources non exécuté.",
            }
        azure(["group", "create", "--subscription", subscription,
               "--name", group, "--location", location])
    arguments = [
        "--subscription", subscription, "--location", location,
        "--template-file", str(template.resolve()), "--parameters", "@" + str(parameters.resolve()),
    ]
    preview = azure(["deployment", "sub", "what-if", *arguments,
                     "--no-pretty-print"], timeout=600)
    if preview.get("status") != "Succeeded" or not isinstance(preview.get("changes"), list):
        raise ValueError("Prévisualisation Azure incomplète ou échouée ; déploiement refusé")
    changes = Counter(change.get("changeType", "Unknown") for change in preview["changes"])
    if changes.get("Delete"):
        raise ValueError("Suppression détectée par what-if ; déploiement refusé")
    if not apply:
        return {"status": "preview", "changes": dict(changes)}
    deployment = azure([
        "deployment", "sub", "create", "--name", "devis-" + uuid.uuid4().hex[:12],
        *arguments,
    ], timeout=1800)
    properties = deployment["properties"]
    if properties.get("provisioningState") != "Succeeded":
        raise ValueError("Déploiement Azure non terminé avec succès")
    outputs = {key: item["value"] for key, item in properties["outputs"].items()}
    if outputs.get("subscriptionGuardPassed") is not True or outputs.get("resourceGroupLocationMatches") is not True:
        raise ValueError("Gardes de souscription/région invalides après déploiement")
    settings = Settings(
        outputs["azureDocumentIntelligenceEndpoint"], outputs["azureOpenAiEndpoint"],
        outputs["azureOpenAiDeployment"], api_version=outputs["azureOpenAiApiVersion"],
        model_revision=outputs["azureOpenAiModelRevision"], tenant_id=outputs["azureTenantId"],
    )
    save(settings_path, asdict(settings))
    return {
        "status": "deployed", "settings": str(settings_path), "changes": dict(changes),
        "notice": "Infrastructure déployée ; aucun PDF analysé. La propagation RBAC peut prendre du temps.",
    }


@contextmanager
def workspace_lock(workspace: Path):
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = workspace / "operation.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("Espace de travail verrouillé ; vérifier le traitement avant reprise") from exc
    try:
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def integrate_pdf(source: Path, workspace: Path, settings_path: Path | None,
                  *, allow_cloud: bool, max_ocr_pages: int, max_llm_calls: int) -> dict:
    if not source.is_file() or source.suffix.lower() != ".pdf" or source.is_symlink():
        raise ValueError("Un PDF existant unique, sans lien symbolique, est requis")
    source, workspace = source.resolve(), workspace.resolve()
    if workspace == source or workspace in source.parents:
        raise ValueError("Le PDF source doit rester hors de l'espace de travail")
    if max_ocr_pages < 0 or max_llm_calls < 0:
        raise ValueError("Plafonds de consommation positifs ou nuls requis")
    records = inventory(source, pdf_only=True)
    if len(records) != 1:
        raise ValueError("PDF exclu de l'inventaire")
    if not allow_cloud:
        return {"status": "preflight" if records[0]["status"] == "ready" else "document_error",
                "document": records[0],
                "notice": "Contrôle local uniquement ; ajouter --allow-cloud pour analyser"}
    key = digest(str(source))
    limited = False
    with workspace_lock(workspace):
        if records[0]["status"] != "ready":
            batch = Batch(schema_version="1", documents=[
                Document(**records[0], ocr=None, extraction=None),
            ])
        else:
            settings = Settings(**load(settings_path)) if settings_path else Settings.from_env()
            journal = EventJournal(workspace / "logs" / (key + ".events.jsonl"))
            try:
                batch = process(
                    source, workspace / "cache" / key, settings, journal=journal,
                    structured=True, pdf_only=True, limits=CloudLimits(max_ocr_pages, max_llm_calls),
                )
            except LimitExceeded as exc:
                limited = True
                batch = Batch(schema_version="1", documents=[
                    Document(**{**records[0], "status": "error", "error": str(exc)},
                             ocr=None, extraction=None),
                ])
        save(workspace / "documents" / (key + ".json"), batch.model_dump())
    counts = Counter(document.status for document in batch.documents)
    return {
        "status": "limit_reached" if limited else ("integrated" if counts["extracted"] else "document_error"),
        "documents": dict(counts), "workspace": str(workspace),
        "notice": "Résultat enregistré ; exécuter consolidate pour les rapports",
    }


def consolidate(workspace: Path) -> dict:
    if not workspace.is_dir():
        raise ValueError("Espace de travail absent")
    with workspace_lock(workspace):
        paths = sorted((workspace / "documents").glob("*.json"))
        if not paths:
            raise ValueError("Aucun PDF intégré dans cet espace de travail")
        documents = [document for path in paths
                     for document in Batch.model_validate(load(path)).documents]
        sources = [document.source for document in documents]
        if len(set(sources)) != len(sources):
            raise ValueError("Sources dupliquées dans l'espace de travail ; consolidation refusée")
        batch = Batch(schema_version="1", documents=documents)
        reports = workspace / "reports"
        reports.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=reports) as temporary:
            staging = Path(temporary)
            summary = export(batch, staging / "prices.xlsx")
            book = load_workbook(staging / "prices.xlsx", read_only=True, data_only=True)
            try:
                docs = list(book["Documents"].values)
                errors = [docs[0]] + [
                    row for row in docs[1:]
                    if row[3] != "extracted" or row[4] != "devis"
                    or not row[5] or row[7] or row[9]
                ]
                write_report(staging / "errors.xlsx", {
                    "Documents à examiner": errors,
                    "Lignes à vérifier": list(book["À vérifier"].values),
                    "Couverture": list(book["Couverture"].values),
                })
            finally:
                book.close()
            # A versioned directory makes the report pair immutable; the manifest is published last.
            generation = reports / uuid.uuid4().hex
            os.replace(staging, generation)
            report = {
                **summary, "status": "consolidated", "export_status": summary["status"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "documents": dict(Counter(d.status for d in documents)),
                "reports": {
                    name: {"path": str((generation / name).resolve()),
                           "sha256": workbook_hash(generation / name)}
                    for name in ("prices.xlsx", "errors.xlsx")
                },
            }
            save(workspace / "latest-report.json", report)
    return report
