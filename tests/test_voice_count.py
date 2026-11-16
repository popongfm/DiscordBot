"""Tests for the /voice-count head count (no Discord or Google access needed)."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord

from ro_guild_bot import bot as botmod
from ro_guild_bot.config import BotConfig


def _room(n: int, name: str = "War Room", start: int = 10**17) -> botmod.VoiceRoomSnapshot:
    return botmod.VoiceRoomSnapshot(
        channel_id=1, channel_name=name, user_ids=tuple(start + i for i in range(n))
    )


def _message_chars(embed: discord.Embed) -> int:
    return len(embed)


class BuildEmbedsTest(unittest.TestCase):
    taken_at = datetime(2026, 11, 16, 23, 30, 5, tzinfo=timezone.utc)

    def test_header_has_total_and_bangkok_time(self):
        embeds = botmod._build_voice_log_embeds(
            [_room(3), _room(2, "Party")], {}, "<@42>", self.taken_at
        )
        self.assertEqual(len(embeds), 1)
        self.assertIn("รวม **5** คน จาก **2** ห้อง", embeds[0].description)
        # 23:30 UTC on the 16th is 06:30 on the 17th in Bangkok.
        self.assertIn("17/11/2026 06:30:05 (GMT+7)", embeds[0].description)
        self.assertIn("<@42>", embeds[0].description)

    def test_empty_room_is_recorded_as_zero(self):
        embeds = botmod._build_voice_log_embeds([_room(0)], {}, "<@42>", self.taken_at)
        field = embeds[0].fields[0]
        self.assertIn("0 คน", field.name)
        self.assertIn("ไม่มีคนในห้อง", field.value)

    def test_registered_members_show_character_name_escaped(self):
        room = _room(2)
        names = {str(room.user_ids[0]): "Kafra_Says*Hi"}
        value = botmod._build_voice_log_embeds([room], names, "<@42>", self.taken_at)[0].fields[0].value
        lines = value.splitlines()
        self.assertEqual(lines[0], f"1. <@{room.user_ids[0]}> — Kafra\\_Says\\*Hi")
        self.assertEqual(lines[1], f"2. <@{room.user_ids[1]}>")

    def test_large_counts_stay_inside_discord_limits(self):
        # 25 rooms x 40 people with 32-char names, far past one message.
        rooms = [_room(40, f"Room {r}", start=10**17 + r * 1000) for r in range(25)]
        names = {str(uid): "N" * 32 for room in rooms for uid in room.user_ids}
        embeds = botmod._build_voice_log_embeds(rooms, names, "<@42>", self.taken_at)
        self.assertGreater(len(embeds), 1)
        listed = 0
        for embed in embeds:
            self.assertLessEqual(_message_chars(embed), 6000)
            self.assertLessEqual(len(embed.fields), 25)
            for field in embed.fields:
                self.assertLessEqual(len(field.value), 1024)
                self.assertLessEqual(len(field.name), 256)
                listed += len(field.value.splitlines())
        self.assertEqual(listed, 1000)  # nobody dropped
        self.assertTrue(embeds[-1].footer.text.startswith(f"หน้า {len(embeds)}/"))


class SnapshotAndPermissionTest(unittest.TestCase):
    def test_snapshot_skips_bots(self):
        channel = SimpleNamespace(
            id=7,
            name="War",
            members=[
                SimpleNamespace(id=1, bot=False),
                SimpleNamespace(id=2, bot=True),
                SimpleNamespace(id=3, bot=False),
            ],
        )
        snap = botmod._snapshot_voice_channel(channel)
        self.assertEqual(snap.user_ids, (1, 3))
        self.assertEqual(snap.count, 2)

    def test_permission_is_manage_roles(self):
        allowed = SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True))
        # discord.py reports administrators' guild_permissions as all().
        admin = SimpleNamespace(guild_permissions=discord.Permissions.all())
        denied = SimpleNamespace(guild_permissions=discord.Permissions(send_messages=True))
        self.assertTrue(botmod._may_count_voice(allowed))
        self.assertTrue(botmod._may_count_voice(admin))
        self.assertFalse(botmod._may_count_voice(denied))


class CommandRegistrationTest(unittest.TestCase):
    def test_both_commands_registered_and_hidden_without_manage_roles(self):
        config = BotConfig(
            discord_token="x",
            discord_guild_id=123,
            absence_channel_id=1,
            member_log_channel_id=2,
            google_sheet_id="s",
            google_service_account_file=Path("k.json"),
            register_approval_channel_id=3,
            voice_log_channel_id=4,
        )

        async def build():
            return botmod.create_bot(config, mock.Mock())

        bot = asyncio.run(build())
        guild = discord.Object(id=123)
        for name in ("นับคน", "voice-count"):
            command = bot.tree.get_command(name, guild=guild)
            self.assertIsNotNone(command, name)
            self.assertEqual(command.default_permissions, discord.Permissions(manage_roles=True))
            self.assertEqual(command.parameters, [])

    def test_picker_preselects_current_channel(self):
        async def build():
            preselect = mock.Mock(spec=discord.VoiceChannel)
            preselect.id = 555
            view = botmod.VoiceCountView(mock.Mock(), 1, "th", preselect)
            return view

        view = asyncio.run(build())
        self.assertEqual([d.id for d in view.picker.default_values], [555])
        self.assertEqual(view.picker.max_values, 25)
        self.assertEqual(view.picker.channel_types, [discord.ChannelType.voice])


class PickFlowTest(unittest.TestCase):
    """Drive VoiceCountView._on_pick end to end with Discord/Sheets mocked."""

    def _run(self, *, names_error=False, post_ok=True):
        humans = [SimpleNamespace(id=11, bot=False), SimpleNamespace(id=12, bot=False)]
        music_bot = SimpleNamespace(id=99, bot=True)
        channel = mock.Mock(spec=discord.VoiceChannel)
        channel.id, channel.name, channel.members = 7, "War", humans + [music_bot]
        picked = mock.Mock()
        picked.resolve.return_value = channel

        store = mock.Mock()
        if names_error:
            store.character_names_by_user_id.side_effect = RuntimeError("sheet down")
        else:
            store.character_names_by_user_id.return_value = {"11": "Kafra"}
        fake_bot = SimpleNamespace(store=store, config=SimpleNamespace(voice_log_channel_id=4))

        log_channel = mock.Mock()
        log_channel.send = mock.AsyncMock(return_value=SimpleNamespace(jump_url="https://d/x"))
        if not post_ok:
            log_channel.send.side_effect = discord.HTTPException(mock.Mock(status=403), "no")

        interaction = mock.Mock()
        interaction.user = SimpleNamespace(id=1, mention="<@1>")
        interaction.response.defer = mock.AsyncMock()
        interaction.edit_original_response = mock.AsyncMock()

        async def go():
            view = botmod.VoiceCountView(fake_bot, 1, "th", None)
            view.picker._values = [picked]
            with mock.patch.object(botmod, "_get_log_channel", mock.AsyncMock(return_value=log_channel)):
                await view._on_pick(interaction)
            return view

        view = asyncio.run(go())
        return view, log_channel, interaction

    def test_success_posts_log_and_links_it(self):
        view, log_channel, interaction = self._run()
        self.assertTrue(view.done)
        embed = log_channel.send.await_args.kwargs["embed"]
        self.assertIn("รวม **2** คน", embed.description)  # music bot excluded
        self.assertIn("<@11> — Kafra", embed.fields[0].value)
        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("**2**", content)
        self.assertIn("https://d/x", content)

    def test_sheet_failure_still_posts_without_names(self):
        _, log_channel, interaction = self._run(names_error=True)
        embed = log_channel.send.await_args.kwargs["embed"]
        self.assertNotIn("Kafra", embed.fields[0].value)
        self.assertIn("https://d/x", interaction.edit_original_response.await_args.kwargs["content"])

    def test_post_failure_tells_requester(self):
        _, _, interaction = self._run(post_ok=False)
        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("VOICE_LOG_CHANNEL_ID", content)


if __name__ == "__main__":
    unittest.main()
