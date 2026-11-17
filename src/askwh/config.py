from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()  # the Anthropic SDK reads its key from the process environment

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The owner connection loads data and stores app state. The read-only connection is the
    # only one the agent's queries ever run on.
    database_url: str = "postgresql://askwh:askwh@localhost:5436/warehouse"
    readonly_database_url: str = "postgresql://askwh_ro:askwh_ro@localhost:5436/warehouse"

    model: str = "claude-haiku-5-5"
    budget_usd: float = 5.0

    metrics_path: Path = ROOT / "semantic" / "metrics.yml"

    # Simulated stream: insert new orders and production runs while the app is running.
    live_ingest: bool = True
    # Answers are flagged as stale when the newest row is older than this.
    stale_after_seconds: int = 300

    # Digests and alerts are POSTed here as {"text": ...}, the shape Slack webhooks accept.
    # When unset they are kept in the in-app notification list only.
    webhook_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
