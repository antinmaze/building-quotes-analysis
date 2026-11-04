import unittest
from unittest.mock import MagicMock

from devis_mngt.chunks import page_batches, read_chunks, normalize_chunks
from devis_mngt.cloud import AzureBackend
from devis_mngt.models import OCR
from devis_mngt.limits import LimitExceeded
from test_devis import LocalFiles, StorageAndPipelineTests
from test_reading import reading, normalization


def document():
    return OCR(content="\n".join(f"Page {p}" for p in range(1, 8)),
               page_texts={str(p): f"Page {p}" for p in range(1, 8)},
               page_count=7, raw={})


def partial(pages):
    result = reading()
    result.lines[0].source_pages = [pages[-1]]
    return result


class ChunkTests(LocalFiles):
    def test_two_page_batches_and_replay_without_calls(self):
        ocr = document()
        backend = MagicMock()
        backend.read_quote.side_effect = lambda ocr, pages, document_context: partial(pages)
        backend.normalize_quote.side_effect = lambda *a, **kw: normalization()
        settings = StorageAndPipelineTests.settings
        raw = read_chunks(backend, ocr, self.directory, settings)
        self.assertEqual([line.source_pages for line in raw.lines], [[2], [4], [6], [7]])
        self.assertEqual(
            [c.kwargs["pages"] for c in backend.read_quote.call_args_list],
            [[1, 2], [3, 4], [5, 6], [7]])
        self.assertNotIn("lines", backend.read_quote.call_args_list[1].kwargs["document_context"])
        norm = normalize_chunks(backend, raw, "test.pdf", ocr, self.directory, settings)
        self.assertEqual([line.line_index for line in norm.lines], [1, 2, 3, 4])
        for call in backend.normalize_quote.call_args_list:
            self.assertLessEqual(len(call.args[2].page_texts), 2)
            self.assertEqual(len(call.args[0].lines), 1)
        backend.reset_mock()
        self.assertEqual(read_chunks(backend, ocr, self.directory, settings), raw)
        self.assertEqual(normalize_chunks(
            backend, raw, "test.pdf", ocr, self.directory, settings), norm)
        backend.read_quote.assert_not_called()
        backend.normalize_quote.assert_not_called()

    def test_completed_chunks_survive_an_interrupted_document(self):
        ocr = document()
        backend = MagicMock()
        backend.read_quote.side_effect = [partial([1, 2]), LimitExceeded("stop")]
        settings = StorageAndPipelineTests.settings
        with self.assertRaises(LimitExceeded):
            read_chunks(backend, ocr, self.directory, settings)
        backend.reset_mock()
        backend.read_quote.side_effect = lambda ocr, pages, document_context: partial(pages)
        result = read_chunks(backend, ocr, self.directory, settings)
        self.assertEqual(len(result.lines), 4)
        self.assertEqual([c.kwargs["pages"] for c in backend.read_quote.call_args_list],
                         [[3, 4], [5, 6], [7]])

    def test_out_of_block_line_is_rejected(self):
        backend = MagicMock()
        backend.read_quote.return_value = partial([7])
        with self.assertRaisesRegex(ValueError, "hors du bloc"):
            read_chunks(backend, document(), self.directory, StorageAndPipelineTests.settings)

    def test_metadata_conflicts_remain_visible(self):
        first, second, third, fourth = (
            partial([2]), partial([4]), partial([6]), partial([7]))
        second.company_raw = "Autre entreprise"
        backend = MagicMock()
        backend.read_quote.side_effect = [first, second, third, fourth]
        result = read_chunks(backend, document(), self.directory, StorageAndPipelineTests.settings)
        self.assertTrue(any("company_raw" in reason for reason in result.blockers))

    def test_normalization_does_not_drop_lines(self):
        raw = partial([3])
        backend = MagicMock()
        bad = normalization()
        bad.lines = []
        backend.normalize_quote.return_value = bad
        with self.assertRaisesRegex(ValueError, "manquantes"):
            normalize_chunks(backend, raw, "test.pdf", document(), self.directory,
                             StorageAndPipelineTests.settings)

    def test_first_block_company_is_kept_with_audit_and_cached_replay(self):
        ocr = document()
        backend = MagicMock()
        backend.read_quote.side_effect = lambda ocr, pages, document_context: partial(pages)
        settings = StorageAndPipelineTests.settings
        raw = read_chunks(backend, ocr, self.directory, settings)
        parts = [normalization() for _ in range(4)]
        for part, company in zip(parts, ["AB énergies", "AB Energies", None, "Autre entreprise"]):
            part.company = company
        backend.normalize_quote.side_effect = parts
        result = normalize_chunks(backend, raw, "test.pdf", ocr, self.directory, settings)
        self.assertEqual(result.company, "AB énergies")
        self.assertEqual([line.line_index for line in result.lines], [1, 2, 3, 4])
        self.assertTrue(any("AB Energies" in w for w in result.lines[1].warnings))
        self.assertTrue(any("Autre entreprise" in w for w in result.lines[3].warnings))
        backend.reset_mock()
        self.assertEqual(normalize_chunks(
            backend, raw, "test.pdf", ocr, self.directory, settings), result)
        backend.normalize_quote.assert_not_called()

    def test_missing_first_block_company_is_not_invented_from_later_pages(self):
        ocr = document()
        backend = MagicMock()
        backend.read_quote.side_effect = lambda ocr, pages, document_context: partial(pages)
        settings = StorageAndPipelineTests.settings
        raw = read_chunks(backend, ocr, self.directory, settings)
        parts = [normalization() for _ in range(4)]
        parts[0].company = None
        backend.normalize_quote.side_effect = parts
        result = normalize_chunks(backend, raw, "test.pdf", ocr, self.directory, settings)
        self.assertIsNone(result.company)

    def test_first_block_client_is_kept_with_audit_and_cached_replay(self):
        ocr = document()
        backend = MagicMock()
        backend.read_quote.side_effect = lambda ocr, pages, document_context: partial(pages)
        settings = StorageAndPipelineTests.settings
        raw = read_chunks(backend, ocr, self.directory, settings)
        parts = [normalization() for _ in range(4)]
        parts[0].project_client = "Bouey-Degueurce"
        parts[1].project_client = "Degueurce-Bouey"
        parts[2].project_client = None
        parts[3].project_client = "Blend"
        backend.normalize_quote.side_effect = parts
        result = normalize_chunks(backend, raw, "test.pdf", ocr, self.directory, settings)
        self.assertEqual(result.project_client, "Bouey-Degueurce")
        self.assertEqual([line.line_index for line in result.lines], [1, 2, 3, 4])
        self.assertTrue(any("Degueurce-Bouey" in w for w in result.lines[1].warnings))
        self.assertTrue(any("Blend" in w for w in result.lines[3].warnings))
        backend.reset_mock()
        self.assertEqual(normalize_chunks(
            backend, raw, "test.pdf", ocr, self.directory, settings), result)
        backend.normalize_quote.assert_not_called()

    def test_missing_first_block_client_is_not_invented_from_later_pages(self):
        ocr = document()
        backend = MagicMock()
        backend.read_quote.side_effect = lambda ocr, pages, document_context: partial(pages)
        settings = StorageAndPipelineTests.settings
        raw = read_chunks(backend, ocr, self.directory, settings)
        parts = [normalization() for _ in range(4)]
        parts[0].project_client = None
        backend.normalize_quote.side_effect = parts
        result = normalize_chunks(backend, raw, "test.pdf", ocr, self.directory, settings)
        self.assertIsNone(result.project_client)
        self.assertTrue(any("Client du bloc" in w for w in result.lines[1].warnings))

    def test_cross_block_recovery_is_not_sent_as_a_larger_request(self):
        raw = partial([3])
        raw.lines[0].source_pages = [2, 3]
        backend = MagicMock()
        with self.assertRaisesRegex(ValueError, "continuité"):
            normalize_chunks(backend, raw, "test.pdf", document(), self.directory,
                             StorageAndPipelineTests.settings)
        backend.normalize_quote.assert_not_called()

    def test_payload_filters_pages_preserving_table_ids(self):
        ocr = document()
        ocr.raw = {"tables": [
            {"cells": [{"rowIndex": 0, "columnIndex": 0, "content": f"Cell {p}",
                        "boundingRegions": [{"pageNumber": p, "polygon": [0, 0, 1, 1]}]}]}
            for p in range(1, 8)
        ]}
        backend = AzureBackend(StorageAndPipelineTests.settings)
        backend.structured = MagicMock()
        backend.read_quote(ocr, pages=[4, 5], document_context={"company_raw": "Atelier"})
        payload = backend.structured.call_args.args[1]
        self.assertEqual(list(payload["page_texts"]), ["4", "5"])
        self.assertEqual([row["id"] for row in payload["tables"]], ["T4R0", "T5R0"])
        self.assertEqual(payload["untrusted_document_context"]["company_raw"], "Atelier")

    def test_noncontiguous_source_pages_are_not_renumbered(self):
        ocr = document()
        ocr.page_texts = {"2": "deux", "5": "cinq", "8": "huit", "12": "douze"}
        ocr.page_count = 4
        self.assertEqual(page_batches(ocr), [[2, 5], [8, 12]])
