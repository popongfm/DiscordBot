from __future__ import annotations

from datetime import date


# Error strings for both command languages. Callers pass the already-localized
# field name, so only the surrounding sentence needs a translation here.
_ERRORS: dict[str, dict[str, str]] = {
    "th": {
        "required": "กรุณากรอก {field}",
        "too_long": "{field} ต้องมีความยาวไม่เกิน {max_length} ตัวอักษร",
        "bad_mmdd": "{field} ต้องอยู่ในรูปแบบ MM/DD",
        "end_before_start": "วันสิ้นสุด ต้องเป็นวันเดียวกับวันเริ่มหรือหลังจากนั้น",
        "start_date": "วันเริ่ม",
        "end_date": "วันสิ้นสุด",
    },
    "en": {
        "required": "Please provide a {field}.",
        "too_long": "The {field} must be at most {max_length} characters long.",
        "bad_mmdd": "The {field} must use the MM/DD format.",
        "end_before_start": "End date must be the same day as the start date or later.",
        "start_date": "start date",
        "end_date": "end date",
    },
}


def _text(lang: str, key: str) -> str:
    return _ERRORS.get(lang, _ERRORS["th"])[key]


def clean_required_text(value: str, field_name: str, max_length: int = 100, lang: str = "th") -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        raise ValueError(_text(lang, "required").format(field=field_name))
    if len(cleaned) > max_length:
        raise ValueError(_text(lang, "too_long").format(field=field_name, max_length=max_length))
    return cleaned


def parse_mmdd_date(value: str, field_name: str, lang: str = "th") -> date:
    try:
        month_str, day_str = value.strip().split("/", 1)
        return date(date.today().year, int(month_str), int(day_str))
    except (ValueError, TypeError) as exc:
        raise ValueError(_text(lang, "bad_mmdd").format(field=field_name)) from exc


def format_mmdd(value: date) -> str:
    return value.strftime("%m/%d")


def parse_date_range(start_value: str, end_value: str, lang: str = "th") -> tuple[date, date]:
    start_date = parse_mmdd_date(start_value, _text(lang, "start_date"), lang)
    end_date = parse_mmdd_date(end_value, _text(lang, "end_date"), lang)
    if end_date < start_date:
        raise ValueError(_text(lang, "end_before_start"))
    return start_date, end_date
