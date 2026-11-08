import asyncio
import csv
import hashlib
import io
import logging
import os
import re
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

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
YT_CHANNELS = [c.strip() for c in os.environ["YT_CHANNEL_ID"].split(",") if c.strip()]
ANNOUNCE_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))  # tuỳ chọn: kênh báo donate
PORT = int(os.getenv("PORT", "0"))  # Render Web Service cần; Worker thì bỏ trống

# Tỷ giá ước lượng về VND để cộng tổng. Hãy chỉnh lại cho đúng ý bạn.
RATES_VND = {
    "VND": 1, "USD": 25400, "EUR": 27500, "GBP": 32000, "JPY": 170,
    "KRW": 18, "THB": 720, "SGD": 19000, "AUD": 16500, "CAD": 18500,
    "PHP": 440, "MYR": 5400, "IDR": 1.6, "TWD": 800, "HKD": 3250, "CNY": 3500,
}

CODE_RE = re.compile(r"DC-[A-Z0-9]{6}")
db = AsyncIOMotorClient(MONGO_URI, tz_aware=True)[os.getenv("DB_NAME", "superchat")]
session: aiohttp.ClientSession | None = None
watchers: dict[str, asyncio.Task] = {}
ended: set[str] = set()  # video đã live xong, không kiểm tra lại


# ---------- YouTube ----------
class YTError(Exception):
    def __init__(self, status, reason, message):
        super().__init__(f"{status} {reason}: {message}")
        self.status, self.reason = status, reason


async def yt_get(path, **params):
    params["key"] = YT_KEY
    async with session.get(f"https://www.googleapis.com/youtube/v3/{path}", params=params) as r:
        data = await r.json()
        if r.status != 200:
            err = data.get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason", "")
            raise YTError(r.status, reason, err.get("message", ""))
        return data


async def find_live_chats() -> dict[str, dict]:
    """Dùng RSS (miễn phí) tìm video mới, rồi videos.list (1 quota) xem video nào đang live."""
    ids = []
    for ch in YT_CHANNELS:
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
        if d.get("activeLiveChatId") and d.get("actualStartTime") and not d.get("actualEndTime"):
            out[v["id"]] = video_info(v, d["activeLiveChatId"])
    return out


announced: set[str] = set()  # live đã thông báo, tránh thông báo lặp


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


async def get_announce_channel():
    doc = await db.settings.find_one({"_id": "announce"})
    cid = doc["channel_id"] if doc else ANNOUNCE_ID
    return bot.get_channel(cid) if cid else None


async def announce_live(vid: str, info: dict, source: str):
    """Thông báo kèm link live và link kênh khi bot bắt đầu theo dõi."""
    if vid in announced:
        return
    announced.add(vid)
    ch = await get_announce_channel()
    if not ch:
        return
    e = discord.Embed(
        title=f"🔴 {info['title']}"[:256],
        url=f"https://www.youtube.com/watch?v={vid}",
        description=(
            f"Kênh [{info['channel']}](https://www.youtube.com/channel/{info['channel_id']}) đang live.\n"
            f"Bot bắt đầu theo dõi donate ({source})."
        ),
        color=discord.Color.red(),
    )
    if info.get("thumb"):
        e.set_image(url=info["thumb"])
    try:
        await ch.send(embed=e)
    except discord.HTTPException as ex:
        log.warning("Không gửi được thông báo live: %s", ex)


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


async def try_link(code: str, au: dict):
    doc = await db.link_codes.find_one_and_delete({"_id": code})
    if not doc or doc["expires"] < datetime.now(timezone.utc):
        return
    await db.links.update_one(
        {"_id": au["channelId"]}, {"$set": {"discord_id": doc["discord_id"]}}, upsert=True
    )
    await migrate_discord_only(doc["discord_id"], au["channelId"])
    await ensure_channel(au)
    log.info("Đã liên kết %s ↔ Discord %s", au["channelId"], doc["discord_id"])
    try:
        user = await bot.fetch_user(doc["discord_id"])
        await user.send(f"✅ Đã liên kết với kênh YouTube **{au['displayName']}**.")
    except discord.HTTPException:
        pass


async def handle_message(item: dict, video_id: str):
    sn, au = item["snippet"], item["authorDetails"]
    kind = sn["type"]

    if kind == "textMessageEvent":
        m = CODE_RE.search(sn.get("textMessageDetails", {}).get("messageText", "").upper())
        if m:
            await try_link(m.group(0), au)
        return

    if kind not in ("superChatEvent", "superStickerEvent"):
        return

    d = sn.get("superChatDetails") or sn.get("superStickerDetails")
    amount = int(d["amountMicros"]) / 1_000_000
    cur = d["currency"]
    if cur not in RATES_VND:
        log.warning("Chưa có tỷ giá cho %s, tính 0 VND", cur)
    vnd = int(amount * RATES_VND.get(cur, 0))
    try:
        await db.donations.insert_one({
            "_id": item["id"],  # chống trùng khi bot khởi động lại
            "channel_id": au["channelId"],
            "video_id": video_id,
            "amount": amount,
            "currency": cur,
            "vnd": vnd,
            "comment": d.get("userComment", ""),
            "at": datetime.now(timezone.utc),
        })
    except DuplicateKeyError:
        return

    await ensure_channel(au)
    log.info("Donate %s %s từ %s", amount, cur, au["displayName"])
    await announce(au, d, vnd)


async def announce(au: dict, d: dict, vnd: int):
    ch = await get_announce_channel()
    if not ch:
        return
    info = await db.channels.find_one({"_id": au["channelId"]}) or {}
    link = await db.links.find_one({"_id": au["channelId"]})
    who = f"<@{link['discord_id']}>" if link else (info.get("handle") or au["displayName"])
    e = discord.Embed(
        color=discord.Color.gold(),
        description=f"{who} vừa donate **{d.get('amountDisplayString', '')}** (~{vnd:,}₫)\n{d.get('userComment', '')}",
    )
    e.set_author(name=info.get("handle") or au["displayName"], icon_url=au["profileImageUrl"])
    await ch.send(embed=e)


async def watch_chat(video_id: str, chat_id: str):
    log.info("Bắt đầu theo dõi live %s", video_id)
    token = None
    while True:
        try:
            params = {"liveChatId": chat_id, "part": "snippet,authorDetails", "maxResults": 2000}
            if token:
                params["pageToken"] = token
            data = await yt_get("liveChat/messages", **params)
            for item in data.get("items", []):
                await handle_message(item, video_id)
            token = data.get("nextPageToken")
            await asyncio.sleep(max(data.get("pollingIntervalMillis", 10000), 10000) / 1000)
        except YTError as e:
            if e.reason in ("liveChatEnded", "liveChatNotFound"):
                log.info("Live %s đã kết thúc", video_id)
                return
            if e.reason in ("quotaExceeded", "rateLimitExceeded"):
                log.error("Hết quota YouTube, chờ 10 phút")
                await asyncio.sleep(600)
            else:
                log.warning("Lỗi YouTube: %s", e)
                await asyncio.sleep(30)
        except Exception:
            log.exception("Lỗi không mong đợi trong watch_chat")
            await asyncio.sleep(30)


@tasks.loop(seconds=60)
async def monitor():
    try:
        live = await find_live_chats()
    except Exception as e:
        log.warning("Không kiểm tra được live: %s", e)
        return
    for vid, info in live.items():
        t = watchers.get(vid)
        if not t or t.done():
            watchers[vid] = asyncio.create_task(watch_chat(vid, info["chat"]))
            await announce_live(vid, info, "tự động")
    for vid in [v for v, t in watchers.items() if t.done() and v not in live]:
        watchers.pop(vid)


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


async def add_manual(youtube, discord_id, amount, cur, note, added_by, batch=None, cache=None):
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
    })
    return did


# ---------- Discord ----------
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        global session
        session = aiohttp.ClientSession()
        await db.link_codes.create_index("expires", expireAfterSeconds=0)
        await self.tree.sync()
        monitor.start()
        if PORT:
            app = web.Application()
            app.router.add_get("/", lambda r: web.Response(text="ok"))
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", PORT).start()


bot = Bot()


@bot.tree.command(name="link", description="Liên kết kênh YouTube của bạn với tài khoản Discord")
async def link_cmd(inter: discord.Interaction):
    code = "DC-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    await db.link_codes.insert_one({
        "_id": code,
        "discord_id": inter.user.id,
        "expires": datetime.now(timezone.utc) + timedelta(minutes=10),
    })
    await inter.response.send_message(
        f"Mã của bạn: **{code}** (hết hạn sau 10 phút).\n"
        "Hãy gõ đúng mã này vào **live chat YouTube** bằng kênh bạn dùng để donate, "
        "khi kênh đang live. Bot sẽ tự liên kết.",
        ephemeral=True,
    )


@bot.tree.command(name="unlink", description="Huỷ liên kết YouTube của bạn")
async def unlink_cmd(inter: discord.Interaction):
    r = await db.links.delete_many({"discord_id": inter.user.id})
    await inter.response.send_message(f"Đã huỷ {r.deleted_count} liên kết.", ephemeral=True)


@bot.tree.command(name="top", description="Bảng xếp hạng top donate")
@app_commands.describe(limit="Số người hiển thị (1-10)")
async def top_cmd(inter: discord.Interaction, limit: app_commands.Range[int, 1, 10] = 10):
    await inter.response.defer()
    rows = await db.donations.aggregate([
        {"$group": {"_id": "$channel_id", "total": {"$sum": "$vnd"}, "count": {"$sum": 1}}},
        {"$sort": {"total": -1}},
        {"$limit": limit},
    ]).to_list(limit)
    if not rows:
        await inter.followup.send("Chưa có donate nào được ghi nhận.")
        return

    colors = [discord.Color.gold(), discord.Color.light_grey(), discord.Color.orange()]
    embeds = []
    for i, row in enumerate(rows, 1):
        cid = row["_id"]
        if cid.startswith("dc:"):
            did = int(cid[3:])
            link = {"discord_id": did}
            try:
                u = await bot.fetch_user(did)
                info = {"avatar": u.display_avatar.url}
                label = f"@{u.name}"
            except discord.HTTPException:
                info, label = {}, str(did)
        else:
            info = await db.channels.find_one({"_id": cid}) or {}
            link = await db.links.find_one({"_id": cid})
            label = info.get("handle") or info.get("name") or cid
        line = f"**{row['total']:,}₫** • {row['count']} lượt"
        if link:
            line = f"<@{link['discord_id']}> (ID: `{link['discord_id']}`)\n" + line
        e = discord.Embed(description=line, color=colors[i - 1] if i <= 3 else discord.Color.blurple())
        e.set_author(name=f"#{i} • {label}", icon_url=info.get("avatar"))
        embeds.append(e)
    await inter.followup.send(content="🏆 **Top donate**", embeds=embeds)


@bot.tree.command(name="watch", description="Theo dõi thủ công một live stream (admin)")
@app_commands.describe(video="Link hoặc ID video đang live")
@app_commands.default_permissions(manage_guild=True)
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
    t = watchers.get(vid)
    if not t or t.done():
        watchers[vid] = asyncio.create_task(watch_chat(vid, chat))
        await announce_live(vid, info, f"thủ công, bởi {inter.user.display_name}")
    await inter.followup.send(f"Đang theo dõi live **{info['title']}** (`{vid}`).")


@bot.tree.command(name="themdonate", description="Thêm tay khoản donate cũ vào bảng xếp hạng (admin)")
@app_commands.describe(
    so_tien="Số tiền đã donate",
    tien_te="Loại tiền: VND, USD...",
    youtube="Link kênh, @handle hoặc ID kênh YouTube của người đó",
    nguoi_dung="Tài khoản Discord của người đó",
    ghi_chu="Ghi chú (tuỳ chọn)",
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
    })
    await inter.followup.send(
        f"Đã thêm {so_tien:,.0f} {cur} (~{vnd:,}₫). Mã: `{did}` (nhập sai thì dùng `/xoadonate`)."
    )


@bot.tree.command(name="xoadonate", description="Xoá một khoản donate hoặc cả lô nhập CSV (admin)")
@app_commands.describe(ma="Chọn khoản trong danh sách gợi ý, hoặc nhập batch-xxxxxx để xoá cả lô CSV")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def xoadonate_cmd(inter: discord.Interaction, ma: str):
    ma = ma.strip()
    if ma.startswith("h:"):  # ID dài từ YouTube được rút gọn thành mã băm
        rows = await db.donations.find({}, {"_id": 1}).sort("at", -1).limit(500).to_list(500)
        flt = {"_id": {"$in": [r["_id"] for r in rows if donation_key(r["_id"]) == ma]}}
    else:
        flt = {"$or": [{"_id": ma}, {"batch": ma}]}
    r = await db.donations.delete_many(flt)
    msg = f"Đã xoá {r.deleted_count} khoản." if r.deleted_count else "Không tìm thấy khoản donate với mã này."
    await inter.response.send_message(msg, ephemeral=True)


def donation_key(did: str) -> str:
    """Giá trị gợi ý (tối đa 100 ký tự). ID quá dài được rút gọn bằng mã băm."""
    return did if len(did) <= 100 else "h:" + hashlib.md5(did.encode()).hexdigest()[:12]


@xoadonate_cmd.autocomplete("ma")
async def xoadonate_autocomplete(inter: discord.Interaction, current: str):
    rows = await db.donations.find().sort("at", -1).limit(25).to_list(25)
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


@bot.tree.command(name="nhapdonate", description="Nhập hàng loạt donate cũ từ file CSV (admin)")
@app_commands.describe(file="CSV có cột: youtube, discord_id, so_tien, tien_te, ghi_chu")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def nhapdonate_cmd(inter: discord.Interaction, file: discord.Attachment):
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
            await add_manual(
                r.get("youtube") or None, discord_id, amount,
                (r.get("tien_te") or "VND").upper(), r.get("ghi_chu", ""),
                inter.user.id, batch, cache,
            )
            ok += 1
        except ValueError as e:
            errors.append(f"Dòng {n}: {e}")

    msg = f"Đã nhập **{ok}/{len(rows)}** dòng. Mã lô: `{batch}` (dùng `/xoadonate` với mã này để huỷ cả lô)."
    if errors:
        msg += "\nLỗi:\n" + "\n".join(errors[:10])
        if len(errors) > 10:
            msg += f"\n... và {len(errors) - 10} lỗi khác."
    await inter.followup.send(msg[:1990])


@bot.tree.command(name="kenhthongbao", description="Chọn kênh Discord nhận thông báo live và donate (admin)")
@app_commands.describe(kenh="Kênh văn bản nhận thông báo")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def kenhthongbao_cmd(inter: discord.Interaction, kenh: discord.TextChannel):
    await db.settings.update_one({"_id": "announce"}, {"$set": {"channel_id": kenh.id}}, upsert=True)
    await inter.response.send_message(f"Đã đặt kênh thông báo: {kenh.mention}", ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
