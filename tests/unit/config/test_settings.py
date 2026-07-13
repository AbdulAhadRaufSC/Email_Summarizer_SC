import pytest

from summarizer.config.settings import Settings


@pytest.fixture
def required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings() reads from the real environment / .env by default; pin it
    # to an isolated, non-existent .env and supply only the fields that
    # have no default, so this test doesn't depend on the dev machine's
    # actual .env file.
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_USER", "svc_summarizer")
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    monkeypatch.setenv("DB_NAME", "TrackEaseV2DB")
    monkeypatch.setenv("RUNPOD_ENDPOINT_ID", "ep-123")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp-secret")


class TestSettings:
    def test_loads_required_fields_from_env(self, required_env: None) -> None:
        settings = Settings(_env_file=None)

        assert settings.db.host == "db.internal"
        assert settings.db.user == "svc_summarizer"
        assert settings.db.password == "hunter2"
        assert settings.db.name == "TrackEaseV2DB"
        assert settings.runpod.endpoint_id == "ep-123"
        assert settings.runpod.api_key == "rp-secret"

    def test_sub_settings_defaults_are_applied(self, required_env: None) -> None:
        settings = Settings(_env_file=None)

        assert settings.db.port == 3306
        assert settings.email_api.timeout_seconds == 30
        assert settings.extraction.max_file_bytes == 10 * 1024 * 1024
        assert settings.llm.max_context_tokens == 16384
        assert settings.pipeline.llm_validation_retries == 3

    def test_env_prefix_overrides_sub_settings_defaults(
        self, required_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_MAX_CONTEXT_TOKENS", "8192")
        monkeypatch.setenv("PIPELINE_PROMPT_VERSION", "v2")

        settings = Settings(_env_file=None)

        assert settings.llm.max_context_tokens == 8192
        assert settings.pipeline.prompt_version == "v2"

    def test_missing_required_field_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DB_HOST", raising=False)

        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            Settings(_env_file=None)
