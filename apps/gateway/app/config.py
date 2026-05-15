from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgres://dexter:dexter@db:5432/dexter"
    LOG_LEVEL: str = "INFO"
    AUTO_SEED: bool = True


settings = Settings()
