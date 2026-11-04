from __future__ import annotations

import subprocess
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from devis_mngt.cli import main
from devis_mngt.cloud import AzureBackend, Settings
from devis_mngt.limits import CloudLimits, LimitExceeded
from devis_mngt.local_ocr import engine_config, searchable_copy
from devis_mngt.local_pdf import (
    local_first, merge_pages, native_page, number, page_data, prepare_pdf,
)
from devis_mngt.models import OCR
from devis_mngt.storage import inventory
from test_devis import LocalFiles


def fake_page(price="10,00", total="20,00"):
    values = [["Description", "Qté", "P.U. HT", "Montant HT"],
              ["Pose de peinture", "2", price, total]]
    table = SimpleNamespace(
        bbox=(0, 0, 400, 100), extract=lambda: values,
        rows=[SimpleNamespace(cells=[(c * 100, r * 50, (c + 1) * 100, (r + 1) * 50)
                                    for c in range(4)]) for r in range(2)],
    )
    return SimpleNamespace(
        page_number=1, width=600, height=800, images=[],
        extract_text=lambda: "Entreprise et client - Devis 2026\n" + "Pose de peinture 2 10,00 20,00 HT\n" * 3,
        find_tables=lambda: [table],
        extract_words=lambda: [{"text": price, "x0": 210, "x1": 240, "top": 60, "bottom": 70}],
    )


class LocalPdfTests(LocalFiles):
    def test_native_table_acceptance_arithmetic_and_spans(self):
        data = page_data(fake_page())
        self.assertIsNone(data["reason"])
        ocr = native_page(data)
        for cell in ocr.raw["tables"][0]["cells"]:
            span = cell["spans"][0]
            self.assertEqual(ocr.content[span["offset"]:span["offset"] + span["length"]], cell["content"])
        self.assertIsNotNone(page_data(fake_page(total="99,00"))["reason"])
        self.assertEqual(number("1 234,56"), Decimal("1234.56"))
        self.assertIsNone(number("1,2,3"))

    def test_scanned_page_requires_ocr_and_bad_outside_price_requires_review(self):
        page = fake_page()
        page.images = [{"x0": 0, "x1": 600, "top": 0, "bottom": 800}]
        self.assertIn("Image", page_data(page)["reason"])
        self.assertIsNone(page_data(page, ocr_layer=True)["reason"])
        page.extract_words = lambda: [{"text": "99,00", "x0": 500, "x1": 530, "top": 600, "bottom": 620}]
        self.assertIn("hors tableau", page_data(page, ocr_layer=True)["reason"])

    def test_merge_adjusts_spans_and_preserves_original_page_numbers(self):
        first = native_page(page_data(fake_page()))
        page = fake_page()
        page.page_number = 4
        second = native_page(page_data(page))
        merged = merge_pages([first, second])
        self.assertEqual(set(merged.page_texts), {"1", "4"})
        for table in merged.raw["tables"]:
            for cell in table["cells"]:
                s = cell["spans"][0]
                self.assertEqual(merged.content[s["offset"]:s["offset"] + s["length"]], cell["content"])
        with self.assertRaises(ValueError):
            merge_pages([first, first])

    def test_cloud_fallback_pages_are_cached(self):
        source = self.pdf()
        page = page_data(fake_page(total="99"))
        page["method"] = "tesseract"
        backend = MagicMock()
        backend.ocr.return_value = OCR(content="text", page_texts={"1": "text"}, page_count=1, raw={})
        with patch("devis_mngt.local_pdf.prepare_pdf", return_value=[page]):
            for _ in range(2):
                local_first(source, backend, self.directory / "cache", "hash", {})
        backend.ocr.assert_called_once_with(source, pages=[1])

    def test_ocr_unavailable_is_explicit_no_cloud_fallback(self):
        with patch("devis_mngt.local_ocr.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ValueError, "indisponible"):
                engine_config()

    def test_missing_french_model_is_not_silently_replaced_by_english(self):
        responses = [
            SimpleNamespace(stdout="17"),
            SimpleNamespace(stdout="tesseract 5\n"),
            SimpleNamespace(stdout="List of available languages:\neng\nosd\n"),
        ]
        with patch("devis_mngt.local_ocr.subprocess.run", side_effect=responses):
            with self.assertRaisesRegex(ValueError, "françaises"):
                engine_config()

    def test_ocr_success_is_cached_verified_and_original_unchanged(self):
        source = self.pdf()
        original = source.read_bytes()

        def execute(command, **kwargs):
            from pathlib import Path
            Path(command[-1]).write_bytes(original)
            self.assertNotIn("shell", kwargs)
            self.assertIn("--pages", command)
            return SimpleNamespace(returncode=0, stderr="OCR completed")

        with patch("devis_mngt.local_ocr.subprocess.run", side_effect=execute) as run:
            first = searchable_copy(source, self.directory / "cache", [1], {"language": "fra"})
            second = searchable_copy(source, self.directory / "cache", [1], {"language": "fra"})
            self.assertEqual(first, second)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(source.read_bytes(), original)
            first.write_bytes(b"corrupted")
            with self.assertRaisesRegex(ValueError, "intégrité"):
                searchable_copy(source, self.directory / "cache", [1], {"language": "fra"})

    def test_only_rejected_pages_are_ocr_processed(self):
        from devis_mngt.local_pdf import page_data
        from contextlib import nullcontext
        source = self.pdf(pages=2)
        good = page_data(fake_page())
        bad = {**good, "number": 2, "reason": "scan", "needs_ocr": True}
        p2 = fake_page()
        p2.page_number = 2
        with (
            patch("devis_mngt.local_pdf.inspect_pdf", return_value=[good, bad]),
            patch("devis_mngt.local_ocr.searchable_copy", return_value=source) as copy,
            patch("devis_mngt.local_pdf.pdfplumber.open",
                  return_value=nullcontext(SimpleNamespace(pages=[None, p2]))),
        ):
            pages = prepare_pdf(source, self.directory / "cache", use_ocr=True, config={"engine": "test"})
        copy.assert_called_once_with(source, self.directory / "cache", [2], {"engine": "test"})
        self.assertEqual([p["method"] for p in pages], ["native", "tesseract"])

    def test_native_table_ambiguity_does_not_rasterize_correct_text(self):
        data = page_data(fake_page(total="99"))
        with (
            patch("devis_mngt.local_pdf.inspect_pdf", return_value=[data]),
            patch("devis_mngt.local_ocr.searchable_copy") as copy,
        ):
            pages = prepare_pdf(self.pdf(), self.directory / "cache", use_ocr=True)
        copy.assert_not_called()
        self.assertEqual(pages[0]["method"], "native")

    def test_ocr_failure_has_private_log_and_preserves_original(self):
        source = self.pdf()
        before = source.read_bytes()
        response = SimpleNamespace(returncode=1, stderr="failed")
        with patch("devis_mngt.local_ocr.subprocess.run", return_value=response):
            with self.assertRaisesRegex(ValueError, "OCR local échoué"):
                searchable_copy(source, self.directory / "cache", [1], {})
        self.assertEqual(before, source.read_bytes())
        self.assertFalse(list((self.directory / "cache" / "local-ocr").glob("*.pdf")))
        self.assertTrue(list((self.directory / "cache" / "local-ocr").glob("*.log")))

    def test_limits_stop_before_excess_calls(self):
        limits = CloudLimits(1, 1)
        limits.reserve_ocr(1)
        limits.reserve_llm()
        with self.assertRaises(LimitExceeded):
            limits.reserve_ocr(1)
        with self.assertRaises(LimitExceeded):
            limits.reserve_llm()
        self.assertEqual((limits.ocr_pages, limits.llm_calls), (1, 1))

    def test_azure_page_selection_and_stop_before_sdk_call(self):
        source = self.pdf(pages=4)
        client = MagicMock()
        client.begin_analyze_document.return_value.result.return_value.as_dict.return_value = {
            "content": "Devis page quatre",
            "pages": [{"pageNumber": 4, "spans": [{"offset": 0, "length": 17}]}],
        }
        backend = AzureBackend(Settings("https://di.example", "https://ai.example", "test"),
                               di_client=client, limits=CloudLimits(max_ocr_pages=1))
        result = backend.ocr(source, pages=[4])
        self.assertEqual(set(result.page_texts), {"4"})
        self.assertEqual(client.begin_analyze_document.call_args.kwargs["pages"], "4")
        with self.assertRaises(LimitExceeded):
            backend.ocr(source, pages=[1])
        self.assertEqual(client.begin_analyze_document.call_count, 1)

    def test_text_presence_is_not_price_line_validation(self):
        from scripts.evaluate_local_ocr import evidence, prices
        self.assertIn(Decimal("2571.85"), prices("Total 2 571,85 €"))
        self.assertEqual(evidence("Ouvrage absent", "2571.85", ["Total 2 571,85 €"]), (True, 0))

    def test_pdf_inventory_prunes_archive_and_other_formats_before_limit(self):
        source = self.pdf()
        folder = self.directory / "input"
        (folder / "Client" / "00_ARCHIVES").mkdir(parents=True)
        (folder / "Client" / "00_ARCHIVES" / "old.pdf").write_bytes(source.read_bytes())
        (folder / "Client" / "a.docx").write_bytes(b"skip")
        (folder / "Client" / "devis.pdf").write_bytes(source.read_bytes())
        rows = inventory(folder, 1, pdf_only=True, exclude_dirs=("00_ARCHIVES",))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["source"].endswith("devis.pdf"))

    def test_plan_has_no_cloud_dependency(self):
        self.pdf()
        with patch("devis_mngt.cloud.AzureBackend") as backend:
            code = main(["plan", "--input", str(self.directory), "--output",
                         str(self.directory.parent / (self.directory.name + ".json")),
                         "--cache", str(self.directory.parent / (self.directory.name + "-cache"))])
        # Output and cache are outside input; clean only the paths created by this test.
        from devis_mngt.storage import load
        import shutil
        report = self.directory.parent / (self.directory.name + ".json")
        cache = self.directory.parent / (self.directory.name + "-cache")
        try:
            self.assertEqual(code, 0)
            self.assertEqual(load(report)["azure_fallback_pages"], 1)
            backend.assert_not_called()
        finally:
            report.unlink(missing_ok=True)
            if cache.exists():
                shutil.rmtree(cache)
