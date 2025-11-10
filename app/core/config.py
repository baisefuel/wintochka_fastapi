from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "Wintochka"
    app_env: str = "development"

    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    
    quote_asset: str = "RUB" 

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra='ignore')

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}@"
            f"{self.db_host}:{self.db_port}/{self.db_name}"
        )

settings = Settings()