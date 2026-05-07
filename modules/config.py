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
    deepgram_api_key: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    google_redirect_uri_web: str = ""
    app_secret_key: str = ""
    cors_origins: str = "https://cogniv.co.in"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_pro_plan_id: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

settings = Settings()
