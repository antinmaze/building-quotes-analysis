from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .cloud import Settings
from .excel import export, validate_destination
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
Après changement de modèle Azure, augmenter AZURE_OPENAI_MODEL_REVISION.
Aucune intégration Batiprix/BatiChiffrage : éventuel rapprochement futur sous licence.
Codes : 0 succès, 1 erreur fichier, 2 configuration/usage, 3 aucune donnée acceptée.
Les lignes À vérifier ne sont jamais publiées dans Base de prix. Des lignes acceptées
peuvent coexister avec des lignes à vérifier : consulter systématiquement l'audit.
Limites : contexte LLM entier (aucune troncature silencieuse ni découpage); modèle
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
    run = commands.add_parser("run", help="Extraction facturable, séquentielle et reprenable")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--template", type=Path, help="Classeur vierge avec A1:G1 exacts; styles conservés")
    run.add_argument("--cache", type=Path, default=Path(".devis-cache"))
    run.add_argument("--limit", type=int, help="Nombre maximal de fichiers, triés par chemin")
    run.add_argument("--allow-cloud", action="store_true", help="Autoriser explicitement OCR et LLM payants")
    offline = commands.add_parser("export", help="Régénérer Excel à partir des résultats validés, sans Azure")
    offline.add_argument("--results", type=Path, default=Path(".devis-cache/results.json"))
    offline.add_argument("--output", type=Path, required=True)
    offline.add_argument("--template", type=Path)
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
                for candidate in (cache, args.output.resolve()):
                    if candidate == root or root in candidate.parents:
                        raise ValueError("Placer cache et sortie en dehors du dossier d'entrée")
            batch = process(args.input, args.cache, Settings.from_env(), args.limit)
            save(args.cache / "results.json", batch.model_dump())
        else:
            if args.output.resolve() == args.results.resolve():
                raise ValueError("La sortie ne peut pas remplacer le cache")
            batch = Batch.model_validate(load(args.results))
            for document in batch.documents:
                if document.error:
                    print(f"{document.source}: {document.error}", file=sys.stderr)
        summary = export(batch, args.output, args.template)
        print(json.dumps({"output": str(args.output), **summary}, ensure_ascii=False))
        if summary["errors"]:
            return 1
        return 0 if summary["accepted_rows"] else 3
    except (ValueError, OSError, KeyError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
