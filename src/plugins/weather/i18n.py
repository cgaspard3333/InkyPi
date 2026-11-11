"""Translations for the weather plugin.

Day and month names are embedded instead of relying on the system `locale`
module: locales are usually not generated on Raspberry Pi OS and setlocale()
is process-wide (not thread-safe with the web UI running alongside).
"""

DEFAULT_LANGUAGE = "en"

LANGUAGES = {
    "en": "English",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "it": "Italiano",
}

# Monday first, like datetime.weekday()
WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "fr": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
    "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
    "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
    "it": ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"],
}

WEEKDAYS_SHORT = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "fr": ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"],
    "de": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
    "es": ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"],
    "it": ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"],
}

MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"],
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
           "August", "September", "Oktober", "November", "Dezember"],
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
           "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
    "it": ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
           "agosto", "settembre", "ottobre", "novembre", "dicembre"],
}

# Long date pattern; {weekday}, {day}, {month} are filled in.
DATE_FORMATS = {
    "en": "{weekday}, {month} {day}",
    "fr": "{weekday} {day} {month}",
    "de": "{weekday}, {day}. {month}",
    "es": "{weekday}, {day} de {month}",
    "it": "{weekday} {day} {month}",
}

# Keys are the English labels produced by the parsers.
STRINGS = {
    "fr": {
        "Sunrise": "Lever", "Sunset": "Coucher", "Wind": "Vent", "Humidity": "Humidité",
        "Pressure": "Pression", "UV Index": "Indice UV", "Visibility": "Visibilité",
        "Air Quality": "Qualité de l'air", "Feels Like": "Ressenti", "Last refresh": "Mis à jour",
        "Good": "Bon", "Fair": "Correct", "Moderate": "Moyen", "Poor": "Mauvais",
        "Very Poor": "Très mauvais", "Ext Poor": "Extrême", "N/A": "N/D",
    },
    "de": {
        "Sunrise": "Aufgang", "Sunset": "Untergang", "Wind": "Wind", "Humidity": "Feuchte",
        "Pressure": "Luftdruck", "UV Index": "UV-Index", "Visibility": "Sicht",
        "Air Quality": "Luftqualität", "Feels Like": "Gefühlt", "Last refresh": "Aktualisiert",
        "Good": "Gut", "Fair": "Mäßig", "Moderate": "Mittel", "Poor": "Schlecht",
        "Very Poor": "Sehr schlecht", "Ext Poor": "Extrem", "N/A": "k. A.",
    },
    "es": {
        "Sunrise": "Amanecer", "Sunset": "Atardecer", "Wind": "Viento", "Humidity": "Humedad",
        "Pressure": "Presión", "UV Index": "Índice UV", "Visibility": "Visibilidad",
        "Air Quality": "Calidad del aire", "Feels Like": "Sensación", "Last refresh": "Actualizado",
        "Good": "Buena", "Fair": "Aceptable", "Moderate": "Moderada", "Poor": "Mala",
        "Very Poor": "Muy mala", "Ext Poor": "Extrema", "N/A": "N/D",
    },
    "it": {
        "Sunrise": "Alba", "Sunset": "Tramonto", "Wind": "Vento", "Humidity": "Umidità",
        "Pressure": "Pressione", "UV Index": "Indice UV", "Visibility": "Visibilità",
        "Air Quality": "Qualità dell'aria", "Feels Like": "Percepita", "Last refresh": "Aggiornato",
        "Good": "Buona", "Fair": "Discreta", "Moderate": "Moderata", "Poor": "Scarsa",
        "Very Poor": "Pessima", "Ext Poor": "Estrema", "N/A": "N/D",
    },
}


def normalize_language(language):
    language = (language or DEFAULT_LANGUAGE).lower()
    if language in LANGUAGES:
        return language
    base = language.split("-")[0]
    return base if base in LANGUAGES else DEFAULT_LANGUAGE


def translate(text, language):
    return STRINGS.get(language, {}).get(text, text)


def format_long_date(dt, language):
    language = normalize_language(language)
    return DATE_FORMATS[language].format(
        weekday=WEEKDAYS[language][dt.weekday()],
        day=dt.day,
        month=MONTHS[language][dt.month - 1],
    )


def short_weekday(d, language):
    return WEEKDAYS_SHORT[normalize_language(language)][d.weekday()]


def localize_template_params(params, language):
    """Translate labels and dates of parsed weather data in place (English is the source)."""
    language = normalize_language(language)
    if params.get("current_dt") is not None:
        params["current_date"] = format_long_date(params["current_dt"], language)
    for day in params.get("forecast", []):
        if day.get("date") is not None:
            day["day"] = short_weekday(day["date"], language)
    for dp in params.get("data_points", []):
        dp["key"] = dp.get("label")
        dp["label"] = translate(dp.get("label"), language)
        if isinstance(dp.get("unit"), str):
            dp["unit"] = translate(dp["unit"], language)
        if dp.get("measurement") == "N/A":
            dp["measurement"] = translate("N/A", language)
    params["text"] = {key: translate(key, language) for key in ("Feels Like", "Last refresh")}
    params["language"] = language
    return params
