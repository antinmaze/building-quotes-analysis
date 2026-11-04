from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import load_workbook

from devis_mngt.batching import prepare_batches, save_batch_plan
from devis_mngt.cli import main
from devis_mngt.costs import az_json, fetch_costs, monitor_costs
from devis_mngt.reporting import write_report
from devis_mngt.storage import load
from test_devis import LocalFiles

SUBSCRIPTION = "5bf8447b-64d2-449a-906e-98a615367c83"


class BatchTests(LocalFiles):
    def test_unique_documents_assigned_once_copies_and_errors_retained(self):
        root = self.directory / "input"
        root.mkdir()
        records = []
        for i in range(8):
            records.append({"source": str(root / f"client-{i % 2}" / f"{i}.pdf"),
                            "sha256": str(i), "pages": 2, "status": "ready", "error": None})
        records += [{**records[0], "source": str(root / "third-client" / "copy.pdf")},
                    {"source": str(root / "client-0" / "bad.pdf"),
                     "sha256": "bad", "pages": None, "status": "error", "error": "invalid"}]
        with patch("devis_mngt.batching.inventory", return_value=records) as inventory:
            report = prepare_batches(root, pilot_size=2, batch_size=3, max_pages=5)
        inventory.assert_called_once_with(root, pdf_only=True, exclude_dirs=("00_ARCHIVES",))
        assigned = [sha for b in report["batches"] for sha in b["sha256"]]
        self.assertEqual(sorted(assigned), [str(i) for i in range(8)])
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(report["duplicate_copies"], 1)
        self.assertEqual(report["inventory_errors"], 1)
        self.assertEqual(len(report["batches"][0]["clients"]), 2)
        self.assertTrue(all(b["pages"] <= 5 for b in report["batches"]))
        self.assertFalse(report["cloud_authorized"])
        manifest, workbook = self.directory / "plan.json", self.directory / "plan.xlsx"
        save_batch_plan(report, manifest, workbook)
        self.assertEqual(load(manifest)["unique_pages"], 16)
        book = load_workbook(workbook)
        self.assertEqual(book["Fichiers"].max_row, 11)
        book.close()
        with self.assertRaises(ValueError):
            save_batch_plan(report, manifest, workbook)

    def test_large_pdf_is_isolated_and_none_are_lost(self):
        root = self.directory / "input"
        root.mkdir()
        records = [{"source": str(root / "client" / f"{i}.pdf"), "sha256": str(i),
                    "pages": pages, "status": "ready", "error": None}
                   for i, pages in enumerate([600, 2, 3])]
        with patch("devis_mngt.batching.inventory", return_value=records):
            report = prepare_batches(root, pilot_size=1, batch_size=100, max_pages=500)
        self.assertEqual(sum(b["documents"] for b in report["batches"]), 3)
        self.assertTrue(any(b["pages"] == 600 and b["documents"] == 1 for b in report["batches"]))

    def test_report_escapes_formula_and_remains_private(self):
        path = self.directory / "report.xlsx"
        write_report(path, {"Example": [["Source"], ["=test"], ["@test"]]})
        book = load_workbook(path)
        self.assertEqual(book.active["A2"].data_type, "s")
        self.assertTrue(book.active["A2"].value.startswith("'"))
        book.close()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cli_refuses_source_overlap_without_scanning(self):
        with patch("devis_mngt.batching.prepare_batches") as prepare:
            status = main(["prepare-batches", "--input", str(self.directory),
                           "--manifest", str(self.directory / "manifest.json"),
                           "--output", str(self.directory / "lots.xlsx")])
        self.assertEqual(status, 2)
        prepare.assert_not_called()


class CostTests(LocalFiles):
    def response(self, rows=None, next_link=None):
        return {"properties": {
            "columns": [{"name": name} for name in ("PreTaxCost", "UsageDate", "ServiceName", "Currency")],
            "rows": rows if rows is not None else [[1.2, 20261015, "Azure OpenAI", "EUR"]],
            "nextLink": next_link}}

    def test_explicit_subscription_pagination_and_currency_separation(self):
        link = (f"https://management.azure.com/subscriptions/{SUBSCRIPTION}"
                "/providers/Microsoft.CostManagement/query?api-version=2025-03-01&$skiptoken=x")
        with patch("devis_mngt.costs.az_json", side_effect=[
            {"id": SUBSCRIPTION, "name": "Test"},
            self.response(next_link=link),
            self.response([[2.3, 20261016, "Cognitive Services", "EUR"],
                           [5, 20261016, "Other", "USD"]]),
        ]) as az:
            result = fetch_costs(SUBSCRIPTION)
        self.assertEqual(result["totals_by_currency"], {"EUR": "3.5", "USD": "5"})
        self.assertIn("--subscription", az.call_args_list[0].args[0])
        self.assertEqual(result["status"], "ok")

    def test_failure_keeps_last_success_and_never_reports_zero(self):
        directory, workbook = self.directory / "costs", self.directory / "costs.xlsx"
        good = {"subscription_id": SUBSCRIPTION, "status": "ok", "period": "2026-10",
                "totals_by_currency": {"EUR": "12.34"}, "rows": []}
        with patch("devis_mngt.costs.fetch_costs", return_value=good):
            monitor_costs(SUBSCRIPTION, directory, workbook)
        with patch("devis_mngt.costs.fetch_costs", side_effect=ValueError("Azure 429")):
            failed = monitor_costs(SUBSCRIPTION, directory, workbook)
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["totals_by_currency"], {})
        self.assertEqual(load(directory / "last-success.json")["totals_by_currency"], {"EUR": "12.34"})
        self.assertEqual(load(directory / "latest.json")["status"], "error")

    def test_empty_cost_data_is_not_free_usage(self):
        with patch("devis_mngt.costs.az_json", side_effect=[
            {"id": SUBSCRIPTION, "name": "Test"}, self.response([]),
        ]):
            result = fetch_costs(SUBSCRIPTION)
        self.assertEqual(result["status"], "no_data")
        self.assertEqual(result["totals_by_currency"], {})

    def test_pagination_cannot_leave_subscription(self):
        with patch("devis_mngt.costs.az_json", side_effect=[
            {"id": SUBSCRIPTION, "name": "Test"},
            self.response(next_link="https://example.com/query"),
        ]):
            with self.assertRaisesRegex(ValueError, "hors souscription"):
                fetch_costs(SUBSCRIPTION)

    def test_rate_limit_is_explicit_not_a_successful_zero(self):
        with patch("devis_mngt.costs.subprocess.run", return_value=SimpleNamespace(
            returncode=1, stdout="", stderr="ERROR: 429 Too Many Requests",
        )):
            with self.assertRaisesRegex(ValueError, "429"):
                az_json(["rest"])

    def test_delta_is_only_within_same_period_and_currency(self):
        directory, workbook = self.directory / "costs", self.directory / "costs.xlsx"
        first = {"subscription_id": SUBSCRIPTION, "status": "ok", "period": "2026-10",
                 "totals_by_currency": {"EUR": "1.25"}, "rows": []}
        second = {**first, "totals_by_currency": {"EUR": "2.35", "USD": "4"}}
        with patch("devis_mngt.costs.fetch_costs", side_effect=[first, second]):
            monitor_costs(SUBSCRIPTION, directory, workbook)
            result = monitor_costs(SUBSCRIPTION, directory, workbook)
        self.assertEqual(result["delta_since_last_check"], {"EUR": "1.10"})
        third = {**first, "period": "2026-11"}
        with patch("devis_mngt.costs.fetch_costs", return_value=third):
            result = monitor_costs(SUBSCRIPTION, directory, workbook)
        self.assertNotIn("delta_since_last_check", result)
