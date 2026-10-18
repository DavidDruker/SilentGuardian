from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    database_url: str = 'sqlite:///./data/guardian.db'
    app_env: str = 'development'
    operator_token: str = 'local-demo-only'
    signing_secret: str = 'local-demo-signing-secret-change-before-deploy'
    public_url: str = 'http://localhost:8000'
    allowed_origins: str = 'http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173,http://127.0.0.1:5173'
    demo_enabled: bool = False
    worker_enabled: bool = True
    action_model_mode: str = 'shadow'
    action_model_path: str = 'models/stgcnpp-ntu60-2d.pth'
    action_model_threads: int = 2
    rules_service_url: str = ''
    ml_service_url: str = ''
    internal_service_token: str = 'local-internal-demo-token'
    # Rules flag candidates generously; the ML decides which become alerts.
    decision_mode: str = 'enforce'
    # Staged falls scored 0.72 (close) and 0.34 (far, partly tracked); the model is only
    # asked about fall candidates, where walking and stumbles scored 0.00-0.04.
    fall_score_threshold: float = .25
    model_timeout_seconds: float = 2
    personalization_previous_weight: float = .8
    personalization_timezone: str = 'America/Toronto'
    gemini_api_key: str = ''
    gemini_model: str = 'gemini-3.8-flash'
    gemini_timeout_seconds: float = 30
    gemini_billed_project_confirmed: bool = False
    notification_mode: str = 'dry_run'
    twilio_account_sid: str = ''
    twilio_auth_token: str = ''
    twilio_from_number: str = ''
    call_test_allowlist: str = ''
    demo_call_to: str = ''
    calls_enabled: bool = False
    call_cooldown_seconds: int = 300
    call_daily_limit: int = 5
    tiger_budget_limit: float = 1000
    vapid_private_key: str = ''
    vapid_public_key: str = ''
    vapid_subject: str = 'mailto:demo@example.com'

    def validate_runtime(self):
        if self.action_model_mode not in ('off', 'on', 'shadow'):
            raise ValueError('ACTION_MODEL_MODE must be on or off')
        if self.decision_mode not in ('shadow', 'enforce'):
            raise ValueError('DECISION_MODE must be shadow or enforce')
        if not 0 < self.fall_score_threshold < 1 or not .1 <= self.model_timeout_seconds <= 10:
            raise ValueError('Model threshold or timeout is outside supported range')
        if not 1 <= self.action_model_threads <= 8:
            raise ValueError('ACTION_MODEL_THREADS must be between 1 and 8')
        if not 0 <= self.personalization_previous_weight < 1:
            raise ValueError('PERSONALIZATION_PREVIOUS_WEIGHT must be at least 0 and less than 1')
        try:
            ZoneInfo(self.personalization_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError('PERSONALIZATION_TIMEZONE must be a valid IANA timezone') from exc
        if self.app_env == 'production':
            if not self.database_url.startswith('postgresql+psycopg://'):
                raise ValueError('Production requires DATABASE_URL=postgresql+psycopg://... for the Tiger Data service.')
            if len(self.operator_token) < 32 or len(self.signing_secret) < 32 or 'local-demo' in self.signing_secret:
                raise ValueError('Production requires unique operator and signing secrets of at least 32 characters.')
            # Labelled synthetic replays may stay on for a hosted demo; secrets and HTTPS never relax.
            if not self.public_url.startswith('https://'):
                raise ValueError('Production requires an HTTPS PUBLIC_URL.')
            if (self.rules_service_url or self.ml_service_url) and len(self.internal_service_token) < 32:
                raise ValueError('Production internal services require a unique token of at least 32 characters.')
        if self.notification_mode not in ('dry_run', 'live'):
            raise ValueError('NOTIFICATION_MODE must be dry_run or live')
        if self.database_url.startswith('sqlite'):
            Path('data').mkdir(exist_ok=True)


settings = Settings()
