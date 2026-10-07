"""Settings: Gemini API key, model name, confidence thresholds."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DISABLED_VALUES = {"", "none", "off", "false", "0"}


class Settings(BaseSettings):
    """App settings, loaded from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-3.8-flash", validation_alias="COX_GEMINI_MODEL")
    # Comma-separated models to try, in order, when the main one is overloaded or out of
    # quota (each model has its own free-tier quota). Empty = no fallback.
    gemini_fallback_models: str = Field(
        default="gemini-3.7-flash,gemini-3.5-flash", validation_alias="COX_GEMINI_FALLBACK_MODELS"
    )
    # Stronger models asked for a second opinion when the answer came from another
    # (e.g. a 'lite' fallback) model and it disagrees with the rules on the total, CO
    # number or date. Comma-separated, tried in order. Empty or "none" = never escalate
    # ("none" also works in PowerShell, where setting a variable to "" deletes it).
    gemini_escalation_models: str = Field(
        default="gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash",
        validation_alias="COX_GEMINI_ESCALATION_MODELS",
    )

    # LLM call behaviour
    llm_temperature: float = Field(default=0.0, ge=0, le=2, validation_alias="COX_LLM_TEMPERATURE")
    llm_max_retries: int = Field(default=4, ge=0, validation_alias="COX_LLM_MAX_RETRIES")
    llm_retry_base_seconds: float = Field(
        default=2.0, ge=0, validation_alias="COX_LLM_RETRY_BASE_SECONDS"
    )
    llm_timeout_seconds: int = Field(default=120, gt=0, validation_alias="COX_LLM_TIMEOUT")
    # Answers are cached on disk by (model, prompt, document) so re-runs are free and
    # repeatable. Set COX_LLM_CACHE_DIR to an empty value to disable.
    llm_cache_dir: Path | None = Field(
        default=Path(".cache/llm"), validation_alias="COX_LLM_CACHE_DIR"
    )

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.gemini_fallback_models.split(",") if m.strip()]

    @property
    def escalation_models(self) -> list[str]:
        if self.gemini_escalation_models.strip().lower() in DISABLED_VALUES:
            return []
        return [m.strip() for m in self.gemini_escalation_models.split(",") if m.strip()]

    @field_validator("llm_cache_dir", mode="before")
    @classmethod
    def empty_means_disabled(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    # The fitted calibration table (written by `cox calibrate`). Empty or "none" = use
    # the raw scores. Default: the table shipped with the package.
    calibration_file: str = Field(default="", validation_alias="COX_CALIBRATION_FILE")

    @property
    def calibration_path(self) -> Path | None:
        """None = calibration switched off."""
        value = self.calibration_file.strip()
        if value.lower() in DISABLED_VALUES - {""}:
            return None
        if not value:
            from change_order_extractor.scoring.calibration import DEFAULT_CALIBRATION_FILE

            return DEFAULT_CALIBRATION_FILE
        return Path(value)

    # Fields (and documents) scoring below this are flagged for human review.
    review_threshold: float = Field(
        default=0.75, ge=0, le=1, validation_alias="COX_REVIEW_THRESHOLD"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
