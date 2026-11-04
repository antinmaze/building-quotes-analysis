from __future__ import annotations

import os
import uuid
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font


def write_report(path: Path, sheets: dict[str, list[list]]) -> None:
    """Write private operational reports, escaping untrusted spreadsheet formulas."""
    if path.suffix.lower() != ".xlsx":
        raise ValueError("Classeur .xlsx requis")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    book = Workbook()
    book.remove(book.active)
    try:
        for name, rows in sheets.items():
            sheet = book.create_sheet(name)
            for row in rows:
                sheet.append(row)
            for cells in sheet:
                for cell in cells:
                    if isinstance(cell.value, str) and cell.value.lstrip().startswith(("=", "+", "-", "@")):
                        cell.value = "'" + cell.value
            for cell in sheet[1]:
                cell.font = Font(bold=True)
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for column in sheet.columns:
                sheet.column_dimensions[column[0].column_letter].width = min(
                    65, max(15, max(len(str(cell.value or "")) for cell in column[:100]) + 2))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            book.save(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        book.close()
        temporary.unlink(missing_ok=True)
