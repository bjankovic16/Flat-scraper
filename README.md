# Stan Watcher — Novi Beograd

Prati oglase za prodaju stanova na **Halo Oglasi** i **4zida.rs** za Novi Beograd i
šalje ti mejl jednom dnevno sa novim oglasima koji zadovoljavaju kriterijume:

- cena do 350.000 €
- kvadratura 70m² ili više
- 2 sobe ili više
- od 2. do 6. sprata (prizemlje, visoko prizemlje i suteren ispadaju)
- ne poslednji sprat u zgradi

Kriterijume menjaš u `scraper.py`, na vrhu fajla (sekcija `CONFIG`).

## Podešavanje (jednom, traje ~10 minuta)

### 1. Napravi "app password" za Gmail (da skripta može da šalje mejlove u tvoje ime)
Google ne dozvoljava prijavu običnom lozinkom iz skripti, pa treba posebna:
1. Uključi dvofaktorsku autentifikaciju na svom Google nalogu (ako već nije):
   https://myaccount.google.com/security
2. Idi na https://myaccount.google.com/apppasswords
3. Napravi novu lozinku za aplikaciju (naziv npr. "stan-watcher"), Google ti da
   16-cifreni kod — sačuvaj ga, treba ti u sledećem koraku.

*(Ako ne koristiš Gmail, može i drugi provajder — samo treba da promeniš
`smtp.gmail.com` u `scraper.py` na SMTP server tvog provajdera.)*

### 2. Dodaj "Secrets" u GitHub repo (da lozinka ne bude vidljiva u kodu)
U repozitorijumu: **Settings → Secrets and variables → Actions → New repository secret**

Dodaj tri secret-a:
| Ime | Vrednost |
|---|---|
| `EMAIL_USER` | tvoja Gmail adresa, npr. `pera@gmail.com` |
| `EMAIL_PASS` | 16-cifreni app password iz koraka 1 |
| `EMAIL_TO` | mejl na koji želiš da stižu obaveštenja (može biti isti kao EMAIL_USER) |

### 3. Uključi Actions i testiraj
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
MIN_FLOOR = 2
MAX_FLOOR = 6
EXCLUDE_TOP_FLOOR = True
```

Promeni brojeve, sačuvaj, i pošalji (commit + push) promenu na GitHub — sledeći
put kad se skripta pokrene koristiće nove vrednosti.

## Veb aplikacija (favoriti, blokirani oglasi, ključne reči)

`docs/index.html` je cela aplikacija u jednom fajlu — nema servera ni baze.
Čita katalog oglasa iz `data/listings.json` i upisuje tvoja podešavanja u
`config.json`, direktno preko GitHub API-ja.

**Kako je pokrenuti:** otvori `docs/index.html` u pregledaču (može i sa diska),
ili je okači na bilo koji besplatan hosting (Cloudflare Pages, Netlify, Vercel,
ili GitHub Pages ako repo prebaciš na public).

**Token:** treba ti fine-grained token sa pristupom samo ovom repozitorijumu i
dozvolom **Contents: Read and write**
(https://github.com/settings/personal-access-tokens/new). Uneseš ga jednom u
tabu Podešavanja; čuva se samo u tvom pregledaču (localStorage) i nikad ne
završava u repozitorijumu.

Šta možeš iz aplikacije:

| Tab | Šta radi |
|---|---|
| Oglasi | Svi oglasi iz kataloga, pretraga po naslovu i opisu, ★ favorit / sakrij |
| Favoriti | Oglasi koje pratiš — javljamo promenu cene i kad nestanu sa sajta |
| Blokirani | Oglasi koje više ne želiš u mejlu (možeš ih vratiti nazad) |
| Reči | Ključne reči — oglas ispada ako se reč nađe u naslovu, opisu **ili adresi** |

Ključne reči ne razlikuju velika/mala slova ni kvačice, i traže se kao deo reči:
`prizemlj` hvata i „prizemlje" i „prizemlju". Koristi koren reči, ne ceo oblik.

Pretražuje se i adresa oglasa, ne samo naslov i opis — zato `Ledine` ili
`Ivana Ribara` izbacuju oglas i kad se ulica nigde ne pominje u naslovu.

## Pokretanje sa telefona
Instaliraj GitHub aplikaciju → otvori repo → **Actions** → „Dnevna provera
stanova" → **Run workflow**. Provera kreće odmah, ne moraš da čekaš 7h ujutru.

## Napomene
- Prva provera će verovatno poslati priličan broj oglasa odjednom (sve što
  trenutno postoji i zadovoljava kriterijume). Od drugog dana dalje dobijaš
  samo NOVE oglase.
- Skripta čuva katalog svih viđenih oglasa u `data/listings.json` i sama ga
  ažurira nazad u repo posle svakog pokretanja. `seen_listings.json` je stari
  format — prenosi se u katalog automatski, pri prvom pokretanju.
- Naslov i opis se skidaju sa stranice oglasa, ali samo jednom po oglasu —
  posle toga se čitaju iz kataloga, pa provere ostaju brze.
- Favorit se prijavljuje kao nestao tek kad ga nema u tri provere zaredom;
  jedno preskakanje obično znači samo da je ispao van prve tri strane.
- Sajtovi s vremena na vreme menjaju izgled stranice, što može da pokvari
  parsiranje — ako ti mejlovi prestanu da stižu ili izgledaju čudno, javi mi
  pa ću prilagoditi kod.
- Ovo je lični alat za praćenje javno dostupnih oglasa — ne koristi se za
  masovno preuzimanje ili komercijalne svrhe.
