from __future__ import annotations

import asyncio
import logging
import random
import re
import socket
import ssl
import time
from datetime import date

import aiohttp
import certifi
import discord
from discord import app_commands
from discord.ext import commands

from ro_guild_bot.classes import RO_CLASSES, autocomplete_classes, resolve_class
from ro_guild_bot.config import BotConfig, load_config
from ro_guild_bot.sheets import (
    STATUS_APPROVED,
    STATUS_REJECTED,
    AbsenceRequest,
    GuildSheetStore,
    MemberProfile,
    RegistrationRequest,
)
from ro_guild_bot.validation import clean_required_text, format_mmdd, parse_date_range, parse_mmdd_date


logger = logging.getLogger(__name__)
DISCORD_NICKNAME_MAX_LENGTH = 32
BASE_ROLE_NAME = "BABYBEAR | ROW"
BACKUP_DIR = "backups"
BACKUP_MIN_INTERVAL_SECONDS = 30.0  # debounce: at most one CSV backup per window

# --- Startup/login retry backoff -------------------------------------------
# Keeping the retry loop inside one long-lived process (rather than letting the
# process crash and be auto-restarted by the host panel) is what prevents a
# restart storm from hammering discord.com's login endpoint and earning a
# Cloudflare 1015 IP ban.
LOGIN_RETRY_BASE_SECONDS = 30.0          # first delay for a generic transient failure
LOGIN_RETRY_MAX_SECONDS = 900.0          # cap any single backoff at 15 minutes
RATE_LIMIT_MIN_BACKOFF_SECONDS = 300.0   # a 429 / Cloudflare block waits >= 5 minutes
RATE_LIMIT_MAX_BACKOFF_SECONDS = 1800.0  # ...but never sleep more than 30 min in one go
LOGIN_MAX_ATTEMPTS = 0                   # 0 = retry forever; set > 0 to eventually give up
SESSION_HEALTHY_SECONDS = 60.0           # a session that ran this long resets the backoff
# The Cloudflare 1015 ban is per-IP. When the banned address is IPv6, forcing
# outbound connections onto the host's IPv4 address routes around it entirely.
# Set to False if your host is IPv4-limited/IPv6-only (you'll see connection
# errors instead of 429s if IPv4 isn't usable).
FORCE_IPV4 = True


# --- Command replies, per language -----------------------------------------
# Every slash command is registered twice (Thai names and English names) and
# both registrations share one implementation; these strings are the only
# thing that differs between them. Messages posted to the guild's log and
# officer channels stay in Thai regardless, so those channels read the same
# no matter which language a member used.
_MSG: dict[str, dict[str, str]] = {
    "th": {
        "field_character_name": "ชื่อตัวละคร",
        "field_nickname": "ชื่อเล่น",
        "field_new_name": "ชื่อใหม่",
        "field_reason": "เหตุผล",
        "field_start_date": "วันเริ่ม",
        "unknown_class": "อาชีพนี้ไม่อยู่ในรายชื่ออาชีพ Classic + Trans กรุณาเลือกจากตัวเลือกอัตโนมัติ",
        "not_registered": "คุณยังไม่ได้ลงทะเบียน กรุณาใช้คำสั่ง `/ลงทะเบียน` ก่อน",
        "register_failed": "ลงทะเบียนไม่สำเร็จ เนื่องจากไม่สามารถอัปเดต Google Sheets ได้",
        "register_pending": (
            "ส่งคำขอลงทะเบียน **{request_id}** ให้หัวกิลด์แล้ว "
            "กรุณารอการอนุมัติ คุณจะได้รับ DM แจ้งผลเมื่อหัวกิลด์ตรวจสอบเสร็จ"
        ),
        "register_not_posted": (
            "บันทึกคำขอลงทะเบียน **{request_id}** แล้ว แต่ไม่สามารถส่งข้อความไปยังห้องอนุมัติได้ "
            "กรุณาตรวจสอบ `REGISTER_APPROVAL_CHANNEL_ID` และสิทธิ์ของบอทในห้องนั้น"
        ),
        "register_already_pending": (
            "คุณมีคำขอลงทะเบียน **{request_id}** ที่รอการอนุมัติอยู่แล้ว "
            "กรุณารอหัวกิลด์ตรวจสอบก่อน"
        ),
        "register_approved_notice": "คำขอลงทะเบียน **{request_id}** ได้รับการอนุมัติแล้ว",
        "register_rejected_notice": "คำขอลงทะเบียน **{request_id}** ไม่ได้รับการอนุมัติ",
        "register_rejected_reason": "เหตุผล: {note}",
        "register_created": (
            "ลงทะเบียน **{character_name}** เป็น **{character_class}** "
            "ชื่อเล่น **{nickname}** เรียบร้อยแล้ว รหัสสมาชิกของคุณคือ **{member_id}**"
        ),
        "register_updated": (
            "อัปเดตข้อมูลลงทะเบียนของคุณเป็น **{character_name}** "
            "(**{character_class}**) ชื่อเล่น **{nickname}** "
            "รหัสสมาชิกของคุณคือ **{member_id}**"
        ),
        "change_name_failed": "เปลี่ยนชื่อไม่สำเร็จ เนื่องจากไม่สามารถอัปเดต Google Sheets ได้",
        "change_name_done": "อัปเดตชื่อตัวละครของคุณเป็น **{character_name}** เรียบร้อยแล้ว",
        "change_class_failed": "เปลี่ยนอาชีพไม่สำเร็จ เนื่องจากไม่สามารถอัปเดต Google Sheets ได้",
        "change_class_done": "อัปเดตอาชีพของคุณเป็น **{character_class}** เรียบร้อยแล้ว",
        "start_date_required": "ต้องระบุวันเริ่ม หากมีการระบุวันสิ้นสุด",
        "absence_failed": "แจ้งลาไม่สำเร็จ เนื่องจากไม่สามารถอัปเดต Google Sheets ได้",
        "absence_sent": (
            "ส่งคำขอลา **{request_id}** สำหรับวันที่ "
            "{start_date} ถึง {end_date} เรียบร้อยแล้ว"
        ),
        "absence_not_posted": (
            "บันทึกคำขอลา **{request_id}** แล้ว แต่ไม่สามารถส่งข้อความไปยังห้องหัวกิลด์ได้ "
            "กรุณาตรวจสอบ `ABSENCE_CHANNEL_ID` และสิทธิ์ของบอทในห้องนั้น"
        ),
        "nick_need_permission": "ไม่ได้เปลี่ยนชื่อเล่นใน Discord: บอทต้องการสิทธิ์ `Manage Nicknames`",
        "nick_role_too_low": "ไม่ได้เปลี่ยนชื่อเล่นใน Discord: กรุณาเลื่อนตำแหน่งโรลของบอทให้สูงกว่าโรลสูงสุดของสมาชิกคนนี้",
        "nick_forbidden": "ไม่ได้เปลี่ยนชื่อเล่นใน Discord: บอทต้องการสิทธิ์ `Manage Nicknames` และโรลที่สูงกว่า",
        "nick_rejected": "ไม่ได้เปลี่ยนชื่อเล่นใน Discord: Discord ปฏิเสธการอัปเดตชื่อเล่น",
        "nick_synced": "ซิงค์ชื่อเล่นใน Discord เรียบร้อยแล้ว",
        "class_role_missing": "ไม่ได้เปลี่ยนโรลอาชีพใน Discord: กรุณาสร้างโรลชื่อ `{role}` ก่อน",
        "class_role_managed": "ไม่ได้เปลี่ยนโรลอาชีพใน Discord: `{role}` เป็นโรลที่ถูกจัดการโดยระบบ (managed role)",
        "class_role_need_permission": "ไม่ได้เปลี่ยนโรลอาชีพใน Discord: บอทต้องการสิทธิ์ `Manage Roles`",
        "class_role_too_low": "ไม่ได้เปลี่ยนโรลอาชีพใน Discord: กรุณาเลื่อนตำแหน่งโรลของบอทให้สูงกว่าโรลอาชีพทั้งหมด",
        "class_role_forbidden": "ไม่ได้เปลี่ยนโรลอาชีพใน Discord: บอทต้องการสิทธิ์ `Manage Roles` และโรลที่สูงกว่า",
        "class_role_rejected": "ไม่ได้เปลี่ยนโรลอาชีพใน Discord: Discord ปฏิเสธการอัปเดตโรล",
        "class_role_synced": "ซิงค์โรลอาชีพใน Discord เป็น **{role}** เรียบร้อยแล้ว",
        "base_role_missing": "ไม่ได้เพิ่มโรล Discord: กรุณาสร้างโรลชื่อ `{role}` ก่อน",
        "base_role_managed": "ไม่ได้เพิ่มโรล Discord: `{role}` เป็นโรลที่ถูกจัดการโดยระบบ (managed role)",
        "base_role_need_permission": "ไม่ได้เพิ่มโรล Discord: บอทต้องการสิทธิ์ `Manage Roles`",
        "base_role_too_low": "ไม่ได้เพิ่มโรล Discord: กรุณาเลื่อนตำแหน่งโรลของบอทให้สูงกว่า `{role}`",
        "base_role_forbidden": "ไม่ได้เพิ่มโรล Discord: บอทต้องการสิทธิ์ `Manage Roles` และโรลที่สูงกว่า",
        "base_role_rejected": "ไม่ได้เพิ่มโรล Discord: Discord ปฏิเสธการเพิ่มโรล",
        "base_role_added": "เพิ่มโรล Discord `{role}` เรียบร้อยแล้ว",
        "sync_skipped_dm": "ข้ามการซิงค์โปรไฟล์ Discord เนื่องจากคำสั่งนี้ไม่ได้ใช้งานภายในเซิร์ฟเวอร์",
        "sync_skipped_no_member": "ข้ามการซิงค์โปรไฟล์ Discord เนื่องจากไม่พบข้อมูลสมาชิกของคุณในเซิร์ฟเวอร์",
    },
    "en": {
        "field_character_name": "character name",
        "field_nickname": "nickname",
        "field_new_name": "new name",
        "field_reason": "reason",
        "field_start_date": "start date",
        "unknown_class": "That class is not in the Classic + Trans class list. Please pick one from the autocomplete options.",
        "not_registered": "You are not registered yet. Please use `/register` first.",
        "register_failed": "Registration failed because Google Sheets could not be updated.",
        "register_pending": (
            "Registration request **{request_id}** has been sent to the guild leaders. "
            "Please wait for approval — you will get a DM once they have reviewed it."
        ),
        "register_not_posted": (
            "Registration request **{request_id}** was saved, but it could not be posted to "
            "the approval channel. Please check `REGISTER_APPROVAL_CHANNEL_ID` and the bot's "
            "permissions in that channel."
        ),
        "register_already_pending": (
            "You already have registration request **{request_id}** waiting for approval. "
            "Please wait for the guild leaders to review it."
        ),
        "register_approved_notice": "Registration request **{request_id}** has been approved.",
        "register_rejected_notice": "Registration request **{request_id}** was not approved.",
        "register_rejected_reason": "Reason: {note}",
        "register_created": (
            "Registered **{character_name}** as **{character_class}**, "
            "nickname **{nickname}**. Your member ID is **{member_id}**."
        ),
        "register_updated": (
            "Updated your registration to **{character_name}** "
            "(**{character_class}**), nickname **{nickname}**. "
            "Your member ID is **{member_id}**."
        ),
        "change_name_failed": "The name change failed because Google Sheets could not be updated.",
        "change_name_done": "Your character name has been updated to **{character_name}**.",
        "change_class_failed": "The class change failed because Google Sheets could not be updated.",
        "change_class_done": "Your class has been updated to **{character_class}**.",
        "start_date_required": "A start date is required when an end date is given.",
        "absence_failed": "The absence could not be filed because Google Sheets could not be updated.",
        "absence_sent": (
            "Absence request **{request_id}** for "
            "{start_date} to {end_date} has been submitted."
        ),
        "absence_not_posted": (
            "Absence request **{request_id}** was saved, but it could not be posted to the "
            "guild leaders' channel. Please check `ABSENCE_CHANNEL_ID` and the bot's "
            "permissions in that channel."
        ),
        "nick_need_permission": "Discord nickname not changed: the bot needs the `Manage Nicknames` permission.",
        "nick_role_too_low": "Discord nickname not changed: please move the bot's role above this member's highest role.",
        "nick_forbidden": "Discord nickname not changed: the bot needs the `Manage Nicknames` permission and a higher role.",
        "nick_rejected": "Discord nickname not changed: Discord rejected the nickname update.",
        "nick_synced": "Your Discord nickname has been synced.",
        "class_role_missing": "Discord class role not changed: please create a role named `{role}` first.",
        "class_role_managed": "Discord class role not changed: `{role}` is a managed role.",
        "class_role_need_permission": "Discord class role not changed: the bot needs the `Manage Roles` permission.",
        "class_role_too_low": "Discord class role not changed: please move the bot's role above every class role.",
        "class_role_forbidden": "Discord class role not changed: the bot needs the `Manage Roles` permission and a higher role.",
        "class_role_rejected": "Discord class role not changed: Discord rejected the role update.",
        "class_role_synced": "Your Discord class role has been synced to **{role}**.",
        "base_role_missing": "Discord role not added: please create a role named `{role}` first.",
        "base_role_managed": "Discord role not added: `{role}` is a managed role.",
        "base_role_need_permission": "Discord role not added: the bot needs the `Manage Roles` permission.",
        "base_role_too_low": "Discord role not added: please move the bot's role above `{role}`.",
        "base_role_forbidden": "Discord role not added: the bot needs the `Manage Roles` permission and a higher role.",
        "base_role_rejected": "Discord role not added: Discord rejected the role addition.",
        "base_role_added": "Added the Discord role `{role}`.",
        "sync_skipped_dm": "Skipped the Discord profile sync because this command was not used inside the server.",
        "sync_skipped_no_member": "Skipped the Discord profile sync because your member record could not be found in the server.",
    },
}


# Officer-facing copy for the approval card and the button replies. Like the
# member-log and absence embeds, these stay Thai no matter which language the
# requesting member used.
_APPROVAL: dict[str, str] = {
    "card_title": "คำขอลงทะเบียนสมาชิก",
    "field_request_id": "รหัสคำขอ",
    "field_member": "ผู้ขอลงทะเบียน",
    "field_character": "ตัวละคร",
    "field_class": "อาชีพ",
    "field_nickname": "ชื่อเล่น",
    "field_decision": "ผลการพิจารณา",
    "field_reason": "เหตุผล",
    "footer_submitted": "ส่งคำขอเมื่อ {submitted_at}",
    "footer_decided": "พิจารณาเมื่อ {decided_at}",
    "button_approve": "อนุมัติ",
    "button_reject": "ปฏิเสธ",
    "decision_approved": "อนุมัติโดย {approver}",
    "decision_rejected": "ปฏิเสธโดย {approver}",
    "no_permission": "คุณไม่มีสิทธิ์พิจารณาคำขอลงทะเบียน",
    "unknown_request": "ไม่พบคำขอลงทะเบียนนี้ อาจถูกลบออกจาก Google Sheets แล้ว",
    "already_decided": "คำขอนี้ถูกพิจารณาไปแล้ว (สถานะ: {status}, ผู้พิจารณา: {approver})",
    "sheet_error": "ไม่สามารถอ่าน/เขียน Google Sheets ได้ กรุณาลองใหม่อีกครั้ง",
    "member_write_failed": (
        "อนุมัติไม่สำเร็จ เนื่องจากไม่สามารถบันทึกลงชีต Members ได้ "
        "คำขอถูกคืนสถานะเป็นรออนุมัติ กรุณากดอนุมัติอีกครั้ง"
    ),
    "member_not_in_guild": "ไม่พบสมาชิกคนนี้ในเซิร์ฟเวอร์ จึงข้ามการซิงค์ชื่อเล่นและโรล",
    "dm_failed": "ไม่สามารถส่ง DM แจ้งผลให้สมาชิกได้ (สมาชิกอาจปิดรับ DM)",
    "approved": "อนุมัติคำขอ **{request_id}** เรียบร้อยแล้ว",
    "rejected": "ปฏิเสธคำขอ **{request_id}** เรียบร้อยแล้ว",
    "reject_modal_title": "ปฏิเสธคำขอลงทะเบียน",
    "reject_reason_label": "เหตุผล (ไม่บังคับ)",
}

# Stamped on a request the bot queued but could not post for approval.
_VOID_NOTE = "ยกเลิกอัตโนมัติ: ส่งการ์ดไปยังห้องอนุมัติไม่สำเร็จ"


async def _sync_to_sheet(label, interaction, func, *args, **kwargs):
    """Run a Google Sheets write off the event loop and log its outcome.

    Emits one line per command: COMPLETE on success, UNSUCCESSFUL on failure.
    Re-raises on failure so each handler's existing error reply still runs.
    """
    user = interaction.user
    try:
        result = await asyncio.to_thread(func, *args, **kwargs)
    except Exception:
        logger.exception("%s: Google Sheet sync UNSUCCESSFUL for %s (%s)", label, user, user.id)
        raise
    logger.info("%s: Google Sheet sync COMPLETE for %s (%s)", label, user, user.id)
    return result


class RagnarokGuildBot(commands.Bot):
    def __init__(self, config: BotConfig, store: GuildSheetStore):
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
        )
        self.config = config
        self.store = store
        self.guild_object = discord.Object(id=config.discord_guild_id)
        self._last_backup = 0.0
        self._backup_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        # Rebuilds Approve / Reject buttons from their custom_id, so cards
        # posted before the last restart stay clickable.
        self.add_dynamic_items(RegistrationDecisionButton)
        synced = await self.tree.sync(guild=self.guild_object)
        logger.info("Synced %s slash commands to guild %s", len(synced), self.config.discord_guild_id)

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> None:
        await self._maybe_backup()

    async def _maybe_backup(self) -> None:
        """Write a CSV snapshot after a command, debounced and off the loop.

        Skips if a backup ran within the last window or is already running, so a
        burst of commands collapses into one snapshot. Failures are logged, never
        raised, so a backup problem can't break a user's command.
        """
        now = time.monotonic()
        if now - self._last_backup < BACKUP_MIN_INTERVAL_SECONDS or self._backup_lock.locked():
            return
        async with self._backup_lock:
            self._last_backup = time.monotonic()
            try:
                paths = await asyncio.to_thread(self.store.backup_to_csv, BACKUP_DIR)
                logger.info("CSV backup written: %s", ", ".join(p.name for p in paths))
            except Exception:
                logger.exception("CSV backup failed")


def create_bot(config: BotConfig, store: GuildSheetStore) -> RagnarokGuildBot:
    bot = RagnarokGuildBot(config=config, store=store)
    guild = bot.guild_object

    async def class_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        return [
            app_commands.Choice(name=class_name, value=class_name)
            for class_name in autocomplete_classes(current)
        ]

    # --- Shared implementations --------------------------------------------
    # Each command exists twice in the command tree: once with Thai names and
    # once with English ones. Both registrations call the same implementation
    # below, so the two languages can never drift apart in behaviour; `lang`
    # only selects which strings the user sees.

    async def _register_impl(
        interaction: discord.Interaction,
        character_name: str,
        character_class: str,
        nickname: str,
        lang: str,
    ) -> None:
        text = _MSG[lang]
        try:
            character_name = clean_required_text(
                character_name,
                text["field_character_name"],
                max_length=DISCORD_NICKNAME_MAX_LENGTH,
                lang=lang,
            )
            nickname = clean_required_text(nickname, text["field_nickname"], max_length=60, lang=lang)
            canonical_class = resolve_class(character_class)
            if canonical_class is None:
                await interaction.response.send_message(text["unknown_class"], ephemeral=True)
                return
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        # Nothing is written to Members here: the request is parked in the
        # Registrations queue and only applied once an officer approves it.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            pending = await asyncio.to_thread(
                bot.store.find_pending_registration, interaction.user.id
            )
            if pending is not None:
                await interaction.followup.send(
                    text["register_already_pending"].format(request_id=pending.request_id),
                    ephemeral=True,
                )
                return

            request = await _sync_to_sheet(
                "register_request",
                interaction,
                bot.store.append_registration_request,
                interaction.user.id,
                _display_name(interaction),
                character_name,
                canonical_class,
                nickname,
                lang,
            )
        except Exception:
            await interaction.followup.send(text["register_failed"], ephemeral=True)
            return

        if await _post_registration_request(bot, request):
            await interaction.followup.send(
                text["register_pending"].format(request_id=request.request_id), ephemeral=True
            )
            return

        # No card means no way to approve it, so close the request rather than
        # leaving one the member can neither get decided nor resubmit.
        try:
            await asyncio.to_thread(bot.store.void_registration, request.request_id, _VOID_NOTE)
        except Exception:
            logger.exception("Could not void unpostable registration %s", request.request_id)
        await interaction.followup.send(
            text["register_not_posted"].format(request_id=request.request_id), ephemeral=True
        )

    async def _change_name_impl(
        interaction: discord.Interaction,
        new_character_name: str,
        lang: str,
    ) -> None:
        text = _MSG[lang]
        try:
            new_character_name = clean_required_text(
                new_character_name,
                text["field_new_name"],
                max_length=DISCORD_NICKNAME_MAX_LENGTH,
                lang=lang,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            profile = await _sync_to_sheet(
                "change_name",
                interaction,
                bot.store.update_character_name,
                interaction.user.id,
                new_character_name,
            )
        except Exception:
            await interaction.followup.send(text["change_name_failed"], ephemeral=True)
            return

        if profile is None:
            await interaction.followup.send(text["not_registered"], ephemeral=True)
            return
        sync_notes = await _sync_discord_profile(
            bot, interaction, profile, sync_nickname=True, sync_class_role=False, lang=lang
        )
        await _post_member_log(bot, _user_reference(interaction.user), profile, "เปลี่ยนชื่อตัวละคร")
        await interaction.followup.send(
            _with_sync_notes(
                text["change_name_done"].format(character_name=profile.character_name), sync_notes
            ),
            ephemeral=True,
        )

    async def _change_class_impl(
        interaction: discord.Interaction,
        new_character_class: str,
        lang: str,
    ) -> None:
        text = _MSG[lang]
        canonical_class = resolve_class(new_character_class)
        if canonical_class is None:
            await interaction.response.send_message(text["unknown_class"], ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            profile = await _sync_to_sheet(
                "change_class",
                interaction,
                bot.store.update_character_class,
                interaction.user.id,
                canonical_class,
            )
        except Exception:
            await interaction.followup.send(text["change_class_failed"], ephemeral=True)
            return

        if profile is None:
            await interaction.followup.send(text["not_registered"], ephemeral=True)
            return
        sync_notes = await _sync_discord_profile(
            bot, interaction, profile, sync_nickname=False, sync_class_role=True, lang=lang
        )
        await _post_member_log(bot, _user_reference(interaction.user), profile, "เปลี่ยนอาชีพ")
        await interaction.followup.send(
            _with_sync_notes(
                text["change_class_done"].format(character_class=profile.character_class), sync_notes
            ),
            ephemeral=True,
        )

    async def _absence_impl(
        interaction: discord.Interaction,
        reason: str,
        start_date: str | None,
        end_date: str | None,
        lang: str,
    ) -> None:
        text = _MSG[lang]
        try:
            if start_date is None and end_date is None:
                parsed_start = parsed_end = date.today()
            elif start_date is None:
                raise ValueError(text["start_date_required"])
            elif end_date is None:
                parsed_start = parsed_end = parse_mmdd_date(start_date, text["field_start_date"], lang)
            else:
                parsed_start, parsed_end = parse_date_range(start_date, end_date, lang)
            reason = clean_required_text(reason, text["field_reason"], max_length=300, lang=lang)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            member = await asyncio.to_thread(bot.store.get_member, interaction.user.id)
            if member is None:
                await interaction.followup.send(text["not_registered"], ephemeral=True)
                return
            request = await _sync_to_sheet(
                "absence",
                interaction,
                bot.store.append_absence,
                member,
                parsed_start,
                parsed_end,
                reason,
            )
        except Exception:
            await interaction.followup.send(text["absence_failed"], ephemeral=True)
            return

        posted = await _post_absence_to_officer_channel(bot, interaction, member, request)
        template = text["absence_sent"] if posted else text["absence_not_posted"]
        await interaction.followup.send(
            template.format(
                request_id=request.request_id,
                start_date=format_mmdd(request.start_date),
                end_date=format_mmdd(request.end_date),
            ),
            ephemeral=True,
        )

    # --- Thai commands ------------------------------------------------------

    @bot.tree.command(
        name="ลงทะเบียน",
        description="ลงทะเบียนหรืออัปเดตตัวละครหลักของคุณใน Ragnarok Online",
        guild=guild,
    )
    @app_commands.rename(character_name="ชื่อตัวละคร", character_class="อาชีพ", nickname="ชื่อเล่น")
    @app_commands.describe(
        character_name="ชื่อตัวละครหลักของคุณ",
        character_class="อาชีพของคุณใน Ragnarok Online",
        nickname="ชื่อเล่นจริงของคุณ",
    )
    @app_commands.autocomplete(character_class=class_autocomplete)
    async def register(
        interaction: discord.Interaction,
        character_name: str,
        character_class: str,
        nickname: str,
    ) -> None:
        await _register_impl(interaction, character_name, character_class, nickname, "th")

    @bot.tree.command(
        name="เปลี่ยนชื่อ",
        description="เปลี่ยนชื่อตัวละครหลักที่ลงทะเบียนไว้",
        guild=guild,
    )
    @app_commands.rename(new_character_name="ชื่อใหม่")
    @app_commands.describe(new_character_name="ชื่อตัวละครหลักใหม่ของคุณ")
    async def change_name(interaction: discord.Interaction, new_character_name: str) -> None:
        await _change_name_impl(interaction, new_character_name, "th")

    @bot.tree.command(
        name="เปลี่ยนอาชีพ",
        description="เปลี่ยนอาชีพที่ลงทะเบียนไว้",
        guild=guild,
    )
    @app_commands.rename(new_character_class="อาชีพใหม่")
    @app_commands.describe(new_character_class="อาชีพใหม่ของคุณ")
    @app_commands.autocomplete(new_character_class=class_autocomplete)
    async def change_class(interaction: discord.Interaction, new_character_class: str) -> None:
        await _change_class_impl(interaction, new_character_class, "th")

    @bot.tree.command(
        name="ลา",
        description="แจ้งลาและแจ้งเตือนหัวกิลด์",
        guild=guild,
    )
    @app_commands.rename(reason="เหตุผล", start_date="วันเริ่ม", end_date="วันสิ้นสุด")
    @app_commands.describe(
        reason="เหตุผลในการลา",
        start_date="วันแรกที่ลา รูปแบบ MM/DD หากเว้นว่างไว้จะถือว่าลาวันนี้",
        end_date="วันสุดท้ายที่ลา รูปแบบ MM/DD หากเว้นว่างไว้จะใช้วันเดียวกับวันเริ่ม",
    )
    async def absence(
        interaction: discord.Interaction,
        reason: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> None:
        await _absence_impl(interaction, reason, start_date, end_date, "th")

    # --- English commands ---------------------------------------------------

    @bot.tree.command(
        name="register",
        description="Register or update your main Ragnarok Online character",
        guild=guild,
    )
    @app_commands.describe(
        character_name="Your main character's name",
        character_class="Your Ragnarok Online class",
        nickname="Your real nickname",
    )
    @app_commands.autocomplete(character_class=class_autocomplete)
    async def register_en(
        interaction: discord.Interaction,
        character_name: str,
        character_class: str,
        nickname: str,
    ) -> None:
        await _register_impl(interaction, character_name, character_class, nickname, "en")

    @bot.tree.command(
        name="change-name",
        description="Change the main character name you registered",
        guild=guild,
    )
    @app_commands.describe(new_name="Your new main character name")
    async def change_name_en(interaction: discord.Interaction, new_name: str) -> None:
        await _change_name_impl(interaction, new_name, "en")

    @bot.tree.command(
        name="change-class",
        description="Change the class you registered",
        guild=guild,
    )
    @app_commands.describe(new_class="Your new class")
    @app_commands.autocomplete(new_class=class_autocomplete)
    async def change_class_en(interaction: discord.Interaction, new_class: str) -> None:
        await _change_class_impl(interaction, new_class, "en")

    @bot.tree.command(
        name="absence",
        description="File an absence and notify the guild leaders",
        guild=guild,
    )
    @app_commands.describe(
        reason="Why you will be absent",
        start_date="First day of the absence, MM/DD. Defaults to today when left empty.",
        end_date="Last day of the absence, MM/DD. Defaults to the start date when left empty.",
    )
    async def absence_en(
        interaction: discord.Interaction,
        reason: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> None:
        await _absence_impl(interaction, reason, start_date, end_date, "en")

    return bot


async def _sync_discord_profile(
    bot: RagnarokGuildBot,
    interaction: discord.Interaction,
    profile: MemberProfile,
    *,
    sync_nickname: bool,
    sync_class_role: bool,
    sync_base_role: bool = False,
    lang: str = "th",
) -> list[str]:
    text = _MSG[lang]
    if interaction.guild is None:
        return [text["sync_skipped_dm"]]

    member = await _interaction_member(interaction)
    if member is None:
        return [text["sync_skipped_no_member"]]

    return await _sync_profile_for_member(
        bot,
        member,
        profile,
        sync_nickname=sync_nickname,
        sync_class_role=sync_class_role,
        sync_base_role=sync_base_role,
        lang=lang,
    )


async def _sync_profile_for_member(
    bot: RagnarokGuildBot,
    member: discord.Member,
    profile: MemberProfile,
    *,
    sync_nickname: bool,
    sync_class_role: bool,
    sync_base_role: bool = False,
    lang: str = "th",
) -> list[str]:
    bot_member = await _bot_member(bot, member.guild)
    notes: list[str] = []
    if sync_nickname:
        note = await _sync_member_nickname(member, profile, bot_member, lang)
        if note:
            notes.append(note)
    if sync_class_role:
        note = await _sync_member_class_role(member, profile.character_class, bot_member, lang)
        if note:
            notes.append(note)
    if sync_base_role:
        note = await _sync_base_role(member, bot_member, lang)
        if note:
            notes.append(note)
    return notes


async def _interaction_member(interaction: discord.Interaction) -> discord.Member | None:
    if isinstance(interaction.user, discord.Member):
        return interaction.user
    if interaction.guild is None:
        return None
    try:
        return await interaction.guild.fetch_member(interaction.user.id)
    except discord.DiscordException:
        logger.exception("Could not fetch member %s for profile sync", interaction.user.id)
        return None


async def _bot_member(bot: RagnarokGuildBot, guild: discord.Guild) -> discord.Member | None:
    if guild.me is not None:
        return guild.me
    if bot.user is None:
        return None
    try:
        return await guild.fetch_member(bot.user.id)
    except discord.DiscordException:
        logger.exception("Could not fetch bot member for guild %s", guild.id)
        return None


async def _sync_member_nickname(
    member: discord.Member,
    profile: MemberProfile,
    bot_member: discord.Member | None,
    lang: str = "th",
) -> str | None:
    text = _MSG[lang]
    target_nickname = _format_discord_nickname(profile)
    if member.display_name == target_nickname:
        return None

    if bot_member is not None:
        if not bot_member.guild_permissions.manage_nicknames:
            return text["nick_need_permission"]
        if member.id != bot_member.id and member.top_role >= bot_member.top_role:
            return text["nick_role_too_low"]

    try:
        await member.edit(nick=target_nickname, reason="Ragnarok character name sync")
    except discord.Forbidden:
        logger.exception("Missing permission to edit nickname for %s", member.id)
        return text["nick_forbidden"]
    except discord.HTTPException:
        logger.exception("Discord rejected nickname edit for %s", member.id)
        return text["nick_rejected"]
    return text["nick_synced"]


def _format_discord_nickname(profile: MemberProfile) -> str:
    prefix = f"[{profile.member_id}] {profile.character_name}" if profile.member_id else profile.character_name
    if not profile.real_nickname:
        return prefix[:DISCORD_NICKNAME_MAX_LENGTH]

    full = f"{prefix} - {profile.real_nickname}"
    if len(full) <= DISCORD_NICKNAME_MAX_LENGTH:
        return full

    available = DISCORD_NICKNAME_MAX_LENGTH - len(prefix) - len(" - ")
    if available > 0:
        return f"{prefix} - {profile.real_nickname[:available]}"
    return prefix[:DISCORD_NICKNAME_MAX_LENGTH]


async def _sync_member_class_role(
    member: discord.Member,
    character_class: str,
    bot_member: discord.Member | None,
    lang: str = "th",
) -> str | None:
    text = _MSG[lang]
    class_role_names = set(RO_CLASSES)
    target_role = discord.utils.get(member.guild.roles, name=character_class)
    if target_role is None:
        return text["class_role_missing"].format(role=character_class)
    if target_role.managed:
        return text["class_role_managed"].format(role=character_class)

    old_class_roles = [
        role
        for role in member.roles
        if role.name in class_role_names and role.id != target_role.id
    ]
    has_target_role = any(role.id == target_role.id for role in member.roles)
    if has_target_role and not old_class_roles:
        return None

    roles_to_manage = old_class_roles + ([] if has_target_role else [target_role])
    if bot_member is not None:
        if not bot_member.guild_permissions.manage_roles:
            return text["class_role_need_permission"]
        if any(role >= bot_member.top_role for role in roles_to_manage):
            return text["class_role_too_low"]

    try:
        if not has_target_role:
            await member.add_roles(target_role, reason="Ragnarok character class sync")
        if old_class_roles:
            await member.remove_roles(*old_class_roles, reason="Ragnarok character class sync")
    except discord.Forbidden:
        logger.exception("Missing permission to sync class role for %s", member.id)
        return text["class_role_forbidden"]
    except discord.HTTPException:
        logger.exception("Discord rejected class role sync for %s", member.id)
        return text["class_role_rejected"]
    return text["class_role_synced"].format(role=character_class)


async def _sync_base_role(
    member: discord.Member,
    bot_member: discord.Member | None,
    lang: str = "th",
) -> str | None:
    text = _MSG[lang]
    target_role = discord.utils.get(member.guild.roles, name=BASE_ROLE_NAME)
    if target_role is None:
        return text["base_role_missing"].format(role=BASE_ROLE_NAME)
    if target_role in member.roles:
        return None
    if target_role.managed:
        return text["base_role_managed"].format(role=BASE_ROLE_NAME)

    if bot_member is not None:
        if not bot_member.guild_permissions.manage_roles:
            return text["base_role_need_permission"]
        if target_role >= bot_member.top_role:
            return text["base_role_too_low"].format(role=BASE_ROLE_NAME)

    try:
        await member.add_roles(target_role, reason="Ragnarok guild base role grant")
    except discord.Forbidden:
        logger.exception("Missing permission to grant base role for %s", member.id)
        return text["base_role_forbidden"]
    except discord.HTTPException:
        logger.exception("Discord rejected base role grant for %s", member.id)
        return text["base_role_rejected"]
    return text["base_role_added"].format(role=BASE_ROLE_NAME)


def _with_sync_notes(message: str, notes: list[str]) -> str:
    if not notes:
        return message
    formatted_notes = "\n".join(f"- {note}" for note in notes)
    return f"{message}\n\n{formatted_notes}"


# --- Registration approval ---------------------------------------------------
# A registration is applied in two steps: /register queues a row in the
# Registrations sheet, and an officer's button press turns that row into a
# Members row plus the Discord nickname/role sync. The buttons are
# ``DynamicItem``s keyed by request id, so they rebuild themselves from the
# message after a restart and the bot never has to keep the queue in memory.


class RegistrationDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"regreq:(?P<action>approve|reject):(?P<request_id>[A-Za-z0-9-]+)",
):
    def __init__(self, action: str, request_id: str) -> None:
        self.action = action
        self.request_id = request_id
        approve = action == "approve"
        super().__init__(
            discord.ui.Button(
                label=_APPROVAL["button_approve"] if approve else _APPROVAL["button_reject"],
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"regreq:{action}:{request_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "RegistrationDecisionButton":
        del interaction, item
        return cls(match["action"], match["request_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        if not _may_approve(bot, interaction.user):
            await interaction.response.send_message(_APPROVAL["no_permission"], ephemeral=True)
            return

        if self.action == "approve":
            await _handle_approval(bot, interaction, self.request_id)
        else:
            # A modal has to be the *first* response to the interaction, so the
            # rejection path defers inside the modal submit instead.
            await interaction.response.send_modal(RegistrationRejectModal(self.request_id))


class RegistrationRejectModal(discord.ui.Modal, title=_APPROVAL["reject_modal_title"]):
    note = discord.ui.TextInput(
        label=_APPROVAL["reject_reason_label"],
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, request_id: str) -> None:
        super().__init__()
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _handle_rejection(
            interaction.client, interaction, self.request_id, str(self.note)
        )


def _may_approve(bot: RagnarokGuildBot, user: discord.abc.User) -> bool:
    """Server admins always qualify; otherwise the configured role, or, when
    no role is configured, anyone who can already manage roles."""
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    role_id = bot.config.register_approver_role_id
    if role_id is not None:
        return any(role.id == role_id for role in user.roles)
    return user.guild_permissions.manage_roles


def _registration_view(request_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RegistrationDecisionButton("approve", request_id))
    view.add_item(RegistrationDecisionButton("reject", request_id))
    return view


def _registration_embed(request: RegistrationRequest) -> discord.Embed:
    embed = discord.Embed(title=_APPROVAL["card_title"], color=discord.Color.gold())
    embed.add_field(name=_APPROVAL["field_request_id"], value=request.request_id, inline=False)
    embed.add_field(
        name=_APPROVAL["field_member"],
        value=_user_id_reference(request.discord_user_id),
        inline=False,
    )
    embed.add_field(name=_APPROVAL["field_character"], value=request.character_name, inline=True)
    embed.add_field(name=_APPROVAL["field_class"], value=request.character_class, inline=True)
    embed.add_field(name=_APPROVAL["field_nickname"], value=request.real_nickname, inline=True)
    embed.set_footer(text=_APPROVAL["footer_submitted"].format(submitted_at=request.submitted_at))
    return embed


def _registration_decided_embed(request: RegistrationRequest) -> discord.Embed:
    approved = request.status == STATUS_APPROVED
    embed = _registration_embed(request)
    embed.colour = discord.Color.green() if approved else discord.Color.red()
    template = _APPROVAL["decision_approved"] if approved else _APPROVAL["decision_rejected"]
    embed.add_field(
        name=_APPROVAL["field_decision"],
        value=template.format(approver=_mention_or_dash(request.decided_by)),
        inline=False,
    )
    if request.note:
        embed.add_field(name=_APPROVAL["field_reason"], value=request.note, inline=False)
    embed.set_footer(text=_APPROVAL["footer_decided"].format(decided_at=request.decided_at))
    return embed


async def _post_registration_request(
    bot: RagnarokGuildBot,
    request: RegistrationRequest,
) -> bool:
    channel = await _get_log_channel(
        bot, bot.config.register_approval_channel_id, "registration approval"
    )
    if channel is None:
        return False

    try:
        await channel.send(
            embed=_registration_embed(request),
            view=_registration_view(request.request_id),
        )
    except discord.DiscordException:
        logger.exception("Could not post registration %s to approval channel", request.request_id)
        return False
    return True


async def _claim_registration(
    bot: RagnarokGuildBot,
    interaction: discord.Interaction,
    request_id: str,
    status: str,
    note: str = "",
) -> RegistrationRequest | None:
    """Take ownership of a pending request, replying to the officer on failure.

    Returns the claimed request, or ``None`` when the caller should stop —
    unknown id, someone else got there first, or Sheets was unreachable.
    """
    try:
        claim = await asyncio.to_thread(
            bot.store.decide_registration, request_id, status, interaction.user.id, note
        )
    except Exception:
        logger.exception("Could not record a decision for registration %s", request_id)
        await interaction.followup.send(_APPROVAL["sheet_error"], ephemeral=True)
        return None

    if claim is None:
        await interaction.followup.send(_APPROVAL["unknown_request"], ephemeral=True)
        return None

    request, claimed = claim
    if not claimed:
        await interaction.followup.send(
            _APPROVAL["already_decided"].format(
                status=request.status, approver=_mention_or_dash(request.decided_by)
            ),
            ephemeral=True,
        )
        return None
    return request


async def _handle_approval(
    bot: RagnarokGuildBot,
    interaction: discord.Interaction,
    request_id: str,
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    request = await _claim_registration(bot, interaction, request_id, STATUS_APPROVED)
    if request is None:
        return

    try:
        profile, created = await asyncio.to_thread(
            bot.store.upsert_member,
            request.discord_user_id,
            request.discord_display_name,
            request.character_name,
            request.character_class,
            request.real_nickname,
        )
    except Exception:
        # The decision is already stamped on the row, so put it back in the
        # queue rather than leaving an "approved" request that never applied.
        logger.exception("Registration %s was approved but the Members write failed", request_id)
        try:
            await asyncio.to_thread(bot.store.reopen_registration, request_id)
        except Exception:
            logger.exception("Could not reopen registration %s", request_id)
        await interaction.followup.send(_APPROVAL["member_write_failed"], ephemeral=True)
        return

    notes: list[str] = []
    member = await _guild_member(interaction.guild, request.discord_user_id)
    if member is None:
        notes.append(_APPROVAL["member_not_in_guild"])
    else:
        notes.extend(
            await _sync_profile_for_member(
                bot,
                member,
                profile,
                sync_nickname=True,
                sync_class_role=True,
                sync_base_role=True,
                lang="th",
            )
        )

    await _post_member_log(
        bot,
        _user_id_reference(request.discord_user_id),
        profile,
        "ลงทะเบียนสมาชิกใหม่" if created else "อัปเดตข้อมูลการลงทะเบียน",
    )
    await _finish_registration_card(interaction, request)

    if not await _notify_requester(bot, request, profile=profile, created=created):
        notes.append(_APPROVAL["dm_failed"])
    await interaction.followup.send(
        _with_sync_notes(_APPROVAL["approved"].format(request_id=request_id), notes),
        ephemeral=True,
    )


async def _handle_rejection(
    bot: RagnarokGuildBot,
    interaction: discord.Interaction,
    request_id: str,
    note: str,
) -> None:
    note = " ".join(note.split())
    await interaction.response.defer(ephemeral=True, thinking=True)
    request = await _claim_registration(bot, interaction, request_id, STATUS_REJECTED, note)
    if request is None:
        return

    await _finish_registration_card(interaction, request)

    notes: list[str] = []
    if not await _notify_requester(bot, request, profile=None, created=False):
        notes.append(_APPROVAL["dm_failed"])
    await interaction.followup.send(
        _with_sync_notes(_APPROVAL["rejected"].format(request_id=request_id), notes),
        ephemeral=True,
    )


async def _finish_registration_card(
    interaction: discord.Interaction,
    request: RegistrationRequest,
) -> None:
    """Restamp the card with the outcome and drop its buttons."""
    message = interaction.message
    if message is None:
        return
    try:
        await message.edit(embed=_registration_decided_embed(request), view=None)
    except discord.DiscordException:
        logger.exception("Could not update the card for registration %s", request.request_id)


async def _notify_requester(
    bot: RagnarokGuildBot,
    request: RegistrationRequest,
    *,
    profile: MemberProfile | None,
    created: bool,
) -> bool:
    """DM the outcome to the member, in the language they registered in."""
    text = _MSG.get(request.lang, _MSG["th"])
    if profile is not None:
        detail = (text["register_created"] if created else text["register_updated"]).format(
            character_name=profile.character_name,
            character_class=profile.character_class,
            nickname=profile.real_nickname,
            member_id=profile.member_id,
        )
        message = (
            f"{text['register_approved_notice'].format(request_id=request.request_id)}\n{detail}"
        )
    else:
        message = text["register_rejected_notice"].format(request_id=request.request_id)
        if request.note:
            message = f"{message}\n{text['register_rejected_reason'].format(note=request.note)}"

    try:
        user_id = int(request.discord_user_id)
    except (TypeError, ValueError):
        return False

    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(message)
    except discord.DiscordException:
        logger.warning("Could not DM the registration outcome to %s", request.discord_user_id)
        return False
    return True


async def _guild_member(
    guild: discord.Guild | None,
    user_id: str | int,
) -> discord.Member | None:
    if guild is None:
        return None
    try:
        target = int(user_id)
    except (TypeError, ValueError):
        return None

    member = guild.get_member(target)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(target)
    except discord.DiscordException:
        logger.warning("Could not fetch member %s for the profile sync", user_id)
        return None


def _user_reference(user: discord.abc.User) -> str:
    return f"{user.mention} (`{user.id}`)"


def _user_id_reference(user_id: str | int) -> str:
    return f"<@{user_id}> (`{user_id}`)"


def _mention_or_dash(user_id: str) -> str:
    return f"<@{user_id}>" if user_id else "-"


async def _get_log_channel(
    bot: RagnarokGuildBot,
    channel_id: int,
    label: str,
) -> discord.abc.Messageable | None:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.DiscordException:
            logger.exception("Could not fetch %s channel %s", label, channel_id)
            return None

    if not hasattr(channel, "send"):
        logger.error("Configured %s channel %s is not messageable", label, channel_id)
        return None
    return channel


async def _post_absence_to_officer_channel(
    bot: RagnarokGuildBot,
    interaction: discord.Interaction,
    member: MemberProfile,
    request: AbsenceRequest,
) -> bool:
    channel = await _get_log_channel(bot, bot.config.absence_channel_id, "absence")
    if channel is None:
        return False

    embed = discord.Embed(
        title="คำขอลา",
        color=discord.Color.blue(),
    )
    embed.add_field(name="รหัสคำขอ", value=request.request_id, inline=False)
    embed.add_field(name="สมาชิก", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
    embed.add_field(name="ตัวละคร", value=member.character_name, inline=True)
    embed.add_field(name="อาชีพ", value=member.character_class, inline=True)
    embed.add_field(
        name="วันที่ลา",
        value=f"{format_mmdd(request.start_date)} ถึง {format_mmdd(request.end_date)}",
        inline=False,
    )
    embed.add_field(name="เหตุผล", value=request.reason, inline=False)
    embed.set_footer(text=f"ส่งคำขอเมื่อ {request.submitted_at}")

    try:
        await channel.send(embed=embed)
    except discord.DiscordException:
        logger.exception("Could not post absence %s to officer channel", request.request_id)
        return False
    return True


async def _post_member_log(
    bot: RagnarokGuildBot,
    member_reference: str,
    profile: MemberProfile,
    action: str,
) -> bool:
    channel = await _get_log_channel(bot, bot.config.member_log_channel_id, "member log")
    if channel is None:
        return False

    embed = discord.Embed(title=action, color=discord.Color.green())
    embed.add_field(name="สมาชิก", value=member_reference, inline=False)
    embed.add_field(name="รหัสสมาชิก", value=profile.member_id or "-", inline=True)
    embed.add_field(name="ตัวละคร", value=profile.character_name, inline=True)
    embed.add_field(name="อาชีพ", value=profile.character_class, inline=True)
    embed.add_field(name="ชื่อเล่น", value=profile.real_nickname, inline=True)
    embed.set_footer(text=f"อัปเดตเมื่อ {profile.updated_at}")

    try:
        await channel.send(embed=embed)
    except discord.DiscordException:
        logger.exception("Could not post member log for %s", member_reference)
        return False
    return True


def _display_name(interaction: discord.Interaction) -> str:
    return getattr(interaction.user, "display_name", interaction.user.name)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "ro_guild_bot starting (build: login-backoff-retry v3, %s)",
        "IPv4-forced" if FORCE_IPV4 else "IPv4+IPv6",
    )
    config = load_config()
    store = GuildSheetStore.from_service_account_file(
        credentials_path=config.google_service_account_file,
        spreadsheet_id=config.google_sheet_id,
    )
    try:
        asyncio.run(_run_bot(config, store))
    except KeyboardInterrupt:
        logger.info("Bot stopped.")


def _retry_after_seconds(exc: BaseException) -> float:
    """Read a Retry-After header off an HTTP error, if one is present."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _login_backoff_delay(exc: BaseException, backoff: float) -> float:
    """Choose how long to wait before the next startup attempt.

    A 429 (Discord hard rate limit, or a Cloudflare 1015 IP block) gets a long
    floor: retrying quickly only deepens the ban. Everything else uses the
    caller's current exponential backoff value.
    """
    status = getattr(exc, "status", None) or getattr(
        getattr(exc, "response", None), "status", None
    )
    if status == 429:
        # Discord/Cloudflare can report a very long Retry-After (a deep 1015
        # ban, or an unreliable value off the HTML block page). Don't block on
        # a multi-hour sleep: cap the wait and re-probe. One attempt every
        # <= 30 min is far too infrequent to extend a ban, and it means we
        # reconnect the moment the ban clears instead of sleeping past it.
        retry_after = _retry_after_seconds(exc)
        base = max(retry_after, RATE_LIMIT_MIN_BACKOFF_SECONDS, backoff)
        base = min(base, RATE_LIMIT_MAX_BACKOFF_SECONDS)
    else:
        base = backoff
    # Add up to 20% jitter so retries never land on an exact boundary.
    return base + random.uniform(0.0, base * 0.2)


def _describe_exc(exc: BaseException) -> str:
    status = getattr(exc, "status", None)
    return f"{type(exc).__name__} (HTTP {status})" if status else type(exc).__name__


async def _run_bot(config: BotConfig, store: GuildSheetStore) -> None:
    """Run the bot, retrying transient startup failures with backoff.

    discord.py clients cannot be restarted once closed, so every attempt builds
    a *fresh* bot and connector. A bad token (LoginFailure / 401) is fatal and
    re-raised immediately -- waiting never fixes wrong credentials. Rate limits
    and network hiccups back off exponentially, with a long floor for 429s.
    """
    attempt = 0
    backoff = LOGIN_RETRY_BASE_SECONDS
    while True:
        attempt += 1
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(
            limit=0,
            ssl=ssl_context,
            family=socket.AF_INET if FORCE_IPV4 else socket.AF_UNSPEC,
        )
        bot = create_bot(config=config, store=store)
        bot.http.connector = connector
        session_start = time.monotonic()
        try:
            await bot.start(config.discord_token)
            return  # clean shutdown (e.g. bot.close() was called elsewhere)
        except discord.LoginFailure:
            logger.error(
                "Login failed: the Discord token is invalid or revoked. Not retrying."
            )
            raise
        except (discord.HTTPException, aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            # If the session had actually logged in and run for a while, treat
            # this as a brand-new problem so a mid-session drop doesn't inherit
            # a huge accumulated backoff.
            if time.monotonic() - session_start >= SESSION_HEALTHY_SECONDS:
                attempt = 1
                backoff = LOGIN_RETRY_BASE_SECONDS
            if LOGIN_MAX_ATTEMPTS and attempt >= LOGIN_MAX_ATTEMPTS:
                logger.error("Giving up after %d startup attempts.", attempt)
                raise
            delay = _login_backoff_delay(exc, backoff)
            logger.warning(
                "Startup attempt %d failed: %s. Waiting %.0fs before retry.",
                attempt,
                _describe_exc(exc),
                delay,
            )
        finally:
            if not bot.is_closed():
                await bot.close()
            if not connector.closed:
                await connector.close()
        # Reached only after a transient failure (LoginFailure/return exited above).
        await asyncio.sleep(delay)
        backoff = min(backoff * 2, LOGIN_RETRY_MAX_SECONDS)