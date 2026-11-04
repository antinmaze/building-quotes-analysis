from __future__ import annotations

import json
import csv
import io
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import Document
from .quality import assess, ACCEPTANCE_THRESHOLD, SCORE_VERSION

CSV_FIELDS = [
    "timestamp_utc", "run_id", "sequence", "event", "source", "stage", "cached",
    "duration_seconds", "line_index", "pages", "designation", "unit_price_ht",
    "review_reasons", "status", "retrospective", "details",
]


class EventJournal:
    """Private append-only JSONL; timestamps describe observation, not reconstructed history."""

    def __init__(self, path: Path):
        self.path = path
        self.csv_path = path.with_suffix(".csv")
        self.run_id = uuid.uuid4().hex
        self.sequence = 0
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        fd = os.open(self.csv_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            if os.fstat(stream.fileno()).st_size == 0:
                csv.writer(stream, delimiter=";").writerow(CSV_FIELDS)
                stream.flush()
                os.fsync(stream.fileno())

    def emit(self, event: str, **details) -> None:
        self.sequence += 1
        record = {
            "schema_version": "1",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id, "sequence": self.sequence, "event": event,
            **details,
        }
        data = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        values = {}
        for field in CSV_FIELDS:
            value = record.get(field, "")
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            values[field] = value
        values["details"] = json.dumps(
            {key: value for key, value in record.items() if key not in CSV_FIELDS},
            ensure_ascii=False,
        )
        # Keep one physical line per event, including multiline source excerpts.
        for key, value in values.items():
            value = str(value).replace("\r", "\\r").replace("\n", "\\n")
            values[key] = "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, delimiter=";")
        writer.writerow(values)
        fd = os.open(self.csv_path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(buffer.getvalue())
            stream.flush()
            os.fsync(stream.fileno())

    def document(self, document: Document, *, retrospective: bool = False) -> None:
        context = {"source": document.source, "sha256": document.sha256,
                   "retrospective": retrospective}
        extraction = document.extraction
        if extraction is None:
            self.emit("document_result", **context, status=document.status,
                      error=document.error, pages=document.pages)
            return
        accepted = 0
        for index, line in enumerate(extraction.lines, 1):
            quality = assess(document, line)
            flags = quality.issues
            accepted += quality.accepted
            self.emit("line_accepted" if quality.accepted else "line_review", **context,
                      line_index=index, pages=line.source_pages, trade=line.trade,
                      designation=line.designation, raw_designation=line.raw_designation,
                      raw_unit=line.raw_unit, unit_price_ht=line.unit_price_ht,
                      quantity=line.quantity, line_total_ht=line.line_total_ht,
                      source_excerpt=line.source_excerpt, review_reasons=flags,
                      warnings=extraction.warnings + line.warnings,
                      unit_basis=line.unit_basis, normalized_unit=line.normalized_unit,
                      export_forfait=line.export_forfait,
                      export_unit_prices=line.export_unit_prices,
                      table_row_ids=line.table_row_ids, technical_signature=line.technical_signature,
                      score=quality.score, price_score=quality.price_score,
                      threshold=ACCEPTANCE_THRESHOLD, score_version=SCORE_VERSION,
                      unit_evidence=quality.unit_evidence,
                      destination="Base de prix" if quality.accepted else "À vérifier")
        self.emit("document_result", **context, status=document.status,
                  classification=extraction.classification,
                  classification_reason=extraction.classification_reason,
                  company=extraction.company, client=extraction.client, year=extraction.year,
                  metadata_excerpt=extraction.metadata_excerpt,
                  filename_conflict=extraction.filename_conflict,
                  review_reasons=extraction.review_reasons,
                  pages=document.pages, extracted_lines=len(extraction.lines),
                  accepted_lines=accepted, review_lines=len(extraction.lines) - accepted)
