from pathlib import Path

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_markets_path = Path(__file__).parent / "markets.yaml"
with _markets_path.open() as _f:
    MARKETS: list[dict] = yaml.safe_load(_f)["markets"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = Field("local", alias="ENV")

    # Polymarket WebSocket
    pm_ws_url: str = Field(
        "wss://ws-subscriptions-clob.polymarket.com",
        alias="PM_WS_URL",
    )
    # Must match the actual market cadence (5 for 5-minute markets, 15 for 15-minute)
    candle_interval_minutes: int = Field(5, alias="CANDLE_INTERVAL_MINUTES")

    # Storage
    local_data_dir: str = Field("/data", alias="LOCAL_DATA_DIR")
    aws_bucket: str = Field("", alias="AWS_BUCKET")
    aws_region: str = Field("eu-central-1", alias="AWS_REGION")

    # Collector tuning
    tick_interval_seconds: float = Field(1.0, alias="TICK_INTERVAL_SECONDS")
    export_interval_minutes: int = Field(5, alias="EXPORT_INTERVAL_MINUTES")

    # WebSocket reconnection backoff
    reconnect_base_delay: float = Field(1.0, alias="RECONNECT_BASE_DELAY")
    reconnect_max_delay: float = Field(60.0, alias="RECONNECT_MAX_DELAY")


settings = Settings()
