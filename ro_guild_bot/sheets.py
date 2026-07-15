from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import gspread
from gspread.exceptions import WorksheetNotFound


MEMBERS_SHEET = "Members"
ABSENCES_SHEET = "Absences"

MEMBER_HEADERS = [
    "Discord User ID",
    "Discord Display Name",
    "Character Name",
    "Class",
    "Real Nickname",
    "Created At",
    "Updated At",
]

ABSENCE_HEADERS = [
    "Request ID",
    "Discord User ID",
    "Character Name",
    "Start Date",
    "End Date",
    "Reason",
    "Submitted At",
]


@dataclass(frozen=True)
class MemberProfile:
    discord_user_id: str
    discord_display_name: str
    character_name: str
    character_class: str
    real_nickname: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: list[str]) -> "MemberProfile":
        padded = row + [""] * (len(MEMBER_HEADERS) - len(row))
        return cls(
            discord_user_id=str(padded[0]).strip(),
            discord_display_name=str(padded[1]).strip(),
            character_name=str(padded[2]).strip(),
            character_class=str(padded[3]).strip(),
            real_nickname=str(padded[4]).strip(),
            created_at=str(padded[5]).strip(),
            updated_at=str(padded[6]).strip(),
        )

    def to_row(self) -> list[str]:
        return [
            self.discord_user_id,
            self.discord_display_name,
            self.character_name,
            self.character_class,
            self.real_nickname,
            self.created_at,
            self.updated_at,
        ]


@dataclass(frozen=True)
class AbsenceRequest:
    request_id: str
    discord_user_id: str
    character_name: str
    start_date: date
    end_date: date
    reason: str
    submitted_at: str

    def to_row(self) -> list[str]:
        return [
            self.request_id,
            self.discord_user_id,
            self.character_name,
            self.start_date.isoformat(),
            self.end_date.isoformat(),
            self.reason,
            self.submitted_at,
        ]


class GuildSheetStore:
    def __init__(self, spreadsheet: Any):
        self.spreadsheet = spreadsheet
        self.members = self._ensure_worksheet(MEMBERS_SHEET, MEMBER_HEADERS)
        self.absences = self._ensure_worksheet(ABSENCES_SHEET, ABSENCE_HEADERS)

    @classmethod
    def from_service_account_file(
        cls,
        credentials_path: str | Path,
        spreadsheet_id: str,
    ) -> "GuildSheetStore":
        client = gspread.service_account(filename=str(credentials_path))
        spreadsheet = client.open_by_key(spreadsheet_id)
        return cls(spreadsheet)

    def _ensure_worksheet(self, title: str, headers: list[str]) -> Any:
        try:
            worksheet = self.spreadsheet.worksheet(title)
        except WorksheetNotFound:
            worksheet = self.spreadsheet.add_worksheet(
                title=title,
                rows=100,
                cols=len(headers),
            )

        header_range = f"A1:{_column_name(len(headers))}1"
        current_headers = worksheet.row_values(1)
        if current_headers[: len(headers)] != headers:
            worksheet.update([headers], header_range)
        return worksheet

    def get_member(self, discord_user_id: str | int) -> MemberProfile | None:
        row_number = self._find_member_row(discord_user_id)
        if row_number is None:
            return None
        return MemberProfile.from_row(self.members.row_values(row_number))

    def upsert_member(
        self,
        discord_user_id: str | int,
        discord_display_name: str,
        character_name: str,
        character_class: str,
        real_nickname: str,
    ) -> tuple[MemberProfile, bool]:
        user_id = str(discord_user_id)
        now = _utc_now()
        row_number = self._find_member_row(user_id)

        if row_number is None:
            profile = MemberProfile(
                discord_user_id=user_id,
                discord_display_name=discord_display_name,
                character_name=character_name,
                character_class=character_class,
                real_nickname=real_nickname,
                created_at=now,
                updated_at=now,
            )
            self.members.append_row(profile.to_row(), value_input_option="RAW")
            return profile, True

        existing = MemberProfile.from_row(self.members.row_values(row_number))
        profile = MemberProfile(
            discord_user_id=user_id,
            discord_display_name=discord_display_name,
            character_name=character_name,
            character_class=character_class,
            real_nickname=real_nickname,
            created_at=existing.created_at or now,
            updated_at=now,
        )
        self._write_member(row_number, profile)
        return profile, False

    def update_character_name(
        self,
        discord_user_id: str | int,
        new_character_name: str,
    ) -> MemberProfile | None:
        return self._update_member_fields(
            discord_user_id,
            character_name=new_character_name,
        )

    def update_character_class(
        self,
        discord_user_id: str | int,
        new_character_class: str,
    ) -> MemberProfile | None:
        return self._update_member_fields(
            discord_user_id,
            character_class=new_character_class,
        )

    def append_absence(
        self,
        member: MemberProfile,
        start_date: date,
        end_date: date,
        reason: str,
    ) -> AbsenceRequest:
        submitted_at = _utc_now()
        request = AbsenceRequest(
            request_id=_request_id(),
            discord_user_id=member.discord_user_id,
            character_name=member.character_name,
            start_date=start_date,
            end_date=end_date,
            reason=reason,
            submitted_at=submitted_at,
        )
        self.absences.append_row(request.to_row(), value_input_option="RAW")
        return request

    def _find_member_row(self, discord_user_id: str | int) -> int | None:
        target = str(discord_user_id).strip()
        user_ids = self.members.col_values(1)
        for row_number, value in enumerate(user_ids[1:], start=2):
            if str(value).strip() == target:
                return row_number
        return None

    def _update_member_fields(
        self,
        discord_user_id: str | int,
        *,
        character_name: str | None = None,
        character_class: str | None = None,
    ) -> MemberProfile | None:
        row_number = self._find_member_row(discord_user_id)
        if row_number is None:
            return None

        existing = MemberProfile.from_row(self.members.row_values(row_number))
        updated = MemberProfile(
            discord_user_id=existing.discord_user_id,
            discord_display_name=existing.discord_display_name,
            character_name=character_name or existing.character_name,
            character_class=character_class or existing.character_class,
            real_nickname=existing.real_nickname,
            created_at=existing.created_at,
            updated_at=_utc_now(),
        )
        self._write_member(row_number, updated)
        return updated

    def _write_member(self, row_number: int, profile: MemberProfile) -> None:
        row_range = f"A{row_number}:{_column_name(len(MEMBER_HEADERS))}{row_number}"
        self.members.update([profile.to_row()], row_range)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _request_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    suffix = secrets.token_hex(3).upper()
    return f"ABS-{timestamp}-{suffix}"


def _column_name(index: int) -> str:
    if index < 1:
        raise ValueError("index must be 1 or greater")

    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name
