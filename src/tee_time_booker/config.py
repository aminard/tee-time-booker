from datetime import date, time
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tee_time_booker.constants import COURSES


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
    # How many minutes off-preferred-window we'll absorb to stay at a
    # higher-priority course. Each step down the course list adds this
    # many "minutes" to a slot's score. 0 = pure time-first (course
    # becomes a tiebreaker); large = course-first (current legacy
    # behavior). Calibrated default 25 keeps the user's top course as
    # long as it's within ~25 min of preferred.
    course_downgrade_minutes: int = Field(default=25, ge=0)
    # How long to keep re-searching + re-claiming after a round comes up
    # empty (sold out, or every claim sniped by faster competitors).
    # Slots reappear as other people's 15-min carts expire, so persisting
    # converts "lost the sprint" into "caught the cancellation". Patience-0
    # semantics: the FIRST grabbable slot ends the stakeout — the bot never
    # holds out hoping something better shows up. 0 = single attempt.
    stakeout_minutes: int = Field(default=15, ge=0)

    # --- Multi-day support ---
    # Extra day(s) to also search in the same run. Both days of a weekend
    # open at the same Monday 8 PM moment, so one run can chase both. Slots
    # from all days are pooled and ranked together.
    additional_dates: list[date] = Field(default_factory=list)
    # Which day wins when more than one has qualifying slots. Defaults to
    # target_date. Must be one of the target dates.
    preferred_day: date | None = None
    # What triggers falling back to a non-preferred day:
    #   "preferred" — jump to another day if the preferred day has no slot in
    #                 the PREFERRED window (8:30-10:30 etc.). "I'd rather a
    #                 morning on the other day than a late slot on this one."
    #   "outer"     — only jump if the preferred day has no slot at all in the
    #                 outer window. "Stay on my day unless it's totally empty."
    # Switchable per weekend via the plan file; no code change needed to flip.
    day_fallback_trigger: str = "preferred"

    @model_validator(mode="after")
    def _validate_courses(self) -> "Plan":
        # A typo'd slug would otherwise fail silently: rank_slots just
        # excludes courses it doesn't recognize, and you'd find out at
        # 8:00 PM Monday via "no slots match preferred courses".
        for c in self.courses + (self.preferred_course_order or []):
            if c not in COURSES:
                raise ValueError(
                    f"unknown course {c!r} — valid slugs: {sorted(COURSES)}"
                )
        return self

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

    @model_validator(mode="after")
    def _validate_multiday(self) -> "Plan":
        if self.day_fallback_trigger not in ("preferred", "outer"):
            raise ValueError(
                f"day_fallback_trigger must be 'preferred' or 'outer', "
                f"got {self.day_fallback_trigger!r}"
            )
        dates = self.all_dates()
        if self.preferred_day is not None and self.preferred_day not in dates:
            raise ValueError(
                f"preferred_day ({self.preferred_day}) must be one of the "
                f"target dates {dates}"
            )
        return self

    def courses_ranked(self) -> list[str]:
        if self.preferred_course_order is None:
            return self.courses
        ranked = [c for c in self.preferred_course_order if c in self.courses]
        ranked += [c for c in self.courses if c not in ranked]
        return ranked

    def all_dates(self) -> list[date]:
        """All dates to search, deduped, target_date first."""
        out = [self.target_date]
        for d in self.additional_dates:
            if d not in out:
                out.append(d)
        return out

    def day_order(self) -> list[date]:
        """Search/ranking day order — preferred day first, then the rest."""
        dates = self.all_dates()
        pref = self.preferred_day or self.target_date
        return [pref] + [d for d in dates if d != pref]


def load_plan(path: Path) -> Plan:
    import yaml

    with path.open() as f:
        raw = yaml.safe_load(f)
    return Plan.model_validate(raw)
