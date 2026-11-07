# YouTube Discord Verification Bot

See the setup, permissions, OAuth configuration, and operating limitations in the project guide below.

## Features
- Sends a verification link when a member joins.
- 5-minute deadline; members who do not finish are kicked.
- Checks the configured YouTube channel using the member's authorized Google account.
- Grants the configured Discord role after a successful check.
- Rechecks stored authorizations every 6 hours. If a successful API check confirms the subscription is gone, the bot kicks the member.
- Stores Discord IDs and encrypted Google refresh tokens. It does not collect IP addresses or attempt to identify devices.

## Setup
1. Create a Discord bot, enable **Server Members Intent**, and invite it with View Channels, Send Messages, Embed Links, Manage Roles, and Kick Members permissions. Move the bot role above the member role.
2. In Google Cloud, enable **YouTube Data API v3**, configure the OAuth consent screen, and create an OAuth Client ID of type **Web application**.
3. Add the exact redirect URI: `https://YOUR-SERVICE.onrender.com/oauth/callback`. While the OAuth app is in Testing, add intended Google accounts as test users.
4. Configure the variables in `.env.example` in the hosting provider. Set `YOUTUBE_CHANNEL_ID` to the channel ID, not its display name.
5. Generate `TOKEN_ENCRYPTION_KEY` once with:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
6. Install `requirements.txt` and run `python bot.py`.

## Important limitations
- YouTube authorization is account-level. It does not reveal whether the person used a phone, PC, or iPad.
- Unsubscription is not detected instantly. The bot checks every 6 hours; only a successful API response showing no subscription triggers a kick. API errors do not trigger a kick.
- Keep the encryption key stable and private. Losing it makes stored refresh tokens unreadable.
- Configure persistent storage for `data.sqlite3`; an ephemeral host disk can erase verification records and tokens on redeploy.
- Google OAuth/API access may require consent-screen configuration and Google review before use by everyone.
- Never commit real tokens, OAuth secrets, or encryption keys.
