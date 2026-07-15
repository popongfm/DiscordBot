from __future__ import annotations

from datetime import date


def clean_required_text(value: str, field_name: str, max_length: int = 100) -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        raise ValueError(f"{field_name} is required.")
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} must be {max_length} characters or less.")
    return cleaned


def parse_iso_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD format.") from exc


def parse_date_range(start_value: str, end_value: str) -> tuple[date, date]:
    start_date = parse_iso_date(start_value, "start_date")
    end_date = parse_iso_date(end_value, "end_date")
    if end_date < start_date:
        raise ValueError("end_date must be the same day as start_date or later.")
    return start_date, end_date
