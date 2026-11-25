from __future__ import annotations

from conductor.domain.uploads import fit_upload


def test_fit_upload_keeps_file_that_fits() -> None:
    parts = fit_upload("a.txt", b"hello", 10)
    assert parts == (("a.txt", b"hello"),)


def test_fit_upload_zips_when_raw_exceeds_limit() -> None:
    data = b"aaaa" * 5000
    parts = fit_upload("big.txt", data, 500)
    assert len(parts) == 1
    name, blob = parts[0]
    assert name == "big.txt.zip"
    assert len(blob) <= 500
    assert len(data) > 500


def test_fit_upload_splits_text_when_zip_still_too_big() -> None:
    text = "\n".join(f"строка-{index}" for index in range(20)) + "\n"
    data = text.encode("utf-8")
    parts = fit_upload("log.txt", data, 40)
    assert len(parts) > 1
    assert all(len(blob) <= 40 for _name, blob in parts)
    assert b"".join(blob for _name, blob in parts).decode("utf-8") == text
    assert parts[0][0] == "log.part1.txt"
