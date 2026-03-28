import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SK_", env_file=".env", extra="ignore")

    # Media library
    media_dir: Path = Path("/tmp/karaoke")
    supported_video_extensions: tuple[str, ...] = (".mp4", ".mkv", ".avi", ".webm", ".mov")
    supported_audio_extensions: tuple[str, ...] = (".mp3",)
    supported_cdg_extensions: tuple[str, ...] = (".cdg",)

    # Server
    host: str = "0.0.0.0"
    port: int = 8080

    # Database
    db_path: Path = Path("superkaraoke.db")

    # Auth — comma-separated CIDR subnets that skip authentication.
    # Example: "192.168.0.0/16,10.0.0.0/8"
    # Empty = all remote clients must log in (bootstrap mode allows open
    # access until the first user account is created).
    allowed_networks: str = "192.168.0.0/16"

    # Streaming
    stream_chunk_size: int = 65536  # 64 KB
    ffmpeg_loglevel: str = "warning"

    # Frontend build output (relative to project root)
    static_dir: Path = Path("frontend/dist")


settings = Settings()
