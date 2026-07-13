"""Sandboxed attachment text extractor.

Dispatches by MIME type to per-format handlers.  Enforces hard limits
on file size, wall-clock time, and decompression ratio.  **Never
raises** — individual failures degrade to ``METADATA_ONLY`` or
``FAILED``, so one bad attachment can never crash the ticket pipeline.
"""

from __future__ import annotations

import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

from summarizer.adapters.extraction.handlers import (
    extract_csv_text,
    extract_docx,
    extract_pdf,
    extract_txt,
    extract_xlsx,
)
from summarizer.domain.models import ExtractedAttachment, RawAttachment
from summarizer.domain.schema.v1 import ExtractionStatus

logger = logging.getLogger(__name__)

# MIME types we extract text from in Phase 1.
_EXTRACTABLE_MIMES: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xlsx",
    "text/csv": "csv",
    "text/plain": "txt",
    "text/tab-separated-values": "csv",
}


class SandboxedExtractor:
    """Guarded attachment text extractor with resource limits.

    Limits enforced:
    * ``max_file_bytes`` — reject files larger than this before decoding.
    * ``timeout_seconds`` — wall-clock cap per extraction.
    * ``max_decompression_ratio`` — decoded bytes / raw bytes cap
      (zip-bomb defence for DOCX/XLSX).
    """

    def __init__(
        self,
        *,
        max_file_bytes: int = 10 * 1024 * 1024,
        timeout_seconds: int = 30,
        max_decompression_ratio: int = 100,
        max_xlsx_rows: int = 50_000,
        max_xlsx_cells: int = 500_000,
    ) -> None:
        self._max_file_bytes = max_file_bytes
        self._timeout_seconds = timeout_seconds
        self._max_decompression_ratio = max_decompression_ratio
        self._max_xlsx_rows = max_xlsx_rows
        self._max_xlsx_cells = max_xlsx_cells

    def extract(self, attachment: RawAttachment) -> ExtractedAttachment:
        """Extract text from a raw attachment.  Never raises."""

        mime_lower = attachment.mime_type.lower()
        fmt = _EXTRACTABLE_MIMES.get(mime_lower)

        # Unsupported format → metadata only (not a failure).
        if fmt is None:
            return ExtractedAttachment(
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                size=attachment.size,
                extraction_status=ExtractionStatus.METADATA_ONLY,
            )

        # No content → metadata only.
        if not attachment.content_base64:
            return ExtractedAttachment(
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                size=attachment.size,
                extraction_status=ExtractionStatus.METADATA_ONLY,
                error_message="No base64 content provided",
            )

        try:
            raw_bytes = base64.b64decode(attachment.content_base64)
        except Exception as exc:
            logger.warning(
                "Base64 decode failed for %s: %s",
                attachment.filename, exc,
            )
            return self._failed(attachment, f"Base64 decode error: {exc}")

        # Size cap.
        if len(raw_bytes) > self._max_file_bytes:
            return self._failed(
                attachment,
                f"File exceeds {self._max_file_bytes} bytes ({len(raw_bytes)})",
            )

        # Decompression ratio check (for archive-based formats).
        if fmt in ("docx", "xlsx") and attachment.size > 0:
            ratio = len(raw_bytes) / attachment.size
            if ratio > self._max_decompression_ratio:
                return self._failed(
                    attachment,
                    f"Decompression ratio {ratio:.1f}x exceeds cap "
                    f"({self._max_decompression_ratio}x)",
                )

        # Run extraction with a wall-clock timeout.
        try:
            text = self._extract_with_timeout(fmt, raw_bytes)
        except FuturesTimeout:
            logger.warning(
                "Extraction timed out for %s after %ds",
                attachment.filename, self._timeout_seconds,
            )
            return self._failed(
                attachment,
                f"Extraction timed out after {self._timeout_seconds}s",
            )
        except Exception as exc:
            logger.warning(
                "Extraction failed for %s: %s",
                attachment.filename, exc,
                exc_info=True,
            )
            return self._failed(attachment, f"Extraction error: {exc}")

        if not text.strip():
            return ExtractedAttachment(
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                size=attachment.size,
                extraction_status=ExtractionStatus.METADATA_ONLY,
                error_message="Extracted text is empty",
            )

        return ExtractedAttachment(
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            size=attachment.size,
            extraction_status=ExtractionStatus.EXTRACTED,
            extracted_text=text,
        )

    def _extract_with_timeout(self, fmt: str, data: bytes) -> str:
        """Run the format handler in a thread with a timeout."""
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._dispatch, fmt, data)
            return future.result(timeout=self._timeout_seconds)

    def _dispatch(self, fmt: str, data: bytes) -> str:
        """Route to the correct format handler."""
        if fmt == "pdf":
            return extract_pdf(data)
        elif fmt == "docx":
            return extract_docx(data)
        elif fmt == "xlsx":
            return extract_xlsx(
                data,
                max_rows=self._max_xlsx_rows,
                max_cells=self._max_xlsx_cells,
            )
        elif fmt == "csv":
            return extract_csv_text(data)
        elif fmt == "txt":
            return extract_txt(data)
        else:
            raise ValueError(f"Unknown format: {fmt}")

    @staticmethod
    def _failed(attachment: RawAttachment, error: str) -> ExtractedAttachment:
        return ExtractedAttachment(
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            size=attachment.size,
            extraction_status=ExtractionStatus.FAILED,
            error_message=error,
        )
