"""Unit test for extract_image specifically -- exercises the real
Pillow image decode (no Tesseract binary needed for that part) while
mocking pytesseract's OCR call itself, since the Tesseract binary is a
system package this dev sandbox doesn't have installed.
"""

from __future__ import annotations

import io

import pytest

from summarizer.adapters.extraction.handlers import extract_image


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 20), color="white").save(buf, format="PNG")
    return buf.getvalue()


class TestExtractImage:
    def test_returns_ocr_text_from_pytesseract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "pytesseract.image_to_string", lambda image: "  Ticket #4521 - Order Confirmed  \n"
        )

        text = extract_image(_png_bytes())

        assert text == "Ticket #4521 - Order Confirmed"

    def test_strips_whitespace_only_ocr_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pytesseract.image_to_string", lambda image: "   \n  ")

        text = extract_image(_png_bytes())

        assert text == ""

    def test_propagates_ocr_engine_errors_to_caller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # extract_image itself doesn't swallow errors -- SandboxedExtractor
        # is what degrades this to FAILED (see TestImageOcr in
        # test_extractor.py).
        def _boom(image: object) -> str:
            raise RuntimeError("tesseract is not installed or it's not in your PATH")

        monkeypatch.setattr("pytesseract.image_to_string", _boom)

        with pytest.raises(RuntimeError):
            extract_image(_png_bytes())

    def test_invalid_image_bytes_raise(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 - PIL raises UnidentifiedImageError
            extract_image(b"not an image")
