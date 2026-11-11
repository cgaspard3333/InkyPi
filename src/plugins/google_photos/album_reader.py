"""Read the photo list of a public Google Photos shared album.

Google removed third-party read access to existing albums from the Photos
Library API on 2025-03-31, so shared albums are read from their public share
page instead. The page embeds the first 300 items as JSON in an
``AF_initDataCallback({key: 'ds:1', ...})`` script block; further pages come
from the page's own ``batchexecute`` endpoint.

This depends on an undocumented page format that can change without notice:
every parsing step validates its input and raises ``AlbumReadError`` instead
of crashing, so callers can fall back to cached data.
"""

import json
import logging
import re
from urllib.parse import urlsplit, parse_qs, urlencode

logger = logging.getLogger(__name__)

PAGE_HOST = "photos.google.com"
IMAGE_URL_PREFIX = "https://lh3.googleusercontent.com/"
BATCH_URL = "https://photos.google.com/_/PhotosUi/data/batchexecute"
MAX_ITEMS_PER_ALBUM = 3000
VIDEO_MARKER = "76647426"  # key present in a video item's metadata object

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
REQUEST_TIMEOUT = (10, 30)

_DATA_START = "AF_initDataCallback({key: 'ds:1'"
_DATA_END = ", sideChannel: {}});"
_CANONICAL_RE = re.compile(r'<link rel="canonical" href="([^"]+)"')


class AlbumReadError(RuntimeError):
    pass


def normalize_album_link(text):
    """Returns a fetchable share URL for a pasted album link, or None if it isn't one."""
    if not isinstance(text, str):
        return None
    link = text.strip()
    if not link:
        return None
    if not re.match(r"^https?://", link, re.IGNORECASE):
        link = "https://" + link.lstrip("/")
    parts = urlsplit(link)
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if host == "photos.app.goo.gl" and len(path) > 1:
        return f"https://photos.app.goo.gl{path}"
    if host == "goo.gl" and path.startswith("/photos/"):
        return f"https://goo.gl{path}"
    if host == PAGE_HOST and path.startswith("/share/"):
        query = f"?{parts.query}" if parts.query else ""
        return f"https://{PAGE_HOST}{path}{query}"
    return None


def parse_items(items):
    """Turns raw album items into photo dicts, skipping videos and malformed entries."""
    photos = []
    if not isinstance(items, list):
        return photos
    for item in items:
        if not (isinstance(item, list) and len(item) > 1
                and isinstance(item[0], str) and isinstance(item[1], list) and item[1]):
            continue
        extras = next((x for x in item if isinstance(x, dict)), None)
        if extras and VIDEO_MARKER in extras:
            continue
        url = item[1][0]
        if not isinstance(url, str) or not url.startswith(IMAGE_URL_PREFIX):
            continue
        width = item[1][1] if len(item[1]) > 1 else 0
        height = item[1][2] if len(item[1]) > 2 else 0
        photos.append({
            "id": item[0],
            "url": url,
            "width": width if isinstance(width, int) else 0,
            "height": height if isinstance(height, int) else 0,
            "taken": item[2] if len(item) > 2 and isinstance(item[2], (int, float)) else None,
        })
    return photos


def parse_album_page(html, final_url=""):
    """Extracts album id, key, title, first page of photos and the next page token."""
    start = html.find(_DATA_START)
    if start < 0:
        raise AlbumReadError("album data not found in page (is link sharing on?)")
    data_start = html.find("data:", start)
    data_end = html.find(_DATA_END, data_start)
    if data_start < 0 or data_end < 0:
        raise AlbumReadError("album data block is malformed")
    try:
        data = json.loads(html[data_start + len("data:"):data_end])
    except ValueError as e:
        raise AlbumReadError(f"album data is not valid JSON: {e}")
    if not isinstance(data, list) or len(data) < 4 or not isinstance(data[3], list) \
            or not data[3] or not isinstance(data[3][0], str):
        raise AlbumReadError("unexpected album data layout")

    meta = data[3]
    canonical = _CANONICAL_RE.search(html)
    share_url = canonical.group(1).replace("&amp;", "&") if canonical else final_url
    key = (parse_qs(urlsplit(share_url).query).get("key") or [""])[0]
    title = meta[1].strip() if len(meta) > 1 and isinstance(meta[1], str) and meta[1].strip() else ""
    return {
        "album_id": meta[0],
        "key": key,
        "title": title or "Google Photos album",
        "photos": parse_items(data[1]),
        "item_count": len(data[1]) if isinstance(data[1], list) else 0,
        "token": data[2] if isinstance(data[2], str) else "",
    }


def parse_batch_response(text):
    """Parses a batchexecute reply into (items, next_token)."""
    for line in text.splitlines():
        if not line.startswith('[["wrb.fr"'):
            continue
        try:
            payload = json.loads(json.loads(line)[0][2])
        except (ValueError, IndexError, TypeError) as e:
            raise AlbumReadError(f"unexpected page reply: {e}")
        if not isinstance(payload, list) or len(payload) < 2:
            raise AlbumReadError("unexpected page reply layout")
        token = payload[2] if len(payload) > 2 and isinstance(payload[2], str) else ""
        return payload[1], token
    raise AlbumReadError("no data in page reply")


def fetch_album(session, link, max_items=MAX_ITEMS_PER_ALBUM):
    """Downloads a shared album's title and photo list (all pages, up to max_items)."""
    url = normalize_album_link(link)
    if not url:
        raise AlbumReadError("not a Google Photos shared album link")

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
    response = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    final_host = (urlsplit(response.url).hostname or "").lower()
    if final_host != PAGE_HOST:
        raise AlbumReadError(f"link redirected to an unexpected host: {final_host}")
    if response.status_code != 200:
        raise AlbumReadError(f"HTTP {response.status_code} when opening the album")

    album = parse_album_page(response.text, response.url)
    photos = album["photos"]
    read = album["item_count"]
    token = album["token"]
    complete = True
    while token and album["key"] and read < max_items:
        inner = json.dumps([album["album_id"], token, None, album["key"]])
        body = urlencode({"f.req": json.dumps([[["snAcKc", inner, None, "generic"]]])})
        try:
            reply = session.post(BATCH_URL, data=body, timeout=REQUEST_TIMEOUT, headers={
                **headers, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
            if reply.status_code != 200:
                raise AlbumReadError(f"HTTP {reply.status_code} when reading the next page")
            items, token = parse_batch_response(reply.text)
        except Exception as e:
            # Keep what was read so far rather than failing the whole album.
            logger.warning(f"Google Photos: could only read the first {len(photos)} photos of "
                           f"'{album['title']}': {e}")
            complete = False
            break
        photos.extend(parse_items(items))
        read += len(items) if isinstance(items, list) else 0

    return {"title": album["title"], "photos": photos, "complete": complete}
