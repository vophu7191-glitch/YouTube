import asyncio
import os
import secrets
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

import discord
from aiohttp import web
from cryptography.fernet import Fernet, InvalidToken
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from motor.motor_asyncio import AsyncIOMotorClient

BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REDIRECT_URI = os.environ["GOOGLE_REDIRECT_URI"]
CHANNEL_ID = int(os.environ["VERIFY_CHANNEL_ID"])
ROLE_ID = int(os.environ["MEMBER_ROLE_ID"])
YOUTUBE_CHANNEL_ID = os.environ["YOUTUBE_CHANNEL_ID"]
GUILD_ID = int(os.environ["GUILD_ID"])
MONGO_URI = os.environ["MONGO_URI"]
DB_NAME = os.getenv("MONGO_DATABASE", "youtube_verification")
ENCRYPTION_KEY = os.environ["TOKEN_ENCRYPTION_KEY"].encode()

VERIFY_SECONDS = 300
RECHECK_SECONDS = 6 * 60 * 60
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
cipher = Fernet(ENCRYPTION_KEY)

mongo = AsyncIOMotorClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=10000,
)
database = mongo[DB_NAME]
members = database["members"]
oauth_states = database["oauth_states"]
tokens = database["tokens"]
bot_ready = asyncio.Event()


async def init_database():
    await mongo.admin.command("ping")
    await members.create_index("user_id", unique=True)
    await members.create_index("joined_at")
    await oauth_states.create_index("state", unique=True)
    await oauth_states.create_index("expires_at", expireAfterSeconds=0)
    await tokens.create_index("user_id", unique=True)


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


async def make_auth_url(user_id):
    state = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    await oauth_states.insert_one({
        "state": state,
        "user_id": str(user_id),
        "created_at": now,
        "expires_at": now + timedelta(seconds=VERIFY_SECONDS),
    })
    flow = oauth_flow(state)
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return url


def has_subscription(credentials):
    response = build(
        "youtube", "v3", credentials=credentials, cache_discovery=False
    ).subscriptions().list(
        part="id",
        mine=True,
        forChannelId=YOUTUBE_CHANNEL_ID,
        maxResults=1,
    ).execute()
    return bool(response.get("items"))


async def check_subscription(credentials):
    return await asyncio.to_thread(has_subscription, credentials)


def fetch_google_credentials(state, code):
    flow = oauth_flow(state)
    flow.fetch_token(code=code)
    return flow.credentials


async def save_token(user_id, refresh_token):
    encrypted = cipher.encrypt(refresh_token.encode())
    await tokens.update_one(
        {"user_id": str(user_id)},
        {"$set": {
            "refresh_token": encrypted,
            "checked_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


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


class VerifyView(discord.ui.View):
    def __init__(self, url):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Xác thực YouTube",
            style=discord.ButtonStyle.link,
            url=url,
        ))


async def send_verification(member):
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(CHANNEL_ID)
    url = await make_auth_url(member.id)
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
    bot_ready.set()
    print(f"Discord connected: {bot.user}")


@bot.event
async def on_disconnect():
    bot_ready.clear()
    print("Discord connection lost; discord.py will reconnect.")


@bot.event
async def on_resumed():
    bot_ready.set()
    print("Discord session resumed.")


@bot.event
async def on_member_join(member):
    if member.guild.id != GUILD_ID or member.bot:
        return
    now = datetime.now(timezone.utc)
    await members.update_one(
        {"user_id": str(member.id)},
        {"$set": {"joined_at": now, "verified": False}},
        upsert=True,
    )
    try:
        await send_verification(member)
    except Exception as exc:
        print(f"Could not send verification: {type(exc).__name__}")


async def timeout_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=VERIFY_SECONDS)
        cursor = members.find({"verified": False, "joined_at": {"$lte": cutoff}})
        async for row in cursor:
            guild = bot.get_guild(GUILD_ID)
            if guild is None:
                continue
            member = guild.get_member(int(row["user_id"]))
            if member is None:
                await members.delete_one({"user_id": row["user_id"]})
                await oauth_states.delete_many({"user_id": row["user_id"]})
                continue
            try:
                await member.kick(reason="Không hoàn tất xác thực YouTube trong 5 phút")
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                continue
            await members.delete_one({"user_id": row["user_id"]})
            await oauth_states.delete_many({"user_id": row["user_id"]})
        await asyncio.sleep(15)


async def subscription_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        async for row in tokens.find({}):
            user_id = row["user_id"]
            try:
                credentials = load_credentials(row["refresh_token"])
                await asyncio.to_thread(credentials.refresh, Request())
                subscribed = await check_subscription(credentials)
                await tokens.update_one(
                    {"user_id": user_id},
                    {"$set": {"checked_at": datetime.now(timezone.utc)}},
                )
                if subscribed:
                    continue

                guild = bot.get_guild(GUILD_ID)
                member = guild.get_member(int(user_id)) if guild else None
                if member:
                    try:
                        await member.kick(reason="Không còn đăng ký kênh YouTube")
                    except discord.NotFound:
                        pass
                    except (discord.Forbidden, discord.HTTPException):
                        continue
                await tokens.delete_one({"user_id": user_id})
                await members.update_one(
                    {"user_id": user_id},
                    {"$set": {"verified": False}},
                )
            except InvalidToken:
                print(f"Stored token cannot be decrypted for user {user_id}; check TOKEN_ENCRYPTION_KEY.")
            except Exception as exc:
                print(f"Subscription check failed for user {user_id}: {type(exc).__name__}")
        await asyncio.sleep(RECHECK_SECONDS)


async def oauth_callback(request):
    state = request.query.get("state", "")
    row = await oauth_states.find_one({"state": state})
    now = datetime.now(timezone.utc)
    if not row or row["expires_at"] <= now:
        return web.Response(text="Liên kết xác thực không hợp lệ hoặc đã hết hạn.", status=400)

    user_id = row["user_id"]
    if request.query.get("error"):
        await oauth_states.delete_one({"state": state})
        return web.Response(text="Bạn đã hủy cấp quyền Google. Hãy quay lại Discord.")

    if not bot_ready.is_set():
        return web.Response(text="Bot đang kết nối lại Discord. Vui lòng thử lại sau.", status=503)

    try:
        credentials = await asyncio.to_thread(
            fetch_google_credentials, state, request.query["code"]
        )
        if not credentials.refresh_token:
            return web.Response(
                text="Google không trả refresh token. Hãy thử lại và cấp quyền.",
                status=400,
            )

        subscribed = await check_subscription(credentials)
        if not subscribed:
            return web.Response(
                text="Chưa tìm thấy đăng ký kênh. Hãy đăng ký kênh rồi mở lại liên kết xác thực trong 5 phút.",
                status=200,
            )

        guild = bot.get_guild(GUILD_ID)
        member = guild.get_member(int(user_id)) if guild else None
        role = guild.get_role(ROLE_ID) if guild else None
        if member is None or role is None:
            await oauth_states.delete_one({"state": state})
            return web.Response(text="Không tìm thấy thành viên hoặc role Discord.", status=404)

        await member.add_roles(role, reason="Đã xác thực đăng ký YouTube")
        await save_token(user_id, credentials.refresh_token)
        await members.update_one(
            {"user_id": user_id},
            {"$set": {"verified": True, "verified_at": now}},
            upsert=True,
        )
        await oauth_states.delete_one({"state": state})
        return web.Response(
            text="Xác thực thành công! Bạn đã được cấp role thành viên. Có thể quay lại Discord.",
            content_type="text/plain",
        )
    except KeyError:
        await oauth_states.delete_one({"state": state})
        return web.Response(text="Google không trả mã xác thực hợp lệ.", status=400)
    except Exception as exc:
        print(f"OAuth verification failed: {type(exc).__name__}")
        return web.Response(
            text="Không thể xác thực lúc này. Liên kết vẫn còn hiệu lực; hãy thử lại.",
            status=500,
        )


async def ping(request):
    return web.Response(text="pong", status=200)


async def health(request):
    try:
        await mongo.admin.command("ping")
    except Exception:
        return web.json_response({"status": "degraded", "database": "unavailable"}, status=503)
    return web.json_response({
        "status": "ok" if bot_ready.is_set() else "degraded",
        "database": "ok",
        "discord": "connected" if bot_ready.is_set() else "disconnected",
    }, status=200 if bot_ready.is_set() else 503)


async def main():
    await init_database()
    app = web.Application()
    app.router.add_get("/ping", ping)
    app.router.add_get("/health", health)
    app.router.add_get("/oauth/callback", oauth_callback)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()

    timeout_task = asyncio.create_task(timeout_loop())
    subscription_task = asyncio.create_task(subscription_loop())
    try:
        await bot.start(BOT_TOKEN, reconnect=True)
    finally:
        timeout_task.cancel()
        subscription_task.cancel()
        with suppress(asyncio.CancelledError):
            await timeout_task
        with suppress(asyncio.CancelledError):
            await subscription_task
        await runner.cleanup()
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
