from functools import lru_cache
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_env: str = Field('development', alias='APP_ENV')
    database_url: str = Field('sqlite:///./tenders.db', alias='DATABASE_URL')

    khmdhs_base_url: str = Field('https://cerpp.eprocurement.gov.gr', alias='KHMDHS_BASE_URL')
    khmdhs_timeout_seconds: int = Field(90, alias='KHMDHS_TIMEOUT_SECONDS')
    khmdhs_interactive_timeout_seconds: int = Field(15, alias='KHMDHS_INTERACTIVE_TIMEOUT_SECONDS')
    khmdhs_interactive_transport_retries: int = Field(1, alias='KHMDHS_INTERACTIVE_TRANSPORT_RETRIES')
    khmdhs_max_pages: int = Field(20, alias='KHMDHS_MAX_PAGES')
    khmdhs_rate_limit_retries: int = Field(4, alias='KHMDHS_RATE_LIMIT_RETRIES')
    khmdhs_rate_limit_base_delay_seconds: float = Field(5.0, alias='KHMDHS_RATE_LIMIT_BASE_DELAY_SECONDS')
    khmdhs_transport_retries: int = Field(3, alias='KHMDHS_TRANSPORT_RETRIES')
    khmdhs_transport_base_delay_seconds: float = Field(2.0, alias='KHMDHS_TRANSPORT_BASE_DELAY_SECONDS')
    khmdhs_requests_per_minute: int = Field(180, alias='KHMDHS_REQUESTS_PER_MINUTE')
    khmdhs_query_cache_hours: int = Field(20, alias='KHMDHS_QUERY_CACHE_HOURS')
    khmdhs_sync_overlap_days: int = Field(1, alias='KHMDHS_SYNC_OVERLAP_DAYS')
    khmdhs_continuation_delay_seconds: int = Field(15, alias='KHMDHS_CONTINUATION_DELAY_SECONDS')
    khmdhs_continuation_max_attempts: int = Field(50, alias='KHMDHS_CONTINUATION_MAX_ATTEMPTS')
    diavgeia_base_url: str = Field('https://diavgeia.gov.gr/luminapi/opendata', alias='DIAVGEIA_BASE_URL')
    diavgeia_timeout_seconds: int = Field(30, alias='DIAVGEIA_TIMEOUT_SECONDS')
    diavgeia_default_page_size: int = Field(10, alias='DIAVGEIA_DEFAULT_PAGE_SIZE')

    schedule_hour: int = Field(7, alias='SCHEDULE_HOUR')
    schedule_minute: int = Field(15, alias='SCHEDULE_MINUTE')
    score_refresh_minutes: int = Field(15, alias='SCORE_REFRESH_MINUTES')
    ingest_days_back: int = Field(3, alias='INGEST_DAYS_BACK')
    match_threshold: int = Field(55, alias='MATCH_THRESHOLD')
    fetch_pdf_for_score_above: int = Field(40, alias='FETCH_PDF_FOR_SCORE_ABOVE')
    auto_fetch_pdf_text: bool = Field(False, alias='AUTO_FETCH_PDF_TEXT')
    pdf_max_bytes: int = Field(60_000_000, alias='PDF_MAX_BYTES')
    app_timezone: str = Field('Europe/Athens', alias='APP_TIMEZONE')

    admin_username: Optional[str] = Field(None, alias='ADMIN_USERNAME')
    admin_password: Optional[str] = Field(None, alias='ADMIN_PASSWORD')
    session_secret_key: Optional[str] = Field(None, alias='SESSION_SECRET_KEY')
    session_max_age_seconds: int = Field(86400, alias='SESSION_MAX_AGE_SECONDS')
    session_cookie_secure: bool = Field(False, alias='SESSION_COOKIE_SECURE')
    require_session_secret: bool = Field(False, alias='REQUIRE_SESSION_SECRET')
    csrf_protection_enabled: bool = Field(True, alias='CSRF_PROTECTION_ENABLED')
    login_rate_limit_attempts: int = Field(5, alias='LOGIN_RATE_LIMIT_ATTEMPTS')
    login_rate_limit_window_seconds: int = Field(300, alias='LOGIN_RATE_LIMIT_WINDOW_SECONDS')
    allow_local_admin_fallback: bool = Field(False, alias='ALLOW_LOCAL_ADMIN_FALLBACK')
    bootstrap_admin_username: Optional[str] = Field(None, alias='BOOTSTRAP_ADMIN_USERNAME')
    bootstrap_admin_password: Optional[str] = Field(None, alias='BOOTSTRAP_ADMIN_PASSWORD')
    bootstrap_admin_email: Optional[str] = Field(None, alias='BOOTSTRAP_ADMIN_EMAIL')
    min_password_length: int = Field(12, alias='MIN_PASSWORD_LENGTH')

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
