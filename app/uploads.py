"""Helpers for reading uploaded files and form fields."""

from __future__ import annotations

import json

from fastapi import HTTPException, UploadFile

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


async def read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 50 MB.")
    return content


def parse_mapping(mapping: str) -> dict[str, str] | None:
    """The mapping form field is JSON like {"date": "Txn Date", ...}; empty means guess."""
    if not mapping:
        return None
    try:
        value = json.loads(mapping)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Mapping must be a JSON object.") from exc
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str | None) for k, v in value.items()
    ):
        raise HTTPException(400, "Mapping must be a JSON object of field to column name.")
    return value
