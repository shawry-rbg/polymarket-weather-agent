"""
One-shot test: Austin + Hong Kong live scan.
"""

import asyncio
import json
import re
import sys

sys.path.insert(0, "/workspaces/polymarket-weather-agent")

import httpx


async def main():
    # 1. Find temperature markets for Austin and Hong Kong
    print("=" * 70)
    print("POLYBOT v2 -- Live Market Scan (Austin + Hong Kong)")
    print("=" * 70)

    # Scan events for temperature markets
    target_cities = ["austin", "hong kong"]
    found_markets = {c: [] for c in target_cities}

    print("\n[1] Scanning Polymarket Gamma API for temperature markets...")
    for offset in range(0, 2000, 100):
        r = httpx.get(
            f"https://poly-proxy.elvischemoiywo.workers.dev/gamma/events",
            params={
                "active": "true", "closed": "false", "limit": 100, "offset": offset,
            },
            timeout=15,
        )
        events = r.json()
        if not events:
            break

        found_any = False
        for event in events:
            title = event.get("title", "").lower()
            if "temperature" in title:
                # Extract city
                cm = re.search(r"in\s+(.+?)\s+on\s+", title)
                city = cm.group(1).strip().lower() if cm else ""
                date_match = re.search(r"on\s+(.+?)\??", title)
                date = date_match.group(1).strip() if date_match else ""

                for target in target_cities:
                    if target in city:
                        found_any = True
                        for m in event.get("markets", []):
                            q = m.get("question", "")
                            try:
                                p = json.loads(m.get("outcomePrices", "[]"))
                            except:
                                p = []
                            # Extract threshold
                            tm = re.search(r"(\d+)\s*°?\s*([Cc])", q)
                            threshold_c = int(tm.group(1)) if tm else None
                            threshold_f = round(threshold_c * 9 / 5 + 32) if threshold_c else None
                            fu = tm.group(2).upper() if tm else ""

                            found_markets[target].append({
                                "question": q[:100],
                                "prices": p,
                                "threshold_c": threshold_c,
                                "threshold_f": threshold_f,
                                "threshold_unit": fu,
                                "date": date,
                                "conditionId": m.get("conditionId", ""),
                                "slug": m.get("slug", ""),
                                "volume24h": float(m.get("volume24hr", 0) or 0),
                            })

        # Early exit if we found both cities
        if all(len(v) > 0 for v in found_markets.values()):
            break

        if offset % 500 == 0:
            counts = {k: len(v) for k, v in found_markets.items()}
            print(f"  Offset {offset}: {counts}")

    for city, markets in found_markets.items():
        print(f"\n[2] {city.upper()}: Found {len(markets)} markets")
        # Show most interesting (price closest to 50/50)
        interesting = []
        for m in markets:
            if m["prices"]:
                try:
                    yes_p = float(m["prices"][0])
                    if 0.01 < yes_p < 0.99:
                        interesting.append((m, abs(yes_p - 0.5)))
                except:
                    pass
        interesting.sort(key=lambda x: -x[1])
        for m, _ in interesting[:5]:
            yes_p = float(m["prices"][0])
            print(f"  [{m['threshold_c']}°{m['threshold_unit']}] YES={yes_p:.3f} | {m['question'][:70]}")

    # 2. Get weather data
    print("\n[3] Fetching weather data...")
    from polybot.cities import CITY_INDEX

    for city_name in ["Austin", "Hong Kong"]:
        slug = city_name.lower().replace(" ", "-")
        city_obj = CITY_INDEX.get(slug)
        if not city_obj:
            print(f"  {city_name}: No coordinates")
            continue

        # Current + forecast from Open-Meteo
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": city_obj.lat, "longitude": city_obj.lon,
                    "current": "temperature_2m",
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "temperature_unit": "fahrenheit",
                    "timezone": "auto",
                    "forecast_days": 1,
                },
            )
            if r.status_code == 200:
                data = r.json()
                current = data.get("current", {}).get("temperature_2m")
                high = data.get("daily", {}).get("temperature_2m_max", [None])[0]
                low = data.get("daily", {}).get("temperature_2m_min", [None])[0]
                print(f"\n  {city_name}:")
                print(f"    Current: {current}°F")
                print(f"    Daily High: {high}°F  Low: {low}°F")

        # Get YES price for the most relevant market
        city_markets = found_markets.get(city_name.lower(), [])
        if city_markets:
            # Find market with threshold closest to forecast high
            best_market = None
            best_diff = float("inf")
            for m in city_markets:
                if m["threshold_f"] and high:
                    diff = abs(m["threshold_f"] - high)
                    if diff < best_diff:
                        best_diff = diff
                        best_market = m
            if best_market:
                print(f"    Closest market: {best_market['question'][:70]}")
                print(f"    Market threshold: {best_market['threshold_f']}°F (vs forecast {high}°F)")
                if best_market["prices"]:
                    yes_p = float(best_market["prices"][0])
                    print(f"    YES price: {yes_p:.4f}  NO price: {float(best_market['prices'][1]):.4f}")
                    # Edge calculation
                    if high and best_market["threshold_f"]:
                        diff = high - best_market["threshold_f"]
                        print(f"    Forecast vs threshold: {diff:+.1f}F")
                        if diff > 0:
                            print(f"    >>> EDGE: Forecast {high}F > threshold {best_market['threshold_f']}F = BUY YES")
                        else:
                            print(f"    >>> EDGE: Forecast {high}F < threshold {best_market['threshold_f']}F = BUY NO")

    print("\n" + "=" * 70)
    print("Scan complete.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
