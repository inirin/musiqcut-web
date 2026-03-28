from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    suno_api_key: str = ""
    gemini_api_key: str = ""
    imagen_api_keys: str = ""  # 쉼표 구분 복수 키 (예: "key1,key2,key3")

    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_redirect_uri: str = "https://musiqcut.com/api/upload/youtube/callback"

    instagram_app_id: str = ""
    instagram_app_secret: str = ""
    instagram_redirect_uri: str = "https://musiqcut.com/api/upload/instagram/callback"

    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = "https://musiqcut.com/api/upload/tiktok/callback"

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    debug: bool = True
    storage_base_path: str = "./storage"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @property
    def storage_path(self) -> Path:
        return Path(self.storage_base_path)

    def missing_keys(self) -> list[str]:
        missing = []
        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        return missing


settings = Settings()
