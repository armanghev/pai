"""Typed config: secrets from .env, everything else from config.toml.

Both load once at startup and fail loudly if something required is missing.
"""

from __future__ import annotations

import re
import tomllib
from datetime import time
from functools import cached_property
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DAYS: list[Day] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _hhmm_field(v: str) -> str:
    if not _HHMM.match(v):
        raise ValueError(f"expected HH:MM, got {v!r}")
    return v


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    telegram_token: SecretStr
    telegram_user_id: int
    anthropic_api_key: SecretStr
    canvas_base_url: str | None = None
    canvas_token: SecretStr | None = None
    github_token: SecretStr | None = None
    # Plaid, read-only. Unset (or malformed tokens) means money features stay off.
    plaid_client_id: str | None = None
    plaid_secret: SecretStr | None = None
    plaid_env: str = "sandbox"
    plaid_access_tokens: SecretStr | None = None  # JSON: {"mybank": "access-...", ...}
    # Encrypts access tokens Pai links itself (/money link). Any long random string.
    pai_token_key: SecretStr | None = None


class Block(BaseModel):
    """A recurring weekly block of busy time."""

    name: str = ""
    days: list[Day] = Field(min_length=1)
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        return _hhmm_field(v)

    @model_validator(mode="after")
    def _order(self) -> Block:
        if self.start_time >= self.end_time:
            raise ValueError(f"start {self.start} must be before end {self.end}")
        return self

    @property
    def start_time(self) -> time:
        return time.fromisoformat(self.start)

    @property
    def end_time(self) -> time:
        return time.fromisoformat(self.end)


class Schedule(BaseModel):
    work: list[Block] = []
    classes: list[Block] = []
    # When studying is welcome. Free blocks are these windows minus work and classes.
    study: list[Block] = [
        Block(days=["mon", "tue", "wed", "thu", "fri"], start="17:30", end="22:00"),
        Block(days=["sat", "sun"], start="10:00", end="18:00"),
    ]
    min_block_minutes: int = Field(default=30, ge=15)


class Jobs(BaseModel):
    weekly_plan_day: Day = "sun"
    weekly_plan_time: str = "20:00"
    briefing_time: str = "06:45"
    standup_day: Day = "sun"
    standup_time: str = "18:00"
    grade_watch_hours: int = Field(default=2, ge=1)
    grade_watch_start: str = "07:00"
    grade_watch_end: str = "23:00"
    recap_time: str = "21:00"
    habits_time: str = "21:15"
    habits_report_day: Day = "sun"
    leads_day: Day = "mon"
    leads_time: str = "09:00"
    review_day: Day = "sun"
    review_time: str = "19:00"
    feeds_minutes: int = Field(default=30, ge=10)  # Canvas announcements and inbox
    price_time: str = "10:00"
    drift_time: str = "18:30"  # vault drift check (needs [vault] path)
    heartbeat_minutes: int = Field(default=45, ge=30, le=60)  # background check-ins
    # "Shift's over" recap: unset = right when each [[schedule.work]] block ends.
    shift_time: str | None = None
    quiet_start: str = "23:00"  # job alerts arrive silently between these times
    quiet_end: str = "07:00"

    @field_validator(
        "weekly_plan_time",
        "briefing_time",
        "standup_time",
        "grade_watch_start",
        "grade_watch_end",
        "recap_time",
        "habits_time",
        "leads_time",
        "review_time",
        "price_time",
        "drift_time",
        "quiet_start",
        "quiet_end",
    )
    @classmethod
    def _hhmm(cls, v: str) -> str:
        return _hhmm_field(v)

    @field_validator("shift_time")
    @classmethod
    def _shift(cls, v: str | None) -> str | None:
        return _hhmm_field(v) if v else None


class Weather(BaseModel):
    city: str = ""  # e.g. "Chicago"; empty = no weather in the briefing
    units: Literal["fahrenheit", "celsius"] = "fahrenheit"


class Site(BaseModel):
    name: str = ""
    url: str


class Monitor(BaseModel):
    sites: list[Site] = []
    interval_minutes: int = Field(default=5, ge=1)
    timeout_seconds: float = 10
    temp_alert_c: float = 75
    disk_alert_pct: float = 85


class GitHub(BaseModel):
    user: str = ""  # your GitHub login; empty = looked up from GITHUB_TOKEN
    # Empty means every repo you pushed to in the last `active_days` days.
    repos: list[str] = []
    active_days: int = 30
    poll_minutes: int = Field(default=30, ge=5)


class Vault(BaseModel):
    """The Obsidian second-brain vault. Unset = vault features off."""

    path: Path | None = None
    inbox: str = "inbox"
    quiet_days: int = Field(default=4, ge=1)  # nudge when an active project goes this long
    # Pull before reading and push captures. For a clone of the vault repo on a server.
    git_sync: bool = False


class Models(BaseModel):
    chat: str = "claude-haiku-5-5"
    planner: str = "claude-sonnet-5-5"
    deep: str = "claude-opus-5-5"


class Budget(BaseModel):
    daily_usd: float = Field(default=1.0, gt=0)


class Digest(BaseModel):
    """Hacker News picks in the morning briefing."""

    enabled: bool = True
    count: int = Field(default=5, ge=1, le=15)
    # Front-page stories matching any of these words rank first; empty = top stories.
    topics: list[str] = []


class Leads(BaseModel):
    """Weekly lead finder for web-dev client work. Off until `area` is set."""

    enabled: bool = True
    area: str = ""  # e.g. "Austin, TX"
    # Optional places inside the area to spread the search across.
    neighborhoods: list[str] = []
    # Who the leads are for, as the prompt describes you.
    pitch_for: str = "a web developer who builds sites for small businesses"
    count: int = Field(default=5, ge=1, le=10)
    # Rotated one per week.
    categories: list[str] = [
        "restaurants and cafes",
        "auto repair shops",
        "barbers and salons",
        "contractors and home services",
        "dentists and small clinics",
        "fitness studios and gyms",
        "retail boutiques",
        "cleaning services",
    ]


class Profile(BaseModel):
    """About you and where Pai runs. Fills placeholders in the prompts."""

    # One or two sentences, e.g. "a student who works full time and takes evening classes".
    about: str = ""
    home: str = "a small computer in your owner's home"  # where the owl "lives"
    school: str = "college"  # writing level for reference drafts


class Money(BaseModel):
    """Display names for Plaid item labels, e.g. {bank = "My Bank"}."""

    labels: dict[str, str] = {}


class Config(BaseModel):
    timezone: str = "UTC"  # IANA name, e.g. "America/Chicago"
    db_path: Path = Path("data/pai.db")
    models: Models = Models()
    budget: Budget = Budget()
    schedule: Schedule = Schedule()
    jobs: Jobs = Jobs()
    weather: Weather = Weather()
    monitor: Monitor = Monitor()
    github: GitHub = GitHub()
    notes_path: Path = Path("data/notes.md")
    vault: Vault = Vault()
    digest: Digest = Digest()
    leads: Leads = Leads()
    profile: Profile = Profile()
    money: Money = Money()

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v

    @field_validator("vault")
    @classmethod
    def _vault(cls, v: Vault) -> Vault:
        if v.path is not None:
            v.path = v.path.expanduser()
            if not (v.path / "projects").is_dir():
                raise ValueError(f"vault path {v.path} has no projects/ folder")
        return v

    def shift_end(self, day: str) -> str | None:
        """When work ends on `day` ("mon".."sun"): jobs.shift_time if set, else the latest
        end among that day's [[schedule.work]] blocks. None = no work that day."""
        ends = [b.end for b in self.schedule.work if day in b.days]
        if not ends:
            return None
        return self.jobs.shift_time or max(ends)

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_config(path: Path | str = "config.toml") -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; copy config.example.toml to {path}")
    with path.open("rb") as f:
        return Config.model_validate(tomllib.load(f))
