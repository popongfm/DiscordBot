from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import date

import aiohttp
import certifi
import discord
from discord import app_commands
from discord.ext import commands

from ro_guild_bot.classes import RO_CLASSES, autocomplete_classes, resolve_class
from ro_guild_bot.config import BotConfig, load_config
from ro_guild_bot.sheets import AbsenceRequest, GuildSheetStore, MemberProfile
from ro_guild_bot.validation import clean_required_text, format_mmdd, parse_date_range, parse_mmdd_date


logger = logging.getLogger(__name__)
DISCORD_NICKNAME_MAX_LENGTH = 32
BASE_ROLE_NAME = "BABYBEAR | ROW"


class RagnarokGuildBot(commands.Bot):
    def __init__(self, config: BotConfig, store: GuildSheetStore):
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.config = config
        self.store = store
        self.guild_object = discord.Object(id=config.discord_guild_id)

    async def setup_hook(self) -> None:
        synced = await self.tree.sync(guild=self.guild_object)
        logger.info("Synced %s slash commands to guild %s", len(synced), self.config.discord_guild_id)

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")


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
        try:
            character_name = clean_required_text(
                character_name,
                "ชื่อตัวละคร",
                max_length=DISCORD_NICKNAME_MAX_LENGTH,
            )
            nickname = clean_required_text(nickname, "ชื่อเล่น", max_length=60)
            canonical_class = resolve_class(character_class)
            if canonical_class is None:
                await interaction.response.send_message(
                    "อาชีพนี้ไม่อยู่ในรายชื่ออาชีพ Classic + Trans กรุณาเลือกจากตัวเลือกอัตโนมัติ",
                    ephemeral=True,
                )
                return
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            profile, created = await asyncio.to_thread(
                bot.store.upsert_member,
                interaction.user.id,
                _display_name(interaction),
                character_name,
                canonical_class,
                nickname,
            )
        except Exception:
            logger.exception("Failed to register member %s", interaction.user.id)
            await interaction.followup.send(
                "ลงทะเบียนไม่สำเร็จ เนื่องจากไม่สามารถอัปเดต Google Sheets ได้",
                ephemeral=True,
            )
            return

        sync_notes = await _sync_discord_profile(
            bot, interaction, profile, sync_nickname=True, sync_class_role=True, sync_base_role=True
        )
        await _post_member_log(bot, interaction, profile, "ลงทะเบียนสมาชิกใหม่" if created else "อัปเดตข้อมูลการลงทะเบียน")

        if created:
            message = (
                f"ลงทะเบียน **{profile.character_name}** เป็น **{profile.character_class}** "
                f"ชื่อเล่น **{profile.real_nickname}** เรียบร้อยแล้ว รหัสสมาชิกของคุณคือ **{profile.member_id}**"
            )
        else:
            message = (
                f"อัปเดตข้อมูลลงทะเบียนของคุณเป็น **{profile.character_name}** "
                f"(**{profile.character_class}**) ชื่อเล่น **{profile.real_nickname}** "
                f"รหัสสมาชิกของคุณคือ **{profile.member_id}**"
            )
        await interaction.followup.send(_with_sync_notes(message, sync_notes), ephemeral=True)

    @bot.tree.command(
        name="เปลี่ยนชื่อ",
        description="เปลี่ยนชื่อตัวละครหลักที่ลงทะเบียนไว้",
        guild=guild,
    )
    @app_commands.rename(new_character_name="ชื่อใหม่")
    @app_commands.describe(new_character_name="ชื่อตัวละครหลักใหม่ของคุณ")
    async def change_name(interaction: discord.Interaction, new_character_name: str) -> None:
        try:
            new_character_name = clean_required_text(
                new_character_name,
                "ชื่อใหม่",
                max_length=DISCORD_NICKNAME_MAX_LENGTH,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            profile = await asyncio.to_thread(
                bot.store.update_character_name,
                interaction.user.id,
                new_character_name,
            )
        except Exception:
            logger.exception("Failed to change character name for %s", interaction.user.id)
            await interaction.followup.send(
                "เปลี่ยนชื่อไม่สำเร็จ เนื่องจากไม่สามารถอัปเดต Google Sheets ได้",
                ephemeral=True,
            )
            return

        if profile is None:
            await interaction.followup.send("คุณยังไม่ได้ลงทะเบียน กรุณาใช้คำสั่ง `/ลงทะเบียน` ก่อน", ephemeral=True)
            return
        sync_notes = await _sync_discord_profile(bot, interaction, profile, sync_nickname=True, sync_class_role=False)
        await _post_member_log(bot, interaction, profile, "เปลี่ยนชื่อตัวละคร")
        await interaction.followup.send(
            _with_sync_notes(f"อัปเดตชื่อตัวละครของคุณเป็น **{profile.character_name}** เรียบร้อยแล้ว", sync_notes),
            ephemeral=True,
        )

    @bot.tree.command(
        name="เปลี่ยนอาชีพ",
        description="เปลี่ยนอาชีพที่ลงทะเบียนไว้",
        guild=guild,
    )
    @app_commands.rename(new_character_class="อาชีพใหม่")
    @app_commands.describe(new_character_class="อาชีพใหม่ของคุณ")
    @app_commands.autocomplete(new_character_class=class_autocomplete)
    async def change_class(interaction: discord.Interaction, new_character_class: str) -> None:
        canonical_class = resolve_class(new_character_class)
        if canonical_class is None:
            await interaction.response.send_message(
                "อาชีพนี้ไม่อยู่ในรายชื่ออาชีพ Classic + Trans กรุณาเลือกจากตัวเลือกอัตโนมัติ",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            profile = await asyncio.to_thread(
                bot.store.update_character_class,
                interaction.user.id,
                canonical_class,
            )
        except Exception:
            logger.exception("Failed to change class for %s", interaction.user.id)
            await interaction.followup.send(
                "เปลี่ยนอาชีพไม่สำเร็จ เนื่องจากไม่สามารถอัปเดต Google Sheets ได้",
                ephemeral=True,
            )
            return

        if profile is None:
            await interaction.followup.send("คุณยังไม่ได้ลงทะเบียน กรุณาใช้คำสั่ง `/ลงทะเบียน` ก่อน", ephemeral=True)
            return
        sync_notes = await _sync_discord_profile(bot, interaction, profile, sync_nickname=False, sync_class_role=True)
        await _post_member_log(bot, interaction, profile, "เปลี่ยนอาชีพ")
        await interaction.followup.send(
            _with_sync_notes(f"อัปเดตอาชีพของคุณเป็น **{profile.character_class}** เรียบร้อยแล้ว", sync_notes),
            ephemeral=True,
        )

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
        try:
            if start_date is None and end_date is None:
                parsed_start = parsed_end = date.today()
            elif start_date is None:
                raise ValueError("ต้องระบุวันเริ่ม หากมีการระบุวันสิ้นสุด")
            elif end_date is None:
                parsed_start = parsed_end = parse_mmdd_date(start_date, "วันเริ่ม")
            else:
                parsed_start, parsed_end = parse_date_range(start_date, end_date)
            reason = clean_required_text(reason, "เหตุผล", max_length=300)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            member = await asyncio.to_thread(bot.store.get_member, interaction.user.id)
            if member is None:
                await interaction.followup.send("คุณยังไม่ได้ลงทะเบียน กรุณาใช้คำสั่ง `/ลงทะเบียน` ก่อน", ephemeral=True)
                return
            request = await asyncio.to_thread(
                bot.store.append_absence,
                member,
                parsed_start,
                parsed_end,
                reason,
            )
        except Exception:
            logger.exception("Failed to create absence for %s", interaction.user.id)
            await interaction.followup.send(
                "แจ้งลาไม่สำเร็จ เนื่องจากไม่สามารถอัปเดต Google Sheets ได้",
                ephemeral=True,
            )
            return

        posted = await _post_absence_to_officer_channel(bot, interaction, member, request)
        if posted:
            await interaction.followup.send(
                f"ส่งคำขอลา **{request.request_id}** สำหรับวันที่ "
                f"{format_mmdd(request.start_date)} ถึง {format_mmdd(request.end_date)} เรียบร้อยแล้ว",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"บันทึกคำขอลา **{request.request_id}** แล้ว แต่ไม่สามารถส่งข้อความไปยังห้องหัวกิลด์ได้ "
                "กรุณาตรวจสอบ `ABSENCE_CHANNEL_ID` และสิทธิ์ของบอทในห้องนั้น",
                ephemeral=True,
            )

    return bot


async def _sync_discord_profile(
    bot: RagnarokGuildBot,
    interaction: discord.Interaction,
    profile: MemberProfile,
    *,
    sync_nickname: bool,
    sync_class_role: bool,
    sync_base_role: bool = False,
) -> list[str]:
    guild = interaction.guild
    if guild is None:
        return ["ข้ามการซิงค์โปรไฟล์ Discord เนื่องจากคำสั่งนี้ไม่ได้ใช้งานภายในเซิร์ฟเวอร์"]

    member = await _interaction_member(interaction)
    if member is None:
        return ["ข้ามการซิงค์โปรไฟล์ Discord เนื่องจากไม่พบข้อมูลสมาชิกของคุณในเซิร์ฟเวอร์"]

    bot_member = await _bot_member(bot, guild)
    notes: list[str] = []
    if sync_nickname:
        note = await _sync_member_nickname(member, profile, bot_member)
        if note:
            notes.append(note)
    if sync_class_role:
        note = await _sync_member_class_role(member, profile.character_class, bot_member)
        if note:
            notes.append(note)
    if sync_base_role:
        note = await _sync_base_role(member, bot_member)
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
) -> str | None:
    target_nickname = _format_discord_nickname(profile)
    if member.display_name == target_nickname:
        return None

    if bot_member is not None:
        if not bot_member.guild_permissions.manage_nicknames:
            return "ไม่ได้เปลี่ยนชื่อเล่นใน Discord: บอทต้องการสิทธิ์ `Manage Nicknames`"
        if member.id != bot_member.id and member.top_role >= bot_member.top_role:
            return "ไม่ได้เปลี่ยนชื่อเล่นใน Discord: กรุณาเลื่อนตำแหน่งโรลของบอทให้สูงกว่าโรลสูงสุดของสมาชิกคนนี้"

    try:
        await member.edit(nick=target_nickname, reason="Ragnarok character name sync")
    except discord.Forbidden:
        logger.exception("Missing permission to edit nickname for %s", member.id)
        return "ไม่ได้เปลี่ยนชื่อเล่นใน Discord: บอทต้องการสิทธิ์ `Manage Nicknames` และโรลที่สูงกว่า"
    except discord.HTTPException:
        logger.exception("Discord rejected nickname edit for %s", member.id)
        return "ไม่ได้เปลี่ยนชื่อเล่นใน Discord: Discord ปฏิเสธการอัปเดตชื่อเล่น"
    return "ซิงค์ชื่อเล่นใน Discord เรียบร้อยแล้ว"


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
) -> str | None:
    class_role_names = set(RO_CLASSES)
    target_role = discord.utils.get(member.guild.roles, name=character_class)
    if target_role is None:
        return f"ไม่ได้เปลี่ยนโรลอาชีพใน Discord: กรุณาสร้างโรลชื่อ `{character_class}` ก่อน"
    if target_role.managed:
        return f"ไม่ได้เปลี่ยนโรลอาชีพใน Discord: `{character_class}` เป็นโรลที่ถูกจัดการโดยระบบ (managed role)"

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
            return "ไม่ได้เปลี่ยนโรลอาชีพใน Discord: บอทต้องการสิทธิ์ `Manage Roles`"
        if any(role >= bot_member.top_role for role in roles_to_manage):
            return "ไม่ได้เปลี่ยนโรลอาชีพใน Discord: กรุณาเลื่อนตำแหน่งโรลของบอทให้สูงกว่าโรลอาชีพทั้งหมด"

    try:
        if not has_target_role:
            await member.add_roles(target_role, reason="Ragnarok character class sync")
        if old_class_roles:
            await member.remove_roles(*old_class_roles, reason="Ragnarok character class sync")
    except discord.Forbidden:
        logger.exception("Missing permission to sync class role for %s", member.id)
        return "ไม่ได้เปลี่ยนโรลอาชีพใน Discord: บอทต้องการสิทธิ์ `Manage Roles` และโรลที่สูงกว่า"
    except discord.HTTPException:
        logger.exception("Discord rejected class role sync for %s", member.id)
        return "ไม่ได้เปลี่ยนโรลอาชีพใน Discord: Discord ปฏิเสธการอัปเดตโรล"
    return f"ซิงค์โรลอาชีพใน Discord เป็น **{character_class}** เรียบร้อยแล้ว"


async def _sync_base_role(
    member: discord.Member,
    bot_member: discord.Member | None,
) -> str | None:
    target_role = discord.utils.get(member.guild.roles, name=BASE_ROLE_NAME)
    if target_role is None:
        return f"ไม่ได้เพิ่มโรล Discord: กรุณาสร้างโรลชื่อ `{BASE_ROLE_NAME}` ก่อน"
    if target_role in member.roles:
        return None
    if target_role.managed:
        return f"ไม่ได้เพิ่มโรล Discord: `{BASE_ROLE_NAME}` เป็นโรลที่ถูกจัดการโดยระบบ (managed role)"

    if bot_member is not None:
        if not bot_member.guild_permissions.manage_roles:
            return "ไม่ได้เพิ่มโรล Discord: บอทต้องการสิทธิ์ `Manage Roles`"
        if target_role >= bot_member.top_role:
            return f"ไม่ได้เพิ่มโรล Discord: กรุณาเลื่อนตำแหน่งโรลของบอทให้สูงกว่า `{BASE_ROLE_NAME}`"

    try:
        await member.add_roles(target_role, reason="Ragnarok guild base role grant")
    except discord.Forbidden:
        logger.exception("Missing permission to grant base role for %s", member.id)
        return f"ไม่ได้เพิ่มโรล Discord: บอทต้องการสิทธิ์ `Manage Roles` และโรลที่สูงกว่า"
    except discord.HTTPException:
        logger.exception("Discord rejected base role grant for %s", member.id)
        return "ไม่ได้เพิ่มโรล Discord: Discord ปฏิเสธการเพิ่มโรล"
    return f"เพิ่มโรล Discord `{BASE_ROLE_NAME}` เรียบร้อยแล้ว"


def _with_sync_notes(message: str, notes: list[str]) -> str:
    if not notes:
        return message
    formatted_notes = "\n".join(f"- {note}" for note in notes)
    return f"{message}\n\n{formatted_notes}"


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
    interaction: discord.Interaction,
    profile: MemberProfile,
    action: str,
) -> bool:
    channel = await _get_log_channel(bot, bot.config.member_log_channel_id, "member log")
    if channel is None:
        return False

    embed = discord.Embed(title=action, color=discord.Color.green())
    embed.add_field(name="สมาชิก", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
    embed.add_field(name="รหัสสมาชิก", value=profile.member_id or "-", inline=True)
    embed.add_field(name="ตัวละคร", value=profile.character_name, inline=True)
    embed.add_field(name="อาชีพ", value=profile.character_class, inline=True)
    embed.add_field(name="ชื่อเล่น", value=profile.real_nickname, inline=True)
    embed.set_footer(text=f"อัปเดตเมื่อ {profile.updated_at}")

    try:
        await channel.send(embed=embed)
    except discord.DiscordException:
        logger.exception("Could not post member log for %s", interaction.user.id)
        return False
    return True


def _display_name(interaction: discord.Interaction) -> str:
    return getattr(interaction.user, "display_name", interaction.user.name)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
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


async def _run_bot(config: BotConfig, store: GuildSheetStore) -> None:
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(limit=0, ssl=ssl_context)
    bot = create_bot(config=config, store=store)
    bot.http.connector = connector
    try:
        await bot.start(config.discord_token)
    finally:
        if not bot.is_closed():
            await bot.close()
