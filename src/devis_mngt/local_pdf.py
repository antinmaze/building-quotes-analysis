from __future__ import annotations

import copy
import re
from decimal import Decimal
from pathlib import Path

import pdfplumber

from .models import OCR, evidence_key
from .storage import digest, load, save

LOCAL_VERSION = "local-tables-3"


def number(text: str | None) -> Decimal | None:
    value = (text or "").replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    if not re.fullmatch(r"-?\d+(?:[,.]\d+)?", value):
        return None
    return Decimal(value.replace(",", "."))


def page_data(page, *, ocr_layer: bool = False) -> dict:
    """Accept native tables only when structure, arithmetic and coverage agree."""
    text = page.extract_text() or ""
    result = {"number": page.page_number, "text": text, "tables": [], "reason": None, "needs_ocr": False}
    if len(text.strip()) < 80 or "\ufffd" in text or "(cid:" in text:
        result["reason"] = "Texte natif absent, trop court ou encodage dégradé"
        result["needs_ocr"] = True
        return result
    area = page.width * page.height
    if not ocr_layer and any((image["x1"] - image["x0"]) * (image["bottom"] - image["top"]) > area * .2
           for image in page.images):
        result["reason"] = "Image importante : contenu scanné ou mixte à contrôler par OCR"
        result["needs_ocr"] = True
        return result
    tables = page.find_tables()
    if ocr_layer and not tables:
        tables = page.find_tables({"vertical_strategy": "text", "horizontal_strategy": "text"})
    priced_boxes = []
    for table in tables:
        values = table.extract()
        header_index = None
        columns = {}
        for index, row in enumerate(values):
            found = {}
            for col, value in enumerate(row):
                key = evidence_key(value)
                if key in {"description", "designation", "libelle", "ouvrage"}:
                    found["description"] = col
                elif key in {"qte", "quantite"}:
                    found["quantity"] = col
                elif key in {"p u ht", "p u h t", "pu ht", "prix unitaire ht"}:
                    found["price"] = col
                elif key in {"montant ht", "total ht", "total h t", "montant h t"}:
                    found["total"] = col
            if set(found) == {"description", "quantity", "price", "total"}:
                header_index, columns = index, found
                break
        if header_index is None:
            continue
        count = 0
        for row in values[header_index + 1:]:
            description = row[columns["description"]] or ""
            if re.match(r"^(?:sous.total|total|net a payer)\b", evidence_key(description)):
                continue
            price_text = row[columns["price"]]
            total_text = row[columns["total"]]
            if not price_text and not total_text:
                continue
            quantity, price, total = (number(row[columns[k]]) for k in ("quantity", "price", "total"))
            if (not description.strip() or quantity is None or price is None or total is None
                    or abs(quantity * price - total) > Decimal(".02")):
                result["reason"] = "Tableau natif ambigu : colonnes, quantité × PU ou total incohérents"
                return result
            count += 1
        if count:
            result["tables"].append({"values": values, "header": header_index, "bbox": table.bbox,
                                     "cells": [row.cells for row in table.rows]})
            priced_boxes.append(table.bbox)
    if not priced_boxes:
        result["reason"] = "Aucun tableau tarifé natif suffisamment corroboré"
        return result
    # A second price list outside the extracted grid must not be silently missed.
    for word in page.extract_words():
        if re.fullmatch(r"-?\d+[,.]\d{2}", word["text"]):
            x, y = (word["x0"] + word["x1"]) / 2, (word["top"] + word["bottom"]) / 2
            if not any(x0 <= x <= x1 and top <= y <= bottom for x0, top, x1, bottom in priced_boxes):
                result["reason"] = "Montant hors tableau natif : couverture non garantie"
                return result
    return result


def inspect_pdf(path: Path, *, ocr_layer: bool = False) -> list[dict]:
    with pdfplumber.open(path) as pdf:
        return [page_data(page, ocr_layer=ocr_layer) for page in pdf.pages]


def prepare_pdf(path: Path, cache: Path, *, use_ocr: bool = False, config: dict | None = None) -> list[dict]:
    pages = inspect_pdf(path)
    for page in pages:
        page["method"] = "native"
    if not use_ocr:
        return pages
    from .local_ocr import engine_config, searchable_copy
    # OCR cannot repair native table geometry and can corrupt otherwise correct digits.
    candidates = [p["number"] for p in pages if p["needs_ocr"]]
    if not candidates:
        return pages
    config = config or engine_config()
    copy_path = searchable_copy(path, cache, candidates, config)
    with pdfplumber.open(copy_path) as pdf:
        for index in candidates:
            data = page_data(pdf.pages[index - 1], ocr_layer=True)
            data["method"] = "tesseract"
            data["native_reason"] = pages[index - 1]["reason"]
            data["searchable_pdf"] = str(copy_path)
            pages[index - 1] = data
    return pages


def native_page(data: dict) -> OCR:
    page = data["number"]
    content = data["text"] + "\n"
    tables = []
    for table in data["tables"]:
        cells = []
        x0, top, x1, bottom = table["bbox"]
        values = table["values"]
        columns = len(values[0])
        for row_index, row in enumerate(values):
            for col, value in enumerate(row):
                if not value:
                    continue
                start = len(content)
                content += value + "\n"
                box = table["cells"][row_index][col]
                if box is None:
                    raise ValueError("Cellule native fusionnée ambiguë")
                left, cell_top, right, cell_bottom = (v / 72 for v in box)
                cells.append({
                    "rowIndex": row_index, "columnIndex": col, "content": value,
                    "kind": "columnHeader" if row_index == table["header"] else "content",
                    "boundingRegions": [{"pageNumber": page,
                                         "polygon": [left, cell_top, right, cell_top,
                                                     right, cell_bottom, left, cell_bottom]}],
                    "spans": [{"offset": start, "length": len(value)}],
                })
        tables.append({"rowCount": len(values), "columnCount": columns, "cells": cells})
    return OCR(content=content, page_texts={str(page): content}, page_count=1,
               raw={"tables": tables, "local_adapter": LOCAL_VERSION})


def merge_pages(parts: list[OCR]) -> OCR:
    content = ""
    page_texts = {}
    tables = []
    for part in parts:
        if set(page_texts) & set(part.page_texts):
            raise ValueError("Pages dupliquées lors de la fusion OCR")
        shift = len(content)
        content += part.content + "\n"
        page_texts.update(part.page_texts)
        for table in copy.deepcopy(part.raw.get("tables", [])):
            for cell in table.get("cells", []):
                for span in cell.get("spans", []):
                    span["offset"] += shift
            tables.append(table)
    return OCR(content=content, page_texts=page_texts, page_count=len(page_texts),
               raw={"tables": tables, "local_adapter": LOCAL_VERSION})


def local_first(path: Path, backend, cache: Path, source_sha: str, config: dict, journal=None,
                *, use_ocr: bool = False, engine: dict | None = None) -> OCR:
    plan = prepare_pdf(path, cache, use_ocr=use_ocr, config=engine)
    parts = []
    for page in plan:
        number_ = page["number"]
        if journal:
            journal.emit("page_routing", source=str(path), page=number_,
                         route="azure" if page["reason"] else page["method"], reason=page["reason"],
                         local_method=page["method"], searchable_pdf=page.get("searchable_pdf"))
        if page["reason"]:
            key = digest({"sha256": source_sha, "pages": [number_], **config})
            target = cache / "ocr-pages" / (key + ".json")
            if target.exists():
                part = OCR.model_validate(load(target))
            else:
                part = backend.ocr(path, pages=[number_])
                if set(part.page_texts) != {str(number_)}:
                    raise ValueError("OCR de page incomplet ou numérotation source perdue")
                save(target, part.model_dump())
        else:
            part = native_page(page)
        if set(part.page_texts) != {str(number_)}:
            raise ValueError("OCR de page incomplet ou numérotation source perdue")
        parts.append(part)
    return merge_pages(parts)
