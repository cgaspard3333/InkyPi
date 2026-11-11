"""Rotate photos from one or more public Google Photos shared albums.

Album photo lists are cached on disk and refreshed every few hours, and the
last downloaded photos are kept, so the frame keeps rotating when Google or
the network is unavailable (or if Google changes its share page format).
Selection logic (album shares, memories, no-repeat bags) lives in selection.py,
composition (framing, pairs, captions) in layout.py.
"""

import glob
import hashlib
import json
import logging
import os
import time
from datetime import datetime

import pytz
import requests

from plugins.base_plugin.base_plugin import BasePlugin
from plugins.google_photos import layout, selection
from plugins.google_photos.album_reader import (AlbumReadError, IMAGE_URL_PREFIX, USER_AGENT,
                                                fetch_album, normalize_album_link)
from plugins.weather.i18n import LANGUAGES

logger = logging.getLogger(__name__)

DEFAULT_ALBUM_REFRESH_HOURS = 3
PHOTO_CACHE_SIZE = 60            # downloaded photos kept on disk (all instances)
DOWNLOAD_ATTEMPTS = 3            # photos tried before falling back to the cache
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

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["album_info"] = self._known_albums()
        template_params["languages"] = LANGUAGES
        return template_params

    def generate_image(self, settings, device_config):
        links = settings.get("albumURLs[]") or []
        shares_in = settings.get("albumShares[]") or []
        albums_cfg = []
        for index, raw in enumerate(links):
            link = normalize_album_link(raw)
            if link:
                share = shares_in[index] if index < len(shares_in) else None
                albums_cfg.append((link, self._float(share, 0)))
        if not albums_cfg:
            raise RuntimeError("Add at least one Google Photos shared album link.")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        tz = self._timezone(device_config)
        now = datetime.now(tz)

        refresh_hours = self._float(settings.get("albumRefreshHours"), DEFAULT_ALBUM_REFRESH_HOURS)
        albums = self._load_albums([link for link, _ in albums_cfg], refresh_hours)
        if not albums:
            raise RuntimeError("Could not read any Google Photos album.")

        options = self._options(settings, albums_cfg)
        photos_by_album = {
            key: [p for p in album["photos"] if selection.matches_orientation(p, options["orientation"])]
            for key, album in albums.items()
        }
        if not any(photos_by_album.values()):
            raise RuntimeError("No photos found in the selected Google Photos albums.")

        state_path = self._cache_path("state", f"{_digest('|'.join(sorted(albums)))}.json")
        state = _read_json(state_path) or {}
        try:
            image = self._render(albums, photos_by_album, options, state, dimensions, now, tz)
        finally:
            _write_json(state_path, state)
        return image

    # ------------------------------------------------------------------ settings

    def _options(self, settings, albums_cfg):
        def choice(key, allowed, default):
            value = settings.get(key)
            return value if value in allowed else default

        return {
            "order": choice("order", ORDERS, "shuffle"),
            "auto_shares": settings.get("albumSharesAuto", "true") != "false",
            "shares": {_digest(link): share for link, share in albums_cfg},
            "memory_share": min(100.0, max(0.0, self._float(settings.get("memoryShare"), 0, allow_zero=True))),
            "memory_type": choice("memoryType", (selection.MEMORY_MONTH, selection.MEMORY_DAY),
                                  selection.MEMORY_MONTH),
            "memory_window": int(self._float(settings.get("memoryWindowDays"), 3, allow_zero=True)),
            "orientation": choice("orientationFilter", selection.ORIENTATIONS, "any"),
            "pairs": settings.get("pairPhotos") == "true",
            "fill": settings.get("fitMode") == "fill",
            "background": choice("backgroundOption", ("blur", "color"), "blur"),
            "background_color": settings.get("backgroundColor") or "#ffffff",
            "caption": choice("caption", ("none", "date", "date_album"), "none"),
            "caption_years_ago": settings.get("captionYearsAgo") == "true",
            "caption_position": choice("captionPosition", ("bottom_left", "bottom_right"), "bottom_left"),
            "language": settings.get("captionLanguage") or "en",
        }

    # ------------------------------------------------------------------ selection + rendering

    def _render(self, albums, photos_by_album, options, state, dimensions, now, tz):
        memory_key = selection.memory_pool_key(now, options["memory_type"], options["memory_window"])
        memories_by_album = {
            key: [p for p in photos if selection.is_memory(p, now, options["memory_type"],
                                                           options["memory_window"], tz)]
            for key, photos in photos_by_album.items()
        }

        for _ in range(DOWNLOAD_ATTEMPTS):
            pick = self._draw(photos_by_album, memories_by_album, memory_key, options, state)
            if pick is None:
                break
            album_key, pool, pool_key, photo = pick
            partner = None
            if options["pairs"] and self._needs_partner(photo, dimensions):
                partner = self._draw_partner(album_key, pool, pool_key, photo, options, state)

            if partner is not None:
                slot = layout.pair_slot_size(dimensions)
                images = [self._get_photo(p, slot, True) for p in (photo, partner)]
                if all(img is not None for img in images):
                    image, regions = layout.compose_pair(images[0], images[1], dimensions,
                                                         options["background_color"])
                    for p, region in zip((photo, partner), regions):
                        image = self._caption(image, p, albums[album_key], region, options, now, tz)
                    return image
            image = self._get_photo(photo, dimensions, options["fill"])
            if image is not None:
                image = layout.frame(image, dimensions, options["fill"], options["background"],
                                     options["background_color"])
                region = (0, 0, dimensions[0], dimensions[1])
                return self._caption(image, photo, albums[album_key], region, options, now, tz)

        # Offline or Google unreachable: show a previously downloaded photo of these albums.
        for key, photos in photos_by_album.items():
            for photo in photos:
                path = self._photo_path(photo, dimensions, options["fill"])
                if os.path.exists(path):
                    image = self._open_photo(path, dimensions)
                    if image is not None:
                        logger.warning("Google Photos: download failed, showing a cached photo")
                        image = layout.frame(image, dimensions, options["fill"], options["background"],
                                             options["background_color"])
                        return self._caption(image, photo, albums[key], (0, 0, *dimensions), options, now, tz)
        raise RuntimeError("Could not download any photo from Google Photos.")

    def _draw(self, photos_by_album, memories_by_album, memory_key, options, state):
        """Returns (album_key, pool photos, pool_key, photo) for the next photo to show."""
        has_memories = any(memories_by_album.values())
        share = options["memory_share"]
        pool_kind = "all"
        if has_memories and share > 0:
            pool_kind = selection.weighted_pick({"memory": share, "all": 100 - share},
                                                state.setdefault("pool_rr", {}))
        pools = memories_by_album if pool_kind == "memory" else photos_by_album
        pool_key = memory_key if pool_kind == "memory" else f"all-{options['order']}"
        order = "shuffle" if pool_kind == "memory" else options["order"]

        counts = {key: len(photos) for key, photos in pools.items()}
        weights = selection.album_weights(list(pools), counts, options["shares"], options["auto_shares"])
        album_key = selection.weighted_pick(weights, state.setdefault("album_rr", {}).setdefault(pool_kind, {}))
        if album_key is None:
            return None

        bags = state.setdefault("bags", {})
        bag = bags.setdefault(f"{album_key}|{pool_key}", [])
        photo = selection.next_from_bag(bag, pools[album_key], order, state.get("last"))
        if photo is None:
            return None
        state["last"] = photo["id"]
        # Drop bags of past months/days so the state file stays small.
        for stale in [k for k in bags if k.split("|", 1)[1] not in (pool_key, f"all-{options['order']}", memory_key)]:
            del bags[stale]
        return album_key, pools[album_key], pool_key, photo

    def _draw_partner(self, album_key, pool, pool_key, photo, options, state):
        wanted = selection.orientation(photo)
        bag = state["bags"].setdefault(f"{album_key}|{pool_key}", [])
        return selection.next_from_bag(
            bag, pool, "shuffle", photo["id"],
            predicate=lambda p: p["id"] != photo["id"] and selection.orientation(p) == wanted)

    @staticmethod
    def _needs_partner(photo, dimensions):
        display_landscape = dimensions[0] >= dimensions[1]
        found = selection.orientation(photo)
        return found == ("portrait" if display_landscape else "landscape")

    def _caption(self, image, photo, album, region, options, now, tz):
        text = layout.caption_text(selection.photo_datetime(photo, tz), album.get("title"),
                                   options["caption"], options["caption_years_ago"], now, options["language"])
        return layout.draw_caption(image, text, region, options["caption_position"])

    # ------------------------------------------------------------------ albums

    def _load_albums(self, links, refresh_hours):
        albums = {}
        for link in links:
            try:
                albums[_digest(link)] = self._get_album(link, refresh_hours)
            except Exception as e:
                logger.error(f"Google Photos: cannot read album {self._album_label(link)}: {e}")
        return albums

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
        album["link"] = link
        _write_json(cache_path, album)
        logger.info(f"Google Photos: album '{album['title']}' has {len(album['photos'])} photos"
                    + ("" if album["complete"] else " (partial)"))
        return album

    def _known_albums(self):
        """{link: {title, count}} of albums read at least once, shown in the settings page."""
        known = {}
        for path in glob.glob(self._cache_path("albums", "*.json")):
            album = _read_json(path)
            if album and album.get("link"):
                known[album["link"]] = {"title": album.get("title", ""), "count": len(album.get("photos", []))}
        return known

    def _album_label(self, link):
        cached = _read_json(self._cache_path("albums", f"{_digest(link)}.json"))
        return f"'{cached['title']}'" if cached and cached.get("title") else f"#{_digest(link)[:8]}"

    # ------------------------------------------------------------------ photos

    def _photo_path(self, photo, size, fill):
        tag = f"{size[0]}x{size[1]}{'c' if fill else ''}"
        return self._cache_path("photos", f"{_digest(photo['id'])}_{tag}.jpg")

    def _get_photo(self, photo, size, fill):
        path = self._photo_path(photo, size, fill)
        if not os.path.exists(path):
            if not photo["url"].startswith(IMAGE_URL_PREFIX):
                return None
            # Google resizes server-side: '=wW-hH' fits in the box, '-c' crops to fill it.
            url = f"{photo['url']}=w{size[0]}-h{size[1]}{'-c' if fill else ''}"
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
        return self._open_photo(path, size)

    def _open_photo(self, path, size):
        image = self.image_loader.from_file(path, size, resize=False)
        if image is None:
            try:
                os.remove(path)  # corrupted download
            except OSError:
                pass
        return image

    def _prune_photo_cache(self):
        files = glob.glob(self._cache_path("photos", "*.jpg"))
        files.sort(key=os.path.getmtime, reverse=True)
        for old in files[PHOTO_CACHE_SIZE:]:
            try:
                os.remove(old)
            except OSError:
                pass

    # ------------------------------------------------------------------ helpers

    def _cache_path(self, *parts):
        return os.path.join(self.get_plugin_dir("cache"), *parts)

    @staticmethod
    def _timezone(device_config):
        try:
            return pytz.timezone(device_config.get_config("timezone", default="UTC") or "UTC")
        except pytz.UnknownTimeZoneError:
            return pytz.utc

    @staticmethod
    def _float(value, default, allow_zero=False):
        try:
            value = float(value)
            return value if value > 0 or (allow_zero and value == 0) else default
        except (TypeError, ValueError):
            return default
