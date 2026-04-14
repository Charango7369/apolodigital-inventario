from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    # Base de datos
    database_url: str

    # Seguridad JWT
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # Entorno
    environment: str = "development"
    debug: bool = False

    # CORS — separar por comas en el .env
    # Ej: ALLOWED_ORIGINS=http://localhost:3000,https://apolodigital.lat
    allowed_origins: str = "http://localhost:3000,http://localhost:8080"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """
    Instancia única de Settings durante toda la vida de la app.
    lru_cache garantiza que el .env se lea una sola vez.
    Uso: from app.config import get_settings; s = get_settings()
    """
    return Settings()
