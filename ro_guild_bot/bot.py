from __future__ import annotations

import asyncio
import logging
import ssl

import aiohttp
import certifi
import discord
from discord import app_commands
from discord.ext import commands

from ro_guild_bot.classes import RO_CLASSES, autocomplete_classes, resolve_class
from ro_guild_bot.config import BotConfig, load_config
from ro_guild_bot.sheets import AbsenceRequest, GuildSheetStore, MemberProfile
from ro_guild_bot.validation import clean_required_text, parse_date_range


logger = logging.getLogger(__name__)
DISCORD_NICKNAME_MAX_LENGTH = 32


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
        name="register",
        description="Register or update your main Ragnarok Online character.",
        guild=guild,
    )
    @app_commands.rename(character_class="class")
    @app_commands.describe(
        character_name="Your main character name.",
        character_class="Your Ragnarok Online class.",
        nickname="Your real nickname.",
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
                "character_name",
                max_length=DISCORD_NICKNAME_MAX_LENGTH,
            )
            nickname = clean_required_text(nickname, "nickname", max_length=60)
            canonical_class = resolve_class(character_class)
            if canonical_class is None:
                await interaction.response.send_message(
                    "That class is not in the Classic + Trans class list. Pick one from autocomplete.",
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
                "Registration failed because Google Sheets could not be updated.",
                ephemeral=True,
            )
            return

        sync_notes = await _sync_discord_profile(bot, interaction, profile, sync_nickname=True, sync_class_role=True)

        if created:
            message = (
                f"Registered **{profile.character_name}** as **{profile.character_class}** "
                f"with nickname **{profile.real_nickname}**."
            )
        else:
            message = (
                f"Updated your registration to **{profile.character_name}** "
                f"(**{profile.character_class}**) with nickname **{profile.real_nickname}**."
            )
        await interaction.followup.send(_with_sync_notes(message, sync_notes), ephemeral=True)

    @bot.tree.command(
        name="change-name",
        description="Change your registered Ragnarok Online character name.",
        guild=guild,
    )
    @app_commands.describe(new_character_name="Your new main character name.")
    async def change_name(interaction: discord.Interaction, new_character_name: str) -> None:
        try:
            new_character_name = clean_required_text(
                new_character_name,
                "new_character_name",
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
                "Name change failed because Google Sheets could not be updated.",
                ephemeral=True,
            )
            return

        if profile is None:
            await interaction.followup.send("You are not registered yet. Use `/register` first.", ephemeral=True)
            return
        sync_notes = await _sync_discord_profile(bot, interaction, profile, sync_nickname=True, sync_class_role=False)
        await interaction.followup.send(
            _with_sync_notes(f"Updated your character name to **{profile.character_name}**.", sync_notes),
            ephemeral=True,
        )

    @bot.tree.command(
        name="change-class",
        description="Change your registered Ragnarok Online class.",
        guild=guild,
    )
    @app_commands.rename(new_character_class="new_class")
    @app_commands.describe(new_character_class="Your new Ragnarok Online class.")
    @app_commands.autocomplete(new_character_class=class_autocomplete)
    async def change_class(interaction: discord.Interaction, new_character_class: str) -> None:
        canonical_class = resolve_class(new_character_class)
        if canonical_class is None:
            await interaction.response.send_message(
                "That class is not in the Classic + Trans class list. Pick one from autocomplete.",
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
                "Class change failed because Google Sheets could not be updated.",
                ephemeral=True,
            )
            return

        if profile is None:
            await interaction.followup.send("You are not registered yet. Use `/register` first.", ephemeral=True)
            return
        sync_notes = await _sync_discord_profile(bot, interaction, profile, sync_nickname=False, sync_class_role=True)
        await interaction.followup.send(
            _with_sync_notes(f"Updated your class to **{profile.character_class}**.", sync_notes),
            ephemeral=True,
        )

    @bot.tree.command(
        name="absence",
        description="Request an absence and notify officers.",
        guild=guild,
    )
    @app_commands.describe(
        start_date="First absence date, in YYYY-MM-DD format.",
        end_date="Last absence date, in YYYY-MM-DD format.",
        reason="Reason for the absence.",
    )
    async def absence(
        interaction: discord.Interaction,
        start_date: str,
        end_date: str,
        reason: str,
    ) -> None:
        try:
            parsed_start, parsed_end = parse_date_range(start_date, end_date)
            reason = clean_required_text(reason, "reason", max_length=300)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            member = await asyncio.to_thread(bot.store.get_member, interaction.user.id)
            if member is None:
                await interaction.followup.send("You are not registered yet. Use `/register` first.", ephemeral=True)
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
                "Absence request failed because Google Sheets could not be updated.",
                ephemeral=True,
            )
            return

        posted = await _post_absence_to_officer_channel(bot, interaction, member, request)
        if posted:
            await interaction.followup.send(
                f"Absence request **{request.request_id}** submitted for "
                f"{request.start_date.isoformat()} to {request.end_date.isoformat()}.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Absence request **{request.request_id}** was saved, but I could not post to the officer channel. "
                "Check `ABSENCE_CHANNEL_ID` and bot channel permissions.",
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
) -> list[str]:
    guild = interaction.guild
    if guild is None:
        return ["Discord server profile sync skipped because this command was not used inside a server."]

    member = await _interaction_member(interaction)
    if member is None:
        return ["Discord server profile sync skipped because I could not find your server member."]

    bot_member = await _bot_member(bot, guild)
    notes: list[str] = []
    if sync_nickname:
        note = await _sync_member_nickname(member, profile.character_name, bot_member)
        if note:
            notes.append(note)
    if sync_class_role:
        note = await _sync_member_class_role(member, profile.character_class, bot_member)
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
    character_name: str,
    bot_member: discord.Member | None,
) -> str | None:
    if member.display_name == character_name:
        return None

    if bot_member is not None:
        if not bot_member.guild_permissions.manage_nicknames:
            return "Discord nickname was not changed: the bot needs `Manage Nicknames` permission."
        if member.id != bot_member.id and member.top_role >= bot_member.top_role:
            return "Discord nickname was not changed: move the bot role above this member's highest role."

    try:
        await member.edit(nick=character_name, reason="Ragnarok character name sync")
    except discord.Forbidden:
        logger.exception("Missing permission to edit nickname for %s", member.id)
        return "Discord nickname was not changed: the bot needs `Manage Nicknames` and a higher role."
    except discord.HTTPException:
        logger.exception("Discord rejected nickname edit for %s", member.id)
        return "Discord nickname was not changed: Discord rejected the nickname update."
    return "Discord nickname synced."


async def _sync_member_class_role(
    member: discord.Member,
    character_class: str,
    bot_member: discord.Member | None,
) -> str | None:
    class_role_names = set(RO_CLASSES)
    target_role = discord.utils.get(member.guild.roles, name=character_class)
    if target_role is None:
        return f"Discord class role was not changed: create a role named `{character_class}` first."
    if target_role.managed:
        return f"Discord class role was not changed: `{character_class}` is a managed role."

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
            return "Discord class role was not changed: the bot needs `Manage Roles` permission."
        if any(role >= bot_member.top_role for role in roles_to_manage):
            return "Discord class role was not changed: move the bot role above all RO class roles."

    try:
        if not has_target_role:
            await member.add_roles(target_role, reason="Ragnarok character class sync")
        if old_class_roles:
            await member.remove_roles(*old_class_roles, reason="Ragnarok character class sync")
    except discord.Forbidden:
        logger.exception("Missing permission to sync class role for %s", member.id)
        return "Discord class role was not changed: the bot needs `Manage Roles` and a higher role."
    except discord.HTTPException:
        logger.exception("Discord rejected class role sync for %s", member.id)
        return "Discord class role was not changed: Discord rejected the role update."
    return f"Discord class role synced to **{character_class}**."


def _with_sync_notes(message: str, notes: list[str]) -> str:
    if not notes:
        return message
    formatted_notes = "\n".join(f"- {note}" for note in notes)
    return f"{message}\n\n{formatted_notes}"


async def _post_absence_to_officer_channel(
    bot: RagnarokGuildBot,
    interaction: discord.Interaction,
    member: MemberProfile,
    request: AbsenceRequest,
) -> bool:
    channel = bot.get_channel(bot.config.absence_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(bot.config.absence_channel_id)
        except discord.DiscordException:
            logger.exception("Could not fetch absence channel %s", bot.config.absence_channel_id)
            return False

    if not hasattr(channel, "send"):
        logger.error("Configured absence channel %s is not messageable", bot.config.absence_channel_id)
        return False

    embed = discord.Embed(
        title="Absence Request",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Request ID", value=request.request_id, inline=False)
    embed.add_field(name="Member", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
    embed.add_field(name="Character", value=member.character_name, inline=True)
    embed.add_field(name="Class", value=member.character_class, inline=True)
    embed.add_field(
        name="Dates",
        value=f"{request.start_date.isoformat()} to {request.end_date.isoformat()}",
        inline=False,
    )
    embed.add_field(name="Reason", value=request.reason, inline=False)
    embed.set_footer(text=f"Submitted at {request.submitted_at}")

    try:
        await channel.send(embed=embed)
    except discord.DiscordException:
        logger.exception("Could not post absence %s to officer channel", request.request_id)
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
