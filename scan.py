#!/usr/bin/env python3
"""Ryanair deal scanner.

Zoekt goedkope retourcombinaties (twee enkele reizen) vanaf de thuisluchthavens
en schrijft ze naar data/deals.json. Draait in GitHub Actions.
"""

import argparse
import json
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------- Instellingen ----------

HOME = {
    "AMS": "Amsterdam",
    "RTM": "Rotterdam",
    "EIN": "Eindhoven",
    "MST": "Maastricht",
    "NRN": "Weeze",
}
MAX_LEG = 100        # prijs per enkele reis moet hieronder liggen
MAX_TOTAL = 200      # totaalprijs moet hieronder liggen
MIN_DAYS, MAX_DAYS = 1, 7
HORIZON_MONTHS = 6

MIN_INTERVAL = 1.0   # seconden tussen requests
MAX_TRIES = 4        # pogingen per request
ABORT_AFTER = 8      # zoveel mislukte requests op rij = we worden geblokkeerd

ROUTES_URL = "https://www.ryanair.com/api/views/locate/searchWidget/routes/en/airport/{airport}"
FARES_URL = "https://www.ryanair.com/api/farfnd/v4/oneWayFares/{orig}/{dest}/cheapestPerDay"
BOOK_URL = "https://www.ryanair.com/nl/nl/trip/flights/select"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}

OUTPUT = Path(__file__).parent / "data" / "deals.json"


class Blocked(Exception):
    """Ryanair weigert (bijna) alle requests."""


# ---------- HTTP ----------

class Client:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.last_request = 0.0
        self.requests = 0
        self.errors = 0
        self.failed_in_a_row = 0
        self.statuses = Counter()

    def get_json(self, url, params=None, ok_404=False):
        """Geeft de JSON terug, of None als het na alle pogingen niet lukt."""
        problem = "?"
        for attempt in range(1, MAX_TRIES + 1):
            wait = MIN_INTERVAL - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            self.requests += 1
            try:
                resp = self.session.get(url, params=params, timeout=30)
                self.statuses[resp.status_code] += 1
                if resp.status_code == 200:
                    data = resp.json()
                    self.failed_in_a_row = 0
                    return data
                if resp.status_code == 404 and ok_404:
                    self.failed_in_a_row = 0
                    return None
                problem = f"HTTP {resp.status_code}"
            except (requests.RequestException, ValueError) as exc:
                self.statuses["netwerk/json"] += 1
                problem = type(exc).__name__
            if attempt < MAX_TRIES:
                time.sleep(2 ** attempt)  # backoff: 2, 4, 8 seconden

        self.errors += 1
        self.failed_in_a_row += 1
        print(f"  FOUT {url.split('/api/')[-1]} {params or ''}: {problem}", flush=True)
        if self.failed_in_a_row >= ABORT_AFTER:
            raise Blocked(f"{ABORT_AFTER} requests op rij mislukt (laatste: {problem})")
        return None


# ---------- Data ophalen ----------

def fetch_destinations(client):
    """Geeft {thuisluchthaven: {bestemmingscode: {city, country}}}."""
    result = {}
    for home in HOME:
        errors_before = client.errors
        data = client.get_json(ROUTES_URL.format(airport=home), ok_404=True)
        if data is None:
            if client.errors > errors_before:
                # Zonder deze routes zou een hele luchthaven stilletjes ontbreken.
                raise Blocked(f"routes voor {home} konden niet worden opgehaald")
            print(f"{home}: geen Ryanair-vluchten (404), overgeslagen")
            continue
        dests = {}
        for route in data:
            if route.get("operator") != "FR":
                continue
            airport = route["arrivalAirport"]
            if airport["code"] in HOME:
                continue
            dests[airport["code"]] = {
                "city": airport["city"]["name"],
                "country": airport["country"]["name"],
            }
        print(f"{home}: {len(dests)} bestemmingen")
        if dests:
            result[home] = dests
    return result


def months_between(first, last):
    """Eerste dag van elke maand van `first` t/m `last`."""
    months = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        months.append(date(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def fetch_fares(client, orig, dest, months):
    """Geeft {dag: {"dep": ..., "price": ...}} voor de goedkoopste vlucht per dag."""
    fares = {}
    for month in months:
        params = {"outboundMonthOfDate": month.isoformat(), "currency": "EUR"}
        data = client.get_json(FARES_URL.format(orig=orig, dest=dest), params)
        if data is None:
            continue
        for fare in data.get("outbound", {}).get("fares", []):
            price = fare.get("price")
            if not price or fare.get("soldOut") or fare.get("unavailable"):
                continue
            if price.get("currencyCode") != "EUR" or not fare.get("departureDate"):
                continue
            fares[date.fromisoformat(fare["day"])] = {
                "dep": fare["departureDate"][:16],
                "price": float(price["value"]),
            }
    return fares


# ---------- Combineren ----------

def stars_for(total):
    if total < 100:
        return 2
    if total < 150:
        return 1
    return 0


COLUMNS = ["from", "dest", "back", "out", "ret", "days",
           "price_out", "price_back", "total", "stars", "open_jaw"]


def combine(destinations, fares, today, last_out):
    """Geeft een lijst rijen in de volgorde van COLUMNS."""
    deals = []
    for home_out, dests in destinations.items():
        for dest, info in dests.items():
            out_fares = fares.get((home_out, dest), {})
            for home_back in destinations:
                if dest not in destinations[home_back]:
                    continue
                back_fares = fares.get((dest, home_back), {})
                for day_out, out in out_fares.items():
                    if not today <= day_out <= last_out or out["price"] >= MAX_LEG:
                        continue
                    for days in range(MIN_DAYS, MAX_DAYS + 1):
                        day_back = day_out + timedelta(days=days)
                        back = back_fares.get(day_back)
                        if not back or back["price"] >= MAX_LEG:
                            continue
                        total = round(out["price"] + back["price"], 2)
                        if total >= MAX_TOTAL:
                            continue
                        deals.append([
                            home_out, dest, home_back, out["dep"], back["dep"], days,
                            out["price"], back["price"], total, stars_for(total),
                            home_back != home_out,
                        ])
    # Vaste volgorde (route, datum) houdt de git-diffs tussen scans klein;
    # het dashboard sorteert zelf op prijs.
    deals.sort()
    return deals


# ---------- Hoofdprogramma ----------

def add_months(day, months):
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    for last_day in (day.day, 30, 29, 28):
        try:
            return date(year, month, last_day)
        except ValueError:
            continue


def print_blocked_help(client, reason):
    print("\n" + "=" * 70)
    print("GEBLOKKEERD: Ryanair weigert de requests vanaf deze machine.")
    print(f"Reden: {reason}")
    print(f"HTTP-statussen deze run: {dict(client.statuses)}")
    print("data/deals.json is NIET aangepast; het dashboard toont de vorige scan.")
    print("Alternatieven:")
    print("  1. Later opnieuw proberen (blokkades op IP zijn vaak tijdelijk).")
    print("  2. Self-hosted GitHub runner op een eigen verbinding (Raspberry Pi/NAS).")
    print("  3. De scanner via een proxy laten lopen (HTTPS_PROXY als secret).")
    print("  4. De scan elders draaien (bv. Cloudflare Worker of kleine VPS) en")
    print("     alleen het resultaat naar deze repo pushen.")
    print("=" * 70)
    # Zichtbaar als rode melding in de GitHub Actions samenvatting
    print(f"::error title=Ryanair blokkeert de scanner::{reason}. deals.json is niet bijgewerkt.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-routes", type=int, help="alleen de eerste N routes (om te testen)")
    args = parser.parse_args()

    started = time.monotonic()
    today = date.today()
    last_out = add_months(today, HORIZON_MONTHS)
    months = months_between(today, last_out + timedelta(days=MAX_DAYS))
    client = Client()

    try:
        destinations = fetch_destinations(client)
        if not destinations:
            raise Blocked("voor geen enkele thuisluchthaven konden routes worden opgehaald")

        routes = [(home, dest) for home, dests in destinations.items() for dest in dests]
        if args.max_routes:
            routes = routes[:args.max_routes]
            keep = set(routes)
            destinations = {
                home: {d: i for d, i in dests.items() if (home, d) in keep}
                for home, dests in destinations.items()
            }
        print(f"\n{len(routes)} routes, {len(months)} maanden, "
              f"circa {len(routes) * 2 * len(months)} requests\n")

        fares = {}
        for number, (home, dest) in enumerate(routes, 1):
            fares[(home, dest)] = fetch_fares(client, home, dest, months)
            fares[(dest, home)] = fetch_fares(client, dest, home, months)
            print(f"[{number}/{len(routes)}] {home}-{dest}: "
                  f"{len(fares[(home, dest)])} dagen heen, "
                  f"{len(fares[(dest, home)])} dagen terug", flush=True)
    except Blocked as exc:
        print_blocked_help(client, str(exc))
        return 2

    # Veel fouten of nergens een prijs: geen betrouwbare data, dus niet wegschrijven.
    if client.errors > client.requests * 0.25:
        print_blocked_help(client, f"{client.errors} van {client.requests} requests mislukt")
        return 2
    if not any(fares.values()):
        print_blocked_help(client, "geen enkele route gaf een prijs terug")
        return 2

    deals = combine(destinations, fares, today, last_out)
    per_star = Counter(d[COLUMNS.index("stars")] for d in deals)
    open_jaws = sum(d[COLUMNS.index("open_jaw")] for d in deals)
    places = {}
    for dests in destinations.values():
        places.update(dests)

    result = {
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "horizon": {"from": today.isoformat(), "to": last_out.isoformat()},
        "airports": {code: HOME[code] for code in destinations},
        "destinations": dict(sorted(places.items())),
        "stats": {"routes": len(routes), "requests": client.requests, "errors": client.errors},
        "book_url": BOOK_URL,
        "columns": COLUMNS,
    }

    old_deals = None
    if OUTPUT.exists():
        try:
            old_deals = json.loads(OUTPUT.read_text())["deals"]
        except (ValueError, KeyError):
            pass
    if old_deals == deals:
        print("\nGeen wijzigingen in de deals; data/deals.json blijft zoals het was.")
    else:
        OUTPUT.parent.mkdir(exist_ok=True)
        # Compact: één deal per regel als rij; de kolomnamen staan in "columns".
        head = json.dumps(result, ensure_ascii=False, indent=1)[:-2]
        rows = ",\n".join(json.dumps(d, separators=(",", ":")) for d in deals)
        OUTPUT.write_text(f'{head},\n "deals": [\n{rows}\n]}}\n')

    print("\n----- Samenvatting -----")
    print(f"Routes:          {len(routes)}")
    print(f"Requests:        {client.requests}")
    print(f"Fouten:          {client.errors}")
    print(f"Deals ★★ (<100): {per_star[2]}")
    print(f"Deals ★  (<150): {per_star[1]}")
    print(f"Deals    (<200): {per_star[0]}")
    print(f"Deals totaal:    {len(deals)} (waarvan {open_jaws} open jaw)")
    print(f"Duur:            {(time.monotonic() - started) / 60:.1f} minuten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
