import json
import logging

from summarizer.config.logging_config import _JsonFormatter, configure_logging


def _make_record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="summarizer.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestJsonFormatter:
    def test_formats_message_as_json(self) -> None:
        record = _make_record()
        payload = json.loads(_JsonFormatter().format(record))

        assert payload["message"] == "hello world"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "summarizer.test"
        assert "ts" in payload

    def test_includes_correlation_fields_when_present(self) -> None:
        record = _make_record(ticket_id=123, message_id="abc", email_meta_id=456)
        payload = json.loads(_JsonFormatter().format(record))

        assert payload["ticket_id"] == 123
        assert payload["message_id"] == "abc"
        assert payload["email_meta_id"] == 456

    def test_omits_correlation_fields_when_absent(self) -> None:
        record = _make_record()
        payload = json.loads(_JsonFormatter().format(record))

        assert "ticket_id" not in payload
        assert "message_id" not in payload
        assert "email_meta_id" not in payload

    def test_includes_operational_fields_when_present(self) -> None:
        # Regression: these were silently dropped until the allow-list
        # was extended past the three original correlation keys --
        # caught by a real CLI run whose completion log line was
        # missing write_outcome/status/timing/tokens.
        record = _make_record(
            write_outcome="written",
            status="OK",
            processing_time_ms=10187,
            retry_count=0,
            token_input=1707,
            token_output=261,
        )
        payload = json.loads(_JsonFormatter().format(record))

        assert payload["write_outcome"] == "written"
        assert payload["status"] == "OK"
        assert payload["processing_time_ms"] == 10187
        assert payload["retry_count"] == 0
        assert payload["token_input"] == 1707
        assert payload["token_output"] == 261

    def test_never_emits_raw_email_body_fields(self) -> None:
        # Guards the "never log email bodies / PII" contract: only the
        # allow-listed correlation keys are ever pulled from the record.
        record = _make_record(html_body="<p>secret</p>", text_body="secret")
        payload = json.loads(_JsonFormatter().format(record))

        assert "html_body" not in payload
        assert "text_body" not in payload

    def test_includes_exception_when_present(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                name="summarizer.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="failed",
                args=(),
                exc_info=__import__("sys").exc_info(),
            )

        payload = json.loads(_JsonFormatter().format(record))
        assert "exception" in payload
        assert "ValueError: boom" in payload["exception"]


class TestConfigureLogging:
    def test_adds_exactly_one_stream_handler(self) -> None:
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        try:
            root.handlers.clear()

            configure_logging()
            configure_logging()  # idempotent: second call must not duplicate

            stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
            assert len(stream_handlers) == 1
        finally:
            root.handlers.clear()
            root.handlers.extend(original_handlers)

    def test_quiets_noisy_third_party_loggers(self) -> None:
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        try:
            root.handlers.clear()
            configure_logging()

            assert logging.getLogger("urllib3").level == logging.WARNING
            assert logging.getLogger("pymysql").level == logging.WARNING
        finally:
            root.handlers.clear()
            root.handlers.extend(original_handlers)
