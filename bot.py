import asyncio
import os
import secrets
import sqlite3
import time
from contextlib import suppress
from urllib.parse import urlencode

import discord
from aiohttp import web
from cryptography.fernet import Fernet
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REDIRECT_URI = os.environ["GOOGLE_REDIRECT_URI"]
BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
CHANNEL_ID = int(os.environ["VERIFY_CHANNEL_ID"])
ROLE_ID = int(os.environ["MEMBER_ROLE_ID"])
YOUTUBE_CHANNEL_ID = os.environ["YOUTUBE_CHANNEL_ID"]
GUILD_ID = int(os.environ["GUILD_ID"])
ENCRYPTION_KEY = os.environ["TOKEN_ENCRYPTION_KEY"].encode()
DB_PATH = os.getenv("DATABASE_PATH", "data.sqlite3")
VERIFY_SECONDS = 300
RECHECK_SECONDS = 6 * 60 * 60
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

cipher = Fernet(ENCRYPTION_KEY)
db = sqlite3.connect(DB_PATH)
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS members (
  user_id TEXT PRIMARY KEY,
  joined_at INTEGER NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
  user_id TEXT PRIMARY KEY,
  refresh_token BLOB NOT NULL,
  checked_at INTEGER NOT NULL
);
""")
db.commit()


def oauth_flow(state=None):
    return Flow.from_client_config(
        {"web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }},
        scopes=SCOPES,
        state=state,
        redirect_uri=REDIRECT_URI,
    )


def make_auth_url(user_id):
    state = secrets.token_urlsafe(32)
    db.execute("INSERT INTO oauth_states VALUES (?, ?, ?)", (state, str(user_id), int(time.time())))
    db.commit()
    flow = oauth_flow(state)
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return url


def youtube_client(credentials):
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def has_subscription(credentials):
    response = youtube_client(credentials).subscriptions().list(
        part="id",
        mine=True,
        forChannelId=YOUTUBE_CHANNEL_ID,
        maxResults=1,
    ).execute()
    return bool(response.get("items"))


async def check_subscription(credentials):
    return await asyncio.to_thread(has_subscription, credentials)


def save_token(user_id, refresh_token):
    encrypted = cipher.encrypt(refresh_token.encode())
    db.execute(
        "INSERT OR REPLACE INTO tokens(user_id, refresh_token, checked_at) VALUES (?, ?, ?)",
        (str(user_id), encrypted, int(time.time())),
    )
    db.commit()


def load_credentials(encrypted):
    refresh_token = cipher.decrypt(encrypted).decode()
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES,
    )


intents = discord.Intents.default()
intents.members = True
bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)


class VerifyView(discord.ui.View):
    def __init__(self, url):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Xác thực YouTube", style=discord.ButtonStyle.link, url=url))


async def send_verification(member):
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        return
    url = make_auth_url(member.id)
    embed = discord.Embed(
        title="Xác thực đăng ký YouTube",
        description=(
            f"{member.mention}, bạn có **5 phút** để xác thực đăng ký kênh. "
            "Nhấn nút bên dưới và cấp quyền Google để bot kiểm tra trạng thái đăng ký. "
            "Không chia sẻ mật khẩu hoặc mã xác minh Google cho bất kỳ ai."
        ),
        color=0x5865F2,
    )
    await channel.send(embed=embed, view=VerifyView(url))


@bot.event
async def on_ready():
    with suppress(Exception):
        await tree.sync()
    print(f"Ready: {bot.user}")


@bot.event
async def on_member_join(member):
    if member.guild.id != GUILD_ID or member.bot:
        return
    db.execute(
        "INSERT OR REPLACE INTO members(user_id, joined_at, verified) VALUES (?, ?, 0)",
        (str(member.id), int(time.time())),
    )
    db.commit()
    await send_verification(member)


async def timeout_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = int(time.time())
        rows = db.execute(
            "SELECT user_id FROM members WHERE verified=0 AND joined_at <= ?",
            (now - VERIFY_SECONDS,),
        ).fetchall()
        guild = bot.get_guild(GUILD_ID)
        if guild:
            for (user_id,) in rows:
                member = guild.get_member(int(user_id))
                if member:
                    with suppress(discord.Forbidden, discord.NotFound, discord.HTTPException):
                        await member.kick(reason="Không hoàn tất xác thực YouTube trong 5 phút")
                db.execute("DELETE FROM members WHERE user_id=?", (user_id,))
                db.execute("DELETE FROM oauth_states WHERE user_id=?", (user_id,))
                db.commit()
        await asyncio.sleep(15)


async def subscription_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        rows = db.execute("SELECT user_id, refresh_token FROM tokens").fetchall()
        guild = bot.get_guild(GUILD_ID)
        role = guild.get_role(ROLE_ID) if guild else None
        for user_id, encrypted in rows:
            try:
                credentials = load_credentials(encrypted)
                await asyncio.to_thread(credentials.refresh, Request())
                subscribed = await check_subscription(credentials)
                db.execute("UPDATE tokens SET checked_at=? WHERE user_id=?", (int(time.time()), user_id))
                db.commit()
                if not subscribed and guild:
                    member = guild.get_member(int(user_id))
                    if member:
                        with suppress(discord.Forbidden, discord.NotFound, discord.HTTPException):
                            await member.kick(reason="Không còn đăng ký kênh YouTube")
                    db.execute("DELETE FROM tokens WHERE user_id=?", (user_id,))
                    db.execute("UPDATE members SET verified=0 WHERE user_id=?", (user_id,))
                    db.commit()
            except Exception as exc:
                print(f"Subscription check failed for user {user_id}: {type(exc).__name__}")
        await asyncio.sleep(RECHECK_SECONDS)


async def oauth_callback(request):
    error = request.query.get("error")
    state = request.query.get("state", "")
    row = db.execute(
        "SELECT user_id, created_at FROM oauth_states WHERE state=?", (state,)
    ).fetchone()
    if not row or int(time.time()) - row[1] > VERIFY_SECONDS:
        return web.Response(text="Liên kết xác thực không hợp lệ hoặc đã hết hạn.", status=400)
    user_id = row[0]
    db.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    db.commit()
    if error:
        return web.Response(text="Bạn đã từ chối hoặc hủy cấp quyền Google. Hãy quay lại Discord.")
    try:
        flow = oauth_flow(state)
        flow.fetch_token(code=request.query["code"])
        credentials = flow.credentials
        if not credentials.refresh_token:
            return web.Response(text="Google không trả refresh token. Hãy thử lại và chọn cấp quyền.")
        subscribed = await check_subscription(credentials)
        if not subscribed:
            return web.Response(text="Chưa tìm thấy đăng ký kênh. Hãy đăng ký kênh rồi xác thực lại trong thời hạn.")
        save_token(user_id, credentials.refresh_token)
        db.execute("UPDATE members SET verified=1 WHERE user_id=?", (user_id,))
        db.commit()
        guild = bot.get_guild(GUILD_ID)
        member = guild.get_member(int(user_id)) if guild else None
        role = guild.get_role(ROLE_ID) if guild else None
        if member and role:
            await member.add_roles(role, reason="Đã xác thực đăng ký YouTube")
        return web.Response(
            text="Xác thực thành công! Bạn đã được cấp role thành viên. Có thể quay lại Discord.",
            content_type="text/plain",
        )
    except Exception as exc:
        print(f"OAuth verification failed: {type(exc).__name__}")
        return web.Response(text="Không thể xác thực lúc này. Hãy thử lại hoặc liên hệ quản trị viên.", status=500)


async def health(request):
    return web.json_response({"status": "ok"})


async def main():
    app = web.Application()
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()
    asyncio.create_task(timeout_loop())
    asyncio.create_task(subscription_loop())
    await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
