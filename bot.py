import asyncio
import csv
import hashlib
import io
import logging
import os
import random
import re
import secrets
import string
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("superchat-bot")

# ---------- Cấu hình (biến môi trường) ----------
TOKEN = os.environ["DISCORD_TOKEN"]
YT_KEY = os.environ["YOUTUBE_API_KEY"]
MONGO_URI = os.environ["MONGODB_URI"]
# Tuỳ chọn: kênh YouTube tự theo dõi cho server chính (HOME). Server khác dùng /theodoikenh.
YT_CHANNELS = [c.strip() for c in os.getenv("YT_CHANNEL_ID", "").split(",") if c.strip()]
ANNOUNCE_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))  # tuỳ chọn: kênh báo donate của server chính
# Server chính: nhận dữ liệu cũ (trước khi tách server). Bỏ trống = tự chọn nếu bot chỉ ở đúng 1 server.
HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID", "0"))
# Cần cho danh sách xác minh thủ công. Bật Server Members Intent trong Developer Portal rồi đặt MEMBERS_INTENT=1
MEMBERS_INTENT = os.getenv("MEMBERS_INTENT", "") == "1"
PORT = int(os.getenv("PORT", "0"))  # Render Web Service cần; Worker thì bỏ trống
MESSAGE_CONTENT_INTENT = os.getenv("MESSAGE_CONTENT_INTENT", "") == "1"
YT_DAILY_QUOTA_LIMIT = max(int(os.getenv("YT_DAILY_QUOTA_LIMIT", "10000")), 1)
YT_QUOTA_RESERVE = max(int(os.getenv("YT_QUOTA_RESERVE", "2000")), 0)
YT_QUOTA_COSTS = {"liveChat/messages": 5, "videos": 1, "channels": 1, "search": 100}

# Tỷ giá ước lượng về VND để cộng tổng. Hãy chỉnh lại cho đúng ý bạn.
RATES_VND = {
    "VND": 1, "USD": 25400, "EUR": 27500, "GBP": 32000, "JPY": 170,
    "KRW": 18, "THB": 720, "SGD": 19000, "AUD": 16500, "CAD": 18500,
    "PHP": 440, "MYR": 5400, "IDR": 1.6, "TWD": 800, "HKD": 3250, "CNY": 3500,
    # Các loại tiền Super Chat khác (tỷ giá ước lượng, bạn chỉnh lại cho đúng ý)
    "INR": 300, "BRL": 4600, "MXN": 1300, "RUB": 320, "TRY": 600, "PLN": 6700,
    "SEK": 2650, "NOK": 2500, "DKK": 3700, "CHF": 31000, "NZD": 15200, "ARS": 20,
    "CLP": 27, "COP": 6, "PEN": 7000, "ILS": 7250, "SAR": 6770, "AED": 6920,
    "UAH": 620, "CZK": 1150, "HUF": 70, "RON": 5650, "BGN": 14000, "ZAR": 1400,
    "EGP": 500, "ISK": 190, "PKR": 90, "BDT": 210,
}

# Quà Ngọc (Jewels) tính theo số tiền CHỦ KÊNH NHẬN: 2 Ngọc = 1 Ruby, 100 Ruby = 1 USD
# => 1 Ngọc = 0,005 USD. Muốn tự đặt số VND/Ngọc thì thêm biến JEWEL_RATE_VND.
JEWEL_RATE_VND = float(os.getenv("JEWEL_RATE_VND") or RATES_VND.get("USD", 25400) * 0.005)
RATES_VND["JEWEL"] = JEWEL_RATE_VND  # loại tiền ảo "JEWEL" = Ngọc
CODE_RE = re.compile(r"DC-[A-Z0-9]{6}")
db = AsyncIOMotorClient(MONGO_URI, tz_aware=True)[os.getenv("DB_NAME", "superchat")]
session: aiohttp.ClientSession | None = None
watchers: dict[str, asyncio.Task] = {}  # video_id -> tác vụ đọc live chat (mỗi video chỉ đọc 1 lần)
subs: dict[str, set[int]] = {}  # video_id -> các server Discord đang theo dõi video đó
vinfo: dict[str, dict] = {}  # video_id -> thông tin live (tên, kênh...)
home_gid: int | None = None  # server chính, xác định lúc khởi động
message_delete_tasks: set[asyncio.Task] = set()
ended: set[str] = set()  # video đã live xong, không kiểm tra lại


# ---------- YouTube ----------
class YTError(Exception):
    def __init__(self, status, reason, message):
        super().__init__(f"{status} {reason}: {message}")
        self.status, self.reason = status, reason


def quota_reset_at(now: datetime | None = None) -> datetime:
    """Mốc reset quota YouTube: 00:00 theo giờ Pacific."""
    pacific = ZoneInfo("America/Los_Angeles")
    local_now = (now or datetime.now(timezone.utc)).astimezone(pacific)
    next_midnight = datetime.combine(
        local_now.date() + timedelta(days=1), datetime.min.time(), tzinfo=pacific
    )
    return next_midnight.astimezone(timezone.utc)


async def quota_status() -> dict:
    """Ước tính quota bot đã dùng trong ngày Pacific; không bao gồm ứng dụng khác dùng chung key."""
    day = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    doc = await db.yt_quota.find_one({"_id": day}) or {}
    return {
        "day": day,
        "used": int(doc.get("used", 0)),
        "blocked_until": doc.get("blocked_until"),
    }


async def yt_get(path, **params):
    cost = YT_QUOTA_COSTS.get(path, 1)
    status = await quota_status()
    now = datetime.now(timezone.utc)
    blocked_until = status.get("blocked_until")
    if blocked_until and blocked_until > now:
        raise YTError(403, "quotaExceeded", "Quota đang tạm dừng đến lần reset tiếp theo.")

    is_live_chat = path == "liveChat/messages"
    if status["used"] + cost > YT_DAILY_QUOTA_LIMIT:
        raise YTError(403, "localQuotaLimit", "Ước tính quota của bot đã chạm giới hạn ngày.")
    if not is_live_chat and status["used"] + cost > max(
        YT_DAILY_QUOTA_LIMIT - YT_QUOTA_RESERVE, 0
    ):
        raise YTError(403, "localQuotaReserve", "Bot đang giữ quota dự phòng cho Live Chat.")

    # Ghi nhận trước request để không mất bộ đếm nếu Render khởi động lại giữa chừng.
    await db.yt_quota.update_one(
        {"_id": status["day"]},
        {"$inc": {"used": cost}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    params["key"] = YT_KEY
    async with session.get(f"https://www.googleapis.com/youtube/v3/{path}", params=params) as r:
        data = await r.json()
        if r.status != 200:
            err = data.get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason", "")
            if reason == "quotaExceeded":
                await db.yt_quota.update_one(
                    {"_id": status["day"]},
                    {"$set": {"blocked_until": quota_reset_at(), "blocked_reason": reason}},
                    upsert=True,
                )
            raise YTError(r.status, reason, err.get("message", ""))
        return data


async def fetch_channel_videos(channel_id: str) -> list[dict]:
    """Lấy vài video mới nhất từ RSS của một kênh, không dùng quota YouTube API."""
    try:
        async with session.get(
            "https://www.youtube.com/feeds/videos.xml",
            params={"channel_id": channel_id},
        ) as response:
            if response.status != 200:
                return []
            root = ET.fromstring(await response.text())
    except (aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError):
        log.exception("Không đọc được RSS kênh %s", channel_id)
        return []

    atom = "{http://www.w3.org/2005/Atom}"
    yt = "{http://www.youtube.com/xml/schemas/2015}"
    media = "{http://search.yahoo.com/mrss/}"
    videos = []
    for entry in root.findall(f"{atom}entry")[:5]:
        video_id = entry.findtext(f"{yt}videoId")
        if not video_id:
            continue
        thumbnail = entry.find(f".//{media}thumbnail")
        videos.append({
            "video_id": video_id,
            "title": entry.findtext(f"{atom}title") or "Video mới",
            "channel": entry.findtext(f"{atom}author/{atom}name") or "Kênh YouTube",
            "published": entry.findtext(f"{atom}published"),
            "thumb": thumbnail.get("url") if thumbnail is not None else None,
            "channel_id": channel_id,
        })
    return videos


async def find_live_chats(channels: list[str]) -> dict[str, dict]:
    """Dùng RSS (miễn phí) tìm video mới, rồi videos.list (1 quota) xem video nào đang live."""
    ids = []
    for ch in channels:
        async with session.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": ch}) as r:
            ids += re.findall(r"<yt:videoId>([\w-]{11})</yt:videoId>", await r.text())[:5]
    ids = [i for i in dict.fromkeys(ids) if i not in ended]
    if not ids:
        return {}
    data = await yt_get("videos", part="liveStreamingDetails,snippet", id=",".join(ids))
    out = {}
    for v in data.get("items", []):
        d = v.get("liveStreamingDetails", {})
        if d.get("actualEndTime"):
            ended.add(v["id"])
            saved = await db.live_sessions.find_one({"_id": v["id"], "quota_paused": True})
            if saved:
                ended_at = datetime.fromisoformat(d["actualEndTime"].replace("Z", "+00:00"))
                await db.recovery_jobs.update_one(
                    {"_id": v["id"]},
                    {"$setOnInsert": {
                        "video_id": v["id"], "channel_id": saved.get("info", {}).get("channel_id"),
                        "title": saved.get("info", {}).get("title"),
                        "gap_start": saved.get("last_success_at"), "gap_end": ended_at,
                        "status": "needs_review",
                        "reason": "Live ended while quota recovery was pending",
                        "created_at": datetime.now(timezone.utc),
                    }},
                    upsert=True,
                )
                await db.live_sessions.update_one(
                    {"_id": v["id"]}, {"$set": {"status": "ended", "ended_at": ended_at}}
                )
        if d.get("activeLiveChatId") and d.get("actualStartTime") and not d.get("actualEndTime"):
            out[v["id"]] = video_info(v, d["activeLiveChatId"])
    return out


announced: set[tuple[str, int]] = set()  # (video, server) đã thông báo, tránh thông báo lặp


def video_info(v: dict, chat_id: str) -> dict:
    sn = v.get("snippet", {})
    thumbs = sn.get("thumbnails", {})
    thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url")
    return {
        "chat": chat_id,
        "title": sn.get("title", "Live stream"),
        "channel": sn.get("channelTitle", "Kênh YouTube"),
        "channel_id": sn.get("channelId", ""),
        "thumb": thumb,
    }


async def guild_cfg(gid: int) -> dict:
    """Cấu hình riêng của một server: announce (kênh thông báo), yt (kênh YouTube tự theo dõi)."""
    return await db.guilds.find_one({"_id": gid}) or {}


async def get_announce_channel(gid: int):
    cid = (await guild_cfg(gid)).get("announce") or (ANNOUNCE_ID if gid == home_gid else 0)
    if not cid:
        return None
    ch = bot.get_channel(cid)
    if ch is None:  # chưa có trong cache -> hỏi thẳng Discord
        try:
            ch = await bot.fetch_channel(cid)
        except discord.HTTPException as ex:
            log.warning("Không truy cập được kênh thông báo %s: %s", cid, ex)
            return None
    return ch


async def send_notice(gid: int, embed: discord.Embed, purpose: str = "content", return_message: bool = False) -> str | discord.Message | None:
    """Gửi thông báo video/live hoặc donate vào kênh riêng của từng loại."""
    cfg = await guild_cfg(gid)
    channel_id = cfg.get("donation_announce") if purpose == "donate" else (
        cfg.get("announce") or (ANNOUNCE_ID if gid == home_gid else 0)
    )
    ch = bot.get_channel(channel_id) if channel_id else None
    if channel_id and ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except discord.HTTPException as ex:
            log.warning("Không truy cập được kênh thông báo %s: %s", channel_id, ex)
            ch = None
    if not ch:
        command = "/set-kênh-thông-báo-donate" if purpose == "donate" else "/set-kênh-thông-báo"
        return f"chưa đặt kênh thông báo (dùng {command}) hoặc bot không thấy kênh đó"
    try:
        message = await ch.send(embed=embed)
        return message if return_message else None
    except discord.Forbidden:
        return f"bot thiếu quyền Send Messages / Embed Links trong {ch.mention}"
    except discord.HTTPException as ex:
        return f"lỗi Discord: {ex}"
    return None


async def announce_video(gid: int, video: dict):
    embed = discord.Embed(
        title=f"🎬 {video['title']}"[:256],
        url=f"https://www.youtube.com/watch?v={video['video_id']}",
        description=f"Kênh **{video['channel']}** vừa đăng video mới.",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    if video.get("thumb"):
        embed.set_image(url=video["thumb"])
    embed.set_footer(text="THÔNG BÁO VIDEO MỚI • YouTube")
    err = await send_notice(gid, embed, purpose="content")
    if err:
        log.warning("Không gửi được thông báo video mới (server %s): %s", gid, err)


async def announce_live(vid: str, info: dict, source: str, gid: int, force: bool = False) -> str | None:
    """Thông báo kèm link live và link kênh vào server gid khi bot bắt đầu theo dõi.
    Trả về None nếu đã gửi (hoặc đã báo trước đó), hoặc lý do lỗi."""
    if (vid, gid) in announced and not force:
        return None
    e = discord.Embed(
        title=f"🔴 {info['title']}"[:256],
        url=f"https://www.youtube.com/watch?v={vid}",
        description=(
            f"Kênh [{info['channel']}](https://www.youtube.com/channel/{info['channel_id']}) đang live.\n"
            f"Bot bắt đầu theo dõi donate ({source})."
        ),
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    if info.get("thumb"):
        e.set_image(url=info["thumb"])
    e.set_footer(text="ĐANG THEO DÕI LIVE CHAT • YouTube")
    err = await send_notice(gid, e)
    if err:
        log.warning("Không gửi được thông báo live (server %s): %s", gid, err)
    else:
        announced.add((vid, gid))
    return err


async def ensure_channel(au: dict):
    """Lưu tên, avatar, @handle của người dùng YouTube."""
    cid = au["channelId"]
    old = await db.channels.find_one({"_id": cid})
    handle = old.get("handle") if old else None
    if not handle:
        try:
            data = await yt_get("channels", part="snippet", id=cid)
            if data.get("items"):
                handle = data["items"][0]["snippet"].get("customUrl")
        except YTError as e:
            log.warning("Không lấy được handle %s: %s", cid, e)
    if handle and not handle.startswith("@"):
        handle = "@" + handle
    await db.channels.update_one(
        {"_id": cid},
        {"$set": {"name": au["displayName"], "avatar": au["profileImageUrl"], "handle": handle}},
        upsert=True,
    )


async def is_member(guild: discord.Guild, uid: int) -> bool:
    if guild.get_member(uid):
        return True
    try:
        await guild.fetch_member(uid)
        return True
    except discord.HTTPException:
        return False


async def try_link(code: str, au: dict, gids: list[int]):
    now = datetime.now(timezone.utc)
    doc = await db.link_codes.find_one({"_id": code, "expires": {"$gt": now}})
    if not doc or (doc.get("channel_id") and doc["channel_id"] != au["channelId"]):
        return
    doc = await db.link_codes.find_one_and_delete({
        "_id": code, "discord_id": doc["discord_id"], "expires": {"$gt": now}
    })
    if not doc:
        return
    old = await db.links.find_one({"_id": au["channelId"]})
    if old and int(old.get("discord_id", 0)) != int(doc["discord_id"]):
        log.warning("Bỏ qua liên kết trùng kênh %s; đã thuộc Discord %s", au["channelId"], old["discord_id"])
        return
    await db.links.update_one(
        {"_id": au["channelId"]}, {"$setOnInsert": {"discord_id": doc["discord_id"]}}, upsert=True
    )
    await migrate_discord_only(doc["discord_id"], au["channelId"])
    await ensure_channel(au)
    log.info("Đã liên kết %s ↔ Discord %s", au["channelId"], doc["discord_id"])

    # Báo vào kênh thông báo của các server đang theo dõi live này mà người đó là thành viên
    # (không phụ thuộc việc người dùng có mở DM hay không)
    e = discord.Embed(
        color=discord.Color.green(),
        description=(
            f"✅ <@{doc['discord_id']}> đã liên kết với kênh YouTube "
            f"[{au['displayName']}](https://www.youtube.com/channel/{au['channelId']})."
        ),
    )
    e.set_author(name=au["displayName"], icon_url=au.get("profileImageUrl"))
    e.title = "✅ LIÊN KẾT YOUTUBE THÀNH CÔNG"
    e.timestamp = datetime.now(timezone.utc)
    e.set_footer(text="Xác minh kênh YouTube")
    for gid in gids:
        g = bot.get_guild(gid)
        if not g or not await is_member(g, doc["discord_id"]):
            continue
        err = await send_notice(gid, e)
        if err:
            log.warning("Không báo được liên kết vào server %s: %s", gid, err)

    # Nhắn riêng thêm một tin (nếu người dùng cho phép DM)
    try:
        user = await bot.fetch_user(doc["discord_id"])
        await user.send(f"✅ Đã liên kết với kênh YouTube **{au['displayName']}**.")
    except discord.HTTPException:
        log.info("Không DM được cho %s (có thể đã tắt tin nhắn riêng)", doc["discord_id"])


async def handle_message(item: dict, video_id: str, info: dict):
    sn, au = item["snippet"], item["authorDetails"]
    kind = sn["type"]
    gids = sorted(subs.get(video_id, ()))  # các server đang theo dõi live này

    if kind == "textMessageEvent":
        m = CODE_RE.search(sn.get("textMessageDetails", {}).get("messageText", "").upper())
        if m:
            await try_link(m.group(0), au, gids)
        return

    if kind == "giftEvent":
        await handle_gift(item, video_id, info, gids)
        return

    if kind not in ("superChatEvent", "superStickerEvent"):
        return

    d = sn.get("superChatDetails") or sn.get("superStickerDetails")
    amount = int(d["amountMicros"]) / 1_000_000
    cur = d["currency"]
    if cur not in RATES_VND:
        log.warning("Chưa có tỷ giá cho %s, tính 0 VND", cur)
    vnd = int(amount * RATES_VND.get(cur, 0))

    # Mỗi server theo dõi live này ghi một bản riêng, nên top của từng server độc lập nhau.
    recorded = []
    for gid in gids:
        # khoản cũ (trước khi tách server) đã được chuyển về server chính với _id gốc
        if await db.donations.find_one({"_id": item["id"], "guild_id": gid}, {"_id": 1}):
            continue
        try:
            await db.donations.insert_one({
                "_id": f"{gid}:{item['id']}",  # chống trùng khi bot khởi động lại
                "guild_id": gid,
                "channel_id": au["channelId"],
                "video_id": video_id,
                "stream_channel": info.get("channel_id") or None,  # kênh YouTube đang live
                "amount": amount,
                "currency": cur,
                "vnd": vnd,
                "comment": d.get("userComment", ""),
                "at": datetime.now(timezone.utc),
            })
        except DuplicateKeyError:
            continue
        recorded.append(gid)
    if not recorded:
        return

    await ensure_channel(au)
    log.info("Donate %s %s từ %s (%d server)", amount, cur, au["displayName"], len(recorded))
    for gid in recorded:
        await announce(gid, au, d, vnd, info)


async def handle_gift(item: dict, video_id: str, info: dict, gids: list[int]):
    """Quà Ngọc (Jewels). Cùng một ID tin nhắn được dùng lại khi combo tăng -> cập nhật, không cộng trùng."""
    au = item["authorDetails"]
    sn = item["snippet"]
    # YouTube Data API v3 trả quà Ngọc ở snippet.giftDetails.
    # Giữ fallback cấu trúc cũ để không làm hỏng dữ liệu từ nguồn khác.
    g = sn.get("giftDetails") or (
        (sn.get("giftEventDetails") or {}).get("giftMetadata")
    ) or {}
    combo = max(int(g.get("comboCount") or 1), 1)
    jewels_each = int(g.get("jewelsAmount") or 0)
    if jewels_each <= 0:
        log.warning("giftEvent không có jewelsAmount hợp lệ; fields=%s", sorted(g.keys()))
        return
    if not gids:
        log.warning("Nhận giftEvent nhưng live %s chưa có server đăng ký theo dõi", video_id)
        return
    jewels = jewels_each * combo
    vnd = int(jewels * JEWEL_RATE_VND)
    name = g.get("giftName") or "quà"
    note = f"🎁 {name}" + (f" x{combo}" if combo > 1 else "")
    donation_ids = []
    for gid in gids:
        donation_id = f"{gid}:{item['id']}"
        await db.donations.update_one(
            {"_id": donation_id},
            {
                "$set": {"amount": jewels, "vnd": vnd, "comment": note},
                "$setOnInsert": {
                    "guild_id": gid, "channel_id": au["channelId"], "video_id": video_id,
                    "stream_channel": info.get("channel_id") or None,
                    "currency": "JEWEL", "at": datetime.now(timezone.utc),
                },
            },
            upsert=True,
        )
        donation_ids.append((gid, donation_id))
    if not donation_ids:
        return
    await ensure_channel(au)
    log.info("Quà %s Ngọc từ %s (%d server)", jewels, au["displayName"], len(donation_ids))
    d = {"amountDisplayString": f"{jewels:,} Ngọc", "userComment": note, "currency": "JEWEL"}
    for gid, donation_id in donation_ids:
        await announce(gid, au, d, vnd, info, donation_id=donation_id)


async def announce(gid: int, au: dict, d: dict, vnd: int, stream: dict | None = None, donation_id: str | None = None):
    info = await db.channels.find_one({"_id": au["channelId"]}) or {}
    link = await db.links.find_one({"_id": au["channelId"]})
    who = f"<@{link['discord_id']}>" if link else (info.get("handle") or au["displayName"])
    is_jewel = d.get("currency") == "JEWEL"
    amount = d.get("amountDisplayString") or "Ủng hộ"
    e = discord.Embed(
        title="💎 QUÀ NGỌC MỚI" if is_jewel else "💛 DONATE MỚI",
        url=f"https://www.youtube.com/channel/{au['channelId']}",
        description=f"**{who}** vừa gửi quà ủng hộ!",
        color=discord.Color.purple() if is_jewel else discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    e.set_author(name=info.get("handle") or au["displayName"], icon_url=au.get("profileImageUrl"))
    e.add_field(name="🎁 Số Ngọc" if is_jewel else "💰 Số tiền ủng hộ",
                value=f"**{amount}**", inline=True)
    e.add_field(name="💵 Giá trị quy đổi ước tính",
                value=f"**{vnd:,}₫**", inline=True)
    comment = (d.get("userComment") or "").strip()
    if comment:
        e.add_field(name="💬 Lời nhắn", value=comment[:1024], inline=False)
    if d.get("currency") not in RATES_VND:
        e.add_field(name="⚠️ Tỷ giá", value=f"Chưa có tỷ giá cho {d.get('currency')}; giá trị quy đổi hiện là 0₫.", inline=False)
    footer = "Cảm ơn bạn đã ủng hộ"
    if stream and stream.get("channel"):
        footer += f" • Live: {stream['channel']}"
    e.set_footer(text=footer)
    if donation_id:
        saved = await db.donations.find_one(
            {"_id": donation_id}, {"announce_channel_id": 1, "announce_message_id": 1}
        ) or {}
        channel_id = saved.get("announce_channel_id")
        message_id = saved.get("announce_message_id")
        if channel_id and message_id:
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    channel = None
            if channel:
                try:
                    message = await channel.fetch_message(message_id)
                    await message.edit(embed=e)
                    return
                except discord.NotFound:
                    pass
                except discord.HTTPException as ex:
                    log.warning("Không cập nhật được thông báo quà Ngọc (%s): %s", donation_id, ex)
                    return
        sent = await send_notice(gid, e, purpose="donate", return_message=True)
        if isinstance(sent, discord.Message):
            await db.donations.update_one(
                {"_id": donation_id},
                {"$set": {
                    "announce_channel_id": sent.channel.id,
                    "announce_message_id": sent.id,
                }},
            )
        elif sent:
            log.warning("Không báo được quà Ngọc (server %s): %s", gid, sent)
    else:
        err = await send_notice(gid, e, purpose="donate")
        if err:
            log.warning("Không báo được donate (server %s): %s", gid, err)


async def watch_chat(video_id: str, chat_id: str, info: dict):
    log.info("Bắt đầu theo dõi live %s (%s)", video_id, info.get("channel"))
    if info.get("channel_id"):
        try:
            await db.streamers.update_one(
                {"_id": info["channel_id"]},
                {"$set": {"name": info.get("channel") or info["channel_id"]}},
                upsert=True,
            )
        except Exception:
            log.exception("Không lưu được thông tin kênh")

    saved = await db.live_sessions.find_one({"_id": video_id}) or {}
    token = saved.get("next_page_token")
    last_success_at = saved.get("last_success_at")
    quota_interrupted = bool(saved.get("quota_paused") or saved.get("paused_at"))
    await db.live_sessions.update_one(
        {"_id": video_id},
        {"$set": {
            "chat_id": chat_id, "info": info, "status": "watching",
            "resumed_at": datetime.now(timezone.utc),
        }, "$setOnInsert": {"started_at": datetime.now(timezone.utc)}},
        upsert=True,
    )

    while True:
        try:
            params = {"liveChatId": chat_id, "part": "snippet,authorDetails", "maxResults": 2000}
            if token:
                params["pageToken"] = token
            data = await yt_get("liveChat/messages", **params)
            for item in data.get("items", []):
                await handle_message(item, video_id, info)
            token = data.get("nextPageToken")
            last_success_at = datetime.now(timezone.utc)
            await db.live_sessions.update_one(
                {"_id": video_id},
                {"$set": {
                    "chat_id": chat_id, "info": info, "next_page_token": token,
                    "last_success_at": last_success_at, "status": "watching",
                }},
                upsert=True,
            )
            await asyncio.sleep(max(data.get("pollingIntervalMillis", 10000), 10000) / 1000)
        except YTError as e:
            if e.reason in ("liveChatEnded", "liveChatNotFound"):
                ended_at = datetime.now(timezone.utc)
                log.info("Live %s đã kết thúc", video_id)
                await db.live_sessions.update_one(
                    {"_id": video_id},
                    {"$set": {"status": "ended", "ended_at": ended_at}},
                    upsert=True,
                )
                if quota_interrupted:
                    await db.recovery_jobs.update_one(
                        {"_id": video_id},
                        {"$setOnInsert": {
                            "video_id": video_id, "channel_id": info.get("channel_id"),
                            "title": info.get("title"), "gap_start": last_success_at,
                            "gap_end": ended_at, "status": "needs_review",
                            "reason": "Live Chat ended after quota interruption",
                            "created_at": ended_at,
                        }},
                        upsert=True,
                    )
                return
            if e.reason in ("quotaExceeded", "localQuotaLimit"):
                reset_at = quota_reset_at()
                paused_at = datetime.now(timezone.utc)
                log.error("Quota YouTube đã hết; tạm dừng live %s đến %s", video_id, reset_at.isoformat())
                quota_interrupted = True
                await db.live_sessions.update_one(
                    {"_id": video_id},
                    {"$set": {
                        "status": "quota_paused", "quota_paused": True,
                        "paused_at": paused_at, "quota_reset_at": reset_at,
                        "next_page_token": token, "last_success_at": last_success_at,
                    }},
                    upsert=True,
                )
                await asyncio.sleep(max((reset_at - datetime.now(timezone.utc)).total_seconds(), 60))
            elif e.reason == "rateLimitExceeded":
                log.warning("YouTube giới hạn tốc độ đọc live %s; chờ 30 giây", video_id)
                await asyncio.sleep(30)
            else:
                log.warning("Lỗi YouTube: %s", e)
                await asyncio.sleep(30)
        except Exception:
            log.exception("Lỗi không mong đợi trong watch_chat")
            await asyncio.sleep(30)


async def follow_map() -> dict[str, set[int]]:
    """kênh YouTube -> các server đang tự theo dõi kênh đó (bỏ qua server bot đã rời)."""
    out: dict[str, set[int]] = {}
    async for g in db.guilds.find({"yt": {"$exists": True, "$ne": []}}):
        if not bot.get_guild(g["_id"]):
            continue
        for c in g["yt"]:
            out.setdefault(c, set()).add(g["_id"])
    return out


@tasks.loop(seconds=60)
async def monitor():
    try:
        follow = await follow_map()
        quota = await quota_status()
        reserve_reached = (
            quota["used"] >= max(YT_DAILY_QUOTA_LIMIT - YT_QUOTA_RESERVE, 0)
            or (quota.get("blocked_until") and quota["blocked_until"] > datetime.now(timezone.utc))
        )
        # Khi gần hết quota, bỏ dò live bằng videos.list; RSS vẫn chạy không tốn quota.
        live = await find_live_chats(list(follow)) if follow and not reserve_reached else {}
        if reserve_reached:
            log.warning("Đang giữ quota dự phòng cho Live Chat (%s/%s units)", quota["used"], YT_DAILY_QUOTA_LIMIT)
        feeds = {
            channel_id: await fetch_channel_videos(channel_id)
            for channel_id in follow
        }
    except Exception as e:
        log.warning("Không kiểm tra được live/video: %s", e)
        return
    for vid, info in live.items():
        gids = follow.get(info["channel_id"], set())
        if not gids:
            continue
        new = [g for g in gids if g not in subs.get(vid, set())]
        subs.setdefault(vid, set()).update(gids)
        vinfo[vid] = info
        t = watchers.get(vid)
        task_new = not t or t.done()
        if task_new:
            watchers[vid] = asyncio.create_task(watch_chat(vid, info["chat"], info))
        for gid in (gids if task_new else new):
            await announce_live(vid, info, "tự động", gid)
    live_ids = set(live)
    for channel_id, gids in follow.items():
        entries = feeds.get(channel_id) or []
        if not entries:
            continue
        latest_id = entries[0]["video_id"]
        for gid in gids:
            state_id = f"{gid}:{channel_id}"
            state = await db.feed_state.find_one({"_id": state_id})
            if not state:
                await db.feed_state.update_one(
                    {"_id": state_id},
                    {"$setOnInsert": {"video_id": latest_id}},
                    upsert=True,
                )
                continue
            previous_id = state.get("video_id")
            if previous_id == latest_id:
                continue
            newer = []
            found_previous = False
            for entry in entries:
                if entry["video_id"] == previous_id:
                    found_previous = True
                    break
                newer.append(entry)
            if not found_previous:
                newer = entries[:1]
            await db.feed_state.update_one(
                {"_id": state_id}, {"$set": {"video_id": latest_id}}
            )
            for entry in reversed(newer):
                if entry["video_id"] not in live_ids:
                    await announce_video(gid, entry)

    for vid in [v for v, t in watchers.items() if t.done() and v not in live]:
        watchers.pop(vid)
        subs.pop(vid, None)
        vinfo.pop(vid, None)


async def resolve_youtube(text: str):
    """Nhận link kênh, @handle hoặc ID kênh (UC...), trả về dict giống authorDetails."""
    text = text.strip()
    m = re.search(r"(UC[\w-]{22})", text)
    if m:
        params = {"id": m.group(1)}
    else:
        h = re.search(r"@[\w.\-]+", text)
        if not h:
            return None
        params = {"forHandle": h.group(0)}
    data = await yt_get("channels", part="snippet", **params)
    items = data.get("items", [])
    if not items:
        return None
    it = items[0]
    return {
        "channelId": it["id"],
        "displayName": it["snippet"]["title"],
        "profileImageUrl": it["snippet"]["thumbnails"]["default"]["url"],
    }


async def migrate_discord_only(discord_id: int, channel_id: str):
    """Donate cũ nhập theo Discord (dc:ID) được gộp vào kênh YouTube khi đã liên kết."""
    await db.donations.update_many(
        {"channel_id": f"dc:{discord_id}"}, {"$set": {"channel_id": channel_id}}
    )


def parse_amount(text: str) -> float:
    t = text.strip().replace(" ", "")
    if t.count(".") > 1:  # 1.000.000 -> 1000000
        t = t.replace(".", "")
    t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        raise ValueError("số tiền không hợp lệ")


async def add_manual(youtube, discord_id, amount, cur, note, added_by, batch=None, cache=None,
                     stream_channel=None, guild_id=None):
    """Thêm một khoản donate cũ. Ném ValueError (kèm lý do) nếu dòng không hợp lệ."""
    if amount <= 0 or cur not in RATES_VND:
        raise ValueError("số tiền hoặc loại tiền không hợp lệ")
    if not youtube and not discord_id:
        raise ValueError("thiếu cả youtube lẫn discord_id")

    if youtube:
        au = cache.get(youtube) if cache is not None else None
        if au is None:
            try:
                au = await resolve_youtube(youtube)
            except YTError as e:
                raise ValueError(f"lỗi YouTube: {e}")
            if not au:
                raise ValueError("không tìm thấy kênh YouTube")
            await ensure_channel(au)
            if cache is not None:
                cache[youtube] = au
        cid = au["channelId"]
        if discord_id:
            await db.links.update_one({"_id": cid}, {"$set": {"discord_id": discord_id}}, upsert=True)
            await migrate_discord_only(discord_id, cid)
    else:
        cid = f"dc:{discord_id}"

    did = f"manual-{uuid.uuid4().hex[:8]}"
    await db.donations.insert_one({
        "_id": did, "channel_id": cid, "video_id": None, "amount": amount,
        "currency": cur, "vnd": int(amount * RATES_VND[cur]), "comment": note,
        "at": datetime.now(timezone.utc), "manual": True, "added_by": added_by, "batch": batch,
        "stream_channel": stream_channel, "guild_id": guild_id,
    })
    return did


UNASSIGNED = "none"  # giá trị đại diện cho các khoản donate chưa gắn kênh nào


async def list_streamers(gid: int) -> list[tuple[str, str]]:
    """Danh sách (id kênh, tên) các kênh YouTube của server gid: kênh đã có donate trong server,
    kênh server tự theo dõi, và kênh server đang theo dõi live.
    Thêm mục 'Chưa phân kênh' nếu server còn khoản donate chưa gắn kênh."""
    ids = set(i for i in await db.donations.distinct("stream_channel", {"guild_id": gid}) if i)
    ids |= set((await guild_cfg(gid)).get("yt", []))
    for vid, gs in subs.items():
        if gid in gs and vinfo.get(vid, {}).get("channel_id"):
            ids.add(vinfo[vid]["channel_id"])
    names = {}
    async for d in db.streamers.find({"_id": {"$in": list(ids)}}):
        names[d["_id"]] = d.get("name") or d["_id"]
    out = sorted(((i, names.get(i) or i) for i in ids), key=lambda x: x[1].lower())
    if await db.donations.find_one({"guild_id": gid, "stream_channel": None}, {"_id": 1}):
        out.append((UNASSIGNED, "Chưa phân kênh"))
    return out


async def pick_streamer(gid: int, text: str | None, *, for_view: bool = False, create: bool = False):
    """Chọn kênh (trong server gid) từ chữ người dùng nhập. Trả về ((id, tên), None) hoặc (None, lý do lỗi).
    Bỏ trống: tự chọn nếu chỉ có đúng 1 kênh. create=True cho phép dán link/@handle kênh mới."""
    items = await list_streamers(gid)
    real = [x for x in items if x[0] != UNASSIGNED]
    pool = items if for_view else real
    text = (text or "").strip()
    if not text:
        if len(real) == 1:
            return real[0], None
        if len(pool) == 1:
            return pool[0], None
        if not pool:
            return None, "Chưa có kênh nào. Hãy để bot theo dõi một live trước, hoặc dán link/@handle kênh vào tùy chọn `kenh`."
        return None, "Có nhiều kênh, hãy chọn một kênh ở tùy chọn `kenh`: " + ", ".join(n for _, n in pool)
    for i, n in pool:
        if text == i:
            return (i, n), None
    low = text.lower()
    for i, n in pool:
        if low in n.lower():
            return (i, n), None
    if create and (re.search(r"UC[\w-]{22}", text) or "@" in text):
        try:
            au = await resolve_youtube(text)
        except YTError as e:
            return None, f"Lỗi YouTube: {e}"
        if au:
            await db.streamers.update_one(
                {"_id": au["channelId"]}, {"$set": {"name": au["displayName"]}}, upsert=True
            )
            return (au["channelId"], au["displayName"]), None
    return None, f"Không tìm thấy kênh «{text}»."


async def backfill_stream_channels():
    """Gán kênh cho các donate live cũ (ghi trước khi tách theo kênh) dựa trên video_id."""
    vids = [v for v in await db.donations.distinct("video_id", {"stream_channel": None}) if v]
    done = 0
    for i in range(0, len(vids), 50):
        data = await yt_get("videos", part="snippet", id=",".join(vids[i:i + 50]))
        for v in data.get("items", []):
            sn = v.get("snippet", {})
            cid = sn.get("channelId")
            if not cid:
                continue
            await db.streamers.update_one(
                {"_id": cid}, {"$set": {"name": sn.get("channelTitle") or cid}}, upsert=True
            )
            r = await db.donations.update_many(
                {"video_id": v["id"], "stream_channel": None}, {"$set": {"stream_channel": cid}}
            )
            done += r.modified_count
    if done:
        log.info("Đã gán kênh cho %d khoản donate cũ", done)


async def migrate_legacy():
    """Chuyển dữ liệu cũ (trước khi tách theo server) về server chính.
    Server chính = HOME_GUILD_ID, hoặc server duy nhất mà bot đang ở."""
    global home_gid
    home_gid = HOME_GUILD_ID or (bot.guilds[0].id if len(bot.guilds) == 1 else None)
    old_filter = {"guild_id": {"$exists": False}}
    if not home_gid:
        if await db.donations.find_one(old_filter, {"_id": 1}):
            log.warning(
                "Bot đang ở %d server nên không biết gán donate cũ cho server nào. "
                "Hãy đặt biến môi trường HOME_GUILD_ID = ID server chính rồi khởi động lại.",
                len(bot.guilds),
            )
        return

    r = await db.donations.update_many(old_filter, {"$set": {"guild_id": home_gid}})
    if r.modified_count:
        log.info("Đã chuyển %d khoản donate cũ về server chính %s", r.modified_count, home_gid)

    cfg = await guild_cfg(home_gid)
    updates = {}
    if not cfg.get("announce"):  # kênh thông báo cũ (dùng chung) -> server chính
        old = await db.settings.find_one({"_id": "announce"})
        old_cid = (old or {}).get("channel_id") or ANNOUNCE_ID
        if old_cid:
            updates["announce"] = old_cid
    if "yt" not in cfg and YT_CHANNELS:  # kênh YouTube trong biến môi trường -> server chính (chỉ lần đầu)
        updates["yt"] = YT_CHANNELS
    if updates:
        await db.guilds.update_one({"_id": home_gid}, {"$set": updates}, upsert=True)
        log.info("Đã nạp cấu hình cũ cho server chính: %s", ", ".join(updates))


async def fix_zero_donations():
    """Tính lại các khoản đang bị 0₫ vì lúc ghi chưa có tỷ giá cho loại tiền đó."""
    fixed = 0
    async for d in db.donations.find({"vnd": 0, "amount": {"$gt": 0}}):
        rate = RATES_VND.get(d.get("currency"))
        if rate:
            await db.donations.update_one(
                {"_id": d["_id"]}, {"$set": {"vnd": int(d["amount"] * rate)}}
            )
            fixed += 1
    if fixed:
        log.info("Đã tính lại %d khoản donate bị 0₫", fixed)


# ---------- Discord ----------
class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = MEMBERS_INTENT
        intents.message_content = MESSAGE_CONTENT_INTENT
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        global session
        session = aiohttp.ClientSession()
        await db.link_codes.create_index("expires", expireAfterSeconds=0)
        await fix_zero_donations()
        try:
            await backfill_stream_channels()
        except Exception:
            log.exception("Không gán được kênh cho donate cũ (sẽ thử lại lần khởi động sau)")
        await db.donations.create_index([("guild_id", 1), ("stream_channel", 1)])
        await db.reviews.create_index([("guild_id", 1), ("cycle", 1), ("status", 1)])
        await self.tree.sync()
        self._boot_task = asyncio.create_task(self._after_ready())
        if PORT:
            app = web.Application()
            app.router.add_get("/", lambda r: web.Response(text="ok"))
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", PORT).start()

    async def _after_ready(self):
        """Chờ bot vào xong các server rồi mới chuyển dữ liệu cũ và bật vòng tự theo dõi."""
        await self.wait_until_ready()
        try:
            await migrate_legacy()
        except Exception:
            log.exception("Không chuyển được dữ liệu cũ về server chính")
        monitor.start()


bot = Bot()


YOUTUBE_CHANNEL_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?youtube\.com/(?:channel/UC[\w-]{22}|@[\w.\-]+)",
    re.IGNORECASE,
)


async def publish_verified_member(guild_id: int | None, discord_id: int, au: dict):
    if not guild_id:
        return
    channel_id = (await guild_cfg(guild_id)).get("youtube_verified_channel")
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            log.warning("Không tìm thấy kênh danh sách xác minh %s", channel_id)
            return
    if not isinstance(channel, discord.TextChannel):
        return

    guild = bot.get_guild(guild_id)
    member = guild.get_member(discord_id) if guild else None
    if guild and member is None:
        try:
            member = await guild.fetch_member(discord_id)
        except discord.HTTPException:
            member = None
    if member is None:
        try:
            member = await bot.fetch_user(discord_id)
        except discord.HTTPException:
            log.warning("Không lấy được thông tin Discord %s để đăng danh sách xác minh", discord_id)
            return

    avatar = member.display_avatar.url
    display_name = getattr(member, "display_name", None) or member.name
    youtube_url = f"https://www.youtube.com/channel/{au['channelId']}"
    embed = discord.Embed(
        title="✅ Thành viên đã xác minh YouTube",
        description=(
            f"**Discord:** <@{discord_id}>\n"
            f"**YouTube:** [{au['displayName']}]({youtube_url})\n"
            "**Trạng thái:** Đã xác minh"
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name=display_name, icon_url=avatar)
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.exception("Không đăng được thành viên đã xác minh vào kênh %s", channel_id)


async def complete_youtube_link(discord_id: int, code: str, au: dict) -> tuple[bool, str]:
    """Xác minh mã trong mô tả đúng Channel ID rồi lưu liên kết Discord ↔ YouTube."""
    now = datetime.now(timezone.utc)
    cid = au["channelId"]
    challenge = await db.link_codes.find_one({
        "_id": code,
        "discord_id": discord_id,
        "expires": {"$gt": now},
    })
    if not challenge:
        return False, "Mã xác minh không tồn tại, đã dùng hoặc hết hạn."

    if challenge.get("channel_id") and challenge["channel_id"] != cid:
        return False, "Mã này được tạo cho một kênh YouTube khác."

    data = await yt_get("channels", part="snippet", id=cid)
    item = next((x for x in data.get("items", []) if x.get("id") == cid), None)
    if not item:
        return False, "YouTube không trả về đúng kênh cần xác minh."

    description = item.get("snippet", {}).get("description", "")
    marker = re.search(
        rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])",
        description,
        re.IGNORECASE,
    )
    if not marker:
        return False, (
            f"Chưa thấy mã trong phần Giới thiệu của kênh {au['displayName']}. "
            "Hãy lưu mô tả, đợi khoảng 10–30 giây rồi bấm kiểm tra lại."
        )

    old = await db.links.find_one({"_id": cid})
    if old and int(old.get("discord_id", 0)) != discord_id:
        return False, "Kênh này đã được liên kết với tài khoản Discord khác. Liên kết cũ được giữ nguyên."

    challenge_filter = {
        "_id": code,
        "discord_id": discord_id,
        "expires": {"$gt": now},
    }
    if challenge.get("channel_id"):
        challenge_filter["channel_id"] = cid
    consumed = await db.link_codes.find_one_and_delete(challenge_filter)
    if not consumed:
        return False, "Mã vừa được sử dụng hoặc đã hết hạn. Hãy tạo mã mới bằng lệnh /liên-kết-youtube."

    try:
        await db.links.update_one(
            {"_id": cid},
            {"$setOnInsert": {
                "discord_id": discord_id,
                "verified_at": now,
                "verified_by": "description_code",
            }},
            upsert=True,
        )
    except DuplicateKeyError:
        current = await db.links.find_one({"_id": cid})
        if not current or int(current.get("discord_id", 0)) != discord_id:
            return False, "Kênh vừa được liên kết với tài khoản Discord khác. Liên kết cũ được giữ nguyên."

    await migrate_discord_only(discord_id, cid)
    await ensure_channel(au)
    await publish_verified_member(challenge.get("guild_id"), discord_id, au)
    log.info("Xác minh bằng mã mô tả: %s ↔ Discord %s", cid, discord_id)
    return True, (
        f"✅ Đã xác minh và ghi nhận kênh {au['displayName']}. "
        "Bạn có thể xoá mã khỏi phần Giới thiệu."
    )


async def safe_delete_message(message: discord.Message | None):
    if message is None:
        return
    try:
        await message.delete()
    except discord.HTTPException:
        pass


def schedule_message_delete(message: discord.Message | None, delay: int):
    if message is None:
        return None

    async def delete_later():
        await asyncio.sleep(delay)
        await safe_delete_message(message)

    task = asyncio.create_task(delete_later())
    message_delete_tasks.add(task)
    task.add_done_callback(message_delete_tasks.discard)
    return task


class YouTubeVerifyView(discord.ui.View):
    def __init__(
        self,
        code: str,
        owner_id: int,
        au: dict,
        source_message: discord.Message | None = None,
    ):
        super().__init__(timeout=600)
        self.code = code
        self.owner_id = owner_id
        self.au = au
        self.source_message = source_message
        self.source_delete_task = None
        self.processing = False

    @discord.ui.button(label="Tôi đã cập nhật mô tả", style=discord.ButtonStyle.success)
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Nút xác minh này chỉ dành cho người đã gửi link kênh.", ephemeral=True
            )
            return
        if self.processing:
            await interaction.response.send_message(
                "Yêu cầu xác minh trước vẫn đang xử lý. Vui lòng chờ.", ephemeral=True
            )
            return

        self.processing = True
        button.disabled = True
        await interaction.response.defer(ephemeral=True)
        try:
            success, message = await complete_youtube_link(
                interaction.user.id, self.code, self.au
            )
        except YTError as exc:
            success, message = False, f"Lỗi YouTube API: {exc}"
        except (aiohttp.ClientError, asyncio.TimeoutError):
            success, message = False, "YouTube đang phản hồi chậm. Hãy thử lại sau."
            log.exception("YouTube description verification request failed")
        except Exception:
            success, message = False, "Có lỗi khi xác minh. Vui lòng thử lại sau."
            log.exception("Unexpected YouTube verification error")

        if success:
            if self.source_delete_task and not self.source_delete_task.done():
                self.source_delete_task.cancel()
            await safe_delete_message(self.source_message)
            await safe_delete_message(interaction.message)
        else:
            self.processing = False
            button.disabled = False
            if self.source_message and not self.source_delete_task:
                self.source_delete_task = schedule_message_delete(self.source_message, 60)

        await interaction.followup.send(message, ephemeral=True)
        if not success:
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass


link_message_cooldowns: dict[tuple[int, int], float] = {}
link_message_pending: set[tuple[int, int]] = set()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild or not MESSAGE_CONTENT_INTENT:
        return

    cfg = await guild_cfg(message.guild.id)
    if message.channel.id != cfg.get("youtube_link_channel"):
        return

    match = YOUTUBE_CHANNEL_LINK_RE.search(message.content)
    if not match:
        return

    key = (message.guild.id, message.author.id)
    now = asyncio.get_running_loop().time()
    if key in link_message_pending or now < link_message_cooldowns.get(key, 0):
        return

    link_message_pending.add(key)
    link_message_cooldowns[key] = now + 20
    for old_key, expiry in list(link_message_cooldowns.items()):
        if expiry <= now:
            link_message_cooldowns.pop(old_key, None)

    try:
        try:
            au = await resolve_youtube(match.group(0))
        except YTError:
            return
        if not au:
            return

        cid = au["channelId"]
        old = await db.links.find_one({"_id": cid})
        if old:
            if int(old.get("discord_id", 0)) == message.author.id:
                notice = await message.channel.send(
                    f"{message.author.mention}, kênh {au['displayName']} đã được liên kết với Discord của bạn."
                )
            else:
                notice = await message.channel.send(
                    f"{message.author.mention}, kênh {au['displayName']} đã được liên kết với một tài khoản Discord khác."
                )
            schedule_message_delete(notice, 5)
            return

        code = "YT-" + "".join(
            secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12)
        )
        await db.link_codes.insert_one({
            "_id": code,
            "discord_id": message.author.id,
            "guild_id": message.guild.id,
            "channel_id": cid,
            "expires": datetime.now(timezone.utc) + timedelta(minutes=10),
        })
        view = YouTubeVerifyView(code, message.author.id, au, source_message=message)
        await message.channel.send(
            f"{message.author.mention}, để xác minh kênh {au['displayName']}:\n"
            f"1. Thêm mã **{code}** vào phần Giới thiệu của đúng kênh YouTube.\n"
            "2. Lưu thay đổi trên YouTube.\n"
            "3. Quay lại đây và bấm nút bên dưới. Mã chỉ dùng một lần và hết hạn sau 10 phút.",
            view=view,
        )
    finally:
        link_message_pending.discard(key)


@bot.tree.command(name="set-kênh-nhận-link", description="Chọn kênh duy nhất để Jion tự nhận diện link YouTube (admin)")
@app_commands.describe(kenh="Kênh văn bản nhận link YouTube")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def setkenhnhanlink_cmd(inter: discord.Interaction, kenh: discord.TextChannel):
    if not inter.user.guild_permissions.manage_guild:
        await inter.response.send_message("Bạn cần quyền Quản lý máy chủ để dùng lệnh này.", ephemeral=True)
        return
    me = inter.guild.me if inter.guild else None
    perms = kenh.permissions_for(me) if me else None
    if not perms or not perms.view_channel or not perms.send_messages or not perms.read_message_history:
        await inter.response.send_message(
            "Jion cần quyền Xem kênh, Gửi tin nhắn và Đọc lịch sử tin nhắn trong kênh đó.",
            ephemeral=True,
        )
        return
    await db.guilds.update_one(
        {"_id": inter.guild_id},
        {"$set": {"youtube_link_channel": kenh.id}},
        upsert=True,
    )
    await inter.response.send_message(
        f"Đã đặt {kenh.mention} làm kênh duy nhất để Jion tự nhận diện link YouTube trong server này.",
        ephemeral=True,
    )


@bot.tree.command(name="set-kênh-xác-minh", description="Chọn kênh công khai danh sách thành viên đã xác minh YouTube (admin)")
@app_commands.describe(kenh="Kênh văn bản công khai danh sách xác minh")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def setkenhxacminh_cmd(inter: discord.Interaction, kenh: discord.TextChannel):
    if not inter.user.guild_permissions.manage_guild:
        await inter.response.send_message("Bạn cần quyền Quản lý máy chủ để dùng lệnh này.", ephemeral=True)
        return
    me = inter.guild.get_member(bot.user.id) if inter.guild and bot.user else None
    perms = kenh.permissions_for(me) if me else None
    if not perms or not perms.view_channel or not perms.send_messages or not perms.embed_links:
        await inter.response.send_message(
            "Bot cần quyền Xem kênh, Gửi tin nhắn và Nhúng liên kết trong kênh đó.",
            ephemeral=True,
        )
        return
    await db.guilds.update_one(
        {"_id": inter.guild_id},
        {"$set": {"youtube_verified_channel": kenh.id}},
        upsert=True,
    )
    await inter.response.send_message(
        f"Đã đặt {kenh.mention} làm kênh công khai danh sách thành viên đã xác minh YouTube.",
        ephemeral=True,
    )


@bot.tree.command(name="xem-kênh-xác-minh", description="Xem kênh công khai danh sách xác minh YouTube")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def xemkenhxacminh_cmd(inter: discord.Interaction):
    if not inter.user.guild_permissions.manage_guild:
        await inter.response.send_message("Bạn cần quyền Quản lý máy chủ để dùng lệnh này.", ephemeral=True)
        return
    cid = (await guild_cfg(inter.guild_id)).get("youtube_verified_channel")
    channel = inter.guild.get_channel(cid) if cid else None
    msg = (
        f"Kênh danh sách xác minh YouTube hiện tại: {channel.mention}."
        if channel else "Server chưa đặt kênh công khai danh sách xác minh YouTube."
    )
    await inter.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="xem-kênh-nhận-link", description="Xem kênh đang nhận diện link YouTube tự động")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def xemkenhnhanlink_cmd(inter: discord.Interaction):
    if not inter.user.guild_permissions.manage_guild:
        await inter.response.send_message("Bạn cần quyền Quản lý máy chủ để dùng lệnh này.", ephemeral=True)
        return
    cid = (await guild_cfg(inter.guild_id)).get("youtube_link_channel")
    channel = inter.guild.get_channel(cid) if cid else None
    if channel:
        msg = f"Jion đang tự nhận diện link YouTube tại {channel.mention}."
    else:
        msg = "Server chưa đặt kênh nhận diện link. Jion hiện không tự động xử lý link trong chat."
    await inter.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="hủy-kênh-nhận-link", description="Tắt nhận diện link YouTube tự động trong server này (admin)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def huykenhnhanlink_cmd(inter: discord.Interaction):
    if not inter.user.guild_permissions.manage_guild:
        await inter.response.send_message("Bạn cần quyền Quản lý máy chủ để dùng lệnh này.", ephemeral=True)
        return
    result = await db.guilds.update_one(
        {"_id": inter.guild_id},
        {"$unset": {"youtube_link_channel": ""}},
    )
    if result.modified_count:
        msg = "Đã tắt nhận diện link YouTube tự động. Các lệnh xác minh thủ công vẫn dùng được."
    else:
        msg = "Server chưa cấu hình kênh nhận diện link."
    await inter.response.send_message(msg, ephemeral=True)


@bot.tree.error
async def on_app_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    """Lỗi lệnh sẽ được ghi vào log và báo lại ngay trong Discord (chỉ người gõ lệnh thấy)."""
    err = getattr(error, "original", error)
    log.error("Lỗi lệnh /%s", inter.command.name if inter.command else "?", exc_info=err)
    text = f"⚠️ Lệnh gặp lỗi: {type(err).__name__}: {err}"[:1900]
    try:
        if inter.response.is_done():
            await inter.followup.send(text, ephemeral=True)
        else:
            await inter.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.tree.command(name="liên-kết-youtube", description="Tạo mã xác minh cho kênh YouTube của bạn")
@app_commands.describe(kenh="Link kênh, @handle hoặc ID kênh YouTube của bạn")
@app_commands.checks.cooldown(1, 20.0)
async def link_cmd(inter: discord.Interaction, kenh: str):
    await inter.response.defer(ephemeral=True)
    try:
        au = await resolve_youtube(kenh)
        if not au:
            await inter.followup.send("Không tìm thấy kênh YouTube này.")
            return
    except YTError as exc:
        await inter.followup.send(f"Lỗi YouTube API: {exc}")
        return

    cid = au["channelId"]
    old = await db.links.find_one({"_id": cid})
    if old:
        if int(old.get("discord_id", 0)) == inter.user.id:
            await inter.followup.send("Kênh này đã được liên kết với Discord của bạn.")
        else:
            await inter.followup.send("Kênh này đã được liên kết với một tài khoản Discord khác.")
        return

    code = "YT-" + "".join(
        secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12)
    )
    await db.link_codes.insert_one({
        "_id": code,
        "discord_id": inter.user.id,
        "guild_id": inter.guild_id,
        "channel_id": cid,
        "expires": datetime.now(timezone.utc) + timedelta(minutes=10),
    })
    await inter.followup.send(
        f"Thêm mã **{code}** vào phần Giới thiệu của kênh **{au['displayName']}**, lưu lại rồi bấm nút bên dưới. "
        "Mã chỉ dùng một lần và hết hạn sau 10 phút.",
        view=YouTubeVerifyView(code, inter.user.id, au),
    )


@bot.tree.command(name="xác-minh-youtube", description="Xác minh kênh YouTube bằng mã trong phần Giới thiệu kênh")
@app_commands.describe(kenh="Link kênh, @handle hoặc ID kênh YouTube của bạn")
@app_commands.checks.cooldown(1, 20.0)
async def xacminh_cmd(inter: discord.Interaction, kenh: str):
    await inter.response.defer(ephemeral=True)
    try:
        au = await resolve_youtube(kenh)
        if not au:
            await inter.followup.send("Không tìm thấy kênh YouTube này.")
            return
        now = datetime.now(timezone.utc)
        codes = [
            item["_id"] async for item in db.link_codes.find({
                "discord_id": inter.user.id,
                "expires": {"$gt": now},
                "$or": [{"channel_id": au["channelId"]}, {"channel_id": {"$exists": False}}],
            })
        ]
        if not codes:
            await inter.followup.send(
                "Bạn chưa có mã phù hợp hoặc mã đã hết hạn. Gửi link kênh vào server hoặc gõ /liên-kết-youtube để lấy mã mới."
            )
            return
        success, result = await complete_youtube_link(inter.user.id, codes[0], au)
        await inter.followup.send(result)
    except YTError as exc:
        await inter.followup.send(f"Lỗi YouTube API: {exc}")


# ---------- Xác minh thủ công (admin): danh sách chưa liên kết / đã xác minh ----------
MEMBERS_HELP = (
    "Tính năng này cần **Server Members Intent**: bật trong Discord Developer Portal (mục Bot), "
    "rồi thêm biến `MEMBERS_INTENT=1` trên Render và deploy lại."
)
NO_PING = discord.AllowedMentions.none()


async def unlinked_members(guild: discord.Guild, query: str = "") -> list[int]:
    linked = {d["discord_id"] async for d in db.links.find({}, {"discord_id": 1})}
    q = query.strip().lower()
    out = [
        m for m in guild.members
        if not m.bot and m.id not in linked
        and (not q or q in m.display_name.lower() or q in m.name.lower())
    ]
    out.sort(key=lambda m: m.display_name.lower())
    return [m.id for m in out]


async def link_member(gid: int, uid: int, text: str, by: int):
    """Admin xác minh thủ công: gắn kênh YouTube `text` cho thành viên Discord `uid`.
    Trả về (au, None) nếu thành công, hoặc (None, lý do lỗi)."""
    try:
        au = await resolve_youtube(text)
    except YTError as e:
        return None, f"Lỗi YouTube: {e}"
    if not au:
        return None, "Không tìm thấy kênh YouTube này."
    cid = au["channelId"]
    old = await db.links.find_one({"_id": cid})
    if old and old["discord_id"] != uid:
        return None, f"Kênh này đang liên kết với <@{old['discord_id']}>. Dùng `/huylienket` để huỷ liên kết cũ trước."
    now = datetime.now(timezone.utc)
    await db.links.update_one(
        {"_id": cid},
        {"$set": {"discord_id": uid, "verified_by": by, "guild_id": gid, "verified_at": now}},
        upsert=True,
    )
    await migrate_discord_only(uid, cid)
    await ensure_channel(au)
    # nếu người này từng nằm trong danh sách kiểm ngẫu nhiên thì ghi nhận là đã liên kết
    await db.reviews.update_one(
        {"_id": f"{gid}:{uid}", "status": {"$ne": "linked"}},
        {"$set": {"status": "linked", "reason": None, "yt": cid, "by": by, "at": now}},
    )
    return au, None


# ---------- Kiểm tra liên kết ngẫu nhiên (/randomlienket) ----------
REVIEW_BATCH = 10  # mỗi lượt bốc bao nhiêu người
review_locks: dict[int, asyncio.Lock] = {}


async def review_draw(guild: discord.Guild) -> dict:
    """Bốc tối đa REVIEW_BATCH thành viên chưa liên kết, KHÔNG trùng người đã kiểm trong vòng hiện tại.
    - Còn người bốc dở (chưa đánh dấu) -> đưa lại đúng những người đó, chưa bốc người mới.
    - Hết người trong vòng -> sang vòng mới (khi đó mới được kiểm lại những người đã kiểm).
    reviews: _id = "guild:user"; status pending | unlinked (kèm reason) | linked; cycle = số vòng."""
    gid = guild.id
    async with review_locks.setdefault(gid, asyncio.Lock()):
        cyc = int((await guild_cfg(gid)).get("review_cycle") or 1)
        members = {m.id for m in guild.members if not m.bot}
        linked = {d["discord_id"] async for d in db.links.find({}, {"discord_id": 1})}
        pool = members - linked  # những người cần kiểm

        reviewed: set[int] = set()  # đã đánh dấu chưa liên kết trong vòng này
        touched: set[int] = set()   # đã đụng tới trong vòng này (kể cả đang chờ)
        pending: list[dict] = []
        async for r in db.reviews.find({"guild_id": gid, "cycle": cyc}, {"user_id": 1, "status": 1, "prev": 1}):
            uid = r["user_id"]
            if r["status"] == "pending":
                if uid not in members:  # đã rời server
                    await db.reviews.delete_one({"_id": r["_id"]})
                    continue
                if uid in linked:  # được liên kết ở chỗ khác trong lúc chờ
                    await db.reviews.update_one(
                        {"_id": r["_id"]}, {"$set": {"status": "linked", "reason": None, "at": datetime.now(timezone.utc)}}
                    )
                    continue
                pending.append(r)
            elif r["status"] == "unlinked" and uid in members:
                reviewed.add(uid)
            touched.add(uid)
        candidates = sorted(pool - touched)

        res = {"cycle": cyc, "new_cycle": False, "resumed": False, "prevs": {}, "ids": [], "all_linked": False}
        if pending:
            res["resumed"] = True
            npend = len(pending)
            pending = pending[:REVIEW_BATCH]
            res["ids"] = [r["user_id"] for r in pending]
            res["prevs"] = {r["user_id"]: r["prev"] for r in pending if r.get("prev")}
            res["done"] = len(reviewed)
            res["total"] = len(reviewed) + npend + len(candidates)
            return res

        if not candidates:
            if not pool:  # ai cũng liên kết rồi
                res["all_linked"] = True
                return res
            cyc += 1  # kiểm hết một vòng -> sang vòng mới, lúc này mới được kiểm lại
            await db.guilds.update_one({"_id": gid}, {"$set": {"review_cycle": cyc}}, upsert=True)
            candidates, reviewed = sorted(pool), set()
            res.update(cycle=cyc, new_cycle=True)

        ids = random.sample(candidates, min(REVIEW_BATCH, len(candidates)))
        olds = {
            o["user_id"]: o
            async for o in db.reviews.find({"_id": {"$in": [f"{gid}:{u}" for u in ids]}})
        }
        now = datetime.now(timezone.utc)
        for uid in ids:
            doc = {"_id": f"{gid}:{uid}", "guild_id": gid, "user_id": uid, "cycle": cyc,
                   "status": "pending", "reason": None, "by": None, "at": now}
            o = olds.get(uid)
            if o and o.get("status") in ("unlinked", "linked") and o.get("cycle") != cyc:
                doc["prev"] = {"cycle": o["cycle"], "status": o["status"], "reason": o.get("reason")}
                res["prevs"][uid] = doc["prev"]
            await db.reviews.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        res["ids"] = ids
        res["done"] = len(reviewed)
        res["total"] = len(reviewed) + len(candidates)
        return res

async def review_mark_unlinked(gid: int, uid: int, cycle: int, by: int, reason: str) -> bool:
    """Đánh dấu 'chưa liên kết' kèm lý do. False nếu người này không còn ở trạng thái chờ (đã có người khác xử lý)."""
    r = await db.reviews.update_one(
        {"_id": f"{gid}:{uid}", "status": "pending", "cycle": cycle},
        {"$set": {"status": "unlinked", "reason": reason, "by": by, "at": datetime.now(timezone.utc)}},
    )
    return r.modified_count == 1


class LinkModal(discord.ui.Modal, title="Liên kết YouTube"):
    kenh = discord.ui.TextInput(label="Kênh YouTube (@handle, link hoặc ID)", placeholder="@tenkenh", max_length=200)

    def __init__(self, owner):  # owner: UnlinkedView hoặc RandomReviewView (cần có apply_link)
        super().__init__()
        self.owner = owner

    async def on_submit(self, inter: discord.Interaction):
        await self.owner.apply_link(inter, str(self.kenh))


class UnlinkedView(discord.ui.View):
    """Hiện từng thành viên chưa liên kết. Người đã xác minh sẽ biến mất khỏi danh sách."""

    def __init__(self, guild: discord.Guild, ids: list[int], owner_id: int, marks: dict | None = None):
        super().__init__(timeout=900)
        self.guild, self.ids, self.owner_id, self.i = guild, ids, owner_id, 0
        self.marks = marks or {}  # uid -> bản ghi kiểm ngẫu nhiên (đã đánh dấu chưa liên kết + lý do)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message("Bảng này của người đã gõ lệnh.", ephemeral=True)
            return False
        return True

    def embed(self) -> discord.Embed:
        if not self.ids:
            return discord.Embed(description="🎉 Không còn ai chưa liên kết trong danh sách.", color=discord.Color.green())
        uid = self.ids[self.i]
        text = (
            f"<@{uid}> : **chưa liên kết**\nID: `{uid}`\n\n"
            "Bấm vào tên để xem profile, mục **Kết Nối** có YouTube không. "
            "Nếu có, bấm **Liên kết** rồi nhập @handle."
        )
        mk = self.marks.get(uid)
        if mk:
            text += f"\n\n📝 Đã kiểm (vòng {mk.get('cycle', '?')}): chưa liên kết — {str(mk.get('reason') or '—')[:300]}"
        e = discord.Embed(color=discord.Color.orange(), description=text)
        m = self.guild.get_member(uid)
        if m:
            e.set_author(name=m.display_name, icon_url=m.display_avatar.url)
            e.set_thumbnail(url=m.display_avatar.url)
        e.set_footer(text=f"{self.i + 1}/{len(self.ids)} người chưa liên kết")
        return e

    @discord.ui.button(label="◀ Trước", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        if self.ids:
            self.i = (self.i - 1) % len(self.ids)
        await inter.response.edit_message(content=None, embed=self.embed(), view=self)

    @discord.ui.button(label="🔗 Liên kết", style=discord.ButtonStyle.success)
    async def link_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        if not self.ids:
            await inter.response.defer()
            return
        await inter.response.send_modal(LinkModal(self))

    @discord.ui.button(label="Bỏ qua ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        if self.ids:
            self.i = (self.i + 1) % len(self.ids)
        await inter.response.edit_message(content=None, embed=self.embed(), view=self)

    async def apply_link(self, inter: discord.Interaction, text: str):
        await inter.response.defer()
        if not self.ids:
            return
        uid = self.ids[self.i]
        au, err = await link_member(self.guild.id, uid, text, inter.user.id)
        if err:
            await inter.followup.send(err, ephemeral=True, allowed_mentions=NO_PING)
            return
        info = await db.channels.find_one({"_id": au["channelId"]}) or {}
        self.ids.pop(self.i)
        if self.i >= len(self.ids):
            self.i = 0
        await inter.edit_original_response(
            content=f"✅ <@{uid}> ↔ {info.get('handle') or au['displayName']}",
            embed=self.embed(), view=self, allowed_mentions=NO_PING,
        )


class VerifiedView(discord.ui.View):
    PER = 10

    def __init__(self, rows: list[tuple], owner_id: int):
        super().__init__(timeout=900)
        self.rows, self.owner_id, self.page = rows, owner_id, 0
        self.pages = max(1, -(-len(rows) // self.PER))

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message("Bảng này của người đã gõ lệnh.", ephemeral=True)
            return False
        return True

    def embed(self) -> discord.Embed:
        chunk = self.rows[self.page * self.PER:(self.page + 1) * self.PER]
        lines = [
            f"{self.page * self.PER + n}. <@{uid}> ↔ [{handle}](https://www.youtube.com/channel/{cid})"
            for n, (uid, cid, handle) in enumerate(chunk, 1)
        ]
        e = discord.Embed(title="✅ Đã xác minh liên kết", description="\n".join(lines) or "Chưa có ai.",
                          color=discord.Color.green())
        e.set_footer(text=f"Trang {self.page + 1}/{self.pages} • {len(self.rows)} người")
        return e

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        self.page = (self.page - 1) % self.pages
        await inter.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        self.page = (self.page + 1) % self.pages
        await inter.response.edit_message(embed=self.embed(), view=self)


@bot.tree.command(name="danh-sách-liên-kết", description="Xem từng thành viên chưa liên kết YouTube và xác minh thủ công (admin)")
@app_commands.describe(tim="Lọc theo tên (tuỳ chọn)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def dslk_cmd(inter: discord.Interaction, tim: str = ""):
    if not MEMBERS_INTENT:
        await inter.response.send_message(MEMBERS_HELP, ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    if not inter.guild.chunked:
        await inter.guild.chunk()
    marks = {
        r["user_id"]: r
        async for r in db.reviews.find({"guild_id": inter.guild.id, "status": "unlinked"})
    }
    view = UnlinkedView(inter.guild, await unlinked_members(inter.guild, tim), inter.user.id, marks)
    await inter.followup.send(embed=view.embed(), view=view, allowed_mentions=NO_PING)


class ReasonModal(discord.ui.Modal, title="Chưa liên kết YouTube"):
    ly_do = discord.ui.TextInput(
        label="Lý do (bắt buộc)", style=discord.TextStyle.paragraph, min_length=3, max_length=300,
        placeholder="VD: Mục Kết Nối trống / chỉ có Twitch / profile để ẩn",
    )

    def __init__(self, owner: "RandomReviewView"):
        super().__init__()
        self.owner = owner

    async def on_submit(self, inter: discord.Interaction):
        await self.owner.mark_unlinked(inter, str(self.ly_do))


class RandomReviewView(discord.ui.View):
    """Một lượt kiểm ngẫu nhiên: xem từng người, có YouTube thì liên kết, không có thì ghi lý do."""

    def __init__(self, guild: discord.Guild, batch: dict, owner_id: int):
        super().__init__(timeout=900)
        self.guild, self.owner_id, self.i = guild, owner_id, 0
        self.ids = list(batch["ids"])
        self.cycle, self.done, self.total = batch["cycle"], batch["done"], batch["total"]
        self.prevs = batch.get("prevs", {})

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message("Bảng này của người đã gõ lệnh.", ephemeral=True)
            return False
        return True

    def embed(self) -> discord.Embed:
        outside = self.total - self.done - len(self.ids)  # còn lại ngoài lượt này
        if not self.ids:
            if outside <= 0:
                text = (f"🎉 **Xong vòng {self.cycle}**: đã kiểm hết mọi thành viên chưa liên kết.\n"
                        f"Gõ `/randomlienket` lần nữa để bắt đầu **vòng {self.cycle + 1}**.")
            else:
                text = "✅ **Xong lượt này.** Gõ `/randomlienket` để bốc 10 người tiếp theo."
            e = discord.Embed(description=text, color=discord.Color.green())
            e.set_footer(text=f"Vòng {self.cycle} • đã kiểm {self.done}/{self.total}")
            return e
        uid = self.ids[self.i]
        text = (
            f"<@{uid}>\nID: `{uid}`\n\n"
            "Bấm vào tên để xem profile, mục **Kết Nối** có YouTube không.\n"
            "• Có → **✅ Có YouTube** rồi nhập @handle\n"
            "• Không → **❌ Chưa liên kết** rồi nhập lý do (bắt buộc)"
        )
        pv = self.prevs.get(uid)
        if pv:
            kq = "đã liên kết" if pv.get("status") == "linked" else f"chưa liên kết — {str(pv.get('reason') or '—')[:200]}"
            text += f"\n\n📝 Vòng {pv.get('cycle', '?')} trước: {kq}"
        e = discord.Embed(color=discord.Color.blurple(), description=text)
        m = self.guild.get_member(uid)
        if m:
            e.set_author(name=m.display_name, icon_url=m.display_avatar.url)
            e.set_thumbnail(url=m.display_avatar.url)
        e.set_footer(
            text=f"Vòng {self.cycle} • người {self.i + 1}/{len(self.ids)} trong lượt • "
                 f"đã kiểm {self.done}/{self.total} • còn {outside} người ngoài lượt này"
        )
        return e

    async def _advance(self, inter: discord.Interaction, content: str | None):
        """Bỏ người vừa xử lý ra khỏi lượt rồi cập nhật bảng."""
        self.ids.pop(self.i)
        if self.i >= len(self.ids):
            self.i = 0
        if not self.ids:
            for c in self.children:
                c.disabled = True
        await inter.edit_original_response(content=content, embed=self.embed(), view=self, allowed_mentions=NO_PING)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        if self.ids:
            self.i = (self.i - 1) % len(self.ids)
        await inter.response.edit_message(content=None, embed=self.embed(), view=self)

    @discord.ui.button(label="✅ Có YouTube", style=discord.ButtonStyle.success)
    async def yes_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        if not self.ids:
            await inter.response.defer()
            return
        await inter.response.send_modal(LinkModal(self))

    @discord.ui.button(label="❌ Chưa liên kết", style=discord.ButtonStyle.danger)
    async def no_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        if not self.ids:
            await inter.response.defer()
            return
        await inter.response.send_modal(ReasonModal(self))

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        if self.ids:
            self.i = (self.i + 1) % len(self.ids)
        await inter.response.edit_message(content=None, embed=self.embed(), view=self)

    async def apply_link(self, inter: discord.Interaction, text: str):
        await inter.response.defer()
        if not self.ids:
            return
        uid = self.ids[self.i]
        au, err = await link_member(self.guild.id, uid, text, inter.user.id)
        if err:
            await inter.followup.send(err, ephemeral=True, allowed_mentions=NO_PING)
            return
        info = await db.channels.find_one({"_id": au["channelId"]}) or {}
        self.total -= 1  # người đã liên kết thì không cần kiểm nữa
        await self._advance(inter, f"✅ <@{uid}> ↔ {info.get('handle') or au['displayName']}")

    async def mark_unlinked(self, inter: discord.Interaction, reason: str):
        reason = " ".join(reason.split())
        if len(reason) < 3:  # chặn cả lý do chỉ toàn dấu cách
            await inter.response.send_message("❌ Phải nhập lý do (ít nhất 3 ký tự).", ephemeral=True)
            return
        await inter.response.defer()
        if not self.ids:
            return
        uid = self.ids[self.i]
        ok = await review_mark_unlinked(self.guild.id, uid, self.cycle, inter.user.id, reason)
        if ok:
            self.done += 1
            note = f"📝 <@{uid}> : chưa liên kết — {reason[:100]}"
        else:  # đã có người khác xử lý người này (hoặc vừa được liên kết)
            self.total -= 1
            note = f"⚠️ <@{uid}> đã được xử lý ở nơi khác, bỏ qua."
        await self._advance(inter, note)


@bot.tree.command(name="chọn-ngẫu-nhiên-liên-kết", description="Bốc ngẫu nhiên 10 thành viên để kiểm tra liên kết YouTube (admin)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def randomlienket_cmd(inter: discord.Interaction):
    if not MEMBERS_INTENT:
        await inter.response.send_message(MEMBERS_HELP, ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    if not inter.guild.chunked:
        await inter.guild.chunk()
    batch = await review_draw(inter.guild)
    if not batch["ids"]:
        await inter.followup.send(
            "🎉 Mọi thành viên (không tính bot) đều đã liên kết YouTube, không còn ai để kiểm.", ephemeral=True
        )
        return
    view = RandomReviewView(inter.guild, batch, inter.user.id)
    n = len(batch["ids"])
    if batch["new_cycle"]:
        banner = f"🔄 **Bắt đầu vòng {batch['cycle']}**: đã kiểm hết vòng trước, được kiểm lại từ đầu. Bốc {n} người."
    elif batch["resumed"]:
        banner = f"↩️ Lượt trước còn dang dở, tiếp tục {n} người này (chưa bốc người mới)."
    else:
        banner = f"🎲 Vòng {batch['cycle']}: bốc ngẫu nhiên {n} người."
    await inter.followup.send(content=banner, embed=view.embed(), view=view, allowed_mentions=NO_PING)


@bot.tree.command(name="bảng-xác-minh", description="Bảng những thành viên đã liên kết YouTube trong server này (admin)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def bangxacminh_cmd(inter: discord.Interaction):
    if not MEMBERS_INTENT:
        await inter.response.send_message(MEMBERS_HELP, ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    if not inter.guild.chunked:
        await inter.guild.chunk()
    rows = []
    async for lk in db.links.find():
        m = inter.guild.get_member(lk["discord_id"])
        if not m:
            continue
        ch = await db.channels.find_one({"_id": lk["_id"]}) or {}
        rows.append((m.display_name.lower(), lk["discord_id"], lk["_id"], ch.get("handle") or ch.get("name") or lk["_id"]))
    rows.sort()
    view = VerifiedView([r[1:] for r in rows], inter.user.id)
    await inter.followup.send(embed=view.embed(), view=view, allowed_mentions=NO_PING)


@bot.tree.command(name="tìm-liên-kết", description="Kiểm tra thành viên đã liên kết YouTube hay chưa (admin)")
@app_commands.describe(nguoi_dung="Thành viên cần tra cứu")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def timlienket_cmd(inter: discord.Interaction, nguoi_dung: discord.Member):
    await inter.response.defer(ephemeral=True)
    links = await db.links.find({"discord_id": nguoi_dung.id}).to_list(length=25)
    if not links:
        await inter.followup.send(
            f"ℹ️ {nguoi_dung.mention} chưa có liên kết YouTube trong dữ liệu bot.",
            ephemeral=True, allowed_mentions=NO_PING,
        )
        return

    lines = []
    for link in links:
        cid = link.get("_id")
        info = await db.channels.find_one({"_id": cid}) or {}
        name = info.get("name") or "Kênh YouTube"
        handle = info.get("handle")
        label = f"{name} ({handle})" if handle else name
        lines.append(f"• **{label}**\n  Channel ID: `{cid}`\n  https://www.youtube.com/channel/{cid}")

    embed = discord.Embed(
        title="🔎 Kết quả tra cứu liên kết",
        description=f"Thành viên: {nguoi_dung.mention}\nDiscord ID: `{nguoi_dung.id}`\n\n" + "\n".join(lines),
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"Tìm thấy {len(links)} liên kết • Chỉ hiển thị với người thực hiện lệnh")
    await inter.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_PING)


@bot.tree.command(name="hủy-liên-kết", description="Huỷ liên kết YouTube của một thành viên (admin)")
@app_commands.describe(nguoi_dung="Thành viên cần huỷ liên kết")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def huylienket_cmd(inter: discord.Interaction, nguoi_dung: discord.Member):
    r = await db.links.delete_many({"discord_id": nguoi_dung.id})
    await inter.response.send_message(
        f"Đã huỷ {r.deleted_count} liên kết của {nguoi_dung.mention}.", ephemeral=True, allowed_mentions=NO_PING
    )


@bot.tree.command(name="gỡ-liên-kết", description="Huỷ liên kết YouTube của bạn")
async def unlink_cmd(inter: discord.Interaction):
    r = await db.links.delete_many({"discord_id": inter.user.id})
    await inter.response.send_message(f"Đã huỷ {r.deleted_count} liên kết.", ephemeral=True)


# ---------- Bảng xếp hạng donate (cộng dồn các khoản đã ghi nhận) ----------
RANK_PER = 10  # số người mỗi trang ở danh sách đầy đủ


def rank_match(gid: int, sid: str) -> dict:
    return {"guild_id": gid, "stream_channel": None if sid == UNASSIGNED else sid}


async def rank_summary(gid: int, sid: str) -> tuple[int, int]:
    """(số người đã donate, tổng tiền VND) của một kênh trong server."""
    r = await db.donations.aggregate([
        {"$match": rank_match(gid, sid)},
        {"$group": {"_id": "$channel_id", "total": {"$sum": "$vnd"}}},
        {"$group": {"_id": None, "donors": {"$sum": 1}, "sum": {"$sum": "$total"}}},
    ]).to_list(1)
    return (r[0]["donors"], r[0]["sum"]) if r else (0, 0)


async def rank_page(gid: int, sid: str, skip: int, limit: int) -> list[dict]:
    # sort thêm _id để khi bằng tiền thì thứ hạng vẫn cố định giữa các trang
    return await db.donations.aggregate([
        {"$match": rank_match(gid, sid)},
        {"$group": {"_id": "$channel_id", "total": {"$sum": "$vnd"}, "count": {"$sum": 1}}},
        {"$sort": {"total": -1, "_id": 1}},
        {"$skip": skip},
        {"$limit": limit},
    ]).to_list(limit)


async def rank_lines(rows: list[dict], start: int) -> list[str]:
    ids = [r["_id"] for r in rows if not r["_id"].startswith("dc:")]
    infos = {c["_id"]: c async for c in db.channels.find({"_id": {"$in": ids}})}
    links = {k["_id"]: k["discord_id"] async for k in db.links.find({"_id": {"$in": ids}})}
    out = []
    for n, r in enumerate(rows, start):
        cid = r["_id"]
        if cid.startswith("dc:"):
            who = f"<@{cid[3:]}>"
        else:
            info = infos.get(cid, {})
            name = str(info.get("handle") or info.get("name") or cid).replace("[", "(").replace("]", ")")
            who = f"[{discord.utils.escape_markdown(name)}](https://www.youtube.com/channel/{cid})"
            if cid in links:
                who += f" • <@{links[cid]}>"
        out.append(f"**{n}.** {who} — **{r['total']:,}₫** • {r['count']} lượt")
    return out


class RankingView(discord.ui.View):
    """Danh sách đầy đủ, mỗi trang RANK_PER người: xem cả những người sau top 10."""

    def __init__(self, gid: int, sid: str, sname: str, donors: int, total: int, page: int = 0):
        super().__init__(timeout=900)
        self.gid, self.sid, self.sname, self.donors, self.total = gid, sid, sname, donors, total
        self.pages = max(1, -(-donors // RANK_PER))
        self.page = min(max(page, 0), self.pages - 1)

    async def embed(self) -> discord.Embed:
        rows = await rank_page(self.gid, self.sid, self.page * RANK_PER, RANK_PER)
        lines = await rank_lines(rows, self.page * RANK_PER + 1)
        e = discord.Embed(
            title=f"🏆 Bảng xếp hạng donate • {self.sname}"[:256],
            description="\n".join(lines) or "Chưa có ai.",
            color=discord.Color.blurple(),
        )
        e.set_footer(text=f"Trang {self.page + 1}/{self.pages} • {self.donors} người • tổng {self.total:,}₫")
        return e

    async def _go(self, inter: discord.Interaction, step: int):
        self.page = (self.page + step) % self.pages
        await inter.response.defer()
        await inter.edit_original_response(embed=await self.embed(), view=self, allowed_mentions=NO_PING)

    @discord.ui.button(label="◀ Trước", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._go(inter, -1)

    @discord.ui.button(label="Sau ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._go(inter, 1)


class TopMoreView(discord.ui.View):
    """Nút dưới /top-donate: mở danh sách đầy đủ (chỉ người bấm thấy) để xem những người sau top 10."""

    def __init__(self, gid: int, sid: str, sname: str, donors: int, total: int, first_page: int):
        super().__init__(timeout=900)
        self.args = (gid, sid, sname, donors, total)
        self.first_page = first_page
        self.message = None

    @discord.ui.button(label="📋 Xem danh sách đầy đủ", style=discord.ButtonStyle.primary)
    async def more_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.defer(ephemeral=True)
        view = RankingView(*self.args, page=self.first_page)
        await inter.followup.send(embed=await view.embed(), view=view, ephemeral=True, allowed_mentions=NO_PING)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


@bot.tree.command(name="bảng-xếp-hạng", description="Xem danh sách xếp hạng donate đầy đủ, gồm cả người ngoài top 10")
@app_commands.describe(
    kenh="Kênh cần xem (chọn từ gợi ý). Bỏ trống = tự chọn nếu server chỉ có 1 kênh",
    trang="Trang bắt đầu (mỗi trang 10 người, trang 2 = hạng 11-20)",
)
@app_commands.guild_only()
async def bangxephang_cmd(inter: discord.Interaction, kenh: str | None = None, trang: app_commands.Range[int, 1, 1000] = 1):
    sc, err = await pick_streamer(inter.guild_id, kenh, for_view=True)
    if err:
        await inter.response.send_message(err, ephemeral=True)
        return
    sid, sname = sc
    await inter.response.defer(ephemeral=True)
    donors, total = await rank_summary(inter.guild_id, sid)
    if not donors:
        await inter.followup.send(f"Chưa có donate nào ở kênh **{sname}**.", ephemeral=True)
        return
    view = RankingView(inter.guild_id, sid, sname, donors, total, page=trang - 1)
    await inter.followup.send(embed=await view.embed(), view=view, ephemeral=True, allowed_mentions=NO_PING)


@bot.tree.command(name="top-donate", description="Xem bảng xếp hạng donate của một kênh YouTube trong server này")
@app_commands.describe(
    kenh="Kênh cần xem (chọn từ gợi ý). Bỏ trống = tự chọn nếu server chỉ có 1 kênh",
    limit="Số người hiển thị (1-10)",
)
@app_commands.guild_only()
async def top_cmd(
    inter: discord.Interaction,
    kenh: str | None = None,
    limit: app_commands.Range[int, 1, 10] = 10,
):
    gid = inter.guild_id
    await inter.response.defer()
    sc, err = await pick_streamer(gid, kenh, for_view=True)
    if err:
        await inter.followup.send(err, ephemeral=True)
        return
    sid, sname = sc
    rows = await db.donations.aggregate([
        {"$match": rank_match(gid, sid)},
        {"$group": {"_id": "$channel_id", "total": {"$sum": "$vnd"}, "count": {"$sum": 1}}},
        {"$sort": {"total": -1, "_id": 1}},
        {"$limit": limit},
    ]).to_list(limit)
    if not rows:
        await inter.followup.send(f"Chưa có donate nào ở kênh **{sname}**.")
        return

    embeds = []
    medals = ["🥇", "🥈", "🥉"]
    for index, row in enumerate(rows):
        cid = row["_id"]
        avatar_url = None
        if cid.startswith("dc:"):
            did = int(cid[3:])
            try:
                user = await bot.fetch_user(did)
                label = f"@{user.name}"
                avatar_url = user.display_avatar.url
            except discord.HTTPException:
                label = str(did)
        else:
            info = await db.channels.find_one({"_id": cid}) or {}
            label = info.get("handle") or info.get("name") or cid
            avatar_url = info.get("avatar")

        rank = medals[index] if index < len(medals) else f"#{index + 1}"
        embed = discord.Embed(
            title="🏆 TOP DONATE" if index == 0 else None,
            description=f"Kênh **{sname}**" if index == 0 else None,
            color=discord.Color.blurple(),
        )
        embed.set_author(name=f"{rank} {label[:240]}", icon_url=avatar_url)
        embed.description = (
            (embed.description + "\n" if embed.description else "")
            + f"**{int(row['total']):,}₫** · {int(row['count'])} lượt donate"
        )
        if index == len(rows) - 1:
            embed.set_footer(text="Bảng xếp hạng donate")
        embeds.append(embed)

    send_args = {"embeds": embeds, "allowed_mentions": NO_PING}
    donors, total = await rank_summary(gid, sid)
    if donors > limit:
        view = TopMoreView(gid, sid, sname, donors, total, first_page=limit // RANK_PER)
        view.message = await inter.followup.send(**send_args, view=view)
    else:
        await inter.followup.send(**send_args)


@bot.tree.command(name="theo-dõi-live", description="Theo dõi thủ công một live stream (admin)")
@app_commands.describe(video="Link hoặc ID video đang live")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def watch_cmd(inter: discord.Interaction, video: str):
    m = re.search(r"(?:v=|youtu\.be/|live/|shorts/)([\w-]{11})", video)
    vid = m.group(1) if m else video.strip()
    if not re.fullmatch(r"[\w-]{11}", vid):
        await inter.response.send_message("Link hoặc ID video không hợp lệ.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    try:
        data = await yt_get("videos", part="liveStreamingDetails,snippet", id=vid)
    except YTError as e:
        await inter.followup.send(f"Lỗi YouTube: {e}")
        return
    items = data.get("items", [])
    chat = items[0].get("liveStreamingDetails", {}).get("activeLiveChatId") if items else None
    if not chat:
        await inter.followup.send("Video này không có live chat đang hoạt động.")
        return
    info = video_info(items[0], chat)
    gid = inter.guild_id
    # Đăng ký server này vào live trước khi bắt đầu đọc chat để không sót donate nào
    subs.setdefault(vid, set()).add(gid)
    vinfo[vid] = info
    t = watchers.get(vid)
    if not t or t.done():
        watchers[vid] = asyncio.create_task(watch_chat(vid, chat, info))
        status = "Đang theo dõi"
    else:
        status = "Đã thêm server này vào live đang được theo dõi"
    # force=True: luôn báo khi admin chủ động gõ /watch, kể cả khi vòng tự động đã báo live này rồi
    err = await announce_live(vid, info, f"thủ công, bởi {inter.user.display_name}", gid, force=True)
    note = f"\n⚠️ Chưa gửi được thông báo: {err}" if err else "\n📣 Đã gửi thông báo vào kênh thông báo."
    await inter.followup.send(f"{status} live **{info['title']}** (`{vid}`).{note}")


@bot.tree.command(name="thêm-donate", description="Thêm tay khoản donate cũ vào bảng xếp hạng (admin)")
@app_commands.describe(
    so_tien="Số tiền đã donate",
    tien_te="Loại tiền: VND, USD...",
    youtube="Link kênh, @handle hoặc ID kênh YouTube của người đó",
    nguoi_dung="Tài khoản Discord của người đó",
    ghi_chu="Ghi chú (tuỳ chọn)",
    kenh="Kênh được donate (chọn từ gợi ý hoặc dán link/@handle). Bỏ trống = tự chọn nếu chỉ có 1 kênh",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def themdonate_cmd(
    inter: discord.Interaction,
    so_tien: float,
    tien_te: str = "VND",
    youtube: str | None = None,
    nguoi_dung: discord.User | None = None,
    ghi_chu: str = "",
    kenh: str | None = None,
):
    cur = tien_te.upper()
    if so_tien <= 0 or cur not in RATES_VND:
        await inter.response.send_message(
            f"Số tiền phải > 0 và loại tiền thuộc: {', '.join(RATES_VND)}.", ephemeral=True
        )
        return
    if not youtube and not nguoi_dung:
        await inter.response.send_message(
            "Cần ít nhất một trong hai: `youtube` hoặc `nguoi_dung`.", ephemeral=True
        )
        return
    await inter.response.defer(ephemeral=True)

    sc, err = await pick_streamer(inter.guild_id, kenh, create=True)
    if err:
        await inter.followup.send(err)
        return

    if youtube:
        try:
            au = await resolve_youtube(youtube)
        except YTError as e:
            await inter.followup.send(f"Lỗi YouTube: {e}")
            return
        if not au:
            await inter.followup.send("Không tìm thấy kênh YouTube này.")
            return
        await ensure_channel(au)
        cid = au["channelId"]
        if nguoi_dung:
            await db.links.update_one(
                {"_id": cid}, {"$set": {"discord_id": nguoi_dung.id}}, upsert=True
            )
            await migrate_discord_only(nguoi_dung.id, cid)
    else:
        cid = f"dc:{nguoi_dung.id}"

    vnd = int(so_tien * RATES_VND[cur])
    did = f"manual-{uuid.uuid4().hex[:8]}"
    await db.donations.insert_one({
        "_id": did, "channel_id": cid, "video_id": None, "amount": so_tien,
        "currency": cur, "vnd": vnd, "comment": ghi_chu,
        "at": datetime.now(timezone.utc), "manual": True, "added_by": inter.user.id,
        "stream_channel": sc[0], "guild_id": inter.guild_id,
    })
    await inter.followup.send(
        f"Đã thêm {so_tien:,.0f} {cur} (~{vnd:,}₫) vào kênh **{sc[1]}**. "
        f"Mã: `{did}` (nhập sai thì dùng `/xóa-donate`)."
    )


@bot.tree.command(name="xóa-donate", description="Xoá một khoản donate hoặc cả lô nhập CSV (admin)")
@app_commands.describe(ma="Chọn khoản trong danh sách gợi ý, hoặc nhập batch-xxxxxx để xoá cả lô CSV")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def xoadonate_cmd(inter: discord.Interaction, ma: str):
    ma = ma.strip()
    gid = inter.guild_id  # chỉ xoá được donate của chính server này
    if ma.startswith("h:"):  # ID dài từ YouTube được rút gọn thành mã băm
        rows = await db.donations.find({"guild_id": gid}, {"_id": 1}).sort("at", -1).limit(500).to_list(500)
        flt = {"guild_id": gid, "_id": {"$in": [r["_id"] for r in rows if donation_key(r["_id"]) == ma]}}
    else:
        flt = {"guild_id": gid, "$or": [{"_id": ma}, {"batch": ma}]}
    r = await db.donations.delete_many(flt)
    msg = f"Đã xoá {r.deleted_count} khoản." if r.deleted_count else "Không tìm thấy khoản donate với mã này."
    await inter.response.send_message(msg, ephemeral=True)


def donation_key(did: str) -> str:
    """Giá trị gợi ý (tối đa 100 ký tự). ID quá dài được rút gọn bằng mã băm."""
    return did if len(did) <= 100 else "h:" + hashlib.md5(did.encode()).hexdigest()[:12]


@xoadonate_cmd.autocomplete("ma")
async def xoadonate_autocomplete(inter: discord.Interaction, current: str):
    rows = await db.donations.find({"guild_id": inter.guild_id}).sort("at", -1).limit(25).to_list(25)
    vn = timezone(timedelta(hours=7))
    out = []
    for r in rows:
        cid = r["channel_id"]
        if cid.startswith("dc:"):
            who = f"Discord {cid[3:]}"
        else:
            info = await db.channels.find_one({"_id": cid}) or {}
            who = info.get("handle") or info.get("name") or cid
        tag = "thêm tay" if r.get("manual") else "live"
        name = f"{r['amount']:,.0f} {r['currency']} • {who} • {tag} • {r['at'].astimezone(vn):%d/%m %H:%M}"
        if current and current.lower() not in name.lower():
            continue
        out.append(app_commands.Choice(name=name[:100], value=donation_key(r["_id"])))
    return out[:25]


@bot.tree.command(name="nhập-donate", description="Nhập hàng loạt donate cũ từ file CSV (admin)")
@app_commands.describe(
    file="CSV có cột: youtube, discord_id, so_tien, tien_te, ghi_chu (thêm cột kenh nếu muốn)",
    kenh="Kênh mặc định cho cả file (dòng nào có cột kenh thì dùng kênh đó)",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def nhapdonate_cmd(inter: discord.Interaction, file: discord.Attachment, kenh: str | None = None):
    if not file.filename.lower().endswith(".csv") or file.size > 1_000_000:
        await inter.response.send_message("Cần file .csv dưới 1MB.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(raw)))
    if not rows:
        await inter.followup.send("File trống hoặc thiếu dòng tiêu đề.")
        return
    if len(rows) > 500:
        await inter.followup.send("Tối đa 500 dòng mỗi lần, hãy chia nhỏ file.")
        return

    batch = f"batch-{uuid.uuid4().hex[:6]}"
    cache: dict = {}
    sc_cache: dict = {}

    async def sc_for(text):
        """Kênh của một dòng CSV (nhớ kết quả để không tra lại cho từng dòng)."""
        key = (text or "").strip().lower()
        if key not in sc_cache:
            sc_cache[key] = await pick_streamer(inter.guild_id, text, create=True)
        sc, err = sc_cache[key]
        if err:
            raise ValueError(err)
        return sc[0]

    ok, errors = 0, []
    for n, row in enumerate(rows, start=2):  # dòng 1 là tiêu đề
        r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k and isinstance(v, (str, type(None)))}
        try:
            amount = parse_amount(r.get("so_tien", ""))
            raw_id = r.get("discord_id", "")
            try:
                discord_id = int(raw_id) if raw_id else None
            except ValueError:
                raise ValueError("discord_id không hợp lệ")
            sid = await sc_for(r.get("kenh") or kenh)
            await add_manual(
                r.get("youtube") or None, discord_id, amount,
                (r.get("tien_te") or "VND").upper(), r.get("ghi_chu", ""),
                inter.user.id, batch, cache, sid, inter.guild_id,
            )
            ok += 1
        except ValueError as e:
            errors.append(f"Dòng {n}: {e}")

    msg = f"Đã nhập **{ok}/{len(rows)}** dòng. Mã lô: `{batch}` (dùng `/xóa-donate` với mã này để huỷ cả lô)."
    if errors:
        msg += "\nLỗi:\n" + "\n".join(errors[:10])
        if len(errors) > 10:
            msg += f"\n... và {len(errors) - 10} lỗi khác."
    await inter.followup.send(msg[:1990])


@bot.tree.command(name="set-kênh-thông-báo", description="Chọn kênh Discord nhận thông báo live và video mới (admin)")
@app_commands.describe(kenh="Chọn kênh từ danh sách gợi ý. Bỏ trống = dùng kênh bạn đang gõ lệnh")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def kenhthongbao_cmd(inter: discord.Interaction, kenh: discord.TextChannel | None = None):
    kenh = kenh or inter.channel
    if not isinstance(kenh, discord.TextChannel):
        await inter.response.send_message("Hãy chọn một kênh văn bản.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    await db.guilds.update_one({"_id": inter.guild_id}, {"$set": {"announce": kenh.id}}, upsert=True)
    await inter.followup.send(f"Đã đặt kênh nhận thông báo live/video mới: {kenh.mention}")


@bot.tree.command(name="set-kênh-thông-báo-donate", description="Chọn kênh Discord chỉ nhận thông báo donate (admin)")
@app_commands.describe(kenh="Kênh văn bản nhận thông báo donate")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def kenhthongbaodonate_cmd(inter: discord.Interaction, kenh: discord.TextChannel):
    if not inter.user.guild_permissions.manage_guild:
        await inter.response.send_message("Bạn cần quyền Quản lý máy chủ để dùng lệnh này.", ephemeral=True)
        return
    me = inter.guild.get_member(bot.user.id) if inter.guild and bot.user else None
    perms = kenh.permissions_for(me) if me else None
    if not perms or not perms.view_channel or not perms.send_messages or not perms.embed_links:
        await inter.response.send_message(
            "Bot cần quyền Xem kênh, Gửi tin nhắn và Nhúng liên kết trong kênh đó.",
            ephemeral=True,
        )
        return
    await db.guilds.update_one(
        {"_id": inter.guild_id}, {"$set": {"donation_announce": kenh.id}}, upsert=True
    )
    await inter.response.send_message(
        f"Đã đặt {kenh.mention} làm kênh chỉ nhận thông báo donate.", ephemeral=True
    )


@bot.tree.command(name="gán-kênh-donate", description="Gán donate chưa phân kênh (hoặc một khoản/lô) vào một kênh (admin)")
@app_commands.describe(
    kenh="Kênh sẽ nhận các khoản donate (chọn từ gợi ý hoặc dán link/@handle)",
    ma="Mã khoản hoặc batch-xxxxxx. Bỏ trống = tất cả khoản chưa phân kênh",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def gankenh_cmd(inter: discord.Interaction, kenh: str, ma: str | None = None):
    await inter.response.defer(ephemeral=True)
    gid = inter.guild_id  # chỉ gán được donate của chính server này
    sc, err = await pick_streamer(gid, kenh, create=True)
    if err:
        await inter.followup.send(err)
        return
    ma = (ma or "").strip()
    flt = {"guild_id": gid, **({"$or": [{"_id": ma}, {"batch": ma}]} if ma else {"stream_channel": None})}
    r = await db.donations.update_many(flt, {"$set": {"stream_channel": sc[0]}})
    await inter.followup.send(f"Đã gán {r.modified_count} khoản vào kênh **{sc[1]}**.")


@bot.tree.command(name="theo-dõi-kênh", description="Tự động theo dõi live của một kênh YouTube trong server này (admin)")
@app_commands.describe(kenh="Link kênh, @handle hoặc ID kênh YouTube (UC...)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def theodoikenh_cmd(inter: discord.Interaction, kenh: str):
    await inter.response.defer(ephemeral=True)
    try:
        au = await resolve_youtube(kenh)
    except YTError as e:
        await inter.followup.send(f"Lỗi YouTube: {e}")
        return
    if not au:
        await inter.followup.send("Không tìm thấy kênh YouTube này. Hãy dán link kênh, @handle hoặc ID bắt đầu bằng UC.")
        return
    await db.streamers.update_one({"_id": au["channelId"]}, {"$set": {"name": au["displayName"]}}, upsert=True)
    await db.guilds.update_one({"_id": inter.guild_id}, {"$addToSet": {"yt": au["channelId"]}}, upsert=True)
    recent = await fetch_channel_videos(au["channelId"])
    if recent:
        await db.feed_state.update_one(
            {"_id": f"{inter.guild_id}:{au['channelId']}"},
            {"$setOnInsert": {"video_id": recent[0]["video_id"]}},
            upsert=True,
        )

    status = (
        f"Đã đăng ký tự động theo dõi live của **{au['displayName']}**. "
        "Bot sẽ tiếp tục dò live mới mỗi 60 giây."
    )
    try:
        live = await find_live_chats([au["channelId"]])
        if live:
            for vid, info in live.items():
                subs.setdefault(vid, set()).add(inter.guild_id)
                vinfo[vid] = info
                task = watchers.get(vid)
                if not task or task.done():
                    watchers[vid] = asyncio.create_task(watch_chat(vid, info["chat"], info))
                    log.info("Bắt đầu theo dõi live ngay sau /theodoikenh: %s", vid)
                err = await announce_live(vid, info, "tự động", inter.guild_id)
                if err:
                    status += f"\n⚠️ Đã tìm thấy live nhưng không gửi được thông báo: {err}"
                else:
                    status += f"\n🔴 Đã tìm thấy live **{info['title']}** và bắt đầu đọc Live Chat."
        else:
            status += "\nHiện kênh chưa có live đang hoạt động; bot sẽ tự phát hiện khi kênh bắt đầu live."
    except Exception:
        log.exception("Không kiểm tra được live ngay sau /theodoikenh")
        status += "\n⚠️ Chưa kiểm tra được live ngay lúc này; bot vẫn sẽ tiếp tục dò tự động."
    await inter.followup.send(status)


@bot.tree.command(name="hủy-theo-dõi", description="Ngừng tự động theo dõi một kênh YouTube trong server này (admin)")
@app_commands.describe(kenh="Chọn kênh đang theo dõi từ danh sách gợi ý")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def huytheodoi_cmd(inter: discord.Interaction, kenh: str):
    r = await db.guilds.update_one({"_id": inter.guild_id}, {"$pull": {"yt": kenh.strip()}})
    msg = ("Đã ngừng tự theo dõi kênh này (donate đã ghi vẫn được giữ)."
           if r.modified_count else "Server này chưa tự theo dõi kênh đó.")
    await inter.response.send_message(msg, ephemeral=True)


@huytheodoi_cmd.autocomplete("kenh")
async def followed_autocomplete(inter: discord.Interaction, current: str):
    ids = (await guild_cfg(inter.guild_id)).get("yt", [])
    names = {}
    async for d in db.streamers.find({"_id": {"$in": ids}}):
        names[d["_id"]] = d.get("name") or d["_id"]
    cur = current.strip().lower()
    return [
        app_commands.Choice(name=(names.get(i) or i)[:100], value=i)
        for i in ids if cur in (names.get(i) or i).lower()
    ][:25]


@top_cmd.autocomplete("kenh")
@bangxephang_cmd.autocomplete("kenh")
@themdonate_cmd.autocomplete("kenh")
@nhapdonate_cmd.autocomplete("kenh")
@gankenh_cmd.autocomplete("kenh")
async def streamer_autocomplete(inter: discord.Interaction, current: str):
    items = await list_streamers(inter.guild_id)
    if not inter.command or inter.command.name != "top-donate":  # chỉ /top-donate được xem mục "Chưa phân kênh"
        items = [x for x in items if x[0] != UNASSIGNED]
    cur = current.strip().lower()
    return [
        app_commands.Choice(name=n[:100], value=i) for i, n in items if cur in n.lower()
    ][:25]


if __name__ == "__main__":
    bot.run(TOKEN)