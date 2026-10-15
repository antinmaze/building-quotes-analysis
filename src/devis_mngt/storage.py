from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from pypdf import PdfReader

SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
CACHE_VERSION = "1"


def digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def save(path: Path, payload: dict) -> None:
    """Atomic, owner-only cache; checksum detects accidental manual modifications."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    envelope = {"version": CACHE_VERSION, "sha256": digest(payload), "payload": payload}
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(envelope, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load(path: Path) -> dict:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if envelope.get("version") != CACHE_VERSION or digest(envelope["payload"]) != envelope.get("sha256"):
        raise ValueError(f"Cache modifié ou incompatible : {path}; ne pas modifier les caches")
    return envelope["payload"]


def inventory(root: Path, limit: int | None = None) -> list[dict]:
    if not root.exists():
        raise ValueError(f"Entrée introuvable : {root}")
    paths = sorted(root.rglob("*")) if root.is_dir() else [root]
    paths = [p for p in paths if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root.parent if root.is_file() else root).parts)]
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit doit être positif")
        paths = paths[:limit]
    rows = []
    for path in paths:
        row = {"source": str(path.absolute()), "sha256": None, "pages": None, "status": "ready", "error": None}
        try:
            with path.open("rb") as stream:
                row["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            if path.suffix.lower() not in SUPPORTED:
                row.update(status="unsupported", error="Format non pris en charge")
            elif path.suffix.lower() == ".pdf":
                reader = PdfReader(path, strict=True)
                if reader.is_encrypted:
                    raise ValueError("PDF chiffré : déchiffrer une copie séparément")
                row["pages"] = len(reader.pages)
                if row["pages"] == 0:
                    raise ValueError("PDF sans page")
        except Exception as exc:
            row.update(status="error", error=f"{type(exc).__name__}: {exc}")
        rows.append(row)
    return rows
