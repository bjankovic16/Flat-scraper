#!/usr/bin/env python3
"""
Stan Watcher — prati oglase za prodaju stanova na Halo Oglasi i 4zida.rs
i šalje dnevni mejl sa novim oglasima koji zadovoljavaju kriterijume.

Numerički kriterijumi (cena/kvadratura/sobe) se podešavaju u CONFIG ispod.
Ključne reči, blokirani oglasi i favoriti se podešavaju iz veb aplikacije
(docs/index.html), koja ih upisuje u config.json.
"""

import json
import os
import re
import smtplib
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi.requests import Session as ImpersonateSession
except ImportError:  # skripta radi i bez njega, samo slabije protiv blokada
    ImpersonateSession = None

# ----------------------------------------------------------------------------
# CONFIG — ovde menjaš numeričke kriterijume pretrage
# ----------------------------------------------------------------------------

MAX_PRICE_EUR = 350_000
MIN_AREA_M2 = 70
MIN_ROOMS = 2.0  # 2.0 = dvosoban i veći (2.5, 3.0, 3.5...)
MIN_FLOOR = 1  # prizemlje (i visoko prizemlje) je 0, suteren -1
MAX_FLOOR = 5
EXCLUDE_TOP_FLOOR = True  # izbaci stanove na poslednjem spratu zgrade

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

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"          # piše ga veb aplikacija
CATALOG_FILE = ROOT / "data" / "listings.json"  # piše ga ova skripta, čita veb aplikacija
LEGACY_SEEN_FILE = ROOT / "seen_listings.json"  # format pre uvođenja kataloga

# Verzija Chrome-a koju curl_cffi TLS/HTTP2 potpisom oponaša — mora se
# poklapati sa Chrome verzijom iz User-Agent-a ispod, jer napredni WAF-ovi
# (npr. Halo Oglasi) uporede TLS/JA3 potpis sa deklarisanim pregledačem.
IMPERSONATE_TARGET = "chrome131"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    # Bez ovih zaglavlja Halo Oglasi odbija zahteve sa GitHub Actions IP adresa.
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "sr-RS,sr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
FETCH_RETRIES = 3  # ponavlja se samo ono što ume da prođe iz drugog puta
REQUEST_DELAY_SECONDS = 1.5  # pristojna pauza između zahteva
RETRY_BACKOFF_SECONDS = 4.5  # osnovna pauza pre ponovnog pokušaja, raste sa pokušajem
MAX_DENIALS_PER_HOST = 3  # posle toliko odbijanja pristupa prestajemo da zovemo domen
MAX_RETRY_AFTER_SECONDS = 60  # duže čekanje ne staje u jedno pokretanje
HOST_WAIT_BUDGET_SECONDS = 120  # ukupno vreme čekanja po domenu u jednom pokretanju

# HTTP statusi po značenju: odbijen pristup se ne rešava ponavljanjem, trajne
# greške nikako, a ograničenje brzine i privremeni kvarovi imaju smisla kasnije.
DENIAL_STATUSES = {401, 403, 407, 451}
RATE_LIMIT_STATUSES = {429, 503}
PERMANENT_STATUSES = {400, 404, 405, 410, 414, 501}
MAX_CARD_TEXT_CHARS = 1500  # veći blok teksta od ovoga nije jedan oglas, nego lista
CONTEXT_CHARS = 250  # koliko teksta kartice pamtimo za pretragu ključnih reči
MAX_DETAIL_FETCHES = 150  # zaštita da prvo pokretanje ne traje unedogled
MISSING_RUNS_BEFORE_ALERT = 3  # favorit se prijavljuje kao nestao tek posle N provera

# Izvori koje pratimo i kako se zovu u izveštaju/mejlu.
SOURCE_NAMES = ("Halo Oglasi", "4zida.rs")
STATUS_LABELS = {
    "complete": "potpuna (svi izvori odgovorili)",
    "partial": "NEPOTPUNA (deo izvora nije odgovorio)",
    "failed": "NEUSPELA (nijedan izvor nije odgovorio)",
}


@dataclass
class Listing:
    source: str
    title: str
    url: str
    price_eur: Optional[int]
    area_m2: Optional[float]
    rooms: Optional[float]
    location: str
    floor: Optional[int] = None
    total_floors: Optional[int] = None
    description: str = ""
    # Pocetak teksta kartice iz rezultata pretrage: tu su adresa i naselje
    # ('Beograd Opstina Novi Beograd Ledine Dusana Krstica'), kojih nema ni
    # u naslovu ni u opisu, a po njima se cesto iskljucuje.
    context: str = ""

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
    """'84 m2' / '59,97 m2' / '84m²' / '76 m 2' (<sup>2</sup>) -> 84.0 / 59.97 / 76.0"""
    m = re.search(r"([\d.,]+)\s*m\s*[²2]", text.replace("\xa0", " "))
    if not m:
        return None
    num = m.group(1).replace(",", ".").strip(".")
    # Tačka je decimalna ('61.29m2' -> 61.29), osim kad razdvaja hiljade
    # ('1.234 m2' -> 1234), što prepoznajemo po tačno tri cifre posle tačke.
    if "." in num and len(num.rsplit(".", 1)[1]) == 3:
        num = num.replace(".", "")
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


# Halo Oglasi piše spratnost skraćenicama, 4zida rečima.
FLOOR_TOKENS = {"PR": 0, "VPR": 0, "SUT": -1, "PSUT": -1}
ROMAN_DIGITS = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def roman_to_int(text: str) -> Optional[int]:
    """'VI' -> 6, 'XXI' -> 21"""
    total = highest = 0
    for char in reversed(text.upper()):
        value = ROMAN_DIGITS.get(char)
        if value is None:
            return None
        total = total - value if value < highest else total + value
        highest = max(highest, value)
    return total or None


def parse_floor(text: str) -> Optional[int]:
    """'VI/6 Spratnost' -> 6 | '3/11 spratova' -> 3 | 'prizemlje' -> 0 | 'suteren' -> -1

    Potkrovlje i sve što ne prepoznamo vraća None — takav oglas prolazi filter,
    kao i kod kvadrature i soba, da ne bismo tiho bacali oglase.
    """
    clean = text.replace("\xa0", " ")

    m = re.search(r"(\d+)\s*/\s*\d+\s*spratova", clean, re.IGNORECASE)
    if m:
        return int(m.group(1))

    m = re.search(r"\b(VPR|PSUT|PR|SUT|PTK)\s*(?:/\s*\d+)?\s*Spratnost", clean)
    if m:
        return FLOOR_TOKENS.get(m.group(1))

    m = re.search(r"\b([IVXLC]+)\s*(?:/\s*\d+)?\s*Spratnost", clean)
    if m:
        return roman_to_int(m.group(1))

    if re.search(r"prizemlj", clean, re.IGNORECASE):
        return 0
    if re.search(r"suteren", clean, re.IGNORECASE):
        return -1
    return None


def parse_total_floors(text: str) -> Optional[int]:
    """Koliko spratova ima zgrada: 'VI/6 Spratnost' -> 6, '3/11 spratova' -> 11.

    Oglasi koji ne pišu ukupan broj spratova ('II Spratnost') vraćaju None i
    prolaze filter poslednjeg sprata — ne možemo znati da li su na vrhu.
    """
    clean = text.replace("\xa0", " ")
    m = re.search(r"/\s*(\d+)\s*spratova", clean, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:[IVXLC]+|VPR|PSUT|PR|SUT|PTK)\s*/\s*(\d+)\s*Spratnost", clean)
    if m:
        return int(m.group(1))
    return None


def normalize(text: str) -> str:
    """Za poređenje ključnih reči: mala slova, bez kvačica ('Prizemlje' -> 'prizemlje')."""
    text = (text or "").lower().replace("đ", "dj")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# Jedna sesija po domenu, ponovo iskorišćena za sve zahteve — čuva kolačiće
# koje sajt (Cloudflare i sl. zaštita) postavi posle prvog uspešnog zahteva,
# što je često neophodno da bi sledeći zahtevi prošli.
SESSION = requests.Session()
_IMPERSONATE_SESSIONS: dict[str, "ImpersonateSession"] = {}
_WARMED_UP_HOSTS: set[str] = set()

# Zašto je koji sajt pao: {"www.halooglasi.com": {"HTTP 403": 12}}. Ide u mejl,
# da se ne mora kopati po logovima GitHub Actions-a.
FETCH_PROBLEMS: dict[str, dict[str, int]] = {}

# Domeni od kojih smo odustali u ovom pokretanju i zašto ({host: razlog}).
# Kad zaštita odbije pristup, ponavljanje istog zahteva daje isti odgovor, pa
# domen preskačemo do kraja pokretanja — ostali izvori rade normalno dalje.
GIVEN_UP_HOSTS: dict[str, str] = {}
_HOST_DENIALS: dict[str, int] = {}
_HOST_WAITED: dict[str, float] = {}


def reset_fetch_state() -> None:
    """Čisto stanje mrežnog sloja za jedno pokretanje (koriste i testovi)."""
    FETCH_PROBLEMS.clear()
    GIVEN_UP_HOSTS.clear()
    _HOST_DENIALS.clear()
    _HOST_WAITED.clear()
    _WARMED_UP_HOSTS.clear()


def _host_of(url: str) -> str:
    return url.split("/")[2] if "//" in url else url


def note_problem(url: str, reason: str) -> None:
    host = _host_of(url)
    FETCH_PROBLEMS.setdefault(host, {})
    FETCH_PROBLEMS[host][reason] = FETCH_PROBLEMS[host].get(reason, 0) + 1


def http_client_name() -> str:
    return ("curl_cffi (Chrome TLS)" if ImpersonateSession is not None
            else "requests (obican TLS)")


def give_up_on_host(host: str, reason: str) -> None:
    if host not in GIVEN_UP_HOSTS:
        GIVEN_UP_HOSTS[host] = reason
        print(f"  [!] Odustajem od {host} u ovom pokretanju: {reason}")


def note_denial(host: str, reason: str) -> bool:
    """Prebroj odbijanje pristupa; vrati True ako smo zbog toga odustali."""
    _HOST_DENIALS[host] = _HOST_DENIALS.get(host, 0) + 1
    if _HOST_DENIALS[host] >= MAX_DENIALS_PER_HOST:
        give_up_on_host(host, f"{reason} x{_HOST_DENIALS[host]} — odbijen pristup")
        return True
    return False


def _wait_within_budget(host: str, seconds: float) -> bool:
    """Sačekaj traženo vreme ako staje u budžet pokretanja.

    Vrati False kad je traženo čekanje duže od onoga što jedno pokretanje sme
    da potroši — tada se izvor preskače (pa se pokuša u sledećoj proveri),
    umesto da zahtev ponovimo ranije nego što je sajt tražio.
    """
    used = _HOST_WAITED.get(host, 0.0)
    if seconds > MAX_RETRY_AFTER_SECONDS or used + seconds > HOST_WAIT_BUDGET_SECONDS:
        return False
    _HOST_WAITED[host] = used + seconds
    time.sleep(seconds)
    return True


def retry_after_seconds(resp) -> Optional[float]:
    """Retry-After zaglavlje u sekundama (podržan je samo zapis brojem)."""
    headers = getattr(resp, "headers", None) or {}
    value = headers.get("Retry-After") or headers.get("retry-after")
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _impersonate_session_for(host: str) -> "ImpersonateSession":
    session = _IMPERSONATE_SESSIONS.get(host)
    if session is None:
        session = ImpersonateSession(impersonate=IMPERSONATE_TARGET)
        _IMPERSONATE_SESSIONS[host] = session
    return session


def _warm_up(url: str) -> None:
    """Prvi zahtev ka domenu je uvek na početnu stranu, da sesija dobije
    kolačiće zaštite (npr. Cloudflare) pre nego što zatražimo pravu stranicu.

    Zagrevanje se radi jednom po domenu i njegov ishod se broji kao i svaki
    drugi zahtev: status ulazi u dijagnostiku, a odbijen pristup i u brojač
    odbijanja — bez dodatnog zahteva i bez tihog "uspeha".
    """
    host = _host_of(url)
    if host in _WARMED_UP_HOSTS:
        return
    _WARMED_UP_HOSTS.add(host)  # pokušaj samo jednom, i kad ne uspe
    homepage = f"https://{host}/"
    headers = {**REQUEST_HEADERS, "Sec-Fetch-Site": "none"}
    try:
        if ImpersonateSession is not None:
            resp = _impersonate_session_for(host).get(
                homepage, headers=headers, timeout=20)
        else:
            resp = SESSION.get(homepage, headers=headers, timeout=15)
    except Exception as e:
        note_problem(homepage, f"{type(e).__name__} (zagrevanje)")
        print(f"  [!] Zagrevanje sesije za {host} nije uspelo: {e}")
        return
    status = getattr(resp, "status_code", None)
    if status != 200:
        note_problem(homepage, f"HTTP {status} (zagrevanje)")
        print(f"  [!] Zagrevanje za {host} -> HTTP {status}")
        if status in DENIAL_STATUSES:
            note_denial(host, f"HTTP {status}")
    time.sleep(REQUEST_DELAY_SECONDS)


def _get(url: str):
    """Halo Oglasi odbija zahteve sa servera (GitHub Actions) i kad zaglavlja
    izgledaju kao iz pregledača, jer prepoznaje TLS potpis Python biblioteke.
    curl_cffi oponaša i Chrome-ov TLS handshake, pa ima bolje izglede; ako ga
    nema, vraćamo se na requests (dovoljno za 4zida i za lokalno pokretanje).
    Ni jedno ni drugo ne garantuje da će sajt pustiti zahtev.
    Koristimo sesiju (ne jednokratni get) da bi se kolačići sa zagrevanja
    (i sa svakog sledećeg odgovora) preneli na naredne zahteve.
    """
    host = _host_of(url)
    headers = {**REQUEST_HEADERS, "Referer": f"https://{host}/"}
    if ImpersonateSession is not None:
        return _impersonate_session_for(host).get(url, headers=headers, timeout=25)
    return SESSION.get(url, headers=headers, timeout=20)


def fetch(url: str) -> Optional[BeautifulSoup]:
    """Učitaj stranicu; None znači da ovaj zahtev nije uspeo.

    Ponavlja se samo ono što ume da prođe iz drugog puta (privremeni kvarovi i
    ograničenje brzine), i to ograničen broj puta. Odbijen pristup i trajne
    greške se ne ponavljaju.
    """
    host = _host_of(url)
    if host in GIVEN_UP_HOSTS:
        note_problem(url, "preskočeno (odustali smo od domena)")
        return None

    _warm_up(url)
    if host in GIVEN_UP_HOSTS:  # zagrevanje je već pokazalo da nas ne puštaju
        note_problem(url, "preskočeno (odustali smo od domena)")
        return None

    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            resp = _get(url)
        except Exception as e:  # curl_cffi ne deli izuzetke sa requests
            note_problem(url, type(e).__name__)
            print(f"  [!] Greška pri učitavanju {url}: {e} (pokušaj {attempt})")
            if attempt == FETCH_RETRIES or not _wait_within_budget(
                    host, RETRY_BACKOFF_SECONDS * attempt):
                return None
            continue

        status = resp.status_code
        if status == 200:
            return BeautifulSoup(resp.text, "html.parser")

        note_problem(url, f"HTTP {status}")
        print(f"  [!] {url} -> HTTP {status} (pokušaj {attempt})")

        if status in DENIAL_STATUSES:
            # Zaštita je odlučila da nas ne pusti; isti zahtev odmah ponovo
            # daje isti odgovor, pa samo brojimo i idemo dalje.
            note_denial(host, f"HTTP {status}")
            return None
        if status in PERMANENT_STATUSES:
            return None
        if status in RATE_LIMIT_STATUSES:
            wait = retry_after_seconds(resp)
            if wait is None:
                wait = RETRY_BACKOFF_SECONDS * attempt
            if attempt == FETCH_RETRIES or not _wait_within_budget(host, wait):
                give_up_on_host(
                    host, f"HTTP {status} — ograničenje brzine, traži {wait:.0f}s")
                return None
            continue
        # Ostalo (5xx i nepoznati statusi) tretiramo kao privremen kvar.
        if attempt == FETCH_RETRIES or not _wait_within_budget(
                host, RETRY_BACKOFF_SECONDS * attempt):
            return None
    return None


def find_card(a):
    """Vrati element koji predstavlja JEDAN oglas.

    Link na oglas je često unutar slike, pa je njegov neposredni roditelj
    prazan <div> bez cene. Penjemo se uz DOM do prvog elementa koji sadrži
    cenu. Ako je taj element prevelik, oglas nema svoju cenu i naleteli smo na
    kontejner sa više oglasa — vraćamo None da ne bismo tom oglasu pripisali
    cenu suseda.
    """
    node = a
    for _ in range(8):
        node = node.parent
        if node is None:
            return None
        text = node.get_text(" ", strip=True)
        if "€" in text:
            return node if len(text) <= MAX_CARD_TEXT_CHARS else None
    return None


def title_from_url(url: str) -> str:
    """'.../trosoban-stan/6a0cda8118003131c808fa4f' -> 'Trosoban stan'"""
    parts = [p for p in url.split("?")[0].split("/")
             if p and not re.fullmatch(r"[0-9a-f]{6,}", p)]
    slug = parts[-1] if parts else url
    title = slug.replace("-", " ").strip()
    return title[:1].upper() + title[1:]


def card_title(card, url: str) -> str:
    """Naslov iz naslovnog taga kartice; 4zida ga nema, pa pada na slug URL-a."""
    heading = card.find(["h1", "h2", "h3", "h4"])
    if heading:
        text = heading.get_text(" ", strip=True)
        if text:
            return text
    return title_from_url(url)


def fetch_detail(url: str) -> tuple[Optional[str], Optional[str]]:
    """Pravi naslov i opis oglasa sa same stranice oglasa (og: meta tagovi)."""
    soup = fetch(url)
    time.sleep(REQUEST_DELAY_SECONDS)
    if soup is None:
        return None, None

    title = None
    og_title = soup.select_one('meta[property="og:title"]')
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)

    description = None
    meta_desc = soup.select_one(
        'meta[name="description"], meta[property="og:description"]')
    if meta_desc and meta_desc.get("content"):
        description = meta_desc["content"].strip()

    return title, description


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

            card = find_card(a)
            if card is None:
                continue
            text = card.get_text(" ", strip=True)
            price = parse_price(text)
            area = parse_area(text)
            rooms = parse_rooms(text)
            floor = parse_floor(text)
            total_floors = parse_total_floors(text)
            title = card_title(card, href)
            context = text[:CONTEXT_CHARS]

            results.append(Listing(
                source="Halo Oglasi",
                title=title,
                url=href,
                price_eur=price,
                area_m2=area,
                rooms=rooms,
                floor=floor,
                total_floors=total_floors,
                context=context,
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

            card = find_card(a)
            if card is None:
                continue
            text = card.get_text(" ", strip=True)
            price = parse_price(text)
            area = parse_area(text)
            rooms = parse_rooms(text)
            floor = parse_floor(text)
            total_floors = parse_total_floors(text)
            title = card_title(card, href)
            context = text[:CONTEXT_CHARS]

            results.append(Listing(
                source="4zida.rs",
                title=title,
                url=href,
                price_eur=price,
                area_m2=area,
                rooms=rooms,
                floor=floor,
                total_floors=total_floors,
                context=context,
                location="Novi Beograd",
            ))
            found_on_page += 1

        if found_on_page == 0:
            break

    return results


# ----------------------------------------------------------------------------
# Konfiguracija iz veb aplikacije (config.json)
# ----------------------------------------------------------------------------

def load_config() -> dict:
    default = {"exclude_keywords": [], "blocked": [], "favorites": []}
    if not CONFIG_FILE.exists():
        return default
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[!] config.json se ne može pročitati ({e}) — koristim prazne liste.")
        return default
    for key in default:
        value = data.get(key)
        default[key] = value if isinstance(value, list) else []
    return default


def excluded_by_keyword(listing: Listing, keywords: list[str]) -> Optional[str]:
    """Vrati ključnu reč zbog koje oglas ispada, ili None ako je oglas u redu."""
    haystack = normalize(
        f"{listing.title} {listing.description} {listing.context} {listing.url}")
    for word in keywords:
        needle = normalize(word).strip()
        if needle and needle in haystack:
            return word
    return None


# ----------------------------------------------------------------------------
# Katalog (data/listings.json) — istorija oglasa, čita ga i veb aplikacija
# ----------------------------------------------------------------------------

def load_catalog() -> dict:
    if not CATALOG_FILE.exists():
        return {}
    try:
        data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def migrate_legacy_seen(catalog: dict, today: str) -> dict:
    """Stara verzija je čuvala samo spisak URL-ova u seen_listings.json.

    Prenosimo ih u katalog kao već poslate, da prvi sledeći mejl ne bi ponovo
    poslao sve oglase koji su ranije već stigli.
    """
    if catalog or not LEGACY_SEEN_FILE.exists():
        return catalog
    try:
        urls = json.loads(LEGACY_SEEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[!] seen_listings.json se ne može pročitati ({e}) — preskačem.")
        return catalog
    for url in urls:
        catalog[url] = {"first_seen": today, "last_seen": today,
                        "notified": True, "missing_runs": 0}
    print(f"Preneto {len(catalog)} oglasa iz seen_listings.json u katalog.")
    return catalog


def save_catalog(catalog: dict) -> None:
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_FILE.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")


# ----------------------------------------------------------------------------
# Filtriranje i mejl
# ----------------------------------------------------------------------------

def passes_filters(listing: Listing) -> bool:
    if listing.price_eur is None or listing.price_eur > MAX_PRICE_EUR:
        return False
    if listing.area_m2 is not None and listing.area_m2 < MIN_AREA_M2:
        return False
    if listing.rooms is not None and listing.rooms < MIN_ROOMS:
        return False
    if listing.floor is not None and not (MIN_FLOOR <= listing.floor <= MAX_FLOOR):
        return False
    if (EXCLUDE_TOP_FLOOR and listing.floor is not None
            and listing.total_floors is not None
            and listing.floor >= listing.total_floors):
        return False
    return True


def format_price(value: Optional[int]) -> str:
    return f"{value:,} €".replace(",", ".") if value else "cena N/A"


def format_floor(floor: Optional[int], total: Optional[int] = None) -> str:
    if floor is None:
        return "sprat N/A"
    if floor == 0:
        label = "prizemlje"
    elif floor < 0:
        label = "suteren"
    else:
        label = f"{floor}. sprat"
    return f"{label} od {total}" if total else label


def format_listing(listing: Listing) -> list[str]:
    area = f"{listing.area_m2:.0f} m²" if listing.area_m2 else "m² N/A"
    rooms = f"{listing.rooms:g} soba" if listing.rooms else "sobe N/A"
    lines = [f"\n[{listing.source}] {listing.title}",
             f"  {format_price(listing.price_eur)} | {area} | {rooms} | "
             f"{format_floor(listing.floor, listing.total_floors)}"]
    if listing.description:
        lines.append(f"  {listing.description[:200]}")
    lines.append(f"  {listing.url}")
    return lines


def run_status(dead_sources: list[str]) -> str:
    """'complete' | 'partial' | 'failed' — koliko je provera zaista uspela."""
    if not dead_sources:
        return "complete"
    if len(dead_sources) >= len(SOURCE_NAMES):
        return "failed"
    return "partial"


def build_email_body(new_listings: list[Listing], favorite_events: list[str],
                     keywords: list[str], dead_sources: list[str]) -> str:
    max_price = f"{MAX_PRICE_EUR:,}".replace(",", ".")
    lines = [
        f"Novi Beograd — do {max_price} €, {MIN_AREA_M2}m2+, "
        f"{MIN_ROOMS:g} sobe ili više, {MIN_FLOOR}-{MAX_FLOOR}. sprat"
        + (", ne poslednji" if EXCLUDE_TOP_FLOOR else ""),
        f"Status provere: {STATUS_LABELS[run_status(dead_sources)]}",
    ]
    if keywords:
        lines.append(f"Isključene ključne reči: {', '.join(keywords)}")
    if dead_sources:
        lines += ["", "!" * 60,
                  f"UPOZORENJE: {', '.join(dead_sources)} nije vratio nijedan "
                  f"oglas.", "Ovaj mejl je nepotpun.", "",
                  f"HTTP klijent: {http_client_name()}"]
        for host, reason in sorted(GIVEN_UP_HOSTS.items()):
            lines.append(f"  {host}: odustali smo u ovom pokretanju ({reason})")
        for host, reasons in sorted(FETCH_PROBLEMS.items()):
            detail = ", ".join(f"{r} x{n}" for r, n in sorted(reasons.items()))
            lines.append(f"  {host}: {detail}")
        if not FETCH_PROBLEMS:
            lines.append("  Nijedan zahtev nije pao — sajt je verovatno "
                         "promenio izgled stranice.")
        lines.append("!" * 60)

    if favorite_events:
        lines += ["", "=" * 60, "PROMENE NA FAVORITIMA", "=" * 60]
        lines += favorite_events

    if new_listings:
        lines += ["", "=" * 60,
                  f"NOVI OGLASI ({len(new_listings)})", "=" * 60]
        for listing in sorted(new_listings, key=lambda l: (l.price_eur or 0)):
            lines += format_listing(listing)

    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    email_user = os.environ["EMAIL_USER"]
    email_pass = os.environ["EMAIL_PASS"]
    email_to = os.environ.get("EMAIL_TO") or email_user

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

def collect_listings() -> tuple[list[Listing], list[str]]:
    """Vrati sve oglase i spisak sajtova koji nisu vratili nijedan oglas.

    Sajt koji vrati nulu je skoro uvek blokada ili promena stranice, a ne
    stvarno prazan rezultat — to mora da se vidi, a ne da tiho prođe.
    """
    all_listings: list[Listing] = []
    dead_sources: list[str] = []

    print("== Halo Oglasi ==")
    halo = [l for cat in HALOOGLASI_CATEGORIES
            for l in scrape_halooglasi_category(cat)]
    all_listings.extend(halo)

    print("== 4zida.rs ==")
    zida = [l for cat in ZIDA_CATEGORIES for l in scrape_4zida_category(cat)]
    all_listings.extend(zida)

    for name, found in zip(SOURCE_NAMES, (len(halo), len(zida))):
        print(f"  {name}: {found} oglasa")
        if found == 0:
            dead_sources.append(name)

    print(f"\nUkupno pronađeno (pre filtera i dedup.): {len(all_listings)}")
    if dead_sources:
        print(f"[!] UPOZORENJE: nijedan oglas sa: {', '.join(dead_sources)}")
    return all_listings, dead_sources


def check_favorites(catalog: dict, favorites: list[str],
                    current: dict[str, Listing],
                    dead_sources: Optional[list[str]] = None) -> list[str]:
    """Prati favorite: promena cene i nestanak oglasa sa sajta.

    Favoriti sa izvora koji u ovom pokretanju nije odgovorio se preskaču:
    odbijen pristup nije dokaz da je oglas prodat, pa brojač nestanka ne sme
    da raste zbog naše greške u dolasku do sajta.
    """
    events: list[str] = []
    failed = set(dead_sources or [])
    for url in favorites:
        entry = catalog.get(url)
        if entry is None:
            continue
        listing = current.get(url)

        if listing is None:
            if entry.get("source") in failed:
                print(f"  [favorit] {url}: izvor {entry.get('source')} nije "
                      f"odgovorio — ne računam kao nestao.")
                continue
            entry["missing_runs"] = entry.get("missing_runs", 0) + 1
            if entry["missing_runs"] == MISSING_RUNS_BEFORE_ALERT:
                events.append(
                    f"\n[NESTAO] {entry.get('title', url)}\n"
                    f"  Ne pojavljuje se u rezultatima {MISSING_RUNS_BEFORE_ALERT} "
                    f"provere zaredom — verovatno je prodat ili skinut.\n  {url}")
            continue

        entry["missing_runs"] = 0
        old_price = entry.get("price_eur")
        new_price = listing.price_eur
        if old_price and new_price and old_price != new_price:
            direction = "SNIŽENJE" if new_price < old_price else "POSKUPLJENJE"
            diff = abs(new_price - old_price)
            events.append(
                f"\n[{direction}] {listing.title}\n"
                f"  {format_price(old_price)} -> {format_price(new_price)} "
                f"({format_price(diff)} razlike)\n  {listing.url}")
    return events


def main() -> int:
    """Vrati izlazni status: 0 = potpuna provera, 1 = nepotpuna ili neuspela.

    Nepotpuna provera ne sme da se prikaže kao uspešna u veb aplikaciji, koja
    gleda ishod GitHub Actions pokretanja — zato izlazni status nije nula.
    Katalog se u tom slučaju svejedno sačuva (korak u workflow-u ide uvek), pa
    se ono što jeste skinuto ne gubi.
    """
    reset_fetch_state()
    print(f"HTTP klijent: {http_client_name()}")
    config = load_config()
    keywords = config["exclude_keywords"]
    blocked = set(config["blocked"])
    favorites = config["favorites"]
    print(f"Config: {len(keywords)} ključnih reči, {len(blocked)} blokiranih, "
          f"{len(favorites)} favorita")

    today = date.today().isoformat()
    catalog = migrate_legacy_seen(load_catalog(), today)

    # Dedup po URL-u (isti oglas se pojavljuje u više kategorija/strana)
    listings, dead_sources = collect_listings()
    unique = {l.key(): l for l in listings}
    filtered = {url: l for url, l in unique.items() if passes_filters(l)}
    print(f"Prošlo numeričke filtere: {len(filtered)}")

    # Favoriti se prate i kad su blokirani/isključeni ključnom rečju.
    favorite_events = check_favorites(catalog, favorites, filtered, dead_sources)

    candidates = {url: l for url, l in filtered.items() if url not in blocked}
    print(f"Posle blokiranih: {len(candidates)}")

    # Naslov i opis se skidaju sa stranice oglasa samo jednom po oglasu —
    # kasnija pokretanja koriste ono što je već u katalogu.
    fetches = 0
    for url, listing in candidates.items():
        entry = catalog.get(url)
        if entry and entry.get("description") is not None:
            listing.title = entry.get("title") or listing.title
            listing.description = entry.get("description") or ""
            continue
        if fetches >= MAX_DETAIL_FETCHES:
            continue
        fetches += 1
        print(f"  detalji [{fetches}]: {url}")
        title, description = fetch_detail(url)
        listing.title = title or listing.title
        listing.description = description or ""
    print(f"Skinuto detalja u ovom pokretanju: {fetches}")

    kept: dict[str, Listing] = {}
    for url, listing in candidates.items():
        hit = excluded_by_keyword(listing, keywords)
        if hit:
            print(f"  [kljucna rec '{hit}'] preskacem: {listing.title}")
            continue
        kept[url] = listing
    print(f"Posle ključnih reči: {len(kept)}")

    new_listings = [l for url, l in kept.items()
                    if not catalog.get(url, {}).get("notified")]
    print(f"Novo (nije ranije poslato): {len(new_listings)}")

    # Katalog pamti sve što je prošlo numeričke filtere — i blokirane i
    # isključene — da bi veb aplikacija mogla da ih prikaže i vrati nazad.
    for url, listing in filtered.items():
        entry = catalog.setdefault(url, {"first_seen": today, "missing_runs": 0})
        entry.update({
            "source": listing.source,
            "title": listing.title or entry.get("title", ""),
            "description": listing.description or entry.get("description", ""),
            "context": listing.context or entry.get("context", ""),
            "price_eur": listing.price_eur,
            "area_m2": listing.area_m2,
            "rooms": listing.rooms,
            "floor": listing.floor,
            "total_floors": listing.total_floors,
            "location": listing.location,
            "last_seen": today,
        })
        entry.setdefault("notified", False)
    for listing in new_listings:
        catalog[listing.url]["notified"] = True
    save_catalog(catalog)

    status = run_status(dead_sources)
    print(f"Status provere: {status} — {STATUS_LABELS[status]}")

    if not new_listings and not favorite_events and not dead_sources:
        print("Nema novih oglasa ni promena na favoritima — mejl se ne šalje.")
        return 0

    body = build_email_body(new_listings, favorite_events, keywords, dead_sources)
    bits = []
    if new_listings:
        bits.append(f"{len(new_listings)} novih")
    if favorite_events:
        bits.append(f"{len(favorite_events)} promena na favoritima")
    if dead_sources:
        bits.append(f"{'NEUSPELO' if status == 'failed' else 'NEPOTPUNO'}: "
                    f"{', '.join(dead_sources)}")
    subject = f"🏠 {', '.join(bits) or 'provera'} — Novi Beograd"
    send_email(subject, body)
    print(f"Mejl poslat: {subject}")
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
