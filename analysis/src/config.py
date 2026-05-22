from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    local_data_dir: str = Field("/data", alias="LOCAL_DATA_DIR")
    candle_interval_minutes: int = Field(5, alias="CANDLE_INTERVAL_MINUTES")
    pm_fee: float = Field(0.02, alias="PM_FEE")
    min_edge_threshold: float = Field(0.01, alias="MIN_EDGE_THRESHOLD")

    # Lookup table bucketing — override via TIME_BUCKET_SECONDS / PCT_CHANGE_BUCKET_SIZE / MIN_BUCKET_COUNT
    time_bucket_seconds: int = Field(10)
    pct_change_bucket_size: float = Field(0.0001)
    min_bucket_count: int = Field(30)


settings = Settings()
