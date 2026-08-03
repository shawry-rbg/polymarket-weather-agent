"""City definitions with bucket configs for the Polymarket weather bot.

Each city has:
  - name: human-readable name
  - lat, lon: geographic coordinates
  - unit: temperature unit for buckets ("C" or "F")
  - buckets: list of integer temperature thresholds (e.g. market will exceed X)
  - bias: degrees to add to forecast before computing probability (+/-)
  - source: weather data source identifier
  - reserve: whether this city is a reserve (not actively traded)
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote
from typing import Any

# ---------------------------------------------------------------------------
# Global city registry
# ---------------------------------------------------------------------------

global_cities: dict[str, dict[str, Any]] = {
    # --- Existing active cities ---
    "london": {
        "name": "London",
        "slug": "london",
        "lat": 51.4700,
        "lon": -0.4543,
        "unit": "C",
        "buckets": list(range(24, 33)),
        "bias": +0.5,
        "source": "openmeteo",
        "reserve": True,  # Rotated out (poor paper P&L)
    },
    "nyc": {
        "name": "New York",
        "slug": "nyc",
        "lat": 40.7772,
        "lon": -73.8726,
        "unit": "F",
        "buckets": list(range(65, 96)),
        "bias": +1.4,
        "source": "wethr",
        "reserve": True,  # Rotated out (poor paper P&L)
    },
    "seoul": {
        "name": "Seoul",
        "slug": "seoul",
        "lat": 37.4602,   # Incheon International Airport (ICN)
        "lon": 126.4407,
        "icao": "RKSI",
        "unit": "C",
        "buckets": list(range(25, 34)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": True,  # Temporarily excluded until calibration improves
        "reliable_months": [6, 7, 8],
    },
    "hong_kong": {
        "name": "Hong Kong",
        "slug": "hong_kong",
        "lat": 22.3080,   # Hong Kong International Airport (HKG)
        "lon": 113.9185,
        "icao": "VHHH",
        "unit": "C",
        "buckets": list(range(28, 38)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
        "reliable_months": [6, 7, 8, 9],
    },
    "shanghai": {
        "name": "Shanghai",
        "slug": "shanghai",
        "lat": 31.1443,   # Pudong International Airport (PVG)
        "lon": 121.8083,
        "icao": "ZSPD",
        "unit": "C",
        "buckets": list(range(28, 37)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": True,  # Temporarily excluded until autumn
        "reliable_months": [],
    },
    "beijing": {
        "name": "Beijing",
        "slug": "beijing",
        "lat": 40.0799,   # Beijing Capital International Airport (PEK)
        "lon": 116.5877,
        "icao": "ZBAA",
        "unit": "C",
        "buckets": list(range(28, 37)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": True,  # Temporarily excluded until autumn
        "reliable_months": [],
    },
    "mumbai": {
        "name": "Mumbai",
        "slug": "mumbai",
        "lat": 19.0896,   # Chhatrapati Shivaji International Airport (BOM)
        "lon": 72.8656,
        "icao": "VABB",
        "unit": "C",
        "buckets": list(range(30, 38)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
        "reliable_months": [5, 6, 7, 8, 9],
    },
    "istanbul": {
        "name": "Istanbul",
        "slug": "istanbul",
        "lat": 41.0082,
        "lon": 28.9784,
        "unit": "C",
        "buckets": list(range(24, 33)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    "mexico_city": {
        "name": "Mexico City",
        "slug": "mexico_city",
        "lat": 19.4326,
        "lon": -99.1332,
        "unit": "F",
        "buckets": list(range(65, 96)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    "jakarta": {
        "name": "Jakarta",
        "slug": "jakarta",
        "lat": -6.2088,
        "lon": 106.8456,
        "unit": "C",
        "buckets": list(range(30, 37)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    # --- New Asian cities ---
    "chongqing": {
        "name": "Chongqing",
        "slug": "chongqing",
        "lat": 29.7192,   # Chongqing Jiangbei International Airport (CKG)
        "lon": 106.6418,
        "icao": "ZUCK",
        "unit": "C",
        "buckets": list(range(23, 29)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": True,  # Temporarily excluded until calibration improves
        "reliable_months": [],
    },
    "bangkok": {
        "name": "Bangkok",
        "slug": "bangkok",
        "lat": 13.7367,
        "lon": 100.5231,
        "unit": "C",
        "buckets": list(range(32, 38)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    "manila": {
        "name": "Manila",
        "slug": "manila",
        "lat": 14.5995,
        "lon": 120.9842,
        "unit": "C",
        "buckets": list(range(32, 38)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    "kuala_lumpur": {
        "name": "Kuala Lumpur",
        "slug": "kuala_lumpur",
        "lat": 3.1390,
        "lon": 101.6869,
        "unit": "C",
        "buckets": list(range(31, 37)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    "ho_chi_minh_city": {
        "name": "Ho Chi Minh City",
        "slug": "ho_chi_minh_city",
        "lat": 10.8231,
        "lon": 106.6297,
        "unit": "C",
        "buckets": list(range(32, 38)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    "taipei": {
        "name": "Taipei",
        "slug": "taipei",
        "lat": 25.0330,
        "lon": 121.5654,
        "unit": "C",
        "buckets": list(range(32, 38)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    "shenzhen": {
        "name": "Shenzhen",
        "slug": "shenzhen",
        "lat": 22.5431,
        "lon": 114.0579,
        "unit": "C",
        "buckets": list(range(30, 36)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    "guangzhou": {
        "name": "Guangzhou",
        "slug": "guangzhou",
        "lat": 23.1291,
        "lon": 113.2644,
        "unit": "C",
        "buckets": list(range(31, 37)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    # --- US cities for HRRR ---
    "atlanta": {
        "name": "Atlanta",
        "slug": "atlanta",
        "lat": 33.6407,   # Hartsfield-Jackson Atlanta International Airport (ATL)
        "lon": -84.4277,
        "icao": "KATL",
        "unit": "F",
        "buckets": list(range(65, 96)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    "dallas": {
        "name": "Dallas",
        "slug": "dallas",
        "lat": 32.8998,   # Dallas/Fort Worth International Airport (DFW)
        "lon": -97.0403,
        "icao": "KDFW",
        "unit": "F",
        "buckets": list(range(65, 96)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    "houston": {
        "name": "Houston",
        "slug": "houston",
        "lat": 29.9844,   # George Bush Intercontinental Airport (IAH)
        "lon": -95.3414,
        "icao": "KIAH",
        "unit": "F",
        "buckets": list(range(65, 96)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    "miami": {
        "name": "Miami",
        "slug": "miami",
        "lat": 25.7959,   # Miami International Airport (MIA)
        "lon": -80.2870,
        "icao": "KMIA",
        "unit": "F",
        "buckets": list(range(65, 96)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    # --- Reserve cities ---
    "cape_town": {
        "name": "Cape Town",
        "slug": "cape_town",
        "lat": -33.9715,  # Cape Town International Airport (CPT)
        "lon": 18.6021,
        "icao": "FACT",
        "unit": "C",
        "buckets": list(range(22, 29)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,
    },
    "lagos": {
        "name": "Lagos",
        "slug": "lagos",
        "lat": 6.5244,
        "lon": 3.3792,
        "unit": "C",
        "buckets": list(range(30, 36)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,  # Activated June 2026
    },
    "buenos_aires": {
        "name": "Buenos Aires",
        "slug": "buenos_aires",
        "lat": -34.8222,
        "lon": -58.5358,
        "unit": "C",
        "buckets": list(range(22, 29)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": False,  # Activated June 2026
    },
    "dubai": {
        "name": "Dubai",
        "slug": "dubai",
        "lat": 25.2048,
        "lon": 55.2708,
        "unit": "C",
        "buckets": list(range(38, 46)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": True,
    },
    "singapore": {
        "name": "Singapore",
        "slug": "singapore",
        "lat": 1.3521,
        "lon": 103.8198,
        "unit": "C",
        "buckets": list(range(31, 37)),
        "bias": 0.0,
        "source": "openmeteo",
        "reserve": True,
    },
}

# ---------------------------------------------------------------------------
# Lookup indexes
# ---------------------------------------------------------------------------

# Slug-indexed lookup:  CITY_INDEX["london"] -> city dict
CITY_INDEX: dict[str, dict[str, Any]] = {slug: city for slug, city in global_cities.items()}

# Same alias for backward compat
SLUG_INDEX: dict[str, dict[str, Any]] = CITY_INDEX

# All cities (reserve flag no longer filters -- scan everything)
ACTIVE_CITIES: list[dict[str, Any]] = list(global_cities.values())

# Reserve cities only
RESERVE_CITIES: list[dict[str, Any]] = [c for c in global_cities.values() if c.get("reserve")]

# Flat list for backward compat with code that iterates CITIES
CITIES: list[dict[str, Any]] = list(global_cities.values())


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------


def open_meteo_url(city: dict[str, Any], days: int = 1) -> str:
    """Return the Open-Meteo forecast API URL for *city*."""
    return (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={city['lat']}"
        f"&longitude={city['lon']}"
        "&daily=temperature_2m_max"
        "&timezone=auto"
        f"&forecast_days={days}"
    )


def wethr_url(city: dict[str, Any]) -> str:
    """Return the wethr.net page URL for *city*."""
    slug = city.get("slug", city["name"].lower().replace(" ", "-"))
    return f"https://wethr.net/{quote(slug)}"


# ---------------------------------------------------------------------------
# Bucket threshold boundaries (Fahrenheit) for crossing detection
# These are the temperature values that separate adjacent buckets.
# For a city with buckets [22C, 23C, 24C] the boundaries are [71.6, 73.4, 75.2].
# ---------------------------------------------------------------------------

BUCKET_THRESHOLDS_F: dict[str, list[float]] = {
    # London: 16-30C markets (60.8-86F), boundaries at 22C=71.6F, 23C=73.4F, 24C=75.2F
    "london":       [71.6, 73.4, 75.2],
    # NYC: 65-95F markets, boundaries every 2F
    "nyc":          [67.0, 69.0, 71.0, 73.0, 75.0, 77.0, 79.0, 81.0, 83.0, 85.0, 87.0, 89.0, 91.0, 93.0],
    # Asian cities (Celsius buckets): 25-38C = 77-100.4F
    "seoul":        [71.6, 73.4, 75.2, 77.0, 78.8, 80.6, 82.4],
    "hong_kong":    [80.6, 82.4, 84.2, 86.0, 87.8, 89.6],
    "shanghai":     [80.6, 82.4, 84.2, 86.0, 87.8, 89.6],
    "beijing":      [80.6, 82.4, 84.2, 86.0, 87.8, 89.6],
    "mumbai":       [86.0, 87.8, 89.6, 91.4, 93.2],
    "istanbul":     [71.6, 73.4, 75.2, 77.0, 78.8, 80.6, 82.4],
    "mexico_city":  [67.0, 69.0, 71.0, 73.0, 75.0, 77.0, 79.0, 81.0, 83.0, 85.0, 87.0, 89.0, 91.0, 93.0],
    "jakarta":      [86.0, 87.8, 89.6, 91.4],
    "chongqing":    [71.6, 73.4, 75.2, 77.0, 78.8],
    "bangkok":      [89.6, 91.4, 93.2, 95.0],
    "manila":       [89.6, 91.4, 93.2, 95.0],
    "kuala_lumpur": [87.8, 89.6, 91.4, 93.2],
    "ho_chi_minh_city": [89.6, 91.4, 93.2, 95.0],
    "taipei":       [89.6, 91.4, 93.2, 95.0],
    "shenzhen":     [82.4, 84.2, 86.0, 87.8],
    "guangzhou":    [87.8, 89.6, 91.4, 93.2],
    # US cities for HRRR
    "atlanta":      [67.0, 69.0, 71.0, 73.0, 75.0, 77.0, 79.0, 81.0, 83.0, 85.0, 87.0, 89.0, 91.0, 93.0],
    "dallas":       [67.0, 69.0, 71.0, 73.0, 75.0, 77.0, 79.0, 81.0, 83.0, 85.0, 87.0, 89.0, 91.0, 93.0],
    # Reserve cities
    "cape_town":    [71.6, 73.4, 75.2, 77.0],
    "lagos":        [86.0, 87.8, 89.6, 91.4],
    "buenos_aires": [71.6, 73.4, 75.2, 77.0],
    "dubai":        [100.4, 102.2, 104.0, 105.8],
    "singapore":    [87.8, 89.6, 91.4, 93.2],
}

# ---------------------------------------------------------------------------
# Timezone-aware market date detection
# ---------------------------------------------------------------------------

CITY_TIMEZONES: dict[str, str] = {
    "london": "Europe/London",
    "nyc": "America/New_York",
    "seoul": "Asia/Seoul",
    "hong_kong": "Asia/Hong_Kong",
    "shanghai": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "mumbai": "Asia/Kolkata",
    "istanbul": "Europe/Istanbul",
    "mexico_city": "America/Mexico_City",
    "jakarta": "Asia/Jakarta",
    "chongqing": "Asia/Shanghai",
    "bangkok": "Asia/Bangkok",
    "manila": "Asia/Manila",
    "kuala_lumpur": "Asia/Kuala_Lumpur",
    "ho_chi_minh_city": "Asia/Ho_Chi_Minh",
    "taipei": "Asia/Taipei",
    "shenzhen": "Asia/Shanghai",
    "guangzhou": "Asia/Shanghai",
    "atlanta": "America/New_York",
    "dallas": "America/Chicago",
    "cape_town": "Africa/Johannesburg",
    "lagos": "Africa/Lagos",
    "buenos_aires": "America/Argentina/Buenos_Aires",
    "dubai": "Asia/Dubai",
    "singapore": "Asia/Singapore",
}


def get_local_date(city_slug: str, offset_days: int = 0) -> str:
    """
    Get the current local date for a city, optionally shifted by offset_days.

    Args:
        city_slug: City identifier (e.g. "london", "nyc")
        offset_days: Number of days to shift (0 = today, 1 = tomorrow)

    Returns:
        Date string in YYYY-MM-DD format
    """
    import datetime
    tz_name = CITY_TIMEZONES.get(city_slug, "UTC")
    try:
        import pytz
        tz = pytz.timezone(tz_name)
        now = datetime.datetime.now(tz)
    except Exception:
        now = datetime.datetime.utcnow()
    if offset_days:
        now += datetime.timedelta(days=offset_days)
    return now.strftime("%Y-%m-%d")


def get_local_datetime(city_slug: str) -> "datetime.datetime":
    """Get the current local datetime for a city."""
    import datetime
    tz_name = CITY_TIMEZONES.get(city_slug, "UTC")
    try:
        import pytz
        tz = pytz.timezone(tz_name)
        return datetime.datetime.now(tz)
    except Exception:
        return datetime.datetime.utcnow()


# ---------------------------------------------------------------------------
# Meta: expose file path for loaders that need it
# ---------------------------------------------------------------------------

CITIES_FILE: Path = Path(__file__).resolve()
