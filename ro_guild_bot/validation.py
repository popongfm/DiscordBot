from __future__ import annotations

from datetime import date


def clean_required_text(value: str, field_name: str, max_length: int = 100) -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        raise ValueError(f"กรุณากรอก {field_name}")
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} ต้องมีความยาวไม่เกิน {max_length} ตัวอักษร")
    return cleaned


def parse_mmdd_date(value: str, field_name: str) -> date:
    try:
        month_str, day_str = value.strip().split("/", 1)
        return date(date.today().year, int(month_str), int(day_str))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field_name} ต้องอยู่ในรูปแบบ MM/DD") from exc


def format_mmdd(value: date) -> str:
    return value.strftime("%m/%d")


def parse_date_range(start_value: str, end_value: str) -> tuple[date, date]:
    start_date = parse_mmdd_date(start_value, "วันเริ่ม")
    end_date = parse_mmdd_date(end_value, "วันสิ้นสุด")
    if end_date < start_date:
        raise ValueError("วันสิ้นสุด ต้องเป็นวันเดียวกับวันเริ่มหรือหลังจากนั้น")
    return start_date, end_date
