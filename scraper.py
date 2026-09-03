#!/usr/bin/env python3
"""
Stan Watcher — prati oglase za prodaju stanova na Halo Oglasi i 4zida.rs
i šalje dnevni mejl sa novim oglasima koji zadovoljavaju kriterijume.

Kriterijumi se podešavaju u CONFIG ispod.
"""

import json
import os
import re
import smtplib
import time
from dataclasses import dataclass, asdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# CONFIG — ovde menjaš kriterijume pretrage
# ----------------------------------------------------------------------------

MAX_PRICE_EUR = 350_000
MIN_AREA_M2 = 70
MIN_ROOMS = 2.0  # 2.0 = dvosoban i veći (2.5, 3.0, 3.5...)

# Kategorije (broj soba) koje pratimo na Halo Oglasi za Novi Beograd.
HALOOGLASI_CATEGORIES = [
    "dvosoban", "dvoiposoban", "trosoban", "troiposoban",
    "cetvorosoban", "cetvoroiposoban", "petosoban", "petosoban-i-veci",
]
HALOOGLASI_BASE = "https://www.halooglasi.com/nekretnine/prodaja-stanova/beograd-novi-beograd"

# Kategorije (broj soba) koje pratimo na 4zida za Novi Beograd.
ZIDA_CATEGORIES = [
    "dvosoban", "dvoiposoban", "trosoban", "troiposoban",
    "cetvorosoban", "cetvoroiposoban", "petosoban", "petoiposoban",
]
ZIDA_BASE = "https://www.4zida.rs/prodaja-stanova/novi-beograd-beograd"

MAX_PAGES_PER_CATEGORY = 3  # koliko stranica po kategoriji da proveri
STATE_FILE = Path(__file__).parent / "seen_listings.json"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
REQUEST_DELAY_SECONDS = 1.5  # pristojna pauza između zahteva


@dataclass
class Listing:
    source: str
    title: str
    url: str
    price_eur: Optional[int]
    area_m2: Optional[float]
    rooms: Optional[float]
    location: str

    def key(self) -> str:
        return self.url


# ----------------------------------------------------------------------------
# Pomoćne funkcije za parsiranje brojeva iz srpskog teksta
# ----------------------------------------------------------------------------

def parse_price(text: str) -> Optional[int]:
    """'233.883 €' -> 233883"""
    m = re.search(r"([\d.]{3,})\s*€", text.replace("\xa0", " "))
    if not m:
        return None
    digits = m.group(1).replace(".", "")
    try:
        return int(digits)
    except ValueError:
        return None


def parse_area(text: str) -> Optional[float]:
    """'84 m2' ili '59,97 m2' ili '84m²' -> 84.0 / 59.97"""
    m = re.search(r"([\d.,]+)\s*m[²2]", text.replace("\xa0", " "))
    if not m:
        return None
    num = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def parse_rooms(text: str) -> Optional[float]:
    """'3.0 Broj soba' ili '3 sobe' ili '2.5 sobe' -> 3.0 / 2.5"""
    m = re.search(r"([\d.,]+)\s*(?:Broj soba|sob[ae])", text)
    if not m:
        return None
    num = m.group(1).replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def fetch(url: str) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"  [!] {url} -> HTTP {resp.status_code}")
            return None
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        print(f"  [!] Greška pri učitavanju {url}: {e}")
        return None


# ----------------------------------------------------------------------------
# Halo Oglasi parser
# ----------------------------------------------------------------------------

def scrape_halooglasi_category(category: str) -> list[Listing]:
    results: list[Listing] = []
    for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
        url = f"{HALOOGLASI_BASE}/{category}"
        if page > 1:
            url += f"?page={page}"
        print(f"  Halo Oglasi [{category}] strana {page}: {url}")
        soup = fetch(url)
        time.sleep(REQUEST_DELAY_SECONDS)
        if soup is None:
            break

        # Svaki pravi oglas je link ka /nekretnine/prodaja-stanova/<slug>/<dugi-broj>
        # (linkovi ka kategorijama/lokacijama nemaju taj brojčani ID na kraju)
        links = soup.select('a[href*="/nekretnine/prodaja-stanova/"]')

        found_on_page = 0
        seen_hrefs_this_page = set()
        for a in links:
            href = a.get("href", "")
            if not href or not re.search(r"/\d{6,}(\?|$)", href):
                continue
            if not href.startswith("http"):
                href = "https://www.halooglasi.com" + href
            href = href.split("?")[0]
            if href in seen_hrefs_this_page:
                continue
            seen_hrefs_this_page.add(href)

            card = a.find_parent(["article", "li", "div"]) or a
            text = card.get_text(" ", strip=True)
            price = parse_price(text)
            area = parse_area(text)
            rooms = parse_rooms(text)
            title = a.get_text(strip=True) or text[:80]

            results.append(Listing(
                source="Halo Oglasi",
                title=title,
                url=href,
                price_eur=price,
                area_m2=area,
                rooms=rooms,
                location="Novi Beograd",
            ))
            found_on_page += 1

        if found_on_page == 0:
            break  # nema više rezultata, nema smisla ići na sledeću stranu

    return results


# ----------------------------------------------------------------------------
# 4zida.rs parser
# ----------------------------------------------------------------------------

def scrape_4zida_category(category: str) -> list[Listing]:
    results: list[Listing] = []
    for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
        url = f"{ZIDA_BASE}/{category}"
        if page > 1:
            url += f"?strana={page}"
        print(f"  4zida [{category}] strana {page}: {url}")
        soup = fetch(url)
        time.sleep(REQUEST_DELAY_SECONDS)
        if soup is None:
            break

        links = soup.select('a[href*="/prodaja-stanova/"]')
        found_on_page = 0
        seen_hrefs_this_page = set()
        for a in links:
            href = a.get("href", "")
            if not href or "/prodaja-stanova/" not in href:
                continue
            # pravi oglasi imaju dugi hex ID na kraju putanje
            if not re.search(r"/[0-9a-f]{20,}$", href):
                continue
            if not href.startswith("http"):
                href = "https://www.4zida.rs" + href
            if href in seen_hrefs_this_page:
                continue
            seen_hrefs_this_page.add(href)

            card = a.find_parent(["li", "div", "article"]) or a
            text = card.get_text(" ", strip=True)
            price = parse_price(text)
            area = parse_area(text)
            rooms = parse_rooms(text)
            title = a.get_text(strip=True) or text[:80]

            results.append(Listing(
                source="4zida.rs",
                title=title,
                url=href,
                price_eur=price,
                area_m2=area,
                rooms=rooms,
                location="Novi Beograd",
            ))
            found_on_page += 1

        if found_on_page == 0:
            break

    return results


# ----------------------------------------------------------------------------
# Filtriranje, state i mejl
# ----------------------------------------------------------------------------

def passes_filters(listing: Listing) -> bool:
    if listing.price_eur is None or listing.price_eur > MAX_PRICE_EUR:
        return False
    if listing.area_m2 is not None and listing.area_m2 < MIN_AREA_M2:
        return False
    if listing.rooms is not None and listing.rooms < MIN_ROOMS:
        return False
    return True


def load_seen() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def build_email_body(new_listings: list[Listing]) -> str:
    lines = [
        f"Novi oglasi za stanove — Novi Beograd, do {MAX_PRICE_EUR:,} €, "
        f"70m2+, 2 sobe ili više\n".replace(",", "."),
        f"Pronađeno: {len(new_listings)} novih oglasa\n",
        "=" * 60,
    ]
    for listing in sorted(new_listings, key=lambda l: (l.price_eur or 0)):
        price_str = f"{listing.price_eur:,} €".replace(",", ".") if listing.price_eur else "cena N/A"
        area_str = f"{listing.area_m2:.0f} m²" if listing.area_m2 else "m² N/A"
        rooms_str = f"{listing.rooms} soba" if listing.rooms else ""
        lines.append(f"\n[{listing.source}] {listing.title}")
        lines.append(f"  {price_str} | {area_str} | {rooms_str}")
        lines.append(f"  {listing.url}")
    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    email_user = os.environ["EMAIL_USER"]
    email_pass = os.environ["EMAIL_PASS"]
    email_to = os.environ.get("EMAIL_TO", email_user)

    msg = MIMEMultipart()
    msg["From"] = email_user
    msg["To"] = email_to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(email_user, email_pass)
        server.sendmail(email_user, email_to, msg.as_string())


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    all_listings: list[Listing] = []

    print("== Halo Oglasi ==")
    for cat in HALOOGLASI_CATEGORIES:
        all_listings.extend(scrape_halooglasi_category(cat))

    print("== 4zida.rs ==")
    for cat in ZIDA_CATEGORIES:
        all_listings.extend(scrape_4zida_category(cat))

    print(f"\nUkupno pronađeno (pre filtera i dedup.): {len(all_listings)}")

    # Dedup po URL-u (isti oglas se pojavljuje u više kategorija/strana)
    unique = {l.key(): l for l in all_listings}.values()

    filtered = [l for l in unique if passes_filters(l)]
    print(f"Prošlo filtere (cena/kvadratura/sobe): {len(filtered)}")

    seen = load_seen()
    new_listings = [l for l in filtered if l.key() not in seen]
    print(f"Novo (nije ranije viđeno): {len(new_listings)}")

    # Ažuriraj state fajl sa SVIM oglasima koji su prošli filtere (ne samo novim),
    # da ne bismo ponovo slali iste kad istekne pa se ponovo pojavi.
    seen.update(l.key() for l in filtered)
    save_seen(seen)

    if not new_listings:
        print("Nema novih oglasa danas — mejl se ne šalje.")
        return

    body = build_email_body(new_listings)
    subject = f"🏠 {len(new_listings)} novih stanova — Novi Beograd"
    send_email(subject, body)
    print(f"Mejl poslat sa {len(new_listings)} novih oglasa.")


if __name__ == "__main__":
    main()
