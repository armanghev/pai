"""Open-Meteo forecast (no API key). The city's coordinates are looked up once and cached."""

from __future__ import annotations

import json
import sqlite3

import httpx

from pai import db

GEO = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"

CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow", 80: "showers",
    81: "showers", 82: "heavy showers", 95: "thunderstorms", 96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}  # fmt: skip


async def coords(conn: sqlite3.Connection, city: str, http: httpx.AsyncClient) -> dict:
    key = f"geo:{city.lower()}"
    cached = db.kv_get(conn, key)
    if cached:
        return json.loads(cached)
    r = await http.get(GEO, params={"name": city, "count": 1})
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        raise LookupError(f"couldn't find a place called {city!r}")
    place = {k: results[0][k] for k in ("latitude", "longitude", "name")}
    db.kv_set(conn, key, json.dumps(place))
    return place


async def forecast(conn: sqlite3.Connection, city: str, tz: str, units: str, days: int = 1) -> dict:
    async with httpx.AsyncClient(timeout=20) as http:
        place = await coords(conn, city, http)
        r = await http.get(
            FORECAST,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "timezone": tz,
                "temperature_unit": units,
                "forecast_days": max(1, min(days, 7)),
                "current": "temperature_2m,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max",
            },
        )
        r.raise_for_status()
    data = r.json()
    unit = "°F" if units == "fahrenheit" else "°C"
    daily = data["daily"]
    return {
        "place": place["name"],
        "now": f"{round(data['current']['temperature_2m'])}{unit}, "
        f"{CODES.get(data['current']['weather_code'], 'unknown')}",
        "days": [
            {
                "date": daily["time"][i],
                "summary": CODES.get(daily["weather_code"][i], "unknown"),
                "high": f"{round(daily['temperature_2m_max'][i])}{unit}",
                "low": f"{round(daily['temperature_2m_min'][i])}{unit}",
                "rain_chance": f"{daily['precipitation_probability_max'][i] or 0}%",
            }
            for i in range(len(daily["time"]))
        ],
    }


def one_line(fc: dict) -> str:
    d = fc["days"][0]
    return f"{d['summary'].capitalize()}, {d['low']}–{d['high']}, {d['rain_chance']} rain"
