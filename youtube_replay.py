from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ReplayError(RuntimeError):
    pass


WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
REPLAY_ENDPOINT = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat_replay"


def _extract_json_assignment(html: str, name: str) -> dict:
    patterns = (
        rf"(?:var\s+)?{re.escape(name)}\s*=\s*",
        rf"window\[{json.dumps(name)}\]\s*=\s*",
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if not match:
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(html[match.end():].lstrip())
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    raise ReplayError(f"YouTube không cung cấp dữ liệu {name} trong trang video.")


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _request(url: str, *, data: dict | None = None, headers: dict | None = None) -> dict | str:
    body = json.dumps(data).encode() if data is not None else None
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    if body is not None:
        request_headers.update({
            "Content-Type": "application/json",
            "Origin": "https://www.youtube.com",
            "Referer": "https://www.youtube.com/",
        })
    if headers:
        request_headers.update(headers)
    request = Request(url, data=body, headers=request_headers)
    try:
        with urlopen(request, timeout=25) as response:
            raw = response.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            raise ReplayError("Phản hồi YouTube vượt giới hạn 12 MiB.")
        text = raw.decode("utf-8", errors="replace")
        if data is None:
            return text
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReplayError("YouTube trả về JSON không hợp lệ.") from exc
        if not isinstance(result, dict):
            raise ReplayError("Cấu trúc phản hồi YouTube không hợp lệ.")
        return result
    except HTTPError as exc:
        if exc.code in (403, 429):
            raise ReplayError(
                f"YouTube từ chối yêu cầu replay (HTTP {exc.code}); "
                "không thử vượt giới hạn truy cập."
            ) from exc
        raise ReplayError(f"YouTube trả HTTP {exc.code} khi đọc replay.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ReplayError(f"Không kết nối được YouTube: {type(exc).__name__}.") from exc


def download_replay(video_id: str, *, max_pages: int = 200) -> tuple[dict, list[dict]]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ReplayError("Video ID không hợp lệ.")

    html = _request(WATCH_URL.format(video_id=video_id))
    if not isinstance(html, str):
        raise ReplayError("Không đọc được trang video.")

    initial = _extract_json_assignment(html, "ytInitialData")
    player = _extract_json_assignment(html, "ytInitialPlayerResponse")
    config = {}
    for match in re.finditer(r"ytcfg\.set\(\s*(\{)", html):
        try:
            value, _ = json.JSONDecoder().raw_decode(html[match.start(1):])
            if isinstance(value, dict):
                config.update(value)
        except json.JSONDecodeError:
            continue
    context = config.get("INNERTUBE_CONTEXT")
    api_key = config.get("INNERTUBE_API_KEY")
    if not isinstance(context, dict) or not api_key:
        raise ReplayError("Trang YouTube thiếu cấu hình Innertube cần thiết.")

    details = player.get("videoDetails") or {}
    if details.get("videoId") not in (None, video_id):
        raise ReplayError("YouTube trả dữ liệu của video khác.")
    info = {
        "id": video_id,
        "title": details.get("title") or "YouTube live",
        "channel": details.get("author") or "YouTube channel",
        "channel_id": details.get("channelId") or "",
        "thumbnail": (details.get("thumbnail") or {}).get("thumbnails", [{}])[-1].get("url"),
    }

    continuation = None
    for node in _walk(initial):
        replay = node.get("liveChatReplayContinuationData")
        if isinstance(replay, dict) and replay.get("continuation"):
            continuation = replay["continuation"]
            break
    if not continuation:
        for node in _walk(initial):
            replay = node.get("liveChatContinuation")
            if isinstance(replay, dict):
                for item in replay.get("continuations", []):
                    candidate = item.get("liveChatReplayContinuationData") or {}
                    if candidate.get("continuation"):
                        continuation = candidate["continuation"]
                        break
            if continuation:
                break
    if not continuation:
        raise ReplayError(
            "Không tìm thấy continuation của Live Chat Replay. "
            "Video có thể không công khai replay hoặc YouTube đã đổi cấu trúc trang."
        )

    events: list[dict] = []
    seen: set[str] = set()
    for page in range(max_pages):
        if not continuation or continuation in seen:
            break
        seen.add(continuation)
        endpoint = f"{REPLAY_ENDPOINT}?{urlencode({'key': api_key})}"
        payload = {"context": context, "continuation": continuation}
        response = _request(endpoint, data=payload)
        if not isinstance(response, dict):
            raise ReplayError("Phản hồi replay không hợp lệ.")

        contents = response.get("continuationContents") or {}
        chat = contents.get("liveChatContinuation") or {}
        actions = chat.get("actions") or []
        for action in actions:
            if isinstance(action, dict):
                events.append({
                    "replayChatItemAction": {
                        "actions": [action],
                    }
                })

        next_token = None
        for item in chat.get("continuations", []):
            replay = (
                item.get("liveChatReplayContinuationData")
                or item.get("timedContinuationData")
                or item.get("invalidationContinuationData")
                or {}
            )
            candidate = replay.get("continuation")
            if candidate:
                next_token = candidate
                break
        continuation = next_token
        if continuation:
            time.sleep(0.15)

    if not events:
        raise ReplayError(
            "YouTube không trả sự kiện chat replay. "
            "Có thể replay không khả dụng hoặc định dạng đã thay đổi."
        )
    return info, events
