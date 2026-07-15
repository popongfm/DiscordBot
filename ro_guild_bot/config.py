from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class BotConfig:
    discord_token: str
    discord_guild_id: int
    absence_channel_id: int
    member_log_channel_id: int
    google_sheet_id: str
    google_service_account_file: Path


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _required_int(name: str) -> int:
    value = _required(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a Discord numeric ID") from exc


def load_config() -> BotConfig:
    load_dotenv()

    return BotConfig(
        discord_token=_required("DISCORD_TOKEN"),
        discord_guild_id=_required_int("DISCORD_GUILD_ID"),
        absence_channel_id=_required_int("ABSENCE_CHANNEL_ID"),
        member_log_channel_id=_required_int("MEMBER_LOG_CHANNEL_ID"),
        google_sheet_id=_required("GOOGLE_SHEET_ID"),
        google_service_account_file=Path(_required("GOOGLE_SERVICE_ACCOUNT_FILE")),
    )
