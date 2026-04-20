from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
import os

class BotConfig(BaseSettings):
    # Credentials
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=6)
    server_url: HttpUrl = Field(...)
    ssl_verify: bool = Field(default=False)
    
    # Operational Limits
    max_queue_size: int = Field(default=1000, ge=1, le=10000)
    poll_interval: float = Field(default=3.0, ge=0.5, le=30.0)
    api_timeout: int = Field(default=30, ge=5, le=120)
    max_copies: int = Field(default=10, ge=1, le=99)
    rate_limit_seconds: int = Field(default=2, ge=1, le=10)
    max_file_size_mb: int = Field(default=50, ge=1, le=500)
    max_concurrent_downloads: int = Field(default=5, ge=1, le=20)
    
    # Paths
    downloads_dir: str = Field(default="downloads")
    storage_file: str = Field(default="user_settings.json")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ROCKET_",
        extra="ignore",
        validate_default=True,
    )

# Singleton instance
config = BotConfig()
