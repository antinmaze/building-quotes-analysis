from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .cloud import Settings
from .comparison import compare
from .excel import export, validate_destination, validate_standalone_output
from .events import EventJournal
from .models import Batch
from .pipeline import process
from .storage import inventory, load, save


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="devis-mngt",
        description="Prix de devis français : inventaire local, extraction Azure explicite et Excel audité.",
        epilog="""Installation après accord : python3 -m pip install -e .
Variables : charger .env.sample via le shell (pas de chargement automatique).
Exemples :
  devis-mngt inventory --input devis-sample
  devis-mngt run --input devis-sample --output outputs/prix.xlsx --limit 1 --allow-cloud
  devis-mngt export --results .devis-cache/results.json --output outputs/prix.xlsx
Les originaux restent intacts. Les caches et classeurs contiennent des données
sensibles. Ne pas éditer les caches : checksum et schéma sont vérifiés à la lecture.
Chaque run/export ajoute un journal JSONL privé dans logs/ (--log-dir configurable).
Les exports hors ligne marquent les résultats reconstruits retrospective=true :
les horodatages sont ceux du journal, pas ceux des anciens appels Azure.
Après changement de modèle Azure, augmenter AZURE_OPENAI_MODEL_REVISION.
Aucune intégration Batiprix/BatiChiffrage : éventuel rapprochement futur sous licence.
Codes : 0 succès, 1 erreur fichier, 2 configuration/usage, 3 aucune donnée acceptée.
Les lignes À vérifier ne sont jamais publiées dans Base de prix. Des lignes acceptées
peuvent coexister avec des lignes à vérifier : consulter systématiquement l'audit.
Limites : lecture et normalisation par blocs de deux pages pour les longs PDF,
sans troncature silencieuse; modèle
Azure compatible structured outputs requis; contrôle humain requis, pas de mesure
statistique de confiance; cache local mono-processus (pas de runs concurrents).
Les endpoints désignent les ressources existantes; les variables abonnement/RG/
région sont indicatives et ne provisionnent ni ne sélectionnent une ressource.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = result.add_subparsers(dest="command", required=True)
    inv = commands.add_parser("inventory", help="Sans Azure : fichiers, SHA256, pages, erreurs")
    inv.add_argument("--input", type=Path, required=True)
    inv.add_argument("--limit", type=int)
    diagnostic = commands.add_parser("plan", help="Diagnostic PDF local sans Azure : routage et doublons")
    diagnostic.add_argument("--input", type=Path, required=True)
    diagnostic.add_argument("--output", type=Path, required=True,
                            help="Rapport JSON privé, jamais une autorisation de traitement")
    diagnostic.add_argument("--cache", type=Path, default=Path(".devis-cache/compet"))
    diagnostic.add_argument("--limit", type=int)
    diagnostic.add_argument("--batch-size", type=int, default=50)
    diagnostic.add_argument("--local-ocr", action="store_true",
                            help="Essayer OCRmyPDF/Tesseract français sans appel Azure")
    batches = commands.add_parser("prepare-batches", help="Préparer des lots privés sans extraction Azure")
    batches.add_argument("--input", type=Path, required=True)
    batches.add_argument("--manifest", type=Path, required=True)
    batches.add_argument("--output", type=Path, required=True)
    batches.add_argument("--pilot-size", type=int, default=50)
    batches.add_argument("--batch-size", type=int, default=100)
    batches.add_argument("--max-pages", type=int, default=500)
    costs = commands.add_parser("costs", help="Lire les coûts de souscription, sans extraction de devis")
    costs.add_argument("--subscription", required=True)
    costs.add_argument("--history", type=Path, default=Path(".devis-cache/azure-costs"))
    costs.add_argument("--output", type=Path, default=Path("outputs/suivi-cout-azure.xlsx"))
    wave = commands.add_parser("wave", help="Vérifier une sélection approuvée ; extraction uniquement avec --allow-cloud")
    wave.add_argument("--manifest", type=Path, default=Path("results/vague-1-selection.json"))
    wave.add_argument("--input", type=Path, default=Path("devis"))
    wave.add_argument("--cache", type=Path, default=Path(".devis-cache/compet-wave-1"))
    wave.add_argument("--output", type=Path, default=Path("outputs/compet.xlsx"))
    wave.add_argument("--allow-cloud", action="store_true")
    wave.add_argument("--continue-on-document-error", action="store_true",
                      help="Reporter les erreurs de validation de document et continuer ; arrêter sur erreur de service")
    wave.add_argument("--defer-source", type=Path, action="append", default=[],
                      help="Reporter explicitement un PDF déjà en erreur sans supprimer l'original")
    wave.add_argument("--max-ocr-pages", type=int, default=55)
    wave.add_argument("--max-llm-calls", type=int, default=32)
    run = commands.add_parser("run", help="Extraction facturable, séquentielle et reprenable")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--template", type=Path, help="Classeur vierge avec A1:G1 exacts; styles conservés")
    run.add_argument("--cache", type=Path, default=Path(".devis-cache"))
    run.add_argument("--limit", type=int, help="Nombre maximal de fichiers, triés par chemin")
    run.add_argument("--allow-cloud", action="store_true", help="Autoriser explicitement OCR et LLM payants")
    run.add_argument("--local-first", action="store_true",
                     help="PDF : tableaux natifs contrôlés, OCR Azure par page en cas de doute")
    run.add_argument("--pdf-only", action="store_true", help="Ignorer tous les formats non-PDF")
    run.add_argument("--local-ocr", action="store_true",
                     help="Après lecture native : OCRmyPDF/Tesseract local, puis Azure si nécessaire")
    run.add_argument("--exclude-dir", action="append", default=["00_ARCHIVES"],
                     help="Nom de dossier exclu récursivement (défaut : 00_ARCHIVES)")
    run.add_argument("--max-ocr-pages", type=int, help="Plafond de pages Azure nouvelles pour ce run")
    run.add_argument("--max-llm-calls", type=int, help="Plafond d'appels IA nouveaux pour ce run")
    offline = commands.add_parser("export", help="Régénérer Excel à partir des résultats validés, sans Azure")
    offline.add_argument("--results", type=Path, default=Path(".devis-cache/results.json"))
    offline.add_argument("--output", type=Path, required=True)
    offline.add_argument("--template", type=Path)
    comparison = commands.add_parser("compare", help="Comparer hors ligne une référence avec les extractions")
    comparison.add_argument("--results", type=Path, default=Path(".devis-cache/results.json"))
    comparison.add_argument("--reference", type=Path, required=True)
    comparison.add_argument("--output", type=Path, required=True)
    for command in (run, offline):
        command.add_argument("--log-dir", type=Path, default=Path("logs"),
                             help="Dossier privé des journaux JSONL (défaut : logs)")
    health = commands.add_parser("serve", help="Sondes HTTP locales optionnelles; aucun appel Azure")
    health.add_argument("--host", default="127.0.0.1")
    health.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    return result


def serve(host: str, port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/healthz", "/readyz"):
                self.send_response(404)
                self.end_headers()
                return
            ready = True
            if self.path == "/readyz":
                try:
                    Settings.from_env()
                except ValueError:
                    ready = False
            data = json.dumps({"status": "ok" if ready else "not_ready",
                               "scope": "configuration locale uniquement, sans validation Azure"}).encode()
            self.send_response(200 if ready else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with ThreadingHTTPServer((host, port), Handler) as server:
        print(f"Sondes locales : http://{host}:{port}/healthz", flush=True)
        server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    journal = None
    try:
        if args.command == "serve":
            serve(args.host, args.port)
            return 0
        if args.command == "inventory":
            rows = inventory(args.input, args.limit)
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            for row in rows:
                if row["status"] != "ready":
                    print(f"{row['source']}: {row['error']}", file=sys.stderr)
            return 1 if any(r["status"] == "error" for r in rows) else 0
        if args.command == "wave":
            from .waves import preflight, run_wave
            plan = preflight(args.manifest, args.input)
            if not args.allow_cloud:
                print(json.dumps({k: v for k, v in plan.items() if k != "records"},
                                 ensure_ascii=False))
                print("Vérification locale uniquement ; aucun appel Azure.")
                return 0
            settings = Settings.from_env()
            summary = run_wave(args.manifest, args.input, args.cache, args.output, settings,
                               max_pages=args.max_ocr_pages, max_calls=args.max_llm_calls,
                               defer_sources=tuple(str(p.resolve()) for p in args.defer_source),
                               continue_on_document_error=args.continue_on_document_error)
            print(json.dumps(summary, ensure_ascii=False))
            return 1 if summary["errors"] or summary["wave_status"] == "paused" else 0
        if args.command == "prepare-batches":
            from .batching import prepare_batches, save_batch_plan
            root = args.input.resolve()
            if args.manifest.suffix.lower() != ".json":
                raise ValueError("Manifeste .json requis")
            for target in (args.manifest.resolve(), args.output.resolve()):
                if target == root or root in target.parents or target in root.parents:
                    raise ValueError("Placer le manifeste et le classeur hors du dossier d'entrée")
            if args.manifest.exists() or args.output.exists():
                raise ValueError("Les sorties de planification ne doivent pas remplacer des fichiers existants")
            report = prepare_batches(root, pilot_size=args.pilot_size, batch_size=args.batch_size,
                                     max_pages=args.max_pages)
            save_batch_plan(report, args.manifest, args.output)
            print(json.dumps({k: v for k, v in report.items() if k not in {"records", "batches"}},
                             ensure_ascii=False))
            print(f"Lots : {len(report['batches'])} ; classeur : {args.output}")
            return 1 if report["inventory_errors"] else 0
        if args.command == "costs":
            from .costs import monitor_costs
            if args.output.resolve() in {args.history.resolve(), (args.history / "latest.json").resolve(),
                                         (args.history / "last-success.json").resolve()}:
                raise ValueError("La sortie des coûts ne peut remplacer le suivi JSON")
            if args.output.suffix.lower() != ".xlsx":
                raise ValueError("Classeur .xlsx requis pour le suivi des coûts")
            snapshot = monitor_costs(args.subscription, args.history, args.output)
            print(json.dumps({k: v for k, v in snapshot.items() if k != "rows"}, ensure_ascii=False))
            return 1 if snapshot["status"] != "ok" else 0
        if args.command == "plan":
            from .planning import plan
            root = args.input.resolve()
            if not root.is_dir():
                raise ValueError("Le diagnostic attend un dossier d'entrée")
            for target in (args.output.resolve(), args.cache.resolve()):
                if target == root or root in target.parents or target in root.parents:
                    raise ValueError("Rapport et cache doivent être hors du dossier d'entrée")
            if args.output.suffix.lower() != ".json" or args.output.exists():
                raise ValueError("Utiliser un nouveau rapport .json, sans écraser de fichier existant")
            report = plan(args.input, args.cache, args.limit, args.batch_size, local_ocr=args.local_ocr)
            save(args.output, report)
            print(json.dumps({k: v for k, v in report.items() if k != "records"}, ensure_ascii=False))
            return 1 if report["inventory_errors"] or report["native_errors"] else 0
        if args.command == "compare":
            if args.output.resolve() == args.results.resolve():
                raise ValueError("La comparaison ne peut remplacer le cache")
            batch = Batch.model_validate(load(args.results))
            summary = compare(batch, args.reference, args.output)
            print(json.dumps({"output": str(args.output), **summary}, ensure_ascii=False))
            return 0
        validate_standalone_output(args.output)
        journal_path = args.log_dir / (args.output.stem + ".events.jsonl")
        if args.command == "run":
            if not args.allow_cloud:
                raise ValueError("Appels Azure refusés : ajouter --allow-cloud après accord sur les coûts")
            validate_destination(args.output, args.template)
            # Cache/output must never be placed among input documents.
            root = args.input.resolve()
            cache = args.cache.resolve()
            if cache == root or cache in root.parents or (root.is_dir() and root in cache.parents):
                raise ValueError("Dossiers d'entrée et de cache : aucun chevauchement autorisé")
            if root.is_dir():
                for candidate in (cache, args.output.resolve(), journal_path.resolve(),
                                  journal_path.with_suffix(".csv").resolve()):
                    if candidate == root or root in candidate.parents:
                        raise ValueError("Placer cache et sortie en dehors du dossier d'entrée")
            settings = Settings.from_env()
            from .limits import CloudLimits
            for value in (args.max_ocr_pages, args.max_llm_calls):
                if value is not None and value < 0:
                    raise ValueError("Plafonds de consommation positifs ou nuls requis")
            limits = (CloudLimits(args.max_ocr_pages, args.max_llm_calls)
                      if args.max_ocr_pages is not None or args.max_llm_calls is not None else None)
            if root in {journal_path.resolve(), journal_path.with_suffix(".csv").resolve()}:
                raise ValueError("Le journal ne peut pas remplacer une source")
            journal = EventJournal(journal_path)
            journal.emit("run_started", input=str(root), output=str(args.output),
                         extraction_config=settings.extraction_config(), ocr_config=settings.ocr_config())
            batch = process(args.input, args.cache, settings, args.limit, journal=journal, structured=True,
                            local_pdf=args.local_first, pdf_only=args.pdf_only,
                            exclude_dirs=tuple(args.exclude_dir), limits=limits, local_ocr=args.local_ocr)
            save(args.cache / "results.json", batch.model_dump())
            journal.emit("results_saved", path=str(args.cache / "results.json"))
        else:
            if args.output.resolve() == args.results.resolve():
                raise ValueError("La sortie ne peut pas remplacer le cache")
            batch = Batch.model_validate(load(args.results))
            validate_destination(args.output, args.template)
            protected = {args.results.resolve(), *(Path(d.source).resolve() for d in batch.documents)}
            if protected.intersection({journal_path.resolve(), journal_path.with_suffix(".csv").resolve()}):
                raise ValueError("Le journal ne peut pas remplacer une source ou le cache")
            journal = EventJournal(journal_path)
            journal.emit("export_started", results=str(args.results), retrospective=True)
            for document in batch.documents:
                journal.document(document, retrospective=True)
                if document.error:
                    print(f"{document.source}: {document.error}", file=sys.stderr)
        summary = export(batch, args.output, args.template)
        journal.emit("export_completed", output=str(args.output), **summary)
        journal.emit("run_completed", documents=len(batch.documents), **summary)
        print(json.dumps({"output": str(args.output), "journal": str(journal.path), **summary}, ensure_ascii=False))
        if summary["errors"]:
            return 1
        return 0 if summary["accepted_rows"] else 3
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        if journal:
            try:
                journal.emit("run_failed", exception_type=type(exc).__name__)
            except OSError as journal_error:
                print(f"Erreur d'écriture du journal : {journal_error}", file=sys.stderr)
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
