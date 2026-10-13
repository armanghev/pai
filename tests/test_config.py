from pathlib import Path

import pytest
from pydantic import ValidationError

from pai.config import Config, Secrets, load_config

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads():
    config = load_config(ROOT / "config.example.toml")
    assert config.timezone == "UTC" and config.tz.key == "UTC"
    assert config.schedule.work == [] and config.shift_end("mon") is None
    assert config.weather.city == "" and config.leads.area == "" and config.github.user == ""
    assert config.profile.about == "" and config.money.labels == {}
    assert config.models.chat == "claude-haiku-5-5"


def test_missing_config_file_is_loud(tmp_path):
    with pytest.raises(FileNotFoundError, match="config.example.toml"):
        load_config(tmp_path / "config.toml")


@pytest.mark.parametrize(
    "block",
    [
        {"days": ["mon"], "start": "8:00", "end": "16:30"},
        {"days": ["mon"], "start": "17:00", "end": "16:30"},
        {"days": ["funday"], "start": "08:00", "end": "16:30"},
        {"days": [], "start": "08:00", "end": "16:30"},
    ],
)
def test_bad_schedule_blocks_rejected(block):
    with pytest.raises(ValidationError):
        Config.model_validate({"schedule": {"work": [block]}})


def test_bad_timezone_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({"timezone": "Mars/Olympus_Mons"})


def test_secrets_require_core_values(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    for var in ("TELEGRAM_TOKEN", "TELEGRAM_USER_ID", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValidationError):
        Secrets()


def test_secrets_from_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for var in ("CANVAS_BASE_URL", "CANVAS_TOKEN", "GITHUB_TOKEN"):  # envy may inject these
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_USER_ID", "42")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    s = Secrets()
    assert s.telegram_user_id == 42
    assert s.canvas_token is None
    assert "sk-test" not in repr(s)  # secrets never print
