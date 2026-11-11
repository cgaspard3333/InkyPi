"""Rotate photos from one or more public Google Photos shared albums.

Album photo lists are cached on disk and refreshed every few hours, and the
last downloaded photos are kept, so the frame keeps rotating when Google or
the network is unavailable (or if Google changes its share page format).
"""

import hashlib
import json
import logging
import os
import random
import time

import requests
from PIL import Image, ImageColor, ImageOps

from plugins.base_plugin.base_plugin import BasePlugin
from plugins.google_photos.album_reader import (AlbumReadError, IMAGE_URL_PREFIX, USER_AGENT,
                                                fetch_album, normalize_album_link)
from utils.image_utils import pad_image_blur

logger = logging.getLogger(__name__)

DEFAULT_ALBUM_REFRESH_HOURS = 3
PHOTO_CACHE_SIZE = 60            # downloaded photos kept on disk (all instances)
DOWNLOAD_ATTEMPTS = 3            # candidate photos tried before falling back to the cache
DOWNLOAD_TIMEOUT = (10, 30)
ORDERS = ("shuffle", "oldest", "newest")


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp_path, path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class GooglePhotos(BasePlugin):

    def generate_image(self, settings, device_config):
        links = [normalize_album_link(link) for link in (settings.get("albumURLs[]") or [])]
        links = [link for link in links if link]
        if not links:
            raise RuntimeError("Add at least one Google Photos shared album link.")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        refresh_hours = self._float(settings.get("albumRefreshHours"), DEFAULT_ALBUM_REFRESH_HOURS)
        photos = self._collect_photos(links, refresh_hours)
        if not photos:
            raise RuntimeError("No photos found in the selected Google Photos albums.")

        order = settings.get("order") if settings.get("order") in ORDERS else "shuffle"
        fill = settings.get("fitMode") == "fill"
        image = self._load_next_photo(links, photos, order, dimensions, fill)

        if fill:
            return ImageOps.fit(image, dimensions, method=Image.LANCZOS)
        if settings.get("backgroundOption", "blur") == "blur":
            return pad_image_blur(image, dimensions)
        background = ImageColor.getcolor(settings.get("backgroundColor") or "white", image.mode)
        return ImageOps.pad(image, dimensions, color=background, method=Image.LANCZOS)

    # ------------------------------------------------------------------ albums

    def _collect_photos(self, links, refresh_hours):
        """Photo list of every album, de-duplicated, using cached lists when needed."""
        photos, seen = [], set()
        errors = []
        for link in links:
            try:
                album = self._get_album(link, refresh_hours)
            except Exception as e:
                errors.append(e)
                logger.error(f"Google Photos: cannot read album {self._album_label(link)}: {e}")
                continue
            for photo in album["photos"]:
                if photo["id"] not in seen:
                    seen.add(photo["id"])
                    photos.append(photo)
        if errors and len(errors) == len(links) and not photos:
            raise RuntimeError(f"Could not read any Google Photos album: {errors[0]}")
        return photos

    def _get_album(self, link, refresh_hours):
        cache_path = self._cache_path("albums", f"{_digest(link)}.json")
        cached = _read_json(cache_path)
        if cached and time.time() - cached.get("fetched_at", 0) < refresh_hours * 3600:
            return cached

        try:
            with requests.Session() as session:
                album = fetch_album(session, link)
        except (AlbumReadError, requests.RequestException) as e:
            if cached:
                age_h = (time.time() - cached.get("fetched_at", 0)) / 3600
                logger.warning(f"Google Photos: using cached list of '{cached.get('title')}' "
                               f"({age_h:.1f}h old): {e}")
                return cached
            raise

        if not album["photos"] and cached and cached.get("photos"):
            # An empty result is more likely a page format change than an emptied album.
            logger.warning(f"Google Photos: '{album['title']}' returned no photos, keeping cached list")
            return cached

        album["fetched_at"] = time.time()
        album["link_digest"] = _digest(link)
        _write_json(cache_path, album)
        logger.info(f"Google Photos: album '{album['title']}' has {len(album['photos'])} photos"
                    + ("" if album["complete"] else " (partial)"))
        return album

    def _album_label(self, link):
        cached = _read_json(self._cache_path("albums", f"{_digest(link)}.json"))
        return f"'{cached['title']}'" if cached and cached.get("title") else f"#{_digest(link)[:8]}"

    # ------------------------------------------------------------------ rotation

    def _next_photos(self, links, photos, order):
        """Yields photos in display order; the rotation state is kept per set of albums."""
        state_path = self._cache_path("state", f"{_digest('|'.join(sorted(links)) + order)}.json")
        state = _read_json(state_path) or {}
        by_id = {p["id"]: p for p in photos}

        if order == "shuffle":
            # Shuffle bag: every photo is shown once before any repeats.
            queue = [i for i in state.get("queue", []) if i in by_id]
            if not queue:
                queue = list(by_id)
                random.shuffle(queue)
                if len(queue) > 1 and queue[0] == state.get("last"):
                    queue.append(queue.pop(0))
        else:
            ordered = sorted(photos, key=lambda p: (p.get("taken") or 0, p["id"]),
                             reverse=(order == "newest"))
            ids = [p["id"] for p in ordered]
            start = (ids.index(state["last"]) + 1) % len(ids) if state.get("last") in by_id else 0
            queue = ids[start:] + ids[:start]

        for photo_id in list(queue):
            queue.remove(photo_id)
            if order == "shuffle":
                _write_json(state_path, {"queue": queue, "last": photo_id})
            else:
                _write_json(state_path, {"last": photo_id})
            yield by_id[photo_id]

    def _load_next_photo(self, links, photos, order, dimensions, fill):
        size_tag = f"{dimensions[0]}x{dimensions[1]}{'c' if fill else ''}"
        attempts = 0
        for photo in self._next_photos(links, photos, order):
            attempts += 1
            image = self._get_photo(photo, dimensions, fill, size_tag)
            if image is not None:
                return image
            if attempts >= DOWNLOAD_ATTEMPTS:
                break

        # Offline or Google unreachable: show a previously downloaded photo of these albums.
        cached = [p for p in photos if os.path.exists(self._photo_path(p, size_tag))]
        if cached:
            photo = random.choice(cached)
            logger.warning("Google Photos: download failed, showing a cached photo")
            image = self._open_photo(self._photo_path(photo, size_tag), dimensions)
            if image is not None:
                return image
        raise RuntimeError("Could not download any photo from Google Photos.")

    # ------------------------------------------------------------------ photos

    def _get_photo(self, photo, dimensions, fill, size_tag):
        path = self._photo_path(photo, size_tag)
        if not os.path.exists(path):
            if not photo["url"].startswith(IMAGE_URL_PREFIX):
                return None
            # Google resizes server-side: '=wW-hH' fits in the box, '-c' crops to fill it.
            url = f"{photo['url']}=w{dimensions[0]}-h{dimensions[1]}{'-c' if fill else ''}"
            try:
                response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DOWNLOAD_TIMEOUT)
                response.raise_for_status()
                if not response.headers.get("Content-Type", "").startswith("image/"):
                    raise ValueError(f"unexpected content type {response.headers.get('Content-Type')}")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp_path = path + ".tmp"
                with open(tmp_path, "wb") as f:
                    f.write(response.content)
                os.replace(tmp_path, path)
            except Exception as e:
                logger.warning(f"Google Photos: failed to download photo {photo['id'][:12]}: {e}")
                return None
            self._prune_photo_cache()
        else:
            os.utime(path)  # keep recently shown photos in the cache
        return self._open_photo(path, dimensions)

    def _open_photo(self, path, dimensions):
        image = self.image_loader.from_file(path, dimensions, resize=False)
        if image is None:
            try:
                os.remove(path)  # corrupted download
            except OSError:
                pass
        return image

    def _prune_photo_cache(self):
        photo_dir = self._cache_path("photos")
        try:
            files = [os.path.join(photo_dir, f) for f in os.listdir(photo_dir) if f.endswith(".jpg")]
        except OSError:
            return
        files.sort(key=os.path.getmtime, reverse=True)
        for old in files[PHOTO_CACHE_SIZE:]:
            try:
                os.remove(old)
            except OSError:
                pass

    def _photo_path(self, photo, size_tag):
        return self._cache_path("photos", f"{_digest(photo['id'])}_{size_tag}.jpg")

    # ------------------------------------------------------------------ helpers

    def _cache_path(self, *parts):
        return os.path.join(self.get_plugin_dir("cache"), *parts)

    @staticmethod
    def _float(value, default):
        try:
            value = float(value)
            return value if value > 0 else default
        except (TypeError, ValueError):
            return default
