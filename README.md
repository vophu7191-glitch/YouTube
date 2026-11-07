# YouTube Discord Verification Bot

Discord members authorize their Google account through OAuth so the bot can check whether that account subscribes to the configured YouTube channel.

## Features

- Sends a verification link when a member joins.
- Enforces a 5-minute verification window.
- Grants the configured Discord role after a successful subscription check.
- Rechecks saved authorizations every 6 hours. A member is removed only after a successful YouTube API response confirms the subscription is absent.
- Stores member state and encrypted Google refresh tokens in MongoDB Atlas.
- Provides `/ping` for UptimeRobot and `/health` for service diagnostics.
- Does not collect IP addresses or attempt to identify devices.

## Setup

1. Create a Discord bot, enable **Server Members Intent**, and invite it with View Channels, Send Messages, Embed Links, Manage Roles, and Kick Members permissions. Move the bot role above the member role.
2. In Google Cloud, enable **YouTube Data API v3**, configure the OAuth consent screen, and create an OAuth Client ID of type **Web application**.
3. Add the exact redirect URI: `https://YOUR-SERVICE.onrender.com/oauth/callback`. While the OAuth app is in Testing, add intended Google accounts as test users.
4. Create a MongoDB Atlas database and database user. Add the Render service's outbound network access as permitted by your Atlas network access policy. Copy the application connection string into `MONGO_URI`.
5. Set the variables from `.env.example` in Render. Set `YOUTUBE_CHANNEL_ID` to the channel ID, not its display name.
6. Generate `TOKEN_ENCRYPTION_KEY` once with:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
7. Set the Render start command to `python bot.py`.

## Render and UptimeRobot

- Use a Render **Web Service** and keep the service bound to `0.0.0.0` using Render's `PORT`.
- Add an UptimeRobot **HTTP(s)** monitor for `https://YOUR-SERVICE.onrender.com/ping`, with a 5-minute interval.
- `/ping` always returns HTTP 200 for the keep-alive check. Use `/health` to check Discord and MongoDB status; it returns 503 when either is unavailable.
- UptimeRobot can reduce inactivity sleep but cannot guarantee 24/7 uptime or prevent Render maintenance/restarts. A free Render service can still restart, and availability is not guaranteed.

## Data and security

- MongoDB is the source of truth; the bot does not silently fall back to Render's ephemeral filesystem.
- Refresh tokens are encrypted before being stored. Keep `TOKEN_ENCRYPTION_KEY` stable and private. Losing or changing it makes existing tokens unreadable.
- Restrict MongoDB network access and use a dedicated database user with access only to this database.
- Never commit real bot tokens, OAuth secrets, MongoDB credentials, or encryption keys.

## Behavior and limitations

- Google OAuth is account-level. It cannot reliably identify whether someone used a phone, computer, or tablet.
- Unsubscription is not detected instantly; the bot checks every 6 hours. API failures do not trigger removal.
- If a member is not subscribed on the first check, the same authorization link can be retried during the original 5-minute window.
- Google OAuth testing mode may limit which Google accounts can authorize. Public use may require Google's app verification.
- MongoDB Atlas free-tier limits and availability are controlled by MongoDB and may change.
- No automated tests have been run as part of this change yet.
