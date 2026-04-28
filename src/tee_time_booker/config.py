from datetime import date, time
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, model_validator
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
    bill_address1: str
    bill_address2: str = ""
    bill_city: str
    bill_state: str
    bill_zip: str
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
    earliest_time: time   # outer fallback window — slots outside this are excluded entirely
    latest_time: time
    holes: int = Field(default=18, ge=9, le=18)
    num_players: int = Field(default=4, ge=1, le=5)
    courses: list[str] = Field(min_length=1)
    preferred_course_order: list[str] | None = None
    # Optional inner range. When both are set, slots falling within
    # [preferred_earliest, preferred_latest] rank above slots outside it
    # (but still inside the outer [earliest_time, latest_time] window).
    # Lets the user say "I'd really like 8:30-10 AM, but I'll take any
    # 7-12 slot if nothing in that range is available."
    preferred_earliest: time | None = None
    preferred_latest: time | None = None

    @model_validator(mode="after")
    def _validate_preferred_range(self) -> "Plan":
        if (self.preferred_earliest is None) != (self.preferred_latest is None):
            raise ValueError(
                "preferred_earliest and preferred_latest must both be set or both omitted"
            )
        if self.preferred_earliest is not None and self.preferred_latest is not None:
            if self.preferred_earliest > self.preferred_latest:
                raise ValueError(
                    f"preferred_earliest ({self.preferred_earliest}) must be <= "
                    f"preferred_latest ({self.preferred_latest})"
                )
            if self.preferred_earliest < self.earliest_time:
                raise ValueError(
                    f"preferred_earliest ({self.preferred_earliest}) must be >= "
                    f"earliest_time ({self.earliest_time}) — the inner range "
                    f"can't extend before the outer window"
                )
            if self.preferred_latest > self.latest_time:
                raise ValueError(
                    f"preferred_latest ({self.preferred_latest}) must be <= "
                    f"latest_time ({self.latest_time}) — the inner range "
                    f"can't extend past the outer window"
                )
        return self

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
