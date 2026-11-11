import os
import sys
from datetime import datetime, timedelta
from unittest import mock

import icalendar
import pytest
import pytz
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from plugins.calendar.calendar import Calendar  # noqa: E402
from plugins.weather import weather as weather_module  # noqa: E402
from plugins.weather.weather import Weather  # noqa: E402

TZ = pytz.timezone("Europe/Paris")


# ---------------------------------------------------------------- calendar

def _cal(body):
    return icalendar.Calendar.from_ical(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\n" + body + "END:VCALENDAR\n")


@pytest.fixture
def calendar_plugin(tmp_path, monkeypatch):
    p = Calendar({"id": "calendar"})
    monkeypatch.setattr(p, "get_plugin_dir", lambda path=None: str(tmp_path / (path or "")))
    return p


def _events(plugin, cal, start, end):
    with mock.patch.object(plugin, "fetch_calendar", return_value=cal):
        return plugin.fetch_ics_events(["u"], ["#ff0000"], TZ, start, end)


def test_event_just_after_local_midnight_is_kept(calendar_plugin):
    cal = _cal("BEGIN:VEVENT\nUID:1\nDTSTART:20261110T233000Z\nDTEND:20261110T234500Z\n"
               "SUMMARY:minuit\nEND:VEVENT\n")
    start = TZ.localize(datetime(2026, 11, 11))
    events = _events(calendar_plugin, cal, start, start + timedelta(weeks=5))
    assert [e["start"] for e in events] == ["2026-11-11T00:30:00+01:00"]


def test_floating_times_use_configured_timezone(calendar_plugin, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    cal = _cal("BEGIN:VEVENT\nUID:1\nDTSTART:20261112T090000\nDTEND:20261112T100000\n"
               "SUMMARY:cours\nEND:VEVENT\n")
    start = TZ.localize(datetime(2026, 11, 11))
    events = _events(calendar_plugin, cal, start, start + timedelta(weeks=1))
    assert events[0]["start"] == "2026-11-12T09:00:00+01:00"
    assert events[0]["end"] == "2026-11-12T10:00:00+01:00"


def test_date_value_with_tzid_stays_all_day(calendar_plugin):
    cal = _cal("BEGIN:VEVENT\nUID:1\nDTSTART;TZID=Europe/Paris;VALUE=DATE:20261114\n"
               "DTEND;TZID=Europe/Paris;VALUE=DATE:20261115\nSUMMARY:conge\nEND:VEVENT\n"
               "BEGIN:VEVENT\nUID:2\nDTSTART;VALUE=DATE:20261116\nRRULE:FREQ=DAILY;COUNT=2\n"
               "SUMMARY:recurrent\nEND:VEVENT\n")
    start = TZ.localize(datetime(2026, 11, 11))
    cal = Calendar._normalize_date_values(cal)
    events = sorted(_events(calendar_plugin, cal, start, start + timedelta(weeks=2)), key=lambda e: e["start"])
    assert [(e["title"], e["start"], e["allDay"]) for e in events] == [
        ("conge", "2026-11-14", True), ("recurrent", "2026-11-16", True), ("recurrent", "2026-11-17", True)]
    assert events[0]["end"] == "2026-11-15"


# ---------------------------------------------------------------- weather

@pytest.fixture
def weather_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(weather_module, "RETRY_BACKOFF_SECONDS", 0)
    p = Weather({"id": "weather"})
    monkeypatch.setattr(p, "get_plugin_dir", lambda path=None: str(tmp_path / (path or "")))
    return p


def _resp(status=200, payload=None, text=""):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload
    r.text = text
    return r


def test_fetch_retries_then_succeeds(weather_plugin):
    responses = [_resp(429, text='{"reason":"The service is overloaded"}'), _resp(200, {"ok": 1})]
    with mock.patch.object(weather_module.requests, "get", side_effect=responses) as get:
        assert weather_plugin._fetch_json("https://api/x", "test") == {"ok": 1}
    assert get.call_count == 2


def test_fetch_falls_back_to_cache(weather_plugin):
    with mock.patch.object(weather_module.requests, "get", return_value=_resp(200, {"v": 42})):
        weather_plugin._fetch_json("https://api/x", "test")
    with mock.patch.object(weather_module.requests, "get",
                           side_effect=requests.exceptions.ReadTimeout("timeout")):
        assert weather_plugin._fetch_json("https://api/x", "test") == {"v": 42}


def test_fetch_without_cache_raises(weather_plugin):
    with mock.patch.object(weather_module.requests, "get",
                           side_effect=requests.exceptions.ConnectionError("down")):
        with pytest.raises(RuntimeError):
            weather_plugin._fetch_json("https://api/never", "test")


def test_optional_fetch_returns_empty_on_error(weather_plugin):
    def boom(*a):
        raise RuntimeError("aqi down")
    assert weather_plugin._optional(boom, 1, 2) == {}


def test_open_meteo_times_are_read_in_configured_timezone(weather_plugin, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    dt = weather_plugin._parse_local("2026-11-11T07:53", TZ)
    assert dt.hour == 7 and dt.utcoffset() == timedelta(hours=1)


def _hourly(now, **series):
    base = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    times = [(base + timedelta(hours=h)).isoformat(timespec="minutes") for h in range(24)]
    return {"time": times, **series}


def test_data_points_tolerate_missing_and_null_values(weather_plugin):
    now = datetime.now(TZ)
    weather_data = {
        "daily": {"sunrise": [], "sunset": []},
        "current": {"windspeed": 3, "winddirection": 90},
        "hourly": _hourly(now, relative_humidity_2m=[None] * 24, surface_pressure=[1013.6] * 24),
    }
    points = weather_plugin.parse_open_meteo_data_points(weather_data, {}, "metric", TZ, "24h")
    by_label = {p["label"]: p["measurement"] for p in points}
    assert by_label["Humidity"] == "N/A"
    assert by_label["Pressure"] == 1014
    assert by_label["Visibility"] == "N/A"
    assert by_label["UV Index"] == "N/A"
    assert by_label["Air Quality"] == "N/A"


def test_forecast_high_low_are_rounded(weather_plugin):
    daily = {"time": ["2026-11-11"], "weathercode": [1],
             "temperature_2m_max": [12.9], "temperature_2m_min": [4.5]}
    forecast = weather_plugin.parse_open_meteo_forecast(daily, "metric", TZ, 1, 44.0)
    assert forecast[0]["high"] == 13


# ---------------------------------------------------------------- clock

def test_word_clock_minute_33_uses_next_hour():
    from plugins.clock.clock import Clock
    at_32 = Clock.translate_word_grid_positions(2, 32)
    at_33 = Clock.translate_word_grid_positions(2, 33)
    TWO, THREE = [6, 8], [5, 6]
    assert TWO in at_32 and THREE not in at_32
    assert THREE in at_33 and TWO not in at_33
