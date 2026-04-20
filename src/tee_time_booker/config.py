from datetime import date, time
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Secrets(BaseSettings):
    """Credentials and PII, loaded from .env (gitignored)."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="TEE_TIME_BOOKER_", extra="ignore")

    base_url: str  # full URL prefix of the reservation platform, e.g. https://<host>/<path>
    username: str
    password: SecretStr
    member_id: str
    bill_firstname: str
    bill_lastname: str
    bill_phone: str
    bill_email: str

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    notify_to: str | None = None


class Plan(BaseModel):
    """One booking attempt — date, preferences, party. Loaded from a YAML file."""

    target_date: date
    earliest_time: time
    latest_time: time
    holes: int = Field(default=18, ge=9, le=18)
    num_players: int = Field(default=4, ge=1, le=5)
    courses: list[str] = Field(min_length=1)
    preferred_course_order: list[str] | None = None

    def courses_ranked(self) -> list[str]:
        if self.preferred_course_order is None:
            return self.courses
        ranked = [c for c in self.preferred_course_order if c in self.courses]
        ranked += [c for c in self.courses if c not in ranked]
        return ranked


def load_plan(path: Path) -> Plan:
    import yaml

    with path.open() as f:
        raw = yaml.safe_load(f)
    return Plan.model_validate(raw)
