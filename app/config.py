import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    devin_api_key: str
    devin_org_id: str
    target_repo: str = "razasekha/superset-devin"

    scan_cron: str = "0 * * * *"

    max_acu_injector: int = 10
    max_acu_scanner: int = 15
    max_acu_fixer: int = 20

    poll_interval_seconds: int = 15
    poll_timeout_seconds: int = 3600

    github_token: str = ""
    github_repo: str = "razasekha/superset-devin"

    db_path: str = os.getenv("DB_PATH", "orchestrator.db")

    devin_base_url: str = "https://api.devin.ai/v3"

    max_parallel_fixers: int = 5
    pr_merge_poll_minutes: int = 2

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
