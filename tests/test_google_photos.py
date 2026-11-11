import json
import os
import sys
import time
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


# ---------------------------------------------------------------- plugin

class Cfg:
    def get_resolution(self):
        return (800, 480)

    def get_config(self, key, default=None):
        return {"orientation": "horizontal"}.get(key, default)


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


def _album(n, taken_base=0):
    return {"title": "A", "complete": True,
            "photos": [{"id": f"id{i}", "url": f"{IMG}p{i}", "width": 1, "height": 1,
                        "taken": taken_base + i} for i in range(n)]}


def _ok_download(url, **kw):
    return mock.Mock(status_code=200, content=_jpeg(), headers={"Content-Type": "image/jpeg"},
                     raise_for_status=mock.Mock())


SETTINGS = {"albumURLs[]": ["https://photos.app.goo.gl/a"], "order": "shuffle"}


def _shown_ids(plugin, settings, n):
    """Renders n times and returns the photo shown each time (from the rotation state)."""
    shown = []
    with mock.patch.object(gp_module.requests, "get", side_effect=_ok_download):
        for _ in range(n):
            assert plugin.generate_image(settings, Cfg()).size == (800, 480)
            state_dir = plugin._cache_path("state")
            state = json.load(open(os.path.join(state_dir, os.listdir(state_dir)[0])))
            shown.append("p" + state["last"][2:])
    return shown


def test_shuffle_shows_every_photo_once_before_repeating(plugin):
    with mock.patch.object(gp_module, "fetch_album", return_value=_album(5)):
        shown = _shown_ids(plugin, SETTINGS, 5)
    assert sorted(shown) == [f"p{i}" for i in range(5)]


def test_newest_order_and_multiple_albums_deduplicated(plugin):
    settings = {**SETTINGS, "albumURLs[]": ["https://photos.app.goo.gl/a", "https://photos.app.goo.gl/b"],
                "order": "newest"}
    albums = {"https://photos.app.goo.gl/a": _album(3), "https://photos.app.goo.gl/b": _album(4)}
    with mock.patch.object(gp_module, "fetch_album", side_effect=lambda s, link: albums[link]):
        shown = _shown_ids(plugin, settings, 5)
    assert shown == ["p3", "p2", "p1", "p0", "p3"]


def test_requested_size_matches_display_and_framing(plugin):
    with mock.patch.object(gp_module, "fetch_album", return_value=_album(1)), \
         mock.patch.object(gp_module.requests, "get", side_effect=_ok_download) as get:
        assert plugin.generate_image({**SETTINGS, "fitMode": "fill"}, Cfg()).size == (800, 480)
    assert get.call_args.args[0].endswith("=w800-h480-c")


def test_album_list_cached_and_used_when_google_fails(plugin):
    with mock.patch.object(gp_module, "fetch_album", return_value=_album(2)) as fetch:
        _shown_ids(plugin, SETTINGS, 2)
    assert fetch.call_count == 1  # second render used the cached list
    album_file = os.path.join(plugin._cache_path("albums"), os.listdir(plugin._cache_path("albums"))[0])
    os.utime(album_file, (0, 0))
    data = json.load(open(album_file)); data["fetched_at"] = 0; json.dump(data, open(album_file, "w"))
    with mock.patch.object(gp_module, "fetch_album", side_effect=AlbumReadError("format changed")):
        assert _shown_ids(plugin, SETTINGS, 1)[0] in ("p0", "p1")


def test_offline_falls_back_to_cached_photo(plugin):
    with mock.patch.object(gp_module, "fetch_album", return_value=_album(3)):
        _shown_ids(plugin, SETTINGS, 1)
        with mock.patch.object(gp_module.requests, "get", side_effect=requests.ConnectionError("offline")):
            assert plugin.generate_image(SETTINGS, Cfg()).size == (800, 480)


def test_no_valid_link_raises(plugin):
    with pytest.raises(RuntimeError):
        plugin.generate_image({"albumURLs[]": ["https://example.com/x"]}, Cfg())


def test_photo_cache_is_bounded(plugin, monkeypatch):
    monkeypatch.setattr(gp_module, "PHOTO_CACHE_SIZE", 3)
    with mock.patch.object(gp_module, "fetch_album", return_value=_album(6)):
        _shown_ids(plugin, SETTINGS, 6)
    assert len(os.listdir(plugin._cache_path("photos"))) == 3
