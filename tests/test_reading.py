from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock
from openpyxl import Workbook

from devis_mngt.models import reasons
from devis_mngt.reading import (
    Reading, ReadLine, Normalization, NormalizedWork, assemble, coverage, priced_rows,
    complete_reading,
)
from devis_mngt.pipeline import process
from test_devis import LocalFiles, StorageAndPipelineTests, sample, batch
from devis_mngt.excel import export
from devis_mngt.comparison import compare
from devis_mngt.models import HEADERS
from devis_mngt.projects import resolve_projects


def reading():
    return Reading(
        classification="devis", classification_reason="Devis HT",
        company_raw="=Atelier", project_client_raw="+Client", billing_client_raw="Facturation SARL",
        year=2025, company_excerpts=["=Atelier"], project_excerpts=["+Client"],
        year_excerpts=["2025"], blockers=[], warnings=["TVA nulle"],
        lines=[ReadLine(
            raw_designation="Pose de peinture", raw_unit="U", unit_price_ht="25.00",
            quantity=None, line_total_ht=None, source_pages=[1],
            source_excerpt="Pose de peinture U 25,00 HT", table_row_ids=[], section="Peinture",
            option_variant=None, blockers=[], warnings=[],
        )],
    )


def normalization():
    return Normalization(company="Atelier", project_client="Client", main_trade="Peinture", lines=[NormalizedWork(
        line_index=1, designation="Pose de peinture", technical_signature="peinture pose",
        trade="Peinture", unit="U", unit_basis="explicit", blockers=[], warnings=[],
    )])


class ReadingTests(LocalFiles):
    def test_validated_normalization_keeps_full_source_evidence(self):
        from devis_mngt.cloud import AzureBackend
        backend = AzureBackend(StorageAndPipelineTests.settings)
        backend.structured = MagicMock(return_value=normalization())
        raw = reading()
        raw.lines[0].table_row_ids = ["T1R2"]
        backend.normalize_quote(raw, "devis.pdf", sample().ocr)
        payload = backend.structured.call_args.args[1]
        self.assertEqual(payload["untrusted_reading"]["lines"][0],
                         {"line_index": 1, **raw.lines[0].model_dump()})
        self.assertEqual(backend.settings.normalization_config()["normalization"], "works-2")

    def test_filename_company_requires_source_identity_and_evidence(self):
        doc = sample()
        doc.source = "2025 - Client - Atelier - devis.pdf"
        doc.extraction = assemble(reading(), normalization(), doc.ocr)
        doc.extraction.company = "ATELIER"
        resolve_projects([doc])
        self.assertEqual(doc.extraction.company, "Atelier")
        doc.source = "2025 - Client - Inconnue - devis.pdf"
        resolve_projects([doc])
        self.assertEqual(doc.extraction.company, "Atelier")
        self.assertTrue(any("Entreprise du nom" in w for w in doc.extraction.warnings))

    def test_independent_evidence_billing_and_nonblocking_warnings(self):
        doc = sample()
        doc.extraction = assemble(reading(), normalization(), doc.ocr)
        self.assertEqual(reasons(doc, doc.extraction.lines[0]), [])
        self.assertEqual(doc.extraction.billing_client, "Facturation SARL")
        self.assertEqual(doc.extraction.warnings, ["TVA nulle"])
        doc.extraction.client = "Autre"
        self.assertTrue(any("Client" in r for r in reasons(doc, doc.extraction.lines[0])))

    def test_metadata_raw_identity_can_span_multiple_independent_fragments(self):
        doc = sample()
        raw = reading()
        raw.project_client_raw = "+Client 51 rue Test 44000 Nantes"
        raw.project_excerpts = ["+Client", "51 rue Test", "44000 Nantes"]
        doc.ocr.content += "\n51 rue Test\n44000 Nantes"
        doc.ocr.page_texts["1"] = doc.ocr.content
        doc.extraction = assemble(raw, normalization(), doc.ocr)
        self.assertEqual(reasons(doc, doc.extraction.lines[0]), [])

    def test_cross_document_project_requires_shared_beneficiary_address_and_filename(self):
        donor, target = sample(), sample()
        donor.source = "2026 - Client - Enterprise - devis.pdf"
        target.source = "2026 - Client - Other - devis.pdf"
        raw = reading()
        raw.project_client_raw = "+Client"
        raw.project_excerpts = ["+Client\n51 rue Test 44000 Nantes"]
        for d in (donor, target):
            d.ocr.content += "\n+Client\n51 rue Test 44000 Nantes"
            d.ocr.page_texts["1"] = d.ocr.content
            d.extraction = assemble(raw, normalization(), d.ocr)
        target.extraction.client = None
        target.extraction.client_raw = "51 rue Test 44000 Nantes"
        self.assertEqual(resolve_projects([donor, target]), [target])
        batch(donor, target)  # verifies linked source SHA, address and identity evidence
        self.assertEqual(reasons(target, target.extraction.lines[0]), [])
        target.extraction.project_link["address"] = "99 rue Autre 75000 Paris"
        with self.assertRaises(ValueError):
            batch(donor, target)

    def test_filename_client_replaces_billing_name_only_with_document_evidence(self):
        doc = sample()
        doc.source = "2026-01-01 - Client - Atelier - devis.pdf"
        doc.extraction = assemble(reading(), normalization(), doc.ocr)
        doc.extraction.client = "Facturation"
        resolve_projects([doc])
        self.assertEqual(doc.extraction.client, "Client")
        self.assertEqual(reasons(doc, doc.extraction.lines[0]), [])
        doc.source = "2026-01-01 - Inconnu - Atelier - devis.pdf"
        resolve_projects([doc])
        self.assertTrue(any("nom de fichier" in r for r in reasons(doc, doc.extraction.lines[0])))

    def test_no_dropped_or_duplicated_normalized_lines(self):
        norm = normalization()
        norm.lines = []
        with self.assertRaises(ValueError):
            assemble(reading(), norm, sample().ocr)
        norm.lines = normalization().lines * 2
        with self.assertRaises(ValueError):
            assemble(reading(), norm, sample().ocr)

    def test_trade_schema_does_not_allow_section_titles(self):
        data = normalization().lines[0].model_dump()
        with self.assertRaises(ValueError):
            NormalizedWork.model_validate({**data, "trade": "INSTALLATION DE CHANTIER"})

    def test_interpreted_unit_is_visible_not_a_numeric_quantity(self):
        raw, norm = reading(), normalization()
        raw.lines[0].raw_unit = None
        norm.lines[0].unit_basis = "interpreted"
        doc = sample()
        doc.extraction = assemble(raw, norm, doc.ocr)
        self.assertEqual(reasons(doc, doc.extraction.lines[0]), [])
        self.assertTrue(doc.extraction.lines[0].warnings)
        raw.lines[0].raw_unit = "1,00"
        doc.extraction = assemble(raw, norm, doc.ocr)
        self.assertTrue(reasons(doc, doc.extraction.lines[0]))

    def test_unit_without_raw_evidence_cannot_be_labeled_explicit(self):
        raw, norm = reading(), normalization()
        raw.lines[0].raw_unit = None
        doc = sample()
        doc.extraction = assemble(raw, norm, doc.ocr)
        self.assertEqual(doc.extraction.lines[0].unit_basis, "interpreted")
        self.assertTrue(doc.extraction.lines[0].warnings)

    def test_coverage_detects_unaccounted_price_cells(self):
        ocr = sample().ocr
        ocr.raw = {"tables": [{"cells": [
            {"rowIndex": 0, "columnIndex": 1, "content": "P.U. H.T.", "kind": "columnHeader"},
            {"rowIndex": 1, "columnIndex": 0, "content": "Pose de peinture"},
            {"rowIndex": 1, "columnIndex": 1, "content": "25,00"},
        ]}]}
        self.assertEqual(priced_rows(ocr)[0]["price"], "25.00")
        ext = assemble(reading(), normalization(), ocr)
        self.assertEqual(coverage(ocr, ext)[0]["row_id"], "T1R1")
        ext.lines[0].table_row_ids = ["T1R1"]
        self.assertEqual(coverage(ocr, ext), [])
        ocr.raw["tables"][0]["cells"] += [
            {"rowIndex": 2, "columnIndex": 0, "content": "Total peinture"},
            {"rowIndex": 2, "columnIndex": 1, "content": "500,00"},
        ]
        self.assertEqual(len(priced_rows(ocr)), 1)

    def test_french_reading_numbers_normalize_without_inventing(self):
        data = reading().lines[0].model_dump()
        line = ReadLine.model_validate({**data, "unit_price_ht": "2 571,85"})
        self.assertEqual(line.unit_price_ht, "2571.85")
        for invalid in ("1,234.56", "NaN", "1 23,40"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ReadLine.model_validate({**data, "unit_price_ht": invalid})

    def test_recover_missing_explicit_table_row_without_calculating_prices(self):
        doc = sample()
        text = "Designation\nPU HT\nPose de peinture\n25,00"
        doc.ocr.content = text
        doc.ocr.page_texts = {"1": text}
        doc.ocr.raw = {"tables": [{"cells": [
            {"rowIndex": 0, "columnIndex": 0, "content": "Désignation", "kind": "columnHeader"},
            {"rowIndex": 0, "columnIndex": 1, "content": "PU HT", "kind": "columnHeader"},
            {"rowIndex": 1, "columnIndex": 0, "content": "Pose de peinture",
             "spans": [{"offset": text.index("Pose"), "length": len("Pose de peinture")}],
             "boundingRegions": [{"pageNumber": 1}]},
            {"rowIndex": 1, "columnIndex": 1, "content": "25,00",
             "spans": [{"offset": text.index("25,00"), "length": 5}],
             "boundingRegions": [{"pageNumber": 1}]},
        ]}]}
        raw = reading()
        raw.lines = []
        completed, recovered = complete_reading(raw, doc.ocr)
        self.assertEqual(recovered, ["T1R1"])
        self.assertEqual(completed.lines[0].unit_price_ht, "25.00")
        self.assertIsNone(completed.lines[0].raw_unit)
        again, recovered = complete_reading(completed, doc.ocr)
        self.assertEqual(recovered, [])
        self.assertEqual(len(again.lines), 1)

    def test_continuation_requires_matching_columns_and_consecutive_pages(self):
        ocr = sample().ocr
        def cell(row, col, text, page, header=False):
            return {
                "rowIndex": row, "columnIndex": col, "content": text,
                "kind": "columnHeader" if header else "content",
                "boundingRegions": [{"pageNumber": page, "polygon": [col, 0, col + .5, 0, col + .5, 1, col, 1]}],
            }
        ocr.raw = {"tables": [
            {"columnCount": 2, "cells": [cell(0, 0, "Désignation", 1, True),
                                        cell(0, 1, "PU HT", 1, True)]},
            {"columnCount": 2, "cells": [cell(0, 0, "Pose de peinture", 2),
                                        cell(0, 1, "25,00", 2)]},
        ]}
        self.assertEqual(priced_rows(ocr)[0]["id"], "T2R0")
        for cell_data in ocr.raw["tables"][1]["cells"]:
            cell_data["boundingRegions"][0]["pageNumber"] = 4
        self.assertEqual(priced_rows(ocr), [])

    def test_normalization_signature_groups_only_equal_prices_and_options(self):
        first, other = sample(), sample()
        first.extraction.lines[0].technical_signature = "même ouvrage"
        other.extraction.lines[0].technical_signature = "même ouvrage"
        other.extraction.lines[0].designation = "Autre formulation"
        result = export(batch(first, other), self.directory / "base.xlsx")
        self.assertEqual(result["accepted_rows"], 1)
        other.extraction.lines[0].option_variant = "Option"
        result = export(batch(first, other), self.directory / "base.xlsx")
        self.assertEqual(result["accepted_rows"], 2)

    def test_identical_raw_labels_merge_different_normalizations_with_all_sources(self):
        first, other = sample(), sample()
        first.extraction.lines[0].technical_signature = "peinture pose"
        other.extraction.lines[0].technical_signature = "pose peinture au mur"
        other.extraction.lines[0].designation = "Peinture appliquée"
        other.source = "other.pdf"
        result = export(batch(first, other), self.directory / "base.xlsx")
        self.assertEqual(result["accepted_rows"], 1)
        self.assertEqual(result["extracted_lines"], 2)

    def test_composite_lot_keeps_technical_classification_in_audit(self):
        raw, norm = reading(), normalization()
        norm.main_trade = "Plâtrerie - Isolation"
        norm.lines[0].trade = "Menuiseries intérieures"
        ext = assemble(raw, norm, sample().ocr)
        self.assertEqual(ext.lines[0].trade, "Plâtrerie - Isolation")
        self.assertEqual(ext.lines[0].technical_trade, "Menuiseries intérieures")

    def test_lost_technical_dimensions_block_and_different_features_never_merge(self):
        doc = sample(designation="Pose de peinture 60mm")
        raw, norm = reading(), normalization()
        raw.lines[0].raw_designation = "Pose de peinture 60mm"
        raw.lines[0].source_excerpt = doc.extraction.lines[0].source_excerpt
        doc.extraction = assemble(raw, norm, doc.ocr)
        self.assertIn("60mm", doc.extraction.lines[0].designation)
        doc.extraction.lines[0].designation = "Pose de peinture"
        self.assertTrue(any("techniques perdues" in r for r in reasons(doc, doc.extraction.lines[0])))
        first, other = sample(designation="Peinture 60mm"), sample(designation="Peinture 70mm")
        for d in (first, other):
            d.extraction.lines[0].technical_signature = "peinture"
        result = export(batch(first, other), self.directory / "base.xlsx")
        self.assertEqual(result["accepted_rows"], 2)

    def test_comparison_is_source_scoped_not_price_only_across_documents(self):
        reference = self.directory / "reference.xlsx"
        book = Workbook()
        base = book.active
        base.title = "Base de prix"
        base.append(HEADERS)
        values = ["Peinture", "Pose de peinture", "U", 25, "Atelier", "Client", 2025]
        base.append(values)
        trace = book.create_sheet("Traçabilité")
        trace.append(["Source"] + HEADERS)
        trace.append(["devis.txt"] + values)
        book.save(reference)
        book.close()
        result = compare(batch(sample()), reference, self.directory / "comparison.csv")
        self.assertEqual(result["matches"], {"libellé et prix identiques": 1})
        wrong_source = sample()
        wrong_source.source = "different.pdf"
        result = compare(batch(wrong_source), reference, self.directory / "comparison.csv")
        self.assertEqual(result["matches"], {"source absente": 1})

    def test_separate_stages_are_cached_and_normalization_does_not_modify_prices(self):
        source = self.pdf()
        backend = MagicMock()
        backend.ocr.return_value = sample().ocr
        backend.read_quote.return_value = reading()
        backend.normalize_quote.return_value = normalization()
        cache = self.directory / "cache"
        for _ in range(2):
            result = process(source, cache, StorageAndPipelineTests.settings,
                             backend=backend, structured=True)
        self.assertEqual(backend.ocr.call_count, 1)
        self.assertEqual(backend.read_quote.call_count, 1)
        self.assertEqual(backend.normalize_quote.call_count, 1)
        self.assertEqual(result.documents[0].extraction.lines[0].unit_price_ht, "25.00")


if __name__ == "__main__":
    unittest.main()
