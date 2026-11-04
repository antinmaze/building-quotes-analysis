from openpyxl import load_workbook

from devis_mngt.excel import export, TRACE_HEADERS
from devis_mngt.quality import assess, assessment_context, corroborated_unit
from test_devis import LocalFiles, sample, batch


class QualityTests(LocalFiles):
    def test_cached_assessment_context_preserves_quality_result(self):
        doc = sample()
        line = doc.extraction.lines[0]
        self.assertEqual(assess(doc, line), assess(doc, line, assessment_context(doc)))

    def test_corroborated_unit_requires_price_anchor_and_matching_total(self):
        doc = sample(designation="Prise de courant")
        line = doc.extraction.lines[0]
        line.unit_basis = "interpreted"
        line.normalized_unit = "U"
        line.quantity, line.line_total_ht = "2", "50.00"
        line.table_row_ids = ["T1R1"]
        doc.ocr.raw = {"tables": [{"cells": [
            {"rowIndex": 0, "columnIndex": 0, "content": "PU HT", "kind": "columnHeader"},
            {"rowIndex": 1, "columnIndex": 0, "content": "25,00"},
        ]}]}
        self.assertEqual(assess(doc, line).score, .95)
        self.assertTrue(assess(doc, line).accepted)
        line.line_total_ht = "99.00"
        self.assertFalse(assess(doc, line).accepted)
        self.assertIsNone(corroborated_unit(doc, line))

    def test_perfect_missing_price_and_interpreted_units(self):
        doc = sample()
        line = doc.extraction.lines[0]
        self.assertEqual(assess(doc, line).score, 1)
        self.assertTrue(assess(doc, line).accepted)
        line.unit_basis = "interpreted"
        line.normalized_unit = "U"
        self.assertEqual(assess(doc, line).score, .85)
        self.assertFalse(assess(doc, line).accepted)
        line.unit_price_ht = None
        self.assertEqual(assess(doc, line).price_score, .05)
        self.assertLess(assess(doc, line).score, .1)

    def test_price_not_in_source_stays_near_zero(self):
        doc = sample()
        doc.extraction.lines[0].unit_price_ht = "999.00"
        quality = assess(doc, doc.extraction.lines[0])
        self.assertEqual(quality.score, .05)
        self.assertFalse(quality.accepted)

    def test_exclusive_routing_with_all_sources_in_trace(self):
        good, suspect, bad = sample(), sample(), sample()
        suspect.source = "suspect.pdf"
        suspect.extraction.lines[0].unit_basis = "interpreted"
        suspect.extraction.lines[0].normalized_unit = "U"
        bad.source = "bad.pdf"
        bad.extraction.lines[0].unit_price_ht = None
        output = self.directory / "classified.xlsx"
        summary = export(batch(good, suspect, bad), output)
        self.assertEqual(summary["accepted_rows"], 1)
        self.assertEqual(summary["review_rows"], 2)
        book = load_workbook(output)
        try:
            self.assertEqual(book.active.title, "Base de prix")
            self.assertEqual(book["Base de prix"].cell(2, 8).value, 1)
            trace = list(book["Traçabilité"].values)[1:]
            destination = TRACE_HEADERS.index("Destination")
            score = TRACE_HEADERS.index("Score fiabilité")
            mapping = TRACE_HEADERS.index("Ligne Base de prix")
            for row in trace:
                self.assertEqual(bool(row[mapping]), row[destination] == "Base de prix")
                self.assertEqual(row[score] >= .9, row[destination] == "Base de prix")
            self.assertEqual(len(trace), 3)
        finally:
            book.close()
