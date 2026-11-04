from __future__ import annotations

from copy import deepcopy
import contextlib
import io
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from pypdf import PdfReader
from openpyxl import load_workbook

from devis_mngt.models import Batch, OCR
from devis_mngt.cli import main
from devis_mngt.limits import LimitExceeded
from devis_mngt.reading import assemble
from devis_mngt.pipeline import process
from devis_mngt.projects import resolve_projects
from devis_mngt.storage import digest, inventory, load, save
from devis_mngt.waves import SelectedPagesBackend, preflight, run_wave, workbook_hash
from test_devis import LocalFiles, StorageAndPipelineTests, sample
from test_reading import reading, normalization


class WaveTests(LocalFiles):
    def test_cli_wave_uses_external_configuration_without_account_lookup(self):
        root, manifest, _ = self.manifest()
        environment = {
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://new-di.example",
            "AZURE_OPENAI_ENDPOINT": "https://new-openai.example",
            "AZURE_OPENAI_DEPLOYMENT": "new-quotes",
            "AZURE_TENANT_ID": "test-tenant",
            "AZURE_OPENAI_MODEL_REVISION": "test-revision",
        }
        with patch.dict(os.environ, environment, clear=True), \
             patch("devis_mngt.cli.subprocess.run", side_effect=AssertionError("No account lookup")), \
             patch("devis_mngt.waves.run_wave", return_value={
                 "errors": 0, "wave_status": "completed",
             }) as run, contextlib.redirect_stdout(io.StringIO()):
            status = main([
                "wave", "--manifest", str(manifest), "--input", str(root),
                "--cache", str(self.directory / "cache"),
                "--output", str(self.directory / "out.xlsx"), "--allow-cloud",
            ])
        self.assertEqual(status, 0)
        settings = run.call_args.args[4]
        self.assertEqual(settings.di_endpoint, environment["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"])
        self.assertEqual(settings.openai_endpoint, environment["AZURE_OPENAI_ENDPOINT"])
        self.assertEqual(settings.deployment, "new-quotes")
        self.assertEqual(settings.tenant_id, "test-tenant")
        self.assertEqual(settings.model_revision, "test-revision")

    def test_cli_wave_preflight_needs_no_azure_configuration(self):
        root, manifest, _ = self.manifest()
        with patch.dict(os.environ, {}, clear=True), \
             patch("devis_mngt.cli.Settings.from_env", side_effect=AssertionError("No Azure")), \
             patch("devis_mngt.waves.run_wave") as run, \
             contextlib.redirect_stdout(io.StringIO()):
            status = main(["wave", "--manifest", str(manifest), "--input", str(root)])
        self.assertEqual(status, 0)
        run.assert_not_called()

    def test_cli_wave_missing_configuration_stops_before_processing(self):
        root, manifest, _ = self.manifest()
        with patch.dict(os.environ, {}, clear=True), \
             patch("devis_mngt.waves.run_wave") as run, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            status = main([
                "wave", "--manifest", str(manifest), "--input", str(root), "--allow-cloud",
            ])
        self.assertEqual(status, 2)
        self.assertIn("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", stderr.getvalue())
        run.assert_not_called()

    def test_run_wave_allows_explicit_unlimited_cloud_allowances(self):
        from devis_mngt.waves import run_wave
        from unittest.mock import patch
        with patch("devis_mngt.waves.preflight", return_value={"wave": "test"}), \
             patch("devis_mngt.waves.campaign") as campaign:
            campaign.return_value.__enter__.return_value = ({}, self.directory / "campaign.json")
            with patch("devis_mngt.waves._run_wave", return_value={"wave_status": "completed"}) as run:
                result = run_wave(
                    self.directory / "manifest.json", self.directory / "input",
                    self.directory / "cache", self.directory / "output.xlsx",
                    StorageAndPipelineTests.settings, max_pages=None, max_calls=None,
                )
        self.assertEqual(result["wave_status"], "completed")
        self.assertIsNone(run.call_args.kwargs["max_pages"])
        self.assertIsNone(run.call_args.kwargs["max_calls"])

    def test_selected_pages_have_separate_cache_and_original_numbers(self):
        source = self.pdf(pages=3)
        backend = MagicMock()
        ocr = sample().ocr
        ocr.page_texts = {"2": ocr.content}
        backend.ocr.return_value = ocr
        raw = reading()
        raw.lines[0].source_pages = [2]
        backend.read_quote.return_value = raw
        backend.normalize_quote.return_value = normalization()
        cache = self.directory / "cache"
        for _ in range(2):
            result = process(source, cache, StorageAndPipelineTests.settings,
                             backend=backend, structured=True, selected_pages=[2], client_hint="Client")
        self.assertIsNone(result.documents[0].error)
        self.assertEqual(result.documents[0].pages, 3)
        self.assertEqual(result.documents[0].ocr.page_texts, {"2": ocr.content})
        backend.ocr.assert_called_once_with(source, pages=[2])
        self.assertEqual(backend.normalize_quote.call_args.kwargs, {"client_hint": "Client"})
        self.assertEqual(len(list((cache / "normalization").glob("*.json"))), 1)
        other = process(source, cache, StorageAndPipelineTests.settings, backend=backend,
                        structured=True, selected_pages=[1], client_hint="Client")
        self.assertIn("numérotation", other.documents[0].error)

    def test_page_subset_uploaded_only_and_all_regions_remapped(self):
        source = self.pdf(pages=4)
        ocr = OCR(content="source", page_texts={"1": "a", "2": "b"}, page_count=2,
                  raw={"pages": [{"pageNumber": 1}, {"pageNumber": 2}],
                       "tables": [{"cells": [{"boundingRegions": [{"pageNumber": 2}]}]}]})
        uploaded = []

        def fake(_, path, pages=None):
            uploaded.append(path)
            self.assertEqual(len(PdfReader(path).pages), 2)
            self.assertIsNone(pages)
            return ocr

        backend = SelectedPagesBackend(StorageAndPipelineTests.settings, self.directory / "cache")
        with patch("devis_mngt.cloud.AzureBackend.ocr", fake):
            result = backend.ocr(source, [2, 4])
        self.assertEqual(result.page_texts, {"2": "a", "4": "b"})
        self.assertEqual(result.raw["pages"], [{"pageNumber": 2}, {"pageNumber": 4}])
        self.assertEqual(result.raw["tables"][0]["cells"][0]["boundingRegions"][0]["pageNumber"], 4)
        self.assertFalse(uploaded[0].exists())
        self.assertTrue(source.exists())

    def test_client_folder_cannot_override_unsubstantiated_identity(self):
        doc = sample()
        doc.extraction = assemble(reading(), normalization(), doc.ocr)
        resolve_projects([doc], {doc.source: "Inconnu"})
        self.assertNotEqual(doc.extraction.client, "Inconnu")
        self.assertTrue(any("non corroboré" in r for r in doc.extraction.review_reasons))

    def manifest(self):
        root = self.directory / "input"
        client = root / "Client"
        client.mkdir(parents=True)
        source = self.pdf(pages=3)
        target = client / "devis.pdf"
        source.rename(target)
        record = inventory(target)[0]
        rows = [{"source": str(target), "sha256": record["sha256"], "client": "Client",
                 "source_pages": 3, "decision": "retenu", "selected_pages": [2, 3],
                 "reason": "test"}]
        report = {"wave": "Vague 1", "records": rows, "selected_pages": 2,
                  "approval": {"selection_approved": True, "selection_digest": digest(rows)}}
        manifest = self.directory / "manifest.json"
        save(manifest, report)
        return root, manifest, report

    def test_preflight_checks_approval_hash_pages_and_client(self):
        root, manifest, report = self.manifest()
        result = preflight(manifest, root)
        self.assertEqual((result["documents"], result["pages"]), (1, 2))
        report["records"][0]["selected_pages"] = [1]
        save(manifest, report)
        with self.assertRaisesRegex(ValueError, "approuvée"):
            preflight(manifest, root)

    def test_preflight_rejects_modified_sources(self):
        root, manifest, report = self.manifest()
        Path(report["records"][0]["source"]).write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "modifié"):
            preflight(manifest, root)

    def test_wave_preserves_ignored_rows_without_calling_their_sources(self):
        root, manifest, report = self.manifest()
        omitted = deepcopy(report["records"][0])
        omitted.update(source=str(root / "Client" / "annexe.pdf"), decision="écarté",
                       selected_pages=[], reason="conditions générales")
        report["records"].append(omitted)
        report["approval"]["selection_digest"] = digest(report["records"])
        save(manifest, report)
        doc = sample()
        doc.source = report["records"][0]["source"]
        with patch("devis_mngt.waves.process", return_value=Batch(schema_version="1", documents=[doc])) as run:
            run_wave(manifest, root, self.directory / "cache", self.directory / "out.xlsx",
                     StorageAndPipelineTests.settings, max_pages=2, max_calls=2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["selected_pages"], [2, 3])
        self.assertEqual(run.call_args.kwargs["client_hint"], "Client")

    def test_wave_limit_is_paused_with_persisted_audit_not_success(self):
        root, manifest, _ = self.manifest()
        cache = self.directory / "cache"
        with patch("devis_mngt.waves.process", side_effect=LimitExceeded("stop")):
            summary = run_wave(
                manifest, root, cache, self.directory / "paused.xlsx",
                StorageAndPipelineTests.settings, max_pages=1, max_calls=1,
            )
        self.assertEqual(summary["wave_status"], "paused")
        self.assertEqual(load(cache / "results.partial.json")["documents"], [])
        self.assertIn("Non traité", load(cache / "results.json")["documents"][0]["error"])

    def test_wave_existing_output_refused_before_inference(self):
        root, manifest, _ = self.manifest()
        output = self.directory / "validated.xlsx"
        output.write_bytes(b"validated")
        with patch("devis_mngt.waves.SelectedPagesBackend") as backend:
            with self.assertRaisesRegex(ValueError, "nouveau classeur"):
                run_wave(manifest, root, self.directory / "cache", output,
                         StorageAndPipelineTests.settings, max_pages=2, max_calls=2)
        backend.assert_not_called()
        self.assertEqual(output.read_bytes(), b"validated")

    def next_manifest(self, root, report):
        report = deepcopy(report)
        target = root / "Client" / "autre.pdf"
        self.pdf(pages=4).rename(target)
        record = report["records"][0]
        record.update(source=str(target), sha256=inventory(target)[0]["sha256"], source_pages=4)
        report["wave"] = "Vague 2"
        report["approval"]["selection_digest"] = digest(report["records"])
        manifest = self.directory / "manifest-2.json"
        save(manifest, report)
        return manifest, report

    def run_mock_wave(self, root, manifest, report, price="25.00", cache="cache"):
        doc = sample(price=price)
        doc.source = report["records"][0]["source"]
        doc.sha256 = report["records"][0]["sha256"]
        with patch("devis_mngt.waves.process", return_value=Batch(schema_version="1", documents=[doc])) as run:
            summary = run_wave(
                manifest, root, self.directory / cache, self.directory / "compet.xlsx",
                StorageAndPipelineTests.settings, max_pages=2, max_calls=2,
            )
        return summary, run.call_count

    def test_campaign_accumulates_waves_and_replay_does_not_process_again(self):
        root, manifest, report = self.manifest()
        self.run_mock_wave(root, manifest, report)
        second, report2 = self.next_manifest(root, report)
        summary, calls = self.run_mock_wave(root, second, report2, price="30.00", cache="cache-2")
        self.assertEqual((summary["campaign_waves"], calls), (2, 1))
        summary, calls = self.run_mock_wave(root, manifest, report)
        self.assertEqual((summary["campaign_waves"], calls, summary["new_llm_calls"]), (2, 0, 0))
        workbook = load_workbook(self.directory / "compet.xlsx")
        try:
            self.assertEqual(workbook["Base de prix"].max_row, 3)
            self.assertEqual(workbook["Traçabilité"].max_row, 3)
            self.assertEqual({workbook["Base de prix"].cell(r, 4).value for r in (2, 3)}, {25, 30})
        finally:
            workbook.close()
        self.assertFalse((self.directory / "compet.xlsx.lock").exists())

    def test_campaign_groups_same_price_but_preserves_both_sources(self):
        root, manifest, report = self.manifest()
        self.run_mock_wave(root, manifest, report)
        second, report2 = self.next_manifest(root, report)
        self.run_mock_wave(root, second, report2, cache="cache-2")
        workbook = load_workbook(self.directory / "compet.xlsx")
        try:
            self.assertEqual(workbook["Base de prix"].max_row, 2)
            self.assertEqual(workbook["Traçabilité"].max_row, 3)
        finally:
            workbook.close()

    def test_campaign_refuses_overlap_or_modified_workbook_before_calls(self):
        root, manifest, report = self.manifest()
        self.run_mock_wave(root, manifest, report)
        report["wave"] = "Vague 2"
        save(manifest, report)
        with patch("devis_mngt.waves.process") as run:
            with self.assertRaisesRegex(ValueError, "autre vague"):
                run_wave(manifest, root, self.directory / "cache-2", self.directory / "compet.xlsx",
                         StorageAndPipelineTests.settings, max_pages=2, max_calls=2)
            run.assert_not_called()
        output = self.directory / "compet.xlsx"
        output.write_bytes(b"modification manuelle")
        with patch("devis_mngt.waves.SelectedPagesBackend") as backend:
            with self.assertRaisesRegex(ValueError, "modifié"):
                run_wave(manifest, root, self.directory / "cache-2", output,
                         StorageAndPipelineTests.settings, max_pages=2, max_calls=2)
            backend.assert_not_called()
        self.assertEqual(output.read_bytes(), b"modification manuelle")

    def test_campaign_resumes_paused_wave_in_same_workbook(self):
        root, manifest, report = self.manifest()
        with patch("devis_mngt.waves.process", side_effect=LimitExceeded("stop")):
            run_wave(manifest, root, self.directory / "cache", self.directory / "compet.xlsx",
                     StorageAndPipelineTests.settings, max_pages=1, max_calls=1)
        summary, calls = self.run_mock_wave(root, manifest, report)
        self.assertEqual((summary["wave_status"], summary["campaign_waves"], calls), ("completed", 1, 1))
        state = load(self.directory / "compet.campaign.json")
        self.assertEqual(len(state["waves"]["Vague 1"]["batch"]["documents"]), 1)
        self.assertEqual(state["waves"]["Vague 1"]["batch"]["documents"][0]["status"], "extracted")

    def test_campaign_recovers_interrupted_publication_without_losing_waves(self):
        root, manifest, report = self.manifest()
        self.run_mock_wave(root, manifest, report)
        second, report2 = self.next_manifest(root, report)
        from devis_mngt import waves
        replace = waves.os.replace
        output = self.directory / "compet.xlsx"

        def fail_workbook_replace(source, target):
            if Path(target) == output:
                raise OSError("publication interrompue")
            return replace(source, target)

        with patch("devis_mngt.waves.os.replace", side_effect=fail_workbook_replace):
            with self.assertRaisesRegex(OSError, "interrompue"):
                self.run_mock_wave(root, second, report2, price="30.00", cache="cache-2")
        self.assertIn("pending", load(self.directory / "compet.campaign.json"))
        summary, calls = self.run_mock_wave(root, second, report2, cache="cache-2")
        self.assertEqual((summary["campaign_waves"], calls), (2, 0))
        state = load(self.directory / "compet.campaign.json")
        self.assertNotIn("pending", state)
        self.assertEqual(state["workbook_sha256"], workbook_hash(output))
        workbook = load_workbook(output)
        try:
            self.assertEqual(workbook["Base de prix"].max_row, 3)
        finally:
            workbook.close()

    def test_campaign_refuses_concurrent_run_before_calls(self):
        root, manifest, report = self.manifest()
        lock = self.directory / "compet.xlsx.lock"
        lock.write_bytes(b"active")
        with patch("devis_mngt.waves.SelectedPagesBackend") as backend:
            with self.assertRaisesRegex(ValueError, "verrouillée"):
                self.run_mock_wave(root, manifest, report)
            backend.assert_not_called()
        self.assertEqual(lock.read_bytes(), b"active")

    def test_campaign_does_not_overwrite_workbook_edited_during_processing(self):
        root, manifest, report = self.manifest()
        self.run_mock_wave(root, manifest, report)
        second, report2 = self.next_manifest(root, report)
        output = self.directory / "compet.xlsx"
        doc = sample()
        doc.source = report2["records"][0]["source"]

        def edit_during_processing(*args, **kwargs):
            output.write_bytes(b"modification pendant traitement")
            return Batch(schema_version="1", documents=[doc])

        with patch("devis_mngt.waves.process", side_effect=edit_during_processing):
            with self.assertRaisesRegex(ValueError, "publication refusée"):
                run_wave(second, root, self.directory / "cache-2", output,
                         StorageAndPipelineTests.settings, max_pages=2, max_calls=2)
        self.assertEqual(output.read_bytes(), b"modification pendant traitement")

    def test_failed_document_can_be_deferred_and_stays_deferred_on_resume(self):
        root, manifest, report = self.manifest()
        doc = sample()
        doc.source = report["records"][0]["source"]
        doc.status = "error"
        doc.error = "Quantité ambiguë"
        doc.extraction = None
        output = self.directory / "compet.xlsx"
        cache = self.directory / "cache"
        with patch("devis_mngt.waves.process", return_value=Batch(schema_version="1", documents=[doc])):
            summary = run_wave(manifest, root, cache, output, StorageAndPipelineTests.settings,
                               max_pages=2, max_calls=2)
        self.assertEqual(summary["wave_status"], "paused")
        with patch("devis_mngt.waves.process") as run:
            for deferred in ((doc.source,), ()):
                summary = run_wave(
                    manifest, root, cache, output, StorageAndPipelineTests.settings,
                    max_pages=2, max_calls=2, defer_sources=deferred,
                )
            run.assert_not_called()
        self.assertEqual(summary["deferred_documents"], 1)
        self.assertEqual(summary["errors"], 0)
        saved = load(cache / "results.json")["documents"][0]
        self.assertEqual(saved["status"], "unsupported")
        self.assertIn("Quantité ambiguë", saved["error"])
        self.assertTrue(Path(doc.source).exists())

    def test_unfailed_document_cannot_be_deferred(self):
        root, manifest, report = self.manifest()
        with patch("devis_mngt.waves.process") as run:
            with self.assertRaisesRegex(ValueError, "déjà en erreur"):
                run_wave(
                    manifest, root, self.directory / "cache", self.directory / "compet.xlsx",
                    StorageAndPipelineTests.settings, max_pages=2, max_calls=2,
                    defer_sources=(report["records"][0]["source"],),
                )
            run.assert_not_called()

    def test_automatic_validation_deferral_continues_and_is_persistent(self):
        root, manifest, report = self.manifest()
        second, report2 = self.next_manifest(root, report)
        report["records"].extend(report2["records"])
        report["selected_pages"] = 4
        report["approval"]["selection_digest"] = digest(report["records"])
        save(manifest, report)
        failed = sample()
        failed.source = report["records"][0]["source"]
        failed.status, failed.error, failed.extraction = "error", "Validation : PU ambigu", None
        good = sample()
        good.source = report["records"][1]["source"]
        output, cache = self.directory / "compet.xlsx", self.directory / "cache"
        with patch("devis_mngt.waves.process", side_effect=[
                Batch(schema_version="1", documents=[failed]),
                Batch(schema_version="1", documents=[good])]) as run:
            summary = run_wave(manifest, root, cache, output, StorageAndPipelineTests.settings,
                               max_pages=4, max_calls=4, continue_on_document_error=True)
        self.assertEqual(run.call_count, 2)
        self.assertEqual((summary["wave_status"], summary["deferred_documents"], summary["errors"]),
                         ("completed", 1, 0))
        with patch("devis_mngt.waves.process") as run:
            run_wave(manifest, root, cache, output, StorageAndPipelineTests.settings,
                     max_pages=4, max_calls=4)
            run.assert_not_called()

    def test_pdf_read_error_is_deferred_and_next_document_continues(self):
        root, manifest, report = self.manifest()
        _, report2 = self.next_manifest(root, report)
        report["records"].extend(report2["records"])
        report["selected_pages"] = 4
        report["approval"]["selection_digest"] = digest(report["records"])
        save(manifest, report)
        failed = sample()
        failed.source = report["records"][0]["source"]
        failed.status, failed.error, failed.extraction = (
            "error", "PdfReadError: traitement échoué; vérifier configuration, réponse ou cache", None,
        )
        good = sample()
        good.source = report["records"][1]["source"]
        with patch("devis_mngt.waves.process", side_effect=[
                Batch(schema_version="1", documents=[failed]),
                Batch(schema_version="1", documents=[good])]) as run:
            summary = run_wave(
                manifest, root, self.directory / "cache", self.directory / "compet.xlsx",
                StorageAndPipelineTests.settings, max_pages=None, max_calls=None,
                continue_on_document_error=True,
            )
        self.assertEqual(run.call_count, 2)
        self.assertEqual((summary["wave_status"], summary["deferred_documents"], summary["errors"]),
                         ("completed", 1, 0))

    def test_service_errors_are_deferred_and_next_document_continues(self):
        root, manifest, report = self.manifest()
        _, report2 = self.next_manifest(root, report)
        report["records"].extend(report2["records"])
        report["selected_pages"] = 4
        report["approval"]["selection_digest"] = digest(report["records"])
        save(manifest, report)
        failed = sample()
        failed.source = report["records"][0]["source"]
        failed.status, failed.error, failed.extraction = "error", "InternalServerError: service unavailable", None
        good = sample()
        good.source = report["records"][1]["source"]
        with patch("devis_mngt.waves.process", side_effect=[
                Batch(schema_version="1", documents=[failed]),
                Batch(schema_version="1", documents=[good])]) as run:
            summary = run_wave(manifest, root, self.directory / "cache", self.directory / "compet.xlsx",
                               StorageAndPipelineTests.settings, max_pages=2, max_calls=2,
                               continue_on_document_error=True)
        self.assertEqual(run.call_count, 2)
        self.assertEqual((summary["wave_status"], summary["deferred_documents"], summary["errors"]),
                         ("completed", 1, 0))

    def test_rate_limit_document_error_is_deferred_when_requested(self):
        root, manifest, report = self.manifest()
        failed = sample()
        failed.source = report["records"][0]["source"]
        failed.status, failed.error, failed.extraction = "error", "RateLimitError: throttled", None
        with patch("devis_mngt.waves.process", return_value=Batch(schema_version="1", documents=[failed])):
            summary = run_wave(manifest, root, self.directory / "cache", self.directory / "compet.xlsx",
                               StorageAndPipelineTests.settings, max_pages=2, max_calls=2,
                               continue_on_document_error=True)
        self.assertEqual((summary["wave_status"], summary["deferred_documents"], summary["errors"]),
                         ("completed", 1, 0))
