from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    neon_database_url: str
    neon_auth_base_url: str
    # Optional full URL for Neon Auth (Better Auth) refresh, e.g. https://.../neondb/auth/refresh
    neon_auth_refresh_url: str = ""
    openai_api_key: str = ""
    # Accepts both GEMINI_API_KEY and GENAI_API_KEY from the environment
    gemini_api_key: str = Field(default="", validation_alias=AliasChoices("GEMINI_API_KEY", "GENAI_API_KEY"))
    # Accepts both ANTHROPIC_API_KEY and CLAUDE_API_KEY from the environment
    anthropic_api_key: str = Field(default="", validation_alias=AliasChoices("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"))
    deepgram_api_key: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    google_redirect_uri_web: str = ""
    # Native mobile OAuth client IDs (used as accepted `aud` values when
    # verifying the id_token from the Google Sign-In SDK).
    google_android_client_id: str = ""
    google_ios_client_id: str = ""
    app_secret_key: str = ""
    cors_origins: str = "https://cogniv.co.in,https://www.cogniv.co.in"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    # Secret set on the Razorpay dashboard webhook (Settings → Webhooks).
    razorpay_webhook_secret: str = ""
    # Where the Neon Auth reset email sends the user (?token=... is appended).
    # Its origin must be a trusted origin in the Neon Auth settings.
    password_reset_redirect_url: str = "https://www.cogniv.co.in/reset-password/"
    # Error reporting; Sentry stays off unless SENTRY_DSN is set.
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

settings = Settings()
