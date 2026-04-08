from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    neon_database_url: str
    neon_auth_base_url: str
    openai_api_key: str = ""
    gemini_api_key: str = ""
    deepgram_api_key: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    app_secret_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
