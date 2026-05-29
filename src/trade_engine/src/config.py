from pathlib import Path

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_models_path = Path(__file__).parent / "models.yaml"
with _models_path.open() as _f:
    MODELS: list[dict] = yaml.safe_load(_f)["models"]

for _m in MODELS:
    _m.setdefault("type", "ml")
    _m.setdefault("stop_loss_delta", None)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = Field("local", alias="ENV")
    local_data_dir: str = Field("/data", alias="LOCAL_DATA_DIR")
    aws_bucket: str = Field("", alias="AWS_BUCKET")
    aws_region: str = Field("eu-central-1", alias="AWS_REGION")

    candle_interval_minutes: int = Field(5, alias="CANDLE_INTERVAL_MINUTES")
    min_edge_threshold: float = Field(0.01, alias="MIN_EDGE_THRESHOLD")
    pm_fee: float = Field(0.02, alias="PM_FEE")


settings = Settings()
