# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Destination-intelligence tools backed by free, keyless public APIs.

* Open-Meteo geocoding + forecast  (https://open-meteo.com)
* Nager.Date public holidays       (https://date.nager.at)
* Frankfurter ECB exchange rates   (https://frankfurter.dev)

Every tool follows the same contract so the LLM can reason about outcomes:
    {"status": "success", ...payload}   or
    {"status": "error", "error_message": "<actionable explanation>"}
Tools never raise; failures become data the model can recover from.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from google.adk.tools import ToolContext

from app.tools._http import ExternalAPIError, get_json

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CLIMATE_URL = "https://climate-api.open-meteo.com/v1/climate"
HOLIDAYS_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/{country}"
FX_URL = "https://api.frankfurter.dev/v1/latest"

# WMO weather interpretation codes -> human text (subset covering all groups).
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
    55: "dense drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "heavy showers", 82: "violent showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}  # fmt: skip


def _err(msg: str) -> dict[str, Any]:
    return {"status": "error", "error_message": msg}


async def geocode_destination(destination: str, tool_context: ToolContext) -> dict:
    """Resolves a destination name to coordinates, country and timezone.

    Call this first whenever you need precise location data for a city or
    region. The result is cached in session state so later tools (weather,
    holidays) can reuse it without another lookup.

    Args:
        destination: City or place name, e.g. "Kyoto" or "Lisbon, Portugal".

    Returns:
        dict with status, name, country, country_code, latitude, longitude,
        timezone and population; or status=error with an explanation.
    """
    name = (destination or "").split(",")[0].strip()
    if len(name) < 2:
        return _err("Destination name is too short. Ask the traveler to clarify.")

    cache: dict = tool_context.state.get("geo_cache", {})
    if name.lower() in cache:
        return {"status": "success", "cached": True, **cache[name.lower()]}

    try:
        data = await get_json(GEOCODE_URL, {"name": name, "count": 1})
    except ExternalAPIError as exc:
        return _err(str(exc))
    results = (data or {}).get("results") or []
    if not results:
        return _err(f"No place named '{destination}' was found. Check spelling.")

    r = results[0]
    place = {
        "name": r.get("name"),
        "region": r.get("admin1"),
        "country": r.get("country"),
        "country_code": r.get("country_code"),
        "latitude": r.get("latitude"),
        "longitude": r.get("longitude"),
        "timezone": r.get("timezone"),
        "population": r.get("population"),
    }
    cache[name.lower()] = place
    tool_context.state["geo_cache"] = cache
    return {"status": "success", "cached": False, **place}


async def get_weather_forecast(
    latitude: float, longitude: float, start_date: str, num_days: int
) -> dict:
    """Gets the daily weather outlook for a location and date range.

    Uses a live 16-day forecast when the dates are near, and falls back to
    climate-model normals for trips further in the future, so it always gives
    the traveler a useful packing/activity signal.

    Args:
        latitude: Latitude from geocode_destination.
        longitude: Longitude from geocode_destination.
        start_date: Trip start date in ISO format YYYY-MM-DD.
        num_days: Number of trip days (1-14).

    Returns:
        dict with status, source ("forecast" or "climate_normals") and a list
        of daily entries with date, high_c, low_c, rain_chance_pct/precip_mm
        and conditions.
    """
    try:
        start = dt.date.fromisoformat(start_date)
    except (TypeError, ValueError):
        return _err("start_date must be ISO format YYYY-MM-DD.")
    if not 1 <= int(num_days) <= 14:
        return _err("num_days must be between 1 and 14.")
    end = start + dt.timedelta(days=int(num_days) - 1)
    today = dt.date.today()

    try:
        if start >= today and end <= today + dt.timedelta(days=15):
            data = await get_json(
                FORECAST_URL,
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "daily": "temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max,weather_code",
                    "timezone": "auto",
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                },
            )
            d = (data or {}).get("daily", {})
            days = [
                {
                    "date": d["time"][i],
                    "high_c": d["temperature_2m_max"][i],
                    "low_c": d["temperature_2m_min"][i],
                    "rain_chance_pct": d["precipitation_probability_max"][i],
                    "conditions": _WMO.get(d["weather_code"][i], "mixed"),
                }
                for i in range(len(d.get("time", [])))
            ]
            return {"status": "success", "source": "forecast", "days": days}

        # Far-future trip: use climate normals from the same calendar window
        # of a reference year (climate API covers 1950-2050).
        ref_year = min(max(start.year, 1951), 2049)
        ref_start = start.replace(year=ref_year)
        ref_end = ref_start + dt.timedelta(days=int(num_days) - 1)
        data = await get_json(
            CLIMATE_URL,
            {
                "latitude": latitude,
                "longitude": longitude,
                "start_date": ref_start.isoformat(),
                "end_date": ref_end.isoformat(),
                "models": "MRI_AGCM3_2_S",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            },
        )
        d = (data or {}).get("daily", {})
        days = [
            {
                "date": (start + dt.timedelta(days=i)).isoformat(),
                "high_c": d["temperature_2m_max"][i],
                "low_c": d["temperature_2m_min"][i],
                "precip_mm": d["precipitation_sum"][i],
                "conditions": "typical seasonal pattern",
            }
            for i in range(len(d.get("time", [])))
        ]
        return {"status": "success", "source": "climate_normals", "days": days}
    except ExternalAPIError as exc:
        return _err(str(exc))
    except (KeyError, IndexError, TypeError):
        return _err("Weather service returned an unexpected payload.")


async def get_public_holidays(
    country_code: str, start_date: str, num_days: int
) -> dict:
    """Lists public holidays that fall inside the trip window.

    Holidays affect opening hours, crowds and prices, so check them before
    scheduling museums, markets or intercity travel.

    Args:
        country_code: ISO-3166 alpha-2 code, e.g. "JP" (from geocode_destination).
        start_date: Trip start date YYYY-MM-DD.
        num_days: Number of trip days.

    Returns:
        dict with status and a list of {date, name, local_name} holidays.
    """
    try:
        start = dt.date.fromisoformat(start_date)
    except (TypeError, ValueError):
        return _err("start_date must be ISO format YYYY-MM-DD.")
    cc = (country_code or "").upper().strip()
    if len(cc) != 2:
        return _err("country_code must be a 2-letter ISO code.")
    end = start + dt.timedelta(days=max(int(num_days), 1) - 1)

    holidays: list[dict] = []
    try:
        for year in sorted({start.year, end.year}):
            data = await get_json(HOLIDAYS_URL.format(year=year, country=cc)) or []
            for h in data:
                day = dt.date.fromisoformat(h["date"])
                if start <= day <= end:
                    holidays.append(
                        {
                            "date": h["date"],
                            "name": h["name"],
                            "local_name": h["localName"],
                        }
                    )
    except ExternalAPIError as exc:
        return _err(str(exc))
    return {"status": "success", "country_code": cc, "holidays": holidays}


async def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict:
    """Converts money between currencies using live ECB reference rates.

    Args:
        amount: Amount to convert (must be positive).
        from_currency: ISO-4217 code of the source currency, e.g. "USD".
        to_currency: ISO-4217 code of the target currency, e.g. "JPY".

    Returns:
        dict with status, converted_amount, rate and rate_date.
    """
    src, dst = (from_currency or "").upper(), (to_currency or "").upper()
    if amount is None or float(amount) <= 0:
        return _err("amount must be a positive number.")
    if len(src) != 3 or len(dst) != 3:
        return _err("Currencies must be 3-letter ISO-4217 codes like USD or EUR.")
    if src == dst:
        return {"status": "success", "converted_amount": round(float(amount), 2),
                "rate": 1.0, "rate_date": dt.date.today().isoformat()}  # fmt: skip
    try:
        data = await get_json(FX_URL, {"base": src, "symbols": dst})
    except ExternalAPIError as exc:
        return _err(str(exc))
    rate = ((data or {}).get("rates") or {}).get(dst)
    if rate is None:
        return _err(f"No exchange rate available for {src}->{dst}.")
    return {
        "status": "success",
        "converted_amount": round(float(amount) * rate, 2),
        "rate": rate,
        "rate_date": data.get("date"),
    }
