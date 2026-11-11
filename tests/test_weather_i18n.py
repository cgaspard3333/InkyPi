import os
import sys
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from plugins.weather import i18n  # noqa: E402


@pytest.mark.parametrize("lang,expected", [
    ("en", "Wednesday, November 11"),
    ("fr", "mercredi 11 novembre"),
    ("de", "Mittwoch, 11. November"),
    ("es", "miércoles, 11 de noviembre"),
    ("it", "mercoledì 11 novembre"),
])
def test_long_date(lang, expected):
    assert i18n.format_long_date(datetime(2026, 11, 11, 17, 0), lang) == expected


def test_language_normalization():
    assert i18n.normalize_language("fr-FR") == "fr"
    assert i18n.normalize_language(None) == "en"
    assert i18n.normalize_language("xx") == "en"


def test_all_languages_have_complete_tables():
    keys = set(i18n.STRINGS["fr"])
    for lang in i18n.LANGUAGES:
        assert len(i18n.WEEKDAYS[lang]) == 7 and len(i18n.WEEKDAYS_SHORT[lang]) == 7
        assert len(i18n.MONTHS[lang]) == 12
        assert lang in i18n.DATE_FORMATS
        if lang != "en":
            assert set(i18n.STRINGS[lang]) == keys


def test_localize_template_params_french():
    params = {
        "current_dt": datetime(2026, 11, 11, 17, 0),
        "current_date": "Wednesday, November 11",
        "forecast": [{"day": "Thu", "date": date(2026, 11, 12)}],
        "data_points": [
            {"label": "Wind", "measurement": 3, "unit": "m/s"},
            {"label": "Air Quality", "measurement": 20, "unit": "Fair"},
            {"label": "Visibility", "measurement": "N/A", "unit": "km"},
        ],
    }
    i18n.localize_template_params(params, "fr")
    assert params["current_date"] == "mercredi 11 novembre"
    assert params["forecast"][0]["day"] == "Jeu"
    wind, aqi, vis = params["data_points"]
    assert (wind["key"], wind["label"], wind["unit"]) == ("Wind", "Vent", "m/s")
    assert (aqi["label"], aqi["unit"]) == ("Qualité de l'air", "Correct")
    assert vis["measurement"] == "N/D"
    assert params["text"]["Feels Like"] == "Ressenti"


def test_english_is_unchanged():
    params = {"current_dt": datetime(2026, 11, 11), "data_points": [{"label": "Wind", "unit": "m/s"}]}
    i18n.localize_template_params(params, "en")
    assert params["current_date"] == "Wednesday, November 11"
    assert params["data_points"][0]["label"] == "Wind"
