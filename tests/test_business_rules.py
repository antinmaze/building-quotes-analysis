from devis_mngt.business_rules import apply_surface_rule, surface_basis, apply_label_rule
from test_devis import sample, LocalFiles, batch
from devis_mngt.reading import qualify_labour
from devis_mngt.excel import group_keys, identity
from devis_mngt.business_rules import forfait_values, unit_price_values
from devis_mngt.quality import assess
from devis_mngt.excel import export
from openpyxl import load_workbook
from decimal import Decimal
import unittest


class SurfaceRuleTests(unittest.TestCase):
    def line(self, quantity="1", surface="60,17"):
        line = sample().extraction.lines[0]
        line.raw_designation = f"Isolation laine de bois. Surface {surface} M2."
        line.designation = "Isolation laine de bois"
        line.technical_signature = "laine de bois"
        line.raw_unit = None
        line.unit_basis = "interpreted"
        line.normalized_unit = "m²"
        line.quantity = quantity
        return line

    def test_forfait_keeps_surface_and_price_and_is_idempotent(self):
        line = self.line()
        self.assertTrue(apply_surface_rule(line))
        self.assertEqual(line.normalized_unit, "Forfait")
        self.assertIn("pour 60,17 m²", line.designation)
        self.assertIn("pour 60,17 m²", line.technical_signature)
        self.assertEqual(line.unit_price_ht, "25.00")
        data = line.model_dump()
        apply_surface_rule(line)
        self.assertEqual(line.model_dump(), data)

    def test_quantity_equal_to_surface_keeps_square_meter(self):
        line = self.line("14", "14")
        apply_surface_rule(line)
        self.assertEqual(line.normalized_unit, "m²")
        self.assertEqual(line.designation, "Isolation laine de bois")

    def test_conflicts_multiple_surfaces_and_other_quantities_not_inferred(self):
        line = self.line("2")
        self.assertIsNone(surface_basis(line))
        line.quantity = "1"
        line.raw_unit = "M2"
        self.assertIsNone(surface_basis(line))
        line.raw_unit = None
        line.raw_designation += " Surface 20 M2."
        self.assertIsNone(surface_basis(line))


class LabelRuleTests(unittest.TestCase):
    def test_short_source_replaces_model_additions(self):
        line = sample(designation="Passage filerie").extraction.lines[0]
        line.designation = "Passage filerie (forfait)"
        self.assertTrue(apply_label_rule(line))
        self.assertEqual(line.designation, "Passage filerie")
        self.assertEqual(line.designation_before_rules, "Passage filerie (forfait)")
        self.assertFalse(apply_label_rule(line))

    def test_source_installation_scope_and_dimensions_retained(self):
        line = sample(designation="Fourniture et pose trappe 600X600mm").extraction.lines[0]
        apply_label_rule(line)
        self.assertIn("Fourniture et pose", line.designation)
        self.assertIn("600x600mm", line.designation)

    def test_do_not_lose_technical_dimensions_from_normalized_label(self):
        line = sample(designation="Fenêtre").extraction.lines[0]
        line.designation = "Fenêtre 900mm"
        self.assertFalse(apply_label_rule(line))
        self.assertEqual(line.designation, "Fenêtre 900mm")

    def test_surface_forfait_label_stays_idempotent(self):
        line = SurfaceRuleTests().line()
        apply_surface_rule(line)
        apply_label_rule(line)
        first = line.designation
        apply_label_rule(line)
        self.assertEqual(line.designation, first)
        self.assertIn("pour 60,17 m²", line.designation)


class LabourContextTests(unittest.TestCase):
    def document(self, heading=True):
        doc = sample(designation="Main d'oeuvre")
        line = doc.extraction.lines[0]
        line.table_row_ids = ["T1R2"]
        values = [
            ["1.1" if heading else "", "Réseau gaz:" if heading else "", "", ""],
            ["1.1.1", "Tube cuivre", "2", "10"],
            ["1.1.2", "Main d'oeuvre", "1", "25"],
            ["", "Sous-total Réseau gaz:", "", "45"],
        ]
        doc.ocr.raw = {"tables": [{"cells": [
            {"rowIndex": i, "columnIndex": j, "content": text}
            for i, row in enumerate(values) for j, text in enumerate(row)
        ]}]}
        return doc

    def test_heading_and_subtotal_qualify_and_survive_label_cleanup(self):
        doc = self.document()
        line = doc.extraction.lines[0]
        self.assertEqual(qualify_labour([line], doc.ocr), 1)
        self.assertEqual(line.designation, "Main d'oeuvre — Réseau gaz")
        self.assertEqual(len(line.designation_context_evidence), 2)
        apply_label_rule(line)
        self.assertEqual(qualify_labour([line], doc.ocr), 0)
        self.assertEqual(line.unit_price_ht, "25.00")
        self.assertEqual(line.raw_designation, "Main d'oeuvre")

    def test_local_subtotal_fallback(self):
        doc = self.document(heading=False)
        qualify_labour(doc.extraction.lines, doc.ocr)
        self.assertEqual(doc.extraction.lines[0].designation_context, "Réseau gaz")

    def test_next_section_is_not_used(self):
        doc = self.document(heading=False)
        for cell in doc.ocr.raw["tables"][0]["cells"]:
            if cell["rowIndex"] == 3:
                cell["content"] = {0: "1.2", 1: "Douche"}.get(cell["columnIndex"], "")
        qualify_labour(doc.extraction.lines, doc.ocr)
        self.assertIsNone(doc.extraction.lines[0].designation_context)
        self.assertTrue(doc.extraction.lines[0].review_reasons)

    def test_raw_label_alias_cannot_merge_different_sections(self):
        first, second = self.document(), self.document()
        qualify_labour(first.extraction.lines, first.ocr)
        other = second.extraction.lines[0]
        other.designation_context = "Douche"
        entries = [(first.extraction, first.extraction.lines[0]), (second.extraction, other)]
        keys = group_keys(entries)
        self.assertNotEqual(keys[identity(*entries[0])], keys[identity(*entries[1])])


class ForfaitPresentationTests(unittest.TestCase):
    def test_surface_is_priced_for_full_quantity_without_changing_source(self):
        line = sample(price="31.20").extraction.lines[0]
        line.raw_unit = "m²"
        line.quantity = "14"
        line.line_total_ht = "436.80"
        before = line.model_dump()
        label, price, _ = forfait_values(line)
        self.assertIn("pour 14 m²", label)
        self.assertEqual(str(price), "436.80")
        self.assertEqual(line.model_dump(), before)

    def test_quantity_times_pu_when_total_missing_and_dimensions_retained(self):
        line = sample(price="135.00", designation="Trappe 600x600mm").extraction.lines[0]
        line.quantity = "3"
        label, price, _ = forfait_values(line)
        self.assertIn("600x600mm", label)
        self.assertIn("quantité facturée : 3 U", label)
        self.assertEqual(str(price), "405.00")

    def test_missing_quantity_and_total_blocks_non_package_price(self):
        doc = sample()
        doc.extraction.lines[0].export_forfait = True
        self.assertIsNone(forfait_values(doc.extraction.lines[0])[1])
        self.assertFalse(assess(doc, doc.extraction.lines[0]).accepted)

    def test_different_quantities_never_merge_even_with_same_source_pu(self):
        first, second = sample(), sample()
        for doc, quantity in ((first, "2"), (second, "3")):
            line = doc.extraction.lines[0]
            line.export_forfait = True
            line.quantity = quantity
        entries = [(d.extraction, d.extraction.lines[0]) for d in (first, second)]
        keys = group_keys(entries)
        self.assertNotEqual(keys[identity(*entries[0])], keys[identity(*entries[1])])


class UnitPricePresentationTests(LocalFiles):
    def test_approved_duct_package_keeps_pu_and_fractional_quantity(self):
        for diameter, length in (("82", "10"), ("127", "6"), ("160", "6")):
            doc = sample(price="61.34", designation=(
                f"Gaine Vmc Resist'r D{diameter} Iso.25 Pvc/intisse {length}ml Renforcee Re2020"
            ))
            line = doc.extraction.lines[0]
            line.raw_unit = None
            line.normalized_unit = "ml"
            line.unit_basis = "interpreted"
            line.quantity = "0.50"
            line.line_total_ht = "30.67"
            line.export_unit_prices = True
            label, price, target, _ = unit_price_values(line)
            self.assertIn(f"longueur de {length} m", label)
            self.assertEqual((price, target), (Decimal("61.34"), "U"))
            self.assertTrue(assess(doc, line).accepted)
            line.line_total_ht = "999"
            self.assertFalse(assess(doc, line).accepted)

    def test_user_waste_rule_uses_total_and_resolves_unit_not_other_blockers(self):
        doc = sample(price="10.00", designation=(
            "Tri, gestion, évacuation et valorisation ou élimination des déchets. "
            "(carton, plastique, palette et chutes)."
        ))
        line = doc.extraction.lines[0]
        line.raw_unit = None
        line.normalized_unit = "t"
        line.unit_basis = "interpreted"
        line.quantity = "18"
        line.line_total_ht = "180"
        line.export_unit_prices = True
        self.assertEqual(unit_price_values(line)[1:3], (Decimal("180"), "Forfait"))
        self.assertTrue(assess(doc, line).accepted)
        line.line_total_ht = None
        self.assertEqual(unit_price_values(line)[1], Decimal("180"))
        line.line_total_ht = "999"
        self.assertFalse(assess(doc, line).accepted)
        line.line_total_ht = None
        line.quantity = None
        self.assertIsNone(unit_price_values(line)[1])
        self.assertFalse(assess(doc, line).accepted)

    def test_forfait_with_surface_divides_price_and_preserves_raw_data(self):
        line = sample(price="2571.85", designation="Laine de bois. Surface 60,17 M2.").extraction.lines[0]
        line.raw_unit = "Forfait"
        line.quantity = "1"
        line.export_unit_prices = True
        before = line.model_dump()
        label, price, target, formula = unit_price_values(line)
        self.assertEqual(label, line.raw_designation)
        self.assertEqual(price, Decimal("2571.85") / Decimal("60.17"))
        self.assertEqual(target, "m²")
        self.assertIn("60.17", formula)
        self.assertEqual(before, line.model_dump())

    def test_native_square_meter_price_not_divided_twice(self):
        line = sample(price="31.20", designation="Isolation. Surface 14M2").extraction.lines[0]
        line.raw_unit = "m²"
        line.quantity = "14"
        self.assertEqual(unit_price_values(line)[1:3], (Decimal("31.20"), "m²"))

    def test_dimensions_are_not_a_takeoff_and_no_measure_keeps_forfait(self):
        line = sample(designation="Porte 830x2040mm").extraction.lines[0]
        self.assertEqual(unit_price_values(line)[2], "U")
        line.raw_unit = "Forfait"
        self.assertEqual(unit_price_values(line)[1:3], (Decimal("25"), "Forfait"))

    def test_length_conversion_and_ambiguous_takeoff_review(self):
        doc = sample(designation="Réseau. Longueur 10 ml")
        line = doc.extraction.lines[0]
        line.raw_unit = "Forfait"
        line.quantity = "1"
        line.export_unit_prices = True
        self.assertEqual(unit_price_values(line)[1:3], (Decimal("2.5"), "ml"))
        line.quantity = "2"
        self.assertTrue(any("Conversion du forfait" in issue for issue in assess(doc, line).issues))
        line.quantity = "1"
        line.raw_designation += ". Longueur 20 ml"
        self.assertIn("à vérifier", unit_price_values(line)[3])

    def test_export_has_complete_raw_descriptions_and_source_prices(self):
        text = "Fourniture et pose " + "description technique complète " * 15 + "Surface 14M2"
        doc = sample(price="436.80", designation=text)
        line = doc.extraction.lines[0]
        line.raw_unit = "Forfait"
        line.quantity = "1"
        line.line_total_ht = "436.80"
        line.export_unit_prices = True
        file = self.directory / "unit-prices.xlsx"
        export(batch(doc), file)
        book = load_workbook(file)
        self.assertEqual(book["Données brutes"].cell(2, 3).value, text)
        self.assertEqual(book["Données brutes"].cell(2, 5).value, 436.8)
        trace = book["Traçabilité"]
        self.assertEqual(trace.cell(2, 2).value, text)
        self.assertEqual(trace.cell(2, 3).value, "m²")
        self.assertAlmostEqual(trace.cell(2, 4).value, 31.2)
        book.close()
