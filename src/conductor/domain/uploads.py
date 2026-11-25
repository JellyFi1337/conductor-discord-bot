"""Подгонка вложения под лимит загрузки Discord: как есть, zip, иначе части."""

from __future__ import annotations

import io
import zipfile

__all__ = ["fit_upload"]


def fit_upload(filename: str, data: bytes, limit: int) -> tuple[tuple[str, bytes], ...]:
    cap = max(1, int(limit))
    if len(data) <= cap:
        return ((filename, data),)
    packed = _deflate(filename, data)
    if len(packed) <= cap:
        return ((f"{filename}.zip", packed),)
    return _split_parts(filename, data, cap)


def _deflate(filename: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, data)
    return buffer.getvalue()


def _split_parts(filename: str, data: bytes, limit: int) -> tuple[tuple[str, bytes], ...]:
    chunks = _line_chunks(data, limit)
    suffix = _text_suffix(filename)
    stem = filename[: -len(suffix)] if suffix and filename.endswith(suffix) else filename
    return tuple((f"{stem}.part{index}{suffix}", chunk) for index, chunk in enumerate(chunks, start=1))


def _text_suffix(filename: str) -> str:
    dot = filename.rfind(".")
    if dot <= 0:
        return ".txt"
    return filename[dot:]


def _line_chunks(data: bytes, limit: int) -> tuple[bytes, ...]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _raw_chunks(data, limit)
    pieces = [line.encode("utf-8") for line in text.splitlines(keepends=True)] or [data]
    parts: list[bytes] = []
    buf = b""
    for piece in pieces:
        if len(piece) > limit:
            if buf:
                parts.append(buf)
                buf = b""
            parts.extend(_raw_chunks(piece, limit))
            continue
        if buf and len(buf) + len(piece) > limit:
            parts.append(buf)
            buf = piece
            continue
        buf += piece
    if buf:
        parts.append(buf)
    return tuple(parts or [b""])


def _raw_chunks(data: bytes, limit: int) -> tuple[bytes, ...]:
    if not data:
        return (b"",)
    return tuple(data[index : index + limit] for index in range(0, len(data), limit))
