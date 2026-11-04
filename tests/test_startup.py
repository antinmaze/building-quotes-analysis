import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openpyxl import load_workbook

from devis_mngt.cli import main
from devis_mngt.limits import LimitExceeded
from devis_mngt.startup import azure, consolidate, deploy_infrastructure, integrate_pdf, workspace_lock
from devis_mngt.storage import load
from devis_mngt.waves import workbook_hash
from test_devis import LocalFiles, sample
from test_reading import normalization, reading


SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"


class StartupTests(LocalFiles):
    def deployment_files(self):
        parameters = self.directory / "parameters.json"
        parameters.write_text(json.dumps({"parameters": {
            "expectedSubscriptionId": {"value": SUBSCRIPTION},
            "resourceGroupName": {"value": "rg-example"},
            "location": {"value": "francecentral"},
        }}))
        template = self.directory / "main.bicep"
        template.write_text("targetScope = 'subscription'\n")
        return parameters, template, self.directory / "settings.json"

    def deployment_responses(self, *, groups=True, changes=None):
        return [
            {"id": SUBSCRIPTION},
            {"groups": [{"name": "rg-example", "location": "francecentral"}] if groups else []},
            {"status": "Succeeded", "changes": changes if changes is not None else [
                {"changeType": "Create"},
            ]},
        ]

    def deployed(self):
        outputs = {
            "subscriptionGuardPassed": True, "resourceGroupLocationMatches": True,
            "azureDocumentIntelligenceEndpoint": "https://new-di.example",
            "azureOpenAiEndpoint": "https://new-ai.example",
            "azureOpenAiDeployment": "new-quotes",
            "azureOpenAiApiVersion": "2024-10-21",
            "azureOpenAiModelRevision": "test-version",
            "azureTenantId": "test-tenant",
        }
        return {"properties": {"provisioningState": "Succeeded",
                               "outputs": {k: {"value": v} for k, v in outputs.items()}}}

    def test_infrastructure_preview_never_deploys_or_writes_settings(self):
        paths = self.deployment_files()
        with patch("devis_mngt.startup.azure", side_effect=self.deployment_responses()) as az:
            result = deploy_infrastructure(*paths, apply=False, create_resource_group=False)
        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["changes"], {"Create": 1})
        self.assertFalse(paths[2].exists())
        self.assertFalse(any("create" in call.args[0] for call in az.call_args_list))

    def test_absent_group_preview_is_explicitly_not_a_successful_what_if(self):
        paths = self.deployment_files()
        with patch("devis_mngt.startup.azure", side_effect=self.deployment_responses(groups=False)[:2]) as az:
            result = deploy_infrastructure(*paths, apply=False, create_resource_group=True)
        self.assertEqual(result["status"], "resource_group_required")
        self.assertEqual(az.call_count, 2)
        self.assertFalse(paths[2].exists())

    def test_deploy_creates_missing_group_only_with_explicit_permission(self):
        paths = self.deployment_files()
        responses = self.deployment_responses(groups=False)
        responses.insert(2, {})
        responses.append(self.deployed())
        with patch("devis_mngt.startup.azure", side_effect=responses) as az:
            result = deploy_infrastructure(*paths, apply=True, create_resource_group=True)
        self.assertEqual(result["status"], "deployed")
        self.assertEqual(az.call_args_list[2].args[0][:2], ["group", "create"])
        self.assertEqual(az.call_args_list[3].args[0][:3], ["deployment", "sub", "what-if"])
        self.assertEqual(az.call_args_list[4].args[0][:3], ["deployment", "sub", "create"])
        settings = load(paths[2])
        self.assertEqual(settings["di_endpoint"], "https://new-di.example")
        self.assertEqual(settings["deployment"], "new-quotes")
        self.assertEqual(paths[2].stat().st_mode & 0o777, 0o600)

    def test_delete_preview_blocks_deployment(self):
        paths = self.deployment_files()
        responses = self.deployment_responses(changes=[{"changeType": "Delete"}])
        with patch("devis_mngt.startup.azure", side_effect=responses) as az:
            with self.assertRaisesRegex(ValueError, "Suppression"):
                deploy_infrastructure(*paths, apply=True, create_resource_group=False)
        self.assertEqual(az.call_count, 3)
        self.assertFalse(paths[2].exists())

    def test_missing_group_requires_permission_even_with_apply(self):
        paths = self.deployment_files()
        with patch("devis_mngt.startup.azure", side_effect=self.deployment_responses(groups=False)[:2]):
            with self.assertRaisesRegex(ValueError, "Groupe absent"):
                deploy_infrastructure(*paths, apply=True, create_resource_group=False)

    def test_existing_settings_are_never_overwritten(self):
        paths = self.deployment_files()
        paths[2].write_text("existing configuration")
        with patch("devis_mngt.startup.azure") as az:
            with self.assertRaisesRegex(ValueError, "sans écrasement"):
                deploy_infrastructure(*paths, apply=True, create_resource_group=False)
        az.assert_not_called()
        self.assertEqual(paths[2].read_text(), "existing configuration")

    def test_wrong_subscription_or_region_blocks_before_preview(self):
        paths = self.deployment_files()
        for responses in (
            [{"id": "22222222-2222-2222-2222-222222222222"}],
            [{"id": SUBSCRIPTION},
             {"groups": [{"name": "rg-example", "location": "westeurope"}]}],
        ):
            with self.subTest(responses=responses), \
                 patch("devis_mngt.startup.azure", side_effect=responses) as az:
                with self.assertRaises(ValueError):
                    deploy_infrastructure(*paths, apply=True, create_resource_group=True)
            self.assertFalse(any("create" in call.args[0] for call in az.call_args_list))

    def test_azure_failure_is_explicit_and_does_not_log_raw_response(self):
        with patch("devis_mngt.startup.subprocess.run", return_value=SimpleNamespace(
            returncode=1, stderr="sensitive raw response", stdout="",
        )):
            with self.assertRaisesRegex(ValueError, "Commande Azure échouée") as error:
                azure(["deployment", "sub", "create"])
        self.assertNotIn("sensitive raw response", str(error.exception))

    def test_local_preflight_needs_no_settings_or_workspace(self):
        source = self.pdf()
        workspace = self.directory / "workspace"
        with patch("devis_mngt.startup.Settings.from_env", side_effect=AssertionError("No Azure")), \
             patch("devis_mngt.startup.process") as run:
            result = integrate_pdf(source, workspace, None, allow_cloud=False,
                                   max_ocr_pages=50, max_llm_calls=100)
        self.assertEqual(result["status"], "preflight")
        self.assertFalse(workspace.exists())
        run.assert_not_called()

    def test_full_cli_flow_reuses_cache_and_consolidates_multiple_pdfs_offline(self):
        parameters, template, settings = self.deployment_files()
        with patch("devis_mngt.startup.azure",
                   side_effect=[*self.deployment_responses(), self.deployed()]), \
             contextlib.redirect_stdout(io.StringIO()):
            code = main(["infra", "--parameters", str(parameters), "--template", str(template),
                         "--settings", str(settings), "--apply"])
        self.assertEqual(code, 0)
        workspace = self.directory / "workspace"
        first, second = self.pdf("first.pdf"), self.pdf("second.pdf", pages=2)
        backend = MagicMock()
        backend.ocr.return_value = sample().ocr
        backend.read_quote.return_value = reading()
        backend.normalize_quote.return_value = normalization()
        with patch("devis_mngt.pipeline.AzureBackend", return_value=backend), \
             contextlib.redirect_stdout(io.StringIO()):
            for source in (first, first):
                self.assertEqual(main(["integrate", "--input", str(source), "--workspace", str(workspace),
                                       "--settings", str(settings), "--allow-cloud"]), 0)
            backend.ocr.return_value = sample().ocr.model_copy(update={
                "page_count": 2, "page_texts": {"1": sample().ocr.content, "2": ""},
            })
            self.assertEqual(main(["integrate", "--input", str(second), "--workspace", str(workspace),
                                   "--settings", str(settings), "--allow-cloud"]), 0)
        self.assertEqual(backend.ocr.call_count, 2)
        self.assertEqual(backend.read_quote.call_count, 2)
        self.assertEqual(len(list((workspace / "documents").glob("*.json"))), 2)
        with patch("devis_mngt.startup.Settings.from_env", side_effect=AssertionError("No Azure")), \
             patch("devis_mngt.startup.process", side_effect=AssertionError("No analysis")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["consolidate", "--workspace", str(workspace)]), 0)
        result = load(workspace / "latest-report.json")
        self.assertEqual(result["status"], "consolidated")
        self.assertEqual(result["documents"], {"extracted": 2})
        self.assertEqual(result["accepted_rows"], 1)
        self.assertEqual(result["extracted_lines"], 2)
        for name, metadata in result["reports"].items():
            path = Path(metadata["path"])
            self.assertEqual(workbook_hash(path), metadata["sha256"])
            book = load_workbook(path, read_only=True, data_only=True)
            try:
                if name == "prices.xlsx":
                    self.assertEqual(book["Documents"].max_row, 3)
                    self.assertEqual(book["Traçabilité"].max_row, 3)
                else:
                    self.assertEqual(book["Documents à examiner"].max_row, 1)
            finally:
                book.close()
        again = consolidate(workspace)
        self.assertNotEqual(again["reports"]["prices.xlsx"]["path"], result["reports"]["prices.xlsx"]["path"])
        self.assertTrue(Path(result["reports"]["prices.xlsx"]["path"]).exists())

    def test_unreadable_pdf_is_recorded_and_reported_without_cloud_calls(self):
        source = self.directory / "broken.pdf"
        source.write_bytes(b"not a pdf")
        workspace = self.directory / "workspace"
        with patch("devis_mngt.startup.Settings.from_env", side_effect=AssertionError("No Azure")), \
             patch("devis_mngt.startup.process") as run, \
             contextlib.redirect_stderr(io.StringIO()):
            result = integrate_pdf(source, workspace, None, allow_cloud=True,
                                   max_ocr_pages=50, max_llm_calls=100)
        self.assertEqual(result["status"], "document_error")
        run.assert_not_called()
        report = consolidate(workspace)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["accepted_rows"], 0)
        book = load_workbook(report["reports"]["errors.xlsx"]["path"], read_only=True, data_only=True)
        try:
            self.assertEqual(book["Documents à examiner"].max_row, 2)
            self.assertIn("Pdf", book["Documents à examiner"].cell(2, 15).value)
        finally:
            book.close()

    def test_limit_is_recorded_for_consolidation_and_resume(self):
        source = self.pdf()
        workspace = self.directory / "workspace"
        with patch("devis_mngt.startup.Settings.from_env", return_value=MagicMock()), \
             patch("devis_mngt.startup.process", side_effect=LimitExceeded("Limite atteinte")), \
             contextlib.redirect_stdout(io.StringIO()):
            code = main(["integrate", "--input", str(source), "--workspace", str(workspace), "--allow-cloud"])
        self.assertEqual(code, 2)
        report = consolidate(workspace)
        self.assertEqual(report["documents"], {"error": 1})

    def test_empty_or_locked_workspace_is_not_modified(self):
        workspace = self.directory / "workspace"
        workspace.mkdir()
        with self.assertRaisesRegex(ValueError, "Aucun PDF"):
            consolidate(workspace)
        with workspace_lock(workspace):
            with self.assertRaisesRegex(ValueError, "verrouillé"):
                consolidate(workspace)
            self.assertTrue((workspace / "operation.lock").exists())
        self.assertFalse((workspace / "operation.lock").exists())

    def test_failed_report_generation_keeps_previous_published_pair(self):
        source = self.pdf()
        workspace = self.directory / "workspace"
        backend = MagicMock()
        backend.ocr.return_value = sample().ocr
        backend.read_quote.return_value = reading()
        backend.normalize_quote.return_value = normalization()
        from test_devis import StorageAndPipelineTests
        with patch("devis_mngt.startup.Settings.from_env", return_value=StorageAndPipelineTests.settings), \
             patch("devis_mngt.pipeline.AzureBackend", return_value=backend):
            integrate_pdf(source, workspace, None, allow_cloud=True,
                          max_ocr_pages=50, max_llm_calls=100)
        first = consolidate(workspace)
        manifest_bytes = (workspace / "latest-report.json").read_bytes()
        with patch("devis_mngt.startup.write_report", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                consolidate(workspace)
        self.assertEqual((workspace / "latest-report.json").read_bytes(), manifest_bytes)
        self.assertFalse((workspace / "operation.lock").exists())
        for metadata in first["reports"].values():
            self.assertEqual(workbook_hash(Path(metadata["path"])), metadata["sha256"])
