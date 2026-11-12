import os
import sys
from unittest import mock

import pytest
from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from model import Playlist, PlaylistManager, PluginInstance  # noqa: E402
from blueprints.playlist import playlist_bp  # noqa: E402


def _playlist(names, index=None, disabled=()):
    plugins = [{"plugin_id": "clock", "name": n, "plugin_settings": {}, "refresh": {"interval": 60},
                "enabled": n not in disabled} for n in names]
    return Playlist("P", "00:00", "24:00", plugins, current_plugin_index=index)


def test_next_plugin_skips_disabled_instances():
    playlist = _playlist(["a", "b", "c"], disabled=("b",))
    assert [playlist.get_next_plugin().name for _ in range(4)] == ["a", "c", "a", "c"]


def test_next_plugin_none_when_all_disabled():
    playlist = _playlist(["a", "b"], index=1, disabled=("a", "b"))
    assert playlist.get_next_plugin() is None
    assert playlist.current_plugin_index == 1
    assert Playlist("E", "00:00", "24:00").get_next_plugin() is None


def test_move_plugin_keeps_rotation_on_same_instance():
    playlist = _playlist(["a", "b", "c"], index=1)  # 'b' currently shown
    assert playlist.move_plugin("clock", "b", -1)
    assert [p.name for p in playlist.plugins] == ["b", "a", "c"]
    assert playlist.plugins[playlist.current_plugin_index].name == "b"
    assert playlist.get_next_plugin().name == "a"
    playlist.move_plugin("clock", "c", 5)  # clamped at the end
    assert [p.name for p in playlist.plugins] == ["b", "a", "c"]
    assert not playlist.move_plugin("clock", "missing", 1)


def test_enabled_round_trips_and_defaults_to_true():
    old = PluginInstance.from_dict({"plugin_id": "x", "name": "n", "plugin_settings": {}, "refresh": {}})
    assert old.enabled is True
    paused = PluginInstance.from_dict({**old.to_dict(), "enabled": False})
    assert paused.enabled is False and paused.to_dict()["enabled"] is False


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(playlist_bp)
    manager = PlaylistManager([_playlist(["a", "b"])])
    config = mock.Mock()
    config.get_playlist_manager.return_value = manager
    app.config["DEVICE_CONFIG"] = config
    return app.test_client(), manager, config


def test_move_and_enable_endpoints(client):
    http, manager, config = client
    body = {"playlist_name": "P", "plugin_id": "clock", "plugin_instance": "b"}
    assert http.post("/move_plugin_instance", json={**body, "direction": "up"}).status_code == 200
    assert [p.name for p in manager.get_playlist("P").plugins] == ["b", "a"]
    assert http.post("/set_plugin_instance_enabled", json={**body, "enabled": False}).status_code == 200
    assert manager.get_playlist("P").find_plugin("clock", "b").enabled is False
    assert config.write_config.call_count == 2


@pytest.mark.parametrize("payload", [
    {"playlist_name": "P", "plugin_id": "clock", "plugin_instance": "a", "direction": "left"},
    {"playlist_name": "nope", "plugin_id": "clock", "plugin_instance": "a", "direction": "up"},
    {"playlist_name": "P", "plugin_id": "clock", "plugin_instance": "zz", "direction": "up"},
])
def test_move_endpoint_rejects_bad_requests(client, payload):
    assert client[0].post("/move_plugin_instance", json=payload).status_code == 400


def test_enable_endpoint_requires_boolean(client):
    body = {"playlist_name": "P", "plugin_id": "clock", "plugin_instance": "a", "enabled": "false"}
    assert client[0].post("/set_plugin_instance_enabled", json=body).status_code == 400


# ---------------------------------------------------------------- skip to next

from refresh_task import RefreshTask  # noqa: E402


class _Cfg:
    def __init__(self, manager):
        self.manager = manager

    def get_config(self, key, default=None):
        return {"timezone": "Europe/Paris", "plugin_cycle_interval_seconds": 3600}.get(key, default)

    def get_playlist_manager(self):
        return self.manager

    def get_refresh_info(self):
        from model import RefreshInfo
        return RefreshInfo(None, None, None, None)


def _task_with(playlists):
    task = RefreshTask(_Cfg(PlaylistManager(playlists)), display_manager=mock.Mock())
    shown = []
    task._perform_refresh = lambda action, dt: shown.append(action.plugin_instance.name)
    return task, shown


def test_display_next_advances_active_playlist_skipping_paused():
    task, shown = _task_with([_playlist(["a", "b", "c"], index=0, disabled=("b",))])
    task.start()
    try:
        task.display_next(timeout=5)
        task.display_next(timeout=5)
    finally:
        task.stop()
    assert shown == ["c", "a"]


@pytest.mark.parametrize("playlists", [
    [],                                                   # no active playlist
    [_playlist(["a"], disabled=("a",))],                  # nothing enabled
])
def test_display_next_errors_are_reported(playlists):
    task, shown = _task_with(playlists)
    task.start()
    try:
        with pytest.raises(RuntimeError):
            task.display_next(timeout=5)
    finally:
        task.stop()
    assert shown == []
def test_reordering_and_rotation_share_a_lock():
    import threading
    from model import Playlist
    playlist = Playlist("P", "00:00", "24:00", plugins=[
        {"plugin_id": "x", "name": n, "plugin_settings": {}, "refresh": {}} for n in ("a", "b", "c")])
    acquired = threading.Event()
    release = threading.Event()

    def hold():
        with Playlist._order_lock:
            acquired.set()
            release.wait(2)

    thread = threading.Thread(target=hold)
    thread.start()
    acquired.wait(2)
    moved = []
    mover = threading.Thread(target=lambda: moved.append(playlist.move_plugin("x", "a", 1)))
    mover.start()
    mover.join(0.2)
    assert mover.is_alive() and not moved      # waits for the rotation to finish
    release.set()
    mover.join(2)
    thread.join(2)
    assert moved == [True]
