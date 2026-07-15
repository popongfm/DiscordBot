# Ragnarok Online Guild Utility Discord Bot

Python Discord bot for one Ragnarok Online guild. It uses slash commands and stores member/absence data in Google Sheets.

## Commands

- `/ลงทะเบียน ชื่อตัวละคร อาชีพ ชื่อเล่น` registers or updates your main character.
- `/เปลี่ยนชื่อ ชื่อใหม่` changes your registered character name.
- `/เปลี่ยนอาชีพ อาชีพใหม่` changes your registered RO class.
- `/ลา เหตุผล วันเริ่ม วันสิ้นสุด` logs an absence request and posts it to the officer channel.

Dates use `MM/DD` (year is assumed to be the current year). Character names are limited to 32 characters because Discord server nicknames have the same limit.

When a member registers or changes their profile, the bot also tries to sync Discord:

- Character name syncs to the member's server nickname.
- Character class syncs to an existing Discord role with the exact same name as the class, such as `High Wizard` or `Assassin Cross`.
- The bot removes other known RO class roles before assigning the new class role.

## Setup

1. Create and activate a virtual environment.

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Create a Discord application and bot in the Discord Developer Portal.

   Invite it to your guild with the `bot` and `applications.commands` scopes. The bot needs these permissions:

   - Send Messages
   - Embed Links
   - Manage Nicknames
   - Manage Roles

   Put the bot's Discord role above your member roles and above every RO class role it should manage.

3. Create Discord roles for each RO class you want the bot to assign.

   Role names must exactly match the class names shown by autocomplete, for example `Knight`, `High Priest`, `Lord Knight`, and `Assassin Cross`.

4. Create a Google Sheet for the bot.

   The bot will create or repair these tabs automatically:

   - `Members`
   - `Absences`

5. Create a Google Cloud service account, download the JSON credentials file, and share the Google Sheet with the service account email.

6. Create your local environment file.

   ```bash
   cp .env.example .env
   ```

   Fill in:

   - `DISCORD_TOKEN`
   - `DISCORD_GUILD_ID`
   - `ABSENCE_CHANNEL_ID`
   - `MEMBER_LOG_CHANNEL_ID`
   - `GOOGLE_SHEET_ID`
   - `GOOGLE_SERVICE_ACCOUNT_FILE`

7. Run the bot.

   ```bash
   python main.py
   ```

Guild-scoped slash commands are synced when the bot starts, so command changes should appear quickly in that server.

## Local Tests

The pure validation tests do not need Discord or Google credentials.

```bash
python3 -m unittest
```
