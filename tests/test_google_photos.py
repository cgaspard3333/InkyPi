import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone as dt_timezone
from unittest import mock

import pytest
import requests
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from plugins.google_photos import album_reader  # noqa: E402
from plugins.google_photos import google_photos as gp_module  # noqa: E402
from plugins.google_photos.album_reader import (AlbumReadError, fetch_album,  # noqa: E402
                                                normalize_album_link, parse_album_page,
                                                parse_batch_response)
from plugins.google_photos.google_photos import GooglePhotos  # noqa: E402
from plugins.google_photos import layout, selection  # noqa: E402

IMG = "https://lh3.googleusercontent.com/pw/"


def _item(i, video=False, taken=None):
    extras = {"15": 1}
    if video:
        extras["76647426"] = [1]
    return [f"id{i}", [f"{IMG}photo{i}", 4000, 3000, None], taken or 1000 + i, "x", 0, 1, [], 2, extras]


def _page(items, token="", title="Vacances", key="KEY123"):
    data = [None, items, token, ["ALBUMID", title, [1, 2]], None, 0]
    return (f'<html><link rel="canonical" href="https://photos.google.com/share/ALBUMID?key={key}">'
            f"<script>AF_initDataCallback({{key: 'ds:1', hash: '2', data:{json.dumps(data)}"
            f", sideChannel: {{}}}});</script></html>")


def _batch(items, token=""):
    inner = json.dumps([None, items, token])
    return ")]}'\n\n123\n" + json.dumps([["wrb.fr", "snAcKc", inner, None, None, None, "generic"]]) + "\n"


# ---------------------------------------------------------------- album reader

@pytest.mark.parametrize("text,expected", [
    ("https://photos.app.goo.gl/abc123", "https://photos.app.goo.gl/abc123"),
    ("photos.app.goo.gl/abc123 ", "https://photos.app.goo.gl/abc123"),
    ("https://photos.google.com/share/AF1Q?key=K", "https://photos.google.com/share/AF1Q?key=K"),
    ("https://photos.google.com/album/AF1Q", None),       # private album link, not shared
    ("https://evil.example/photos.app.goo.gl/x", None),
    ("", None), (None, None),
])
def test_normalize_album_link(text, expected):
    assert normalize_album_link(text) == expected


def test_parse_album_page_skips_videos_and_bad_items():
    items = [_item(1), _item(2, video=True), ["bad"], ["id3", ["https://evil.example/x", 1, 1]], _item(4)]
    album = parse_album_page(_page(items, token="TOK"))
    assert [p["id"] for p in album["photos"]] == ["id1", "id4"]
    assert album["title"] == "Vacances" and album["key"] == "KEY123" and album["token"] == "TOK"
    assert album["photos"][0]["width"] == 4000


@pytest.mark.parametrize("html", ["<html>nothing</html>",
                                  "AF_initDataCallback({key: 'ds:1', data:[1,2, sideChannel: {}});",
                                  _page([]).replace('"ALBUMID"', "1")])
def test_parse_album_page_rejects_unexpected_pages(html):
    with pytest.raises(AlbumReadError):
        parse_album_page(html)


def test_parse_batch_response():
    items, token = parse_batch_response(_batch([_item(7)], "NEXT"))
    assert items[0][0] == "id7" and token == "NEXT"
    with pytest.raises(AlbumReadError):
        parse_batch_response("garbage")


class FakeSession:
    def __init__(self, page, batches=(), final_url="https://photos.google.com/share/ALBUMID?key=KEY123"):
        self.page, self.batches, self.final_url, self.posts = page, list(batches), final_url, []

    def get(self, url, **kw):
        return mock.Mock(status_code=200, text=self.page, url=self.final_url)

    def post(self, url, data=None, **kw):
        self.posts.append(data)
        if not self.batches:
            return mock.Mock(status_code=500, text="")
        return mock.Mock(status_code=200, text=self.batches.pop(0))


def test_fetch_album_follows_pages():
    session = FakeSession(_page([_item(1)], token="T1"), [_batch([_item(2)], "T2"), _batch([_item(3)], "")])
    album = fetch_album(session, "https://photos.app.goo.gl/x")
    assert [p["id"] for p in album["photos"]] == ["id1", "id2", "id3"] and album["complete"]
    assert "KEY123" in session.posts[0] and "T1" in session.posts[0]


def test_fetch_album_keeps_first_page_when_paging_fails():
    album = fetch_album(FakeSession(_page([_item(1)], token="T1")), "https://photos.app.goo.gl/x")
    assert [p["id"] for p in album["photos"]] == ["id1"] and not album["complete"]


def test_fetch_album_rejects_unexpected_redirect():
    with pytest.raises(AlbumReadError):
        fetch_album(FakeSession(_page([]), final_url="https://accounts.google.com/login"),
                    "https://photos.app.goo.gl/x")


# ---------------------------------------------------------------- selection

TZ = __import__("pytz").timezone("Europe/Paris")


def _ms(y, m, d, h=12):
    return int(datetime(y, m, d, h, tzinfo=dt_timezone.utc).timestamp() * 1000)


def test_weighted_pick_matches_shares_evenly():
    state, picks = {}, []
    for _ in range(12):
        picks.append(selection.weighted_pick({"a": 75, "b": 25}, state))
    assert Counter(picks) == {"a": 9, "b": 3}
    assert "bbb" not in "".join(picks) and "aaaa" not in "".join(picks)


def test_album_weights_auto_manual_and_fallback():
    counts = {"a": 300, "b": 100, "c": 0}
    assert selection.album_weights(["a", "b", "c"], counts, {}, auto=True) == {"a": 300, "b": 100}
    assert selection.album_weights(["a", "b", "c"], counts, {"a": 20, "b": 80, "c": 50}, auto=False) == \
        {"a": 20, "b": 80}  # c has no photo in this pool
    assert selection.album_weights(["a", "b"], counts, {"a": 0, "b": 0}, auto=False) == {"a": 300, "b": 100}


def test_auto_shares_sum_to_100():
    assert sum(selection.auto_shares({"a": 1, "b": 1, "c": 1}).values()) == 100
    assert selection.auto_shares({"a": 300, "b": 100}) == {"a": 75, "b": 25}


def test_memories_month_and_day():
    now = TZ.localize(datetime(2026, 11, 11, 9))
    this_month_2019 = {"taken": _ms(2019, 11, 25)}
    this_month_now = {"taken": _ms(2026, 11, 2)}
    october = {"taken": _ms(2019, 10, 31)}
    assert selection.is_memory(this_month_2019, now, "month", 3, TZ)
    assert selection.is_memory(this_month_now, now, "month", 3, TZ)        # current year included
    assert not selection.is_memory(october, now, "month", 3, TZ)
    assert selection.is_memory({"taken": _ms(2020, 11, 13)}, now, "day", 3, TZ)
    assert not selection.is_memory({"taken": _ms(2020, 11, 20)}, now, "day", 3, TZ)
    assert not selection.is_memory({"taken": _ms(2026, 11, 10)}, now, "day", 3, TZ)  # past years only
    new_year = TZ.localize(datetime(2027, 1, 1, 9))
    assert selection.is_memory({"taken": _ms(2024, 12, 30)}, new_year, "day", 3, TZ)  # wraps around


def test_bag_never_repeats_before_full_round():
    photos = [{"id": f"id{i}"} for i in range(5)]
    bag, seen = [], []
    for _ in range(10):
        seen.append(selection.next_from_bag(bag, photos, "shuffle", seen[-1] if seen else None)["id"])
    assert sorted(seen[:5]) == sorted(seen[5:]) == [f"id{i}" for i in range(5)]
    assert seen[4] != seen[5]


def test_caption_text():
    now = TZ.localize(datetime(2026, 11, 11))
    taken = TZ.localize(datetime(2019, 11, 25))
    assert layout.caption_text(taken, "Vacances", "date_album", True, now, "fr") == \
        "Il y a 7 ans · Novembre 2019 · Vacances"
    assert layout.caption_text(taken, "Vacances", "date", False, now, "en") == "November 2019"
    assert layout.caption_text(None, "Vacances", "date_album", True, now, "fr") == "Vacances"
    assert layout.caption_text(taken, "Vacances", "none", True, now, "fr") == ""


# ---------------------------------------------------------------- plugin

class Cfg:
    def get_resolution(self):
        return (800, 480)

    def get_config(self, key, default=None):
        return {"orientation": "horizontal", "timezone": "Europe/Paris"}.get(key, default)


def _jpeg(w=640, h=480):
    from io import BytesIO
    buf = BytesIO()
    Image.new("RGB", (w, h), "green").save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    p = GooglePhotos({"id": "google_photos"})
    monkeypatch.setattr(p, "get_plugin_dir", lambda path=None: str(tmp_path / (path or "")))
    return p


def _album(prefix, n, taken=None, size=(4000, 3000), title="A"):
    return {"title": title, "complete": True,
            "photos": [{"id": f"{prefix}{i}", "url": f"{IMG}{prefix}{i}", "width": size[0], "height": size[1],
                        "taken": taken(i) if taken else _ms(2015, 1, 1) + i} for i in range(n)]}


def _ok_download(url, **kw):
    return mock.Mock(status_code=200, content=_jpeg(), headers={"Content-Type": "image/jpeg"},
                     raise_for_status=mock.Mock())


A, B = "https://photos.app.goo.gl/a", "https://photos.app.goo.gl/b"
SETTINGS = {"albumURLs[]": [A], "order": "shuffle"}


def _albums(mapping):
    return mock.patch.object(gp_module, "fetch_album", side_effect=lambda s, link: mapping[link])


def _state(plugin):
    state_dir = plugin._cache_path("state")
    return json.load(open(os.path.join(state_dir, os.listdir(state_dir)[0])))


def _shown(plugin, settings, n, cfg=None):
    shown = []
    with mock.patch.object(gp_module.requests, "get", side_effect=_ok_download):
        for _ in range(n):
            assert plugin.generate_image(settings, cfg or Cfg()).size == (800, 480)
            shown.append(_state(plugin)["last"])
    return shown


def test_shuffle_shows_every_photo_once_before_repeating(plugin):
    with _albums({A: _album("a", 5)}):
        shown = _shown(plugin, SETTINGS, 5)
    assert sorted(shown) == [f"a{i}" for i in range(5)]


def test_manual_album_shares_are_respected(plugin):
    settings = {"albumURLs[]": [A, B], "albumShares[]": ["75", "25"], "albumSharesAuto": "false"}
    with _albums({A: _album("a", 3), B: _album("b", 30)}):
        shown = _shown(plugin, settings, 8)
    assert Counter(p[0] for p in shown) == {"a": 6, "b": 2}


def test_auto_album_shares_follow_photo_counts(plugin):
    settings = {"albumURLs[]": [A, B], "albumShares[]": ["50", "50"], "albumSharesAuto": "true"}
    with _albums({A: _album("a", 30), B: _album("b", 10)}):
        shown = _shown(plugin, settings, 8)
    assert Counter(p[0] for p in shown) == {"a": 6, "b": 2}


def test_memory_mix_and_fallback(plugin):
    november = lambda i: _ms(2018 + i, 11, 5)   # noqa: E731
    march = lambda i: _ms(2018 + i, 3, 5)       # noqa: E731
    mixed = {"title": "M", "complete": True,
             "photos": _album("nov", 2, november)["photos"] + _album("mar", 6, march)["photos"]}
    with mock.patch.object(gp_module, "datetime") as fake_dt:
        fake_dt.now.return_value = TZ.localize(datetime(2026, 11, 11, 9))
        with _albums({A: mixed}):
            half = _shown(plugin, {**SETTINGS, "memoryShare": "50"}, 4)
            assert sum(p.startswith("nov") for p in half) == 2
            only = _shown(plugin, {**SETTINGS, "memoryShare": "100"}, 3)
            assert all(p.startswith("nov") for p in only)
        with _albums({A: _album("mar", 4, march)}):
            plugin2 = GooglePhotos({"id": "google_photos"})
            plugin2.get_plugin_dir = lambda path=None: plugin.get_plugin_dir("other")
            assert len(_shown(plugin2, {**SETTINGS, "memoryShare": "100"}, 2)) == 2  # no memory: random


def test_orientation_filter(plugin):
    album = {"title": "A", "complete": True,
             "photos": _album("land", 2)["photos"] + _album("port", 2, size=(3000, 4000))["photos"]}
    with _albums({A: album}):
        shown = _shown(plugin, {**SETTINGS, "orientationFilter": "landscape"}, 4)
    assert all(p.startswith("land") for p in shown)


def test_portrait_photos_are_paired_side_by_side(plugin):
    with _albums({A: _album("p", 4, size=(3000, 4000))}), \
         mock.patch.object(gp_module.requests, "get", side_effect=_ok_download) as get:
        image = plugin.generate_image({**SETTINGS, "pairPhotos": "true", "caption": "date"}, Cfg())
    assert image.size == (800, 480)
    urls = [c.args[0] for c in get.call_args_list]
    assert len(urls) == 2 and all(u.endswith("=w397-h480-c") for u in urls)
    assert len({u.split("=")[0] for u in urls}) == 2


def test_requested_size_matches_display_and_framing(plugin):
    with _albums({A: _album("a", 1)}), mock.patch.object(gp_module.requests, "get", side_effect=_ok_download) as get:
        assert plugin.generate_image({**SETTINGS, "fitMode": "fill"}, Cfg()).size == (800, 480)
    assert get.call_args.args[0].endswith("=w800-h480-c")


def test_album_list_cached_and_used_when_google_fails(plugin):
    with _albums({A: _album("a", 2)}) as fetch:
        _shown(plugin, SETTINGS, 2)
    assert fetch.call_count == 1  # second render used the cached list
    album_file = os.path.join(plugin._cache_path("albums"), os.listdir(plugin._cache_path("albums"))[0])
    data = json.load(open(album_file)); data["fetched_at"] = 0; json.dump(data, open(album_file, "w"))
    with mock.patch.object(gp_module, "fetch_album", side_effect=AlbumReadError("format changed")):
        assert _shown(plugin, SETTINGS, 1)[0] in ("a0", "a1")


def test_settings_page_knows_album_titles_and_counts(plugin):
    with _albums({A: _album("a", 7, title="Vacances")}):
        _shown(plugin, SETTINGS, 1)
    assert plugin._known_albums() == {A: {"title": "Vacances", "count": 7}}


def test_offline_falls_back_to_cached_photo(plugin):
    with _albums({A: _album("a", 3)}):
        _shown(plugin, SETTINGS, 1)
        with mock.patch.object(gp_module.requests, "get", side_effect=requests.ConnectionError("offline")):
            assert plugin.generate_image(SETTINGS, Cfg()).size == (800, 480)


def test_no_valid_link_raises(plugin):
    with pytest.raises(RuntimeError):
        plugin.generate_image({"albumURLs[]": ["https://example.com/x"]}, Cfg())


def test_photo_cache_is_bounded(plugin, monkeypatch):
    monkeypatch.setattr(gp_module, "PHOTO_CACHE_SIZE", 3)
    with _albums({A: _album("a", 6)}):
        _shown(plugin, SETTINGS, 6)
    assert len(os.listdir(plugin._cache_path("photos"))) == 3
