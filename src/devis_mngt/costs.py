from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from .reporting import write_report
from .storage import load, save

API_VERSION = "2025-03-01"
NOTICE = ("Coûts remontés avec retard, potentiellement incomplets ; ni solde de crédit "
          "ni plafond temps réel. Aucune extraction Azure déclenchée.")


def az_json(arguments: list[str]) -> dict:
    try:
        response = subprocess.run(["az", *arguments, "--output", "json"],
                                  capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Délai Azure CLI dépassé pour la lecture des coûts") from exc
    if response.returncode:
        if "429" in response.stderr or "Too Many Requests" in response.stderr:
            raise ValueError("Azure Cost Management limité (429) ; aucun coût déduit de cet échec")
        raise ValueError("Lecture des coûts refusée ou indisponible ; vérifier connexion et droits Cost Management")
    value = json.loads(response.stdout)
    if not isinstance(value, dict):
        raise ValueError("Réponse Azure inattendue")
    return value


def fetch_costs(subscription: str) -> dict:
    subscription = str(uuid.UUID(subscription))
    account = az_json(["account", "show", "--subscription", subscription])
    if account.get("id", "").lower() != subscription:
        raise ValueError("Souscription retournée différente de la souscription demandée")
    prefix = f"/subscriptions/{subscription}/providers/Microsoft.CostManagement/query"
    url = f"https://management.azure.com{prefix}?api-version={API_VERSION}"
    body = {"type": "ActualCost", "timeframe": "MonthToDate",
            "dataset": {"granularity": "Daily",
                        "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
                        "grouping": [{"type": "Dimension", "name": "ServiceName"}]}}
    rows = []
    columns = None
    for _ in range(10):
        response = az_json(["rest", "--method", "post", "--url", url,
                            "--headers", "ClientType=devis-mngt-cost-monitor",
                            "--body", json.dumps(body)])
        properties = response["properties"]
        names = [c["name"] for c in properties["columns"]]
        if columns is not None and columns != names:
            raise ValueError("Colonnes de coûts modifiées entre pages")
        columns = names
        for values in properties["rows"]:
            if len(values) != len(names):
                raise ValueError("Ligne de coût incomplète")
            row = dict(zip(names, values))
            if not {"PreTaxCost", "Currency", "UsageDate", "ServiceName"} <= row.keys():
                raise ValueError("Schéma de coût non pris en charge")
            try:
                amount = Decimal(str(row["PreTaxCost"]))
            except InvalidOperation as exc:
                raise ValueError("Montant Azure non numérique") from exc
            if not amount.is_finite():
                raise ValueError("Montant Azure non fini")
            row["PreTaxCost"] = str(amount)
            rows.append(row)
        url = properties.get("nextLink")
        if not url:
            break
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "management.azure.com"
                or parsed.path.lower() != prefix.lower() or parsed.fragment):
            raise ValueError("Lien de pagination Azure hors souscription refusé")
    else:
        raise ValueError("Trop de pages de coûts ; instantané incomplet refusé")
    totals = {}
    for row in rows:
        currency = row["Currency"]
        totals[currency] = str(Decimal(totals.get(currency, "0")) + Decimal(row["PreTaxCost"]))
    return {"subscription_id": subscription, "subscription_name": account["name"],
            "status": "ok" if rows else "no_data", "notice": NOTICE,
            "period": datetime.now(timezone.utc).strftime("%Y-%m"),
            "totals_by_currency": totals, "rows": rows}


def monitor_costs(subscription: str, directory: Path, workbook: Path) -> dict:
    subscription = str(uuid.UUID(subscription))
    timestamp = datetime.now(timezone.utc)
    try:
        snapshot = fetch_costs(subscription)
    except (ValueError, OSError, KeyError) as exc:
        snapshot = {"subscription_id": subscription, "status": "error", "error": str(exc),
                    "notice": NOTICE, "totals_by_currency": {}, "rows": []}
    snapshot["checked_at"] = timestamp.isoformat()
    latest = directory / "latest.json"
    previous = directory / "last-success.json"
    last = load(previous) if previous.exists() else None
    if last and last["subscription_id"] != subscription:
        raise ValueError("Répertoire de suivi déjà lié à une autre souscription")
    if snapshot["status"] == "ok":
        if last and snapshot["period"] == last["period"]:
            snapshot["delta_since_last_check"] = {
                currency: str(Decimal(total) - Decimal(last["totals_by_currency"][currency]))
                for currency, total in snapshot["totals_by_currency"].items()
                if currency in last["totals_by_currency"]}
        save(previous, snapshot)
        last = snapshot
    save(directory / "history" / (timestamp.strftime("%Y%m%dT%H%M%S%fZ") + ".json"), snapshot)
    save(latest, snapshot)
    effective = last or snapshot
    summary = [["Indicateur", "Valeur"], ["Souscription", subscription],
               ["Vérification UTC", snapshot["checked_at"]], ["État", snapshot["status"]],
               ["Interprétation", "Aucune donnée retournée ; coût inconnu, pas zéro"
                if snapshot["status"] == "no_data" else snapshot["status"]],
               ["Erreur", snapshot.get("error")], ["Limites", NOTICE],
               ["Dernière lecture réussie UTC", last["checked_at"] if last else None],
               ["Période du dernier coût connu", last["period"] if last else None],
               ["Crédit disponible", "Non fourni par cette API ; 128,99 EUR était la référence utilisateur"],
               *[[f"Coût MTD ({currency})", amount]
                 for currency, amount in effective["totals_by_currency"].items()]]
    detail = [["Date de consommation", "Service", "Coût avant taxes", "Devise"]]
    detail += [[r["UsageDate"], r["ServiceName"], r["PreTaxCost"], r["Currency"]]
               for r in effective["rows"]]
    write_report(workbook, {"Suivi": summary, "Coûts par service": detail})
    return snapshot
