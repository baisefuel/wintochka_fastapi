from pydantic import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 8000

    class Config:
        env_file = ".env"

settings = Settings()
