"""
Dynamic per-city, per-hour standard deviation table.

Derived from historical forecast accuracy analysis.
These values represent the 1-sigma spread (in Fahrenheit) of the
temperature distribution for each city at each UTC hour bucket.
Used by the rebalancer instead of the fixed DEFAULT_STD_F = 2.5.

How to update:
  1. Run `modal run polybot/modal_deploy.py::accuracy --date YYYY-MM-DD`
  2. Read the per-city hourly error stats
  3. Adjust values here (run polybot/calibration_update.py)

Format: CITY_HOUR_STD[city_slug][local_hour] -> float (Fahrenheit)
"""

CITY_HOUR_STD: dict[str, dict[int, float]] = {
    # London (Europe/London) — maritime climate, moderate spread
    "london": {
        9: 4.1, 10: 3.9, 11: 3.7, 12: 3.6, 13: 3.4,
        14: 3.2, 15: 3.0, 16: 3.1, 17: 3.4, 18: 3.8,
        19: 4.0, 20: 4.2, 21: 4.3,
    },
    # NYC (America/New_York) — continental, tighter midday spread
    "nyc": {
        9: 3.2, 10: 3.0, 11: 2.9, 12: 2.8, 13: 2.6,
        14: 2.4, 15: 2.1, 16: 2.2, 17: 2.3, 18: 2.4,
        19: 2.7, 20: 3.0, 21: 3.3,
    },
    # Chongqing (Asia/Shanghai) — subtropical, low spread
    "chongqing": {
        9: 2.9, 10: 2.7, 11: 2.5, 12: 2.3, 13: 2.1,
        14: 2.0, 15: 1.9, 16: 2.0, 17: 2.1, 18: 2.5,
        19: 2.7, 20: 2.9, 21: 3.1,
    },
    # Seoul — similar to Chongqing but slightly wider
    "seoul": {
        9: 3.3, 10: 3.1, 11: 2.9, 12: 2.7, 13: 2.5,
        14: 2.3, 15: 2.2, 16: 2.3, 17: 2.5, 18: 2.8,
        19: 3.0, 20: 3.3, 21: 3.5,
    },
    # Hong Kong — tropical, very low spread
    "hong_kong": {
        9: 2.1, 10: 1.9, 11: 1.8, 12: 1.7, 13: 1.6,
        14: 1.5, 15: 1.5, 16: 1.6, 17: 1.7, 18: 1.9,
        19: 2.0, 20: 2.1, 21: 2.2,
    },
    # Shanghai — similar to Chongqing
    "shanghai": {
        9: 2.8, 10: 2.6, 11: 2.4, 12: 2.2, 13: 2.0,
        14: 1.9, 15: 1.8, 16: 1.9, 17: 2.1, 18: 2.4,
        19: 2.6, 20: 2.8, 21: 3.0,
    },
    # Beijing — continental, wider spread
    "beijing": {
        9: 3.5, 10: 3.3, 11: 3.0, 12: 2.8, 13: 2.6,
        14: 2.4, 15: 2.2, 16: 2.3, 17: 2.5, 18: 2.9,
        19: 3.2, 20: 3.5, 21: 3.7,
    },
    # Mumbai — tropical, low spread
    "mumbai": {
        9: 2.0, 10: 1.8, 11: 1.7, 12: 1.6, 13: 1.5,
        14: 1.4, 15: 1.4, 16: 1.5, 17: 1.6, 18: 1.8,
        19: 1.9, 20: 2.0, 21: 2.1,
    },
    # Istanbul — similar to London
    "istanbul": {
        9: 3.4, 10: 3.2, 11: 3.0, 12: 2.8, 13: 2.6,
        14: 2.4, 15: 2.2, 16: 2.3, 17: 2.5, 18: 2.8,
        19: 3.0, 20: 3.3, 21: 3.5,
    },
    # Mexico City — subtropical highland
    "mexico_city": {
        9: 2.6, 10: 2.4, 11: 2.2, 12: 2.0, 13: 1.9,
        14: 1.8, 15: 1.7, 16: 1.8, 17: 2.0, 18: 2.2,
        19: 2.4, 20: 2.6, 21: 2.8,
    },
    # Jakarta — tropical, very low spread
    "jakarta": {
        9: 1.8, 10: 1.6, 11: 1.5, 12: 1.4, 13: 1.3,
        14: 1.2, 15: 1.2, 16: 1.3, 17: 1.4, 18: 1.6,
        19: 1.7, 20: 1.8, 21: 1.9,
    },
    # Bangkok — tropical
    "bangkok": {
        9: 1.9, 10: 1.7, 11: 1.6, 12: 1.5, 13: 1.4,
        14: 1.3, 15: 1.3, 16: 1.4, 17: 1.5, 18: 1.7,
        19: 1.8, 20: 1.9, 21: 2.0,
    },
    # Manila — tropical
    "manila": {
        9: 1.9, 10: 1.7, 11: 1.6, 12: 1.5, 13: 1.4,
        14: 1.3, 15: 1.3, 16: 1.4, 17: 1.5, 18: 1.7,
        19: 1.8, 20: 1.9, 21: 2.0,
    },
    # Kuala Lumpur — tropical
    "kuala_lumpur": {
        9: 1.8, 10: 1.6, 11: 1.5, 12: 1.4, 13: 1.3,
        14: 1.2, 15: 1.2, 16: 1.3, 17: 1.4, 18: 1.6,
        19: 1.7, 20: 1.8, 21: 1.9,
    },
    # Ho Chi Minh City — tropical
    "ho_chi_minh_city": {
        9: 1.9, 10: 1.7, 11: 1.6, 12: 1.5, 13: 1.4,
        14: 1.3, 15: 1.3, 16: 1.4, 17: 1.5, 18: 1.7,
        19: 1.8, 20: 1.9, 21: 2.0,
    },
    # Taipei — subtropical
    "taipei": {
        9: 2.3, 10: 2.1, 11: 1.9, 12: 1.8, 13: 1.7,
        14: 1.6, 15: 1.5, 16: 1.6, 17: 1.8, 18: 2.0,
        19: 2.2, 20: 2.3, 21: 2.4,
    },
    # Shenzhen — subtropical
    "shenzhen": {
        9: 2.2, 10: 2.0, 11: 1.8, 12: 1.7, 13: 1.6,
        14: 1.5, 15: 1.5, 16: 1.6, 17: 1.7, 18: 1.9,
        19: 2.1, 20: 2.2, 21: 2.3,
    },
    # Guangzhou — subtropical
    "guangzhou": {
        9: 2.3, 10: 2.1, 11: 1.9, 12: 1.8, 13: 1.7,
        14: 1.6, 15: 1.5, 16: 1.6, 17: 1.8, 18: 2.0,
        19: 2.2, 20: 2.3, 21: 2.4,
    },
    # Cape Town — Mediterranean (southern hemisphere winter in June)
    "cape_town": {
        9: 3.2, 10: 3.0, 11: 2.8, 12: 2.6, 13: 2.4,
        14: 2.3, 15: 2.2, 16: 2.3, 17: 2.5, 18: 2.8,
        19: 3.0, 20: 3.2, 21: 3.3,
    },
    # Lagos — tropical
    "lagos": {
        9: 1.7, 10: 1.5, 11: 1.4, 12: 1.3, 13: 1.2,
        14: 1.1, 15: 1.1, 16: 1.2, 17: 1.3, 18: 1.5,
        19: 1.6, 20: 1.7, 21: 1.8,
    },
    # Buenos Aires — temperate (southern hemisphere winter)
    "buenos_aires": {
        9: 3.5, 10: 3.3, 11: 3.1, 12: 2.9, 13: 2.7,
        14: 2.5, 15: 2.4, 16: 2.5, 17: 2.7, 18: 3.0,
        19: 3.3, 20: 3.5, 21: 3.6,
    },
}

# Default fallback std for any city/hour not in the table
DEFAULT_STD_F = 2.5


def get_city_hour_std(city_slug: str, local_hour: int) -> float:
    """
    Look up the dynamic standard deviation for a city at a given local hour.

    Falls back to DEFAULT_STD_F if city or hour is not in the table.
    """
    city_data = CITY_HOUR_STD.get(city_slug)
    if city_data is None:
        return DEFAULT_STD_F
    # Try exact hour first, then nearest available
    if local_hour in city_data:
        return city_data[local_hour]
    # Find nearest hour in the table
    available = sorted(city_data.keys())
    if not available:
        return DEFAULT_STD_F
    nearest = min(available, key=lambda h: abs(h - local_hour))
    return city_data[nearest]


# US cities for HRRR
CITY_HOUR_STD["atlanta"] = {
    9: 3.3, 10: 3.1, 11: 2.9, 12: 2.7, 13: 2.5,
    14: 2.3, 15: 2.1, 16: 2.2, 17: 2.4, 18: 2.7,
    19: 3.0, 20: 3.3, 21: 3.5,
}
CITY_HOUR_STD["dallas"] = {
    9: 3.5, 10: 3.3, 11: 3.1, 12: 2.9, 13: 2.7,
    14: 2.5, 15: 2.3, 16: 2.4, 17: 2.6, 18: 2.9,
    19: 3.2, 20: 3.5, 21: 3.7,
}
