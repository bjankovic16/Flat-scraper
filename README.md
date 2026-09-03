# Stan Watcher — Novi Beograd

Prati oglase za prodaju stanova na **Halo Oglasi** i **4zida.rs** za Novi Beograd i
šalje ti mejl jednom dnevno sa novim oglasima koji zadovoljavaju kriterijume:

- cena do 350.000 €
- kvadratura 70m² ili više
- 2 sobe ili više

Kriterijume menjaš u `scraper.py`, na vrhu fajla (sekcija `CONFIG`).

## Podešavanje (jednom, traje ~10 minuta)

### 1. Napravi GitHub nalog (ako ga nemaš) i novi repozitorijum
- Idi na github.com → **New repository** → nazovi ga npr. `stan-watcher` → **Private** → Create.
- U taj repo otpakuj/uploaduj sve fajlove iz ovog paketa (zadrži strukturu foldera,
  uključujući `.github/workflows/daily-check.yml`).

### 2. Napravi "app password" za Gmail (da skripta može da šalje mejlove u tvoje ime)
Google ne dozvoljava prijavu običnom lozinkom iz skripti, pa treba posebna:
1. Uključi dvofaktorsku autentifikaciju na svom Google nalogu (ako već nije):
   https://myaccount.google.com/security
2. Idi na https://myaccount.google.com/apppasswords
3. Napravi novu lozinku za aplikaciju (naziv npr. "stan-watcher"), Google ti da
   16-cifreni kod — sačuvaj ga, treba ti u sledećem koraku.

*(Ako ne koristiš Gmail, može i drugi provajder — samo treba da promeniš
`smtp.gmail.com` u `scraper.py` na SMTP server tvog provajdera.)*

### 3. Dodaj "Secrets" u GitHub repo (da lozinka ne bude vidljiva u kodu)
U repozitorijumu: **Settings → Secrets and variables → Actions → New repository secret**

Dodaj tri secret-a:
| Ime | Vrednost |
|---|---|
| `EMAIL_USER` | tvoja Gmail adresa, npr. `pera@gmail.com` |
| `EMAIL_PASS` | 16-cifreni app password iz koraka 2 |
| `EMAIL_TO` | mejl na koji želiš da stižu obaveštenja (može biti isti kao EMAIL_USER) |

### 4. Uključi Actions i testiraj
1. U repozitorijumu idi na tab **Actions**. Ako pita da potvrdiš workflow, klikni potvrdi.
2. Klikni na "Dnevna provera stanova" → **Run workflow** → **Run workflow**
   (ovo pokreće proveru odmah, ne moraš da čekaš do sutra).
3. Sačekaj 1-3 minuta, pa proveri mejl (i spam folder!).

Ako je sve prošlo kako treba, od sad će se automatski pokretati svaki dan u
7-8h ujutru i slati ti mejl SAMO ako ima novih oglasa.

## Kako da promeniš kriterijume kasnije
Otvori `scraper.py`, na vrhu:

```python
MAX_PRICE_EUR = 350_000
MIN_AREA_M2 = 70
MIN_ROOMS = 2.0
```

Promeni brojeve, sačuvaj, i pošalji (commit + push) promenu na GitHub — sledeći
put kad se skripta pokrene koristiće nove vrednosti.

## Napomene
- Prva provera će verovatno poslati priličan broj oglasa odjednom (sve što
  trenutno postoji i zadovoljava kriterijume). Od drugog dana dalje dobijaš
  samo NOVE oglase.
- Skripta čuva listu već viđenih oglasa u `seen_listings.json` i sama je ažurira
  nazad u repo posle svakog pokretanja.
- Sajtovi s vremena na vreme menjaju izgled stranice, što može da pokvari
  parsiranje — ako ti mejlovi prestanu da stižu ili izgledaju čudno, javi mi
  pa ću prilagoditi kod.
- Ovo je lični alat za praćenje javno dostupnih oglasa — ne koristi se za
  masovno preuzimanje ili komercijalne svrhe.
