from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./freshsales_capi.db"
    meta_verify_token: SecretStr
    meta_app_secret: SecretStr
    meta_page_access_token: SecretStr
    meta_pixel_id: str = Field(min_length=1, max_length=128)
    meta_capi_access_token: SecretStr = Field(validation_alias=AliasChoices("META_CAPI_ACCESS_TOKEN", "META_PAGE_ACCESS_TOKEN"))
    meta_dataset_id: str = Field(min_length=1, max_length=128)
    freshsales_domain: AnyHttpUrl
    freshsales_api_key: SecretStr
    freshsales_webhook_secret: SecretStr
    freshsales_meta_lead_field: str = Field(default="cf_meta_lead_id", min_length=1, max_length=80)
    freshsales_campaign_name_field: str = Field(default="cf_campaign_name", min_length=1, max_length=80)
    freshsales_campaign_id_field: str = Field(default="cf_campaign_id", min_length=1, max_length=80)
    freshsales_ad_set_field: str = Field(default="cf_ad_set", min_length=1, max_length=80)
    freshsales_ad_name_field: str = Field(default="cf_ad_name", min_length=1, max_length=80)
    meta_graph_version: str = Field(default="v20.0", pattern=r"^v\d+\.\d+$")
    lead_replay_interval_seconds: int = Field(default=300, ge=0, le=86400)
    lead_replay_max_age_hours: int = Field(default=48, ge=1, le=720)
    lead_max_concurrency: int = Field(default=10, ge=1, le=100)
    slack_webhook_url: SecretStr | None = None
    alert_channel: Literal["slack", "none"] = "slack"
    max_http_attempts: int = Field(default=3, ge=1, le=8)
    http_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    match_retry_attempts: int = Field(default=3, ge=1, le=6)
    match_retry_min_seconds: float = Field(default=5.0, ge=0.1, le=60.0)
    match_retry_max_seconds: float = Field(default=10.0, ge=0.1, le=120.0)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=1000)
    debug_log_pii: bool = False
    log_level: str = "INFO"

    meta_to_freshsales_field_mapping: dict[str, str] = {
        "full_name": "first_name",
        "email": "email",
        "phone_number": "mobile_number",
        "phone": "mobile_number",
    }

    event_mapping: dict[str, str] = {
        "CC Qualified": "Qualified",
        "Future Prospect": "Qualified",
        "Dropped": "Not Qualified",
        "Won": "Conversion",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
