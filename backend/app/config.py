from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    database_url: str = 'sqlite:///./data/guardian.db'
    app_env: str = 'development'
    operator_token: str = 'local-demo-only'
    signing_secret: str = 'local-demo-signing-secret-change-before-deploy'
    public_url: str = 'http://localhost:8000'
    allowed_origins: str = 'http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173'
    demo_enabled: bool = True
    worker_enabled: bool = True
    gemini_api_key: str = ''
    gemini_model: str = 'gemini-3.8-flash'
    gemini_timeout_seconds: float = 15
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
        if self.app_env == 'production':
            if len(self.operator_token) < 32 or len(self.signing_secret) < 32 or 'local-demo' in self.signing_secret:
                raise ValueError('Production requires unique operator and signing secrets of at least 32 characters.')
            if not self.public_url.startswith('https://') or self.demo_enabled:
                raise ValueError('Production requires HTTPS and DEMO_ENABLED=false.')
        if self.notification_mode not in ('dry_run', 'live'):
            raise ValueError('NOTIFICATION_MODE must be dry_run or live')
        if self.database_url.startswith('sqlite'):
            Path('data').mkdir(exist_ok=True)


settings = Settings()
