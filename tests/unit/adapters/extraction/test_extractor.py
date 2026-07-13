from __future__ import annotations

import base64
import time

import pytest

from summarizer.adapters.extraction.extractor import SandboxedExtractor
from summarizer.domain.models import RawAttachment
from summarizer.domain.schema.v1 import ExtractionStatus


def _attachment(**overrides: object) -> RawAttachment:
    fields: dict[str, object] = {
        "filename": "report.pdf",
        "mime_type": "application/pdf",
        "size": 12,
        "attachment_id": "att-1",
        "content_base64": base64.b64encode(b"hello world").decode(),
        **overrides,
    }
    return RawAttachment(**fields)  # type: ignore[arg-type]


class TestUnsupportedAndMissingContent:
    def test_unsupported_mime_is_metadata_only(self) -> None:
        # message/rfc822 (an .eml attachment) is intentionally out of
        # Phase 1 scope -- unlike images, which now OCR (see
        # TestImageOcr below).
        extractor = SandboxedExtractor()
        result = extractor.extract(_attachment(mime_type="message/rfc822"))

        assert result.extraction_status is ExtractionStatus.METADATA_ONLY
        assert result.extracted_text is None

    def test_missing_content_is_metadata_only(self) -> None:
        extractor = SandboxedExtractor()
        result = extractor.extract(_attachment(content_base64=None))

        assert result.extraction_status is ExtractionStatus.METADATA_ONLY
        assert result.error_message == "No base64 content provided"

    def test_bad_base64_fails_without_raising(self) -> None:
        extractor = SandboxedExtractor()
        result = extractor.extract(_attachment(content_base64="not-valid-base64!!!"))

        assert result.extraction_status is ExtractionStatus.FAILED


class TestSizeAndRatioCaps:
    def test_oversized_file_fails(self) -> None:
        big = base64.b64encode(b"x" * 1000).decode()
        extractor = SandboxedExtractor(max_file_bytes=100)
        result = extractor.extract(_attachment(content_base64=big, mime_type="text/plain"))

        assert result.extraction_status is ExtractionStatus.FAILED
        assert "exceeds" in (result.error_message or "")

    def test_decompression_ratio_exceeded_fails(self) -> None:
        decoded = b"x" * 10_000
        extractor = SandboxedExtractor(max_decompression_ratio=10)
        result = extractor.extract(
            _attachment(
                content_base64=base64.b64encode(decoded).decode(),
                size=10,  # 10_000 / 10 = 1000x, over the cap
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        )

        assert result.extraction_status is ExtractionStatus.FAILED
        assert "ratio" in (result.error_message or "").lower()

    def test_ratio_cap_not_applied_to_non_archive_formats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # txt/csv aren't zip-based, so the ratio guard must not fire even
        # with a tiny declared size relative to decoded content.
        monkeypatch.setattr(
            "summarizer.adapters.extraction.extractor.extract_txt", lambda data: "ok"
        )
        extractor = SandboxedExtractor(max_decompression_ratio=2)
        result = extractor.extract(
            _attachment(
                content_base64=base64.b64encode(b"x" * 1000).decode(),
                size=1,
                mime_type="text/plain",
            )
        )

        assert result.extraction_status is ExtractionStatus.EXTRACTED


class TestExtractionOutcomes:
    def test_successful_extraction_returns_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "summarizer.adapters.extraction.extractor.extract_pdf", lambda data: "extracted text"
        )
        extractor = SandboxedExtractor()
        result = extractor.extract(_attachment())

        assert result.extraction_status is ExtractionStatus.EXTRACTED
        assert result.extracted_text == "extracted text"

    def test_empty_extracted_text_is_metadata_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "summarizer.adapters.extraction.extractor.extract_pdf", lambda data: "   "
        )
        extractor = SandboxedExtractor()
        result = extractor.extract(_attachment())

        assert result.extraction_status is ExtractionStatus.METADATA_ONLY
        assert result.error_message == "Extracted text is empty"

    def test_handler_exception_fails_without_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(data: bytes) -> str:
            raise ValueError("corrupt PDF")

        monkeypatch.setattr("summarizer.adapters.extraction.extractor.extract_pdf", _boom)
        extractor = SandboxedExtractor()
        result = extractor.extract(_attachment())

        assert result.extraction_status is ExtractionStatus.FAILED
        assert "corrupt PDF" in (result.error_message or "")

    def test_timeout_fails_without_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _slow(data: bytes) -> str:
            time.sleep(0.3)
            return "too late"

        monkeypatch.setattr("summarizer.adapters.extraction.extractor.extract_pdf", _slow)
        extractor = SandboxedExtractor(timeout_seconds=0.05)  # type: ignore[arg-type]
        result = extractor.extract(_attachment())

        assert result.extraction_status is ExtractionStatus.FAILED
        assert "timed out" in (result.error_message or "")

    def test_never_raises_for_a_single_bad_attachment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Core pipeline guarantee from CLAUDE.md: extraction must never
        # raise, regardless of what goes wrong.
        def _boom(data: bytes) -> str:
            raise RuntimeError("disk on fire")

        monkeypatch.setattr("summarizer.adapters.extraction.extractor.extract_pdf", _boom)
        extractor = SandboxedExtractor()
        try:
            result = extractor.extract(_attachment())
        except Exception as exc:  # pragma: no cover - the assertion is that this doesn't happen
            pytest.fail(f"extract() raised {exc!r}, but must never raise")
        assert result.extraction_status is ExtractionStatus.FAILED

    @pytest.mark.parametrize(
        "mime_type",
        [
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "text/csv",
        ],
    )
    def test_mime_dispatch_routes_to_correct_handler(
        self, monkeypatch: pytest.MonkeyPatch, mime_type: str
    ) -> None:
        called: dict[str, bool] = {"xlsx": False, "csv": False}

        def _fake_xlsx(data: bytes, **kw: object) -> str:
            called["xlsx"] = True
            return "xlsx text"

        def _fake_csv(data: bytes) -> str:
            called["csv"] = True
            return "csv text"

        monkeypatch.setattr(
            "summarizer.adapters.extraction.extractor.extract_xlsx", _fake_xlsx
        )
        monkeypatch.setattr(
            "summarizer.adapters.extraction.extractor.extract_csv_text", _fake_csv
        )
        extractor = SandboxedExtractor()
        result = extractor.extract(_attachment(mime_type=mime_type))

        assert result.extraction_status is ExtractionStatus.EXTRACTED
        expected_key = "xlsx" if "spreadsheet" in mime_type else "csv"
        assert called[expected_key] is True


class TestImageOcr:
    @pytest.mark.parametrize(
        "mime_type",
        ["image/png", "image/jpeg", "image/jpg", "image/tiff", "image/bmp", "image/webp"],
    )
    def test_image_mime_types_route_to_ocr_handler(
        self, monkeypatch: pytest.MonkeyPatch, mime_type: str
    ) -> None:
        monkeypatch.setattr(
            "summarizer.adapters.extraction.extractor.extract_image",
            lambda data: "Error code: E404",
        )
        extractor = SandboxedExtractor()
        result = extractor.extract(_attachment(mime_type=mime_type))

        assert result.extraction_status is ExtractionStatus.EXTRACTED
        assert result.extracted_text == "Error code: E404"

    def test_image_with_no_readable_text_is_metadata_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A photo with no text in it -- OCR runs fine but returns
        # nothing, which is the existing "empty extracted text" path,
        # not a failure.
        monkeypatch.setattr(
            "summarizer.adapters.extraction.extractor.extract_image", lambda data: ""
        )
        extractor = SandboxedExtractor()
        result = extractor.extract(_attachment(mime_type="image/png"))

        assert result.extraction_status is ExtractionStatus.METADATA_ONLY

    def test_ocr_engine_failure_degrades_to_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # e.g. TesseractNotFoundError if the binary isn't on PATH --
        # must degrade like any other handler exception, never raise.
        def _boom(data: bytes) -> str:
            raise RuntimeError("tesseract is not installed or it's not in your PATH")

        monkeypatch.setattr("summarizer.adapters.extraction.extractor.extract_image", _boom)
        extractor = SandboxedExtractor()
        result = extractor.extract(_attachment(mime_type="image/png"))

        assert result.extraction_status is ExtractionStatus.FAILED
        assert "tesseract" in (result.error_message or "").lower()
