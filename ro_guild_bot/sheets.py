from __future__ import annotations

import csv
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

import gspread
from gspread.exceptions import APIError, WorksheetNotFound


logger = logging.getLogger(__name__)

MEMBERS_SHEET = "Members"
ABSENCES_SHEET = "Absences"
NAME_CHANGES_SHEET = "Name Changes"
REGISTRATIONS_SHEET = "Registrations"

# Registration request lifecycle.
STATUS_PENDING = "Pending"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"

# Persisted member columns. 'Discord Display Name', 'Created At' and
# 'Updated At' are intentionally NOT stored in the sheet. They still exist as
# in-memory fields on MemberProfile so the Discord logs render unchanged
# (the member-log footer stamps updated_at at write time).
MEMBER_HEADERS = [
    "Discord User ID",
    "Character Name",
    "Class",
    "Real Nickname",
    "Member ID",
]

_MEMBER_ID_RE = re.compile(r"^P(\d+)$", re.IGNORECASE)

ABSENCE_HEADERS = [
    "Request ID",
    "Discord User ID",
    "Character Name",
    "Start Date",
    "End Date",
    "Reason",
    "Submitted At",
]

# Audit log: one row appended every time a member's character name changes.
NAME_CHANGE_HEADERS = [
    "Discord User ID",
    "Old Character Name",
    "New Character Name",
    "Submitted At",
]

# Registration requests waiting on an officer's decision. Keeping them in the
# sheet (rather than in memory) means a pending request — and the Approve /
# Reject buttons on its message — still works after the bot restarts.
REGISTRATION_HEADERS = [
    "Request ID",
    "Status",
    "Discord User ID",
    "Discord Display Name",
    "Character Name",
    "Class",
    "Real Nickname",
    "Language",
    "Submitted At",
    "Decided By",
    "Decided At",
    "Note",
]

# --- CSV backup defaults -----------------------------------------------------
DEFAULT_BACKUP_DIR = "backups"
DEFAULT_BACKUP_KEEP = 20  # snapshots to retain per worksheet

# --- Retry / backoff for transient Google Sheets API failures ----------------
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 5
_BASE_DELAY = 1.0
_MAX_DELAY = 16.0

T = TypeVar("T")


def _call(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call a gspread method, retrying on rate limits / transient errors.

    Google's Sheets API enforces a per-minute write quota; under a burst of
    simultaneous commands it answers 429 (RESOURCE_EXHAUSTED). We back off
    exponentially instead of failing the user's command outright.
    """
    delay = _BASE_DELAY
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return func(*args, **kwargs)
        except APIError as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None) or getattr(exc, "code", None)
            if status not in _RETRYABLE_STATUS or attempt == _MAX_ATTEMPTS:
                raise
            logger.warning(
                "Google Sheets API returned %s; retrying in %.1fs (attempt %d/%d)",
                status, delay, attempt, _MAX_ATTEMPTS,
            )
            time.sleep(delay)
            delay = min(delay * 2, _MAX_DELAY)
    # Unreachable: the loop either returns or raises.
    raise RuntimeError("retry loop exited without returning")


@dataclass(frozen=True)
class MemberProfile:
    discord_user_id: str
    discord_display_name: str
    character_name: str
    character_class: str
    real_nickname: str
    created_at: str
    updated_at: str
    member_id: str = ""

    @classmethod
    def from_row(cls, row: list[str]) -> "MemberProfile":
        # Row layout matches MEMBER_HEADERS:
        #   0 Discord User ID | 1 Character Name | 2 Class
        #   3 Real Nickname   | 4 Member ID
        padded = row + [""] * (len(MEMBER_HEADERS) - len(row))
        return cls(
            discord_user_id=str(padded[0]).strip(),
            character_name=str(padded[1]).strip(),
            character_class=str(padded[2]).strip(),
            real_nickname=str(padded[3]).strip(),
            member_id=str(padded[4]).strip(),
            # Not stored in the sheet — populated at write time when needed.
            discord_display_name="",
            created_at="",
            updated_at="",
        )

    def to_row(self) -> list[str]:
        return [
            self.discord_user_id,
            self.character_name,
            self.character_class,
            self.real_nickname,
            self.member_id,
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


@dataclass(frozen=True)
class RegistrationRequest:
    request_id: str
    status: str
    discord_user_id: str
    discord_display_name: str
    character_name: str
    character_class: str
    real_nickname: str
    lang: str
    submitted_at: str
    decided_by: str = ""
    decided_at: str = ""
    note: str = ""

    @classmethod
    def from_row(cls, row: list[str]) -> "RegistrationRequest":
        padded = row + [""] * (len(REGISTRATION_HEADERS) - len(row))
        return cls(
            request_id=str(padded[0]).strip(),
            status=str(padded[1]).strip() or STATUS_PENDING,
            discord_user_id=str(padded[2]).strip(),
            discord_display_name=str(padded[3]).strip(),
            character_name=str(padded[4]).strip(),
            character_class=str(padded[5]).strip(),
            real_nickname=str(padded[6]).strip(),
            lang=str(padded[7]).strip() or "th",
            submitted_at=str(padded[8]).strip(),
            decided_by=str(padded[9]).strip(),
            decided_at=str(padded[10]).strip(),
            note=str(padded[11]).strip(),
        )

    def to_row(self) -> list[str]:
        return [
            self.request_id,
            self.status,
            self.discord_user_id,
            self.discord_display_name,
            self.character_name,
            self.character_class,
            self.real_nickname,
            self.lang,
            self.submitted_at,
            self.decided_by,
            self.decided_at,
            self.note,
        ]

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING


class GuildSheetStore:
    """Thread-safe Google Sheets store.

    Every public operation acquires a process-wide lock so a full
    read-modify-write sequence (find row -> allocate id -> append/update)
    runs atomically. Command callbacks reach these methods through
    ``asyncio.to_thread`` (real worker threads), so simultaneous users would
    otherwise race — producing duplicate member IDs or lost updates. The lock
    serialises them; for a guild-sized bot the throughput cost is negligible.
    """

    def __init__(self, spreadsheet: Any):
        self._lock = threading.Lock()
        self.spreadsheet = spreadsheet
        self.members = self._ensure_worksheet(MEMBERS_SHEET, MEMBER_HEADERS)
        self.absences = self._ensure_worksheet(ABSENCES_SHEET, ABSENCE_HEADERS)
        self.name_changes = self._ensure_worksheet(NAME_CHANGES_SHEET, NAME_CHANGE_HEADERS)
        self.registrations = self._ensure_worksheet(REGISTRATIONS_SHEET, REGISTRATION_HEADERS)
        # Discord IDs are 17-19 digits. If Sheets ever treats that column as a
        # number it rounds it (doubles keep ~15 digits), the read-back no longer
        # equals the real ID, and member lookups miss -> updates append a
        # duplicate row instead of editing in place. Force the column to text so
        # manually-typed or imported IDs stay exact going forward.
        self._force_text_column(self.members, MEMBER_HEADERS.index("Discord User ID") + 1)
        self._force_text_column(self.name_changes, NAME_CHANGE_HEADERS.index("Discord User ID") + 1)
        self._force_text_column(
            self.registrations, REGISTRATION_HEADERS.index("Discord User ID") + 1
        )
        self._backfill_member_ids()

    def _force_text_column(self, worksheet: Any, column_index: int) -> None:
        col = _column_name(column_index)
        try:
            _call(worksheet.format, f"{col}2:{col}", {"numberFormat": {"type": "TEXT"}})
        except Exception:
            logger.warning("Could not set %s!%s to text format", worksheet.title, col)

    @classmethod
    def from_service_account_file(
        cls,
        credentials_path: str | Path,
        spreadsheet_id: str,
    ) -> "GuildSheetStore":
        client = gspread.service_account(filename=str(credentials_path))
        spreadsheet = client.open_by_key(spreadsheet_id)
        return cls(spreadsheet)

    # --- public API (locked) -------------------------------------------------

    def get_member(self, discord_user_id: str | int) -> MemberProfile | None:
        with self._lock:
            row_number = self._find_member_row(discord_user_id)
            if row_number is None:
                return None
            return MemberProfile.from_row(_call(self.members.row_values, row_number))

    def upsert_member(
        self,
        discord_user_id: str | int,
        discord_display_name: str,
        character_name: str,
        character_class: str,
        real_nickname: str,
    ) -> tuple[MemberProfile, bool]:
        with self._lock:
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
                    member_id=self._allocate_member_id(),
                )
                _call(self.members.append_row, profile.to_row(), value_input_option="RAW")
                return profile, True

            existing = MemberProfile.from_row(_call(self.members.row_values, row_number))
            profile = MemberProfile(
                discord_user_id=user_id,
                discord_display_name=discord_display_name,
                character_name=character_name,
                character_class=character_class,
                real_nickname=real_nickname,
                created_at=existing.created_at or now,
                updated_at=now,
                member_id=existing.member_id or self._allocate_member_id(),
            )
            self._write_member(row_number, profile)
            if existing.character_name != profile.character_name:
                self._append_name_change(
                    profile.discord_user_id, existing.character_name, profile.character_name
                )
            return profile, False

    def update_character_name(
        self,
        discord_user_id: str | int,
        new_character_name: str,
    ) -> MemberProfile | None:
        with self._lock:
            return self._update_member_fields(
                discord_user_id,
                character_name=new_character_name,
            )

    def update_character_class(
        self,
        discord_user_id: str | int,
        new_character_class: str,
    ) -> MemberProfile | None:
        with self._lock:
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
        with self._lock:
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
            _call(self.absences.append_row, request.to_row(), value_input_option="RAW")
            return request

    # --- registration approval queue ----------------------------------------

    def append_registration_request(
        self,
        discord_user_id: str | int,
        discord_display_name: str,
        character_name: str,
        character_class: str,
        real_nickname: str,
        lang: str = "th",
    ) -> RegistrationRequest:
        """Queue a registration for approval. Nothing touches Members yet."""
        with self._lock:
            request = RegistrationRequest(
                request_id=_registration_id(),
                status=STATUS_PENDING,
                discord_user_id=str(discord_user_id),
                discord_display_name=discord_display_name,
                character_name=character_name,
                character_class=character_class,
                real_nickname=real_nickname,
                lang=lang,
                submitted_at=_utc_now(),
            )
            _call(
                self.registrations.append_row,
                [_strip_cr(cell) for cell in request.to_row()],
                value_input_option="RAW",
            )
            return request

    def find_pending_registration(
        self,
        discord_user_id: str | int,
    ) -> RegistrationRequest | None:
        """Return this member's outstanding request, if they already have one."""
        target = str(discord_user_id).strip()
        with self._lock:
            for row in _call(self.registrations.get_all_values)[1:]:
                request = RegistrationRequest.from_row(row)
                if request.discord_user_id == target and request.is_pending:
                    return request
            return None

    def decide_registration(
        self,
        request_id: str,
        status: str,
        decided_by: str | int,
        note: str = "",
    ) -> tuple[RegistrationRequest, bool] | None:
        """Claim a pending request and stamp the decision onto it.

        Returns ``None`` when the id is unknown, otherwise the request plus a
        flag saying whether *this* call made the decision. Two officers hitting
        Approve at the same moment therefore produce one winner: the loser sees
        ``False`` and can tell the clicker it was already handled.
        """
        with self._lock:
            row_number = self._find_registration_row(request_id)
            if row_number is None:
                return None

            existing = RegistrationRequest.from_row(
                _call(self.registrations.row_values, row_number)
            )
            if not existing.is_pending:
                return existing, False

            decided = replace(
                existing,
                status=status,
                decided_by=str(decided_by),
                decided_at=_utc_now(),
                note=note,
            )
            self._write_registration(row_number, decided)
            return decided, True

    def void_registration(self, request_id: str, note: str) -> RegistrationRequest | None:
        """Close a request the bot could never present for approval.

        Without this a request whose approval card failed to post would stay
        pending forever, and the one-request-at-a-time guard would lock the
        member out of retrying.
        """
        with self._lock:
            row_number = self._find_registration_row(request_id)
            if row_number is None:
                return None

            existing = RegistrationRequest.from_row(
                _call(self.registrations.row_values, row_number)
            )
            if not existing.is_pending:
                return existing

            voided = replace(
                existing,
                status=STATUS_REJECTED,
                decided_at=_utc_now(),
                note=note,
            )
            self._write_registration(row_number, voided)
            return voided

    def reopen_registration(self, request_id: str) -> RegistrationRequest | None:
        """Put a claimed request back in the queue.

        Used when the decision was recorded but the follow-up write to Members
        failed, so the officer can simply click Approve again.
        """
        with self._lock:
            row_number = self._find_registration_row(request_id)
            if row_number is None:
                return None

            existing = RegistrationRequest.from_row(
                _call(self.registrations.row_values, row_number)
            )
            reopened = replace(
                existing,
                status=STATUS_PENDING,
                decided_by="",
                decided_at="",
                note="",
            )
            self._write_registration(row_number, reopened)
            return reopened

    def backup_to_csv(
        self,
        backup_dir: str | Path = DEFAULT_BACKUP_DIR,
        keep: int = DEFAULT_BACKUP_KEEP,
    ) -> list[Path]:
        """Write a timestamped CSV snapshot of every worksheet.

        Runs under the same lock as writes, so a snapshot is never captured
        mid-update. Filenames are ``<worksheet>_<UTC-timestamp>.csv``; snapshots
        older than the newest ``keep`` (per worksheet) are pruned. Returns the
        paths written.
        """
        with self._lock:
            directory = Path(backup_dir)
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            written: list[Path] = []
            for worksheet in (self.members, self.absences, self.name_changes, self.registrations):
                prefix = str(worksheet.title).lower()
                rows = _call(worksheet.get_all_values)
                path = directory / f"{prefix}_{stamp}.csv"
                # utf-8-sig adds a BOM so Excel opens the Thai text correctly.
                # lineterminator="\n" overrides csv's default "\r\n", so rows
                # don't end with a stray carriage return; _strip_cr scrubs any
                # "\r" living inside a cell value too.
                with path.open("w", newline="", encoding="utf-8-sig") as handle:
                    writer = csv.writer(handle, lineterminator="\n")
                    writer.writerows([_strip_cr(cell) for cell in row] for row in rows)
                written.append(path)
                self._prune_backups(directory, prefix, keep)
            return written

    @staticmethod
    def _prune_backups(directory: Path, prefix: str, keep: int) -> None:
        if keep <= 0:
            return
        # Timestamp format sorts chronologically as text.
        snapshots = sorted(directory.glob(f"{prefix}_*.csv"))
        for stale in snapshots[:-keep]:
            try:
                stale.unlink()
            except OSError:
                logger.warning("Could not delete old backup %s", stale)

    # --- internal helpers (assume the lock is already held) ------------------

    def _ensure_worksheet(self, title: str, headers: list[str]) -> Any:
        try:
            worksheet = _call(self.spreadsheet.worksheet, title)
        except WorksheetNotFound:
            worksheet = _call(
                self.spreadsheet.add_worksheet,
                title=title,
                rows=100,
                cols=len(headers),
            )

        header_range = f"A1:{_column_name(len(headers))}1"
        current_headers = _call(worksheet.row_values, 1)
        if current_headers[: len(headers)] != headers:
            _call(worksheet.update, [headers], header_range)
        return worksheet

    def _find_member_row(self, discord_user_id: str | int) -> int | None:
        target = str(discord_user_id).strip()
        user_ids = _call(self.members.col_values, 1)
        for row_number, value in enumerate(user_ids[1:], start=2):
            if str(value).strip() == target:
                return row_number
        return None

    def _find_registration_row(self, request_id: str) -> int | None:
        target = str(request_id).strip()
        request_ids = _call(self.registrations.col_values, 1)
        for row_number, value in enumerate(request_ids[1:], start=2):
            if str(value).strip() == target:
                return row_number
        return None

    def _write_registration(self, row_number: int, request: RegistrationRequest) -> None:
        row_range = f"A{row_number}:{_column_name(len(REGISTRATION_HEADERS))}{row_number}"
        _call(
            self.registrations.update,
            [[_strip_cr(cell) for cell in request.to_row()]],
            row_range,
        )

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

        existing = MemberProfile.from_row(_call(self.members.row_values, row_number))
        updated = MemberProfile(
            discord_user_id=existing.discord_user_id,
            discord_display_name=existing.discord_display_name,
            character_name=character_name or existing.character_name,
            character_class=character_class or existing.character_class,
            real_nickname=existing.real_nickname,
            created_at=existing.created_at,
            updated_at=_utc_now(),
            member_id=existing.member_id or self._allocate_member_id(),
        )
        self._write_member(row_number, updated)
        if character_name is not None and existing.character_name != updated.character_name:
            self._append_name_change(
                updated.discord_user_id, existing.character_name, updated.character_name
            )
        return updated

    def _append_name_change(self, discord_user_id: str, old_name: str, new_name: str) -> None:
        """Append one audit row: Discord ID, old name, new name, timestamp.

        Best-effort: a failure here is logged but never raised, so an audit-log
        hiccup can't break the actual rename the user requested.
        """
        row = [str(discord_user_id), old_name, new_name, _utc_now()]
        try:
            _call(
                self.name_changes.append_row,
                [_strip_cr(cell) for cell in row],
                value_input_option="RAW",
            )
            logger.info("Name change logged: %s '%s' -> '%s'", discord_user_id, old_name, new_name)
        except Exception:
            logger.exception("Could not write name-change log for %s", discord_user_id)

    def _write_member(self, row_number: int, profile: MemberProfile) -> None:
        row_range = f"A{row_number}:{_column_name(len(MEMBER_HEADERS))}{row_number}"
        _call(self.members.update, [profile.to_row()], row_range)

    def _allocate_member_id(self) -> str:
        member_id_col = MEMBER_HEADERS.index("Member ID") + 1
        existing_numbers = [
            int(match.group(1))
            for value in _call(self.members.col_values, member_id_col)[1:]
            if (match := _MEMBER_ID_RE.match(value.strip()))
        ]
        return _format_member_id(max(existing_numbers, default=0) + 1)

    def _backfill_member_ids(self) -> None:
        member_id_col = MEMBER_HEADERS.index("Member ID") + 1
        data_rows = _call(self.members.get_all_values)[1:]
        if not data_rows:
            return

        existing_numbers = [
            int(match.group(1))
            for row in data_rows
            if len(row) >= member_id_col
            and (match := _MEMBER_ID_RE.match(row[member_id_col - 1].strip()))
        ]
        next_number = max(existing_numbers, default=0) + 1

        updates = []
        for offset, row in enumerate(data_rows):
            value = row[member_id_col - 1] if len(row) >= member_id_col else ""
            if not value.strip():
                row_number = offset + 2
                cell = f"{_column_name(member_id_col)}{row_number}"
                updates.append({"range": cell, "values": [[_format_member_id(next_number)]]})
                next_number += 1
        if updates:
            _call(self.members.batch_update, updates)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _strip_cr(value: Any) -> str:
    """Scrub carriage returns so a value never lands in a cell/CSV as a stray
    '\\r'. Converts CRLF/CR to LF and preserves any genuine newlines."""
    return str(value).replace("\r\n", "\n").replace("\r", "")


def _format_member_id(number: int) -> str:
    return f"P{number:03d}"


def _request_id() -> str:
    return _prefixed_id("ABS")


def _registration_id() -> str:
    return _prefixed_id("REG")


def _prefixed_id(prefix: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    suffix = secrets.token_hex(3).upper()
    return f"{prefix}-{timestamp}-{suffix}"


def _column_name(index: int) -> str:
    if index < 1:
        raise ValueError("index must be 1 or greater")

    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name