from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    database_url: str = Field('sqlite:///./tenders.db', alias='DATABASE_URL')
    openai_api_key: Optional[str] = Field(None, alias='OPENAI_API_KEY')
    openai_model: str = Field('gpt-4.1-mini', alias='OPENAI_MODEL')

    khmdhs_base_url: str = Field('https://cerpp.eprocurement.gov.gr', alias='KHMDHS_BASE_URL')
    khmdhs_timeout_seconds: int = Field(45, alias='KHMDHS_TIMEOUT_SECONDS')
    khmdhs_max_pages: int = Field(20, alias='KHMDHS_MAX_PAGES')
    khmdhs_page_delay_seconds: float = Field(1.0, alias='KHMDHS_PAGE_DELAY_SECONDS')
    khmdhs_rate_limit_retries: int = Field(4, alias='KHMDHS_RATE_LIMIT_RETRIES')
    khmdhs_rate_limit_base_delay_seconds: float = Field(5.0, alias='KHMDHS_RATE_LIMIT_BASE_DELAY_SECONDS')
    enable_diavgeia_rss: bool = Field(False, alias='ENABLE_DIAVGEIA_RSS')

    diavgeia_base_url: str = Field('https://diavgeia.gov.gr/luminapi/opendata', alias='DIAVGEIA_BASE_URL')
    diavgeia_timeout_seconds: int = Field(30, alias='DIAVGEIA_TIMEOUT_SECONDS')
    diavgeia_default_page_size: int = Field(10, alias='DIAVGEIA_DEFAULT_PAGE_SIZE')

    profile_config_path: Path = Field(Path('config/profiles.yml'), alias='PROFILE_CONFIG_PATH')
    schedule_hour: int = Field(7, alias='SCHEDULE_HOUR')
    schedule_minute: int = Field(15, alias='SCHEDULE_MINUTE')
    ingest_days_back: int = Field(3, alias='INGEST_DAYS_BACK')
    match_threshold: int = Field(55, alias='MATCH_THRESHOLD')
    fetch_pdf_for_score_above: int = Field(40, alias='FETCH_PDF_FOR_SCORE_ABOVE')
    auto_fetch_pdf_text: bool = Field(False, alias='AUTO_FETCH_PDF_TEXT')
    app_timezone: str = Field('Europe/Athens', alias='APP_TIMEZONE')

    admin_username: Optional[str] = Field(None, alias='ADMIN_USERNAME')
    admin_password: Optional[str] = Field(None, alias='ADMIN_PASSWORD')

    smtp_host: Optional[str] = Field(None, alias='SMTP_HOST')
    smtp_port: int = Field(587, alias='SMTP_PORT')
    smtp_username: Optional[str] = Field(None, alias='SMTP_USERNAME')
    smtp_password: Optional[str] = Field(None, alias='SMTP_PASSWORD')
    smtp_from: Optional[str] = Field(None, alias='SMTP_FROM')
    digest_recipients: Optional[str] = Field(None, alias='DIGEST_RECIPIENTS')

    @property
    def digest_recipient_list(self) -> List[str]:
        if not self.digest_recipients:
            return []
        return [x.strip() for x in self.digest_recipients.split(',') if x.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
