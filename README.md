# Ragnarok Online Guild Utility Discord Bot

Python Discord bot for one Ragnarok Online guild. It uses slash commands and stores member/absence data in Google Sheets.

## Commands

- `/ลงทะเบียน ชื่อตัวละคร อาชีพ ชื่อเล่น` submits a registration (or an update) for officer approval.
- `/เปลี่ยนชื่อ ชื่อใหม่` changes your registered character name.
- `/เปลี่ยนอาชีพ อาชีพใหม่` changes your registered RO class.
- `/ลา เหตุผล วันเริ่ม วันสิ้นสุด` logs an absence request and posts it to the officer channel.

Each command is also registered under an English name, with English options and replies. The two versions share one implementation, so they behave identically:

- `/register character_name character_class nickname`
- `/change-name new_name`
- `/change-class new_class`
- `/absence reason start_date end_date`

Messages posted to the member-log and officer channels stay in Thai no matter which version a member uses, so those channels read consistently.

## Registration approval

`/register` does not write to the sheet on its own. It queues the request in the `Registrations` tab and posts a card to the dedicated approval channel (`REGISTER_APPROVAL_CHANNEL_ID`) with **อนุมัติ** / **ปฏิเสธ** buttons:

- Approving writes the member to `Members`, syncs their nickname and roles, posts the member log, and DMs them the result in the language they registered in.
- Rejecting records the decision (with an optional reason typed into a popup) and DMs the member. `Members` is left untouched.
- A member may only have one request in flight; using `/register` again while one is pending just repeats the request id.
- The buttons carry the request id in their `custom_id`, so cards posted before a restart keep working.
- Two officers pressing a button at once cannot both win: the first press claims the request, and the second is told it was already decided.

Approvers are the members holding `REGISTER_APPROVER_ROLE_ID`, or — when that is unset — anyone with the Manage Roles permission. Server administrators always qualify.

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
   - `Name Changes`
   - `Registrations`

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
   - `REGISTER_APPROVAL_CHANNEL_ID`
   - `GOOGLE_SHEET_ID`
   - `GOOGLE_SERVICE_ACCOUNT_FILE`

   Optional:

   - `REGISTER_APPROVER_ROLE_ID` — the role allowed to approve or reject. Falls back to the Manage Roles permission.

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
