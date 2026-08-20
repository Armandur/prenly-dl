# prenly-dl

Laddar ner tidningsnummer som PDF från Prenly-plattformen (Textalk), med
webbgränssnitt, schemaläggning och jobbkö.

> Verktyget är till för personligt bruk, för tidningar du redan har laglig
> tillgång till via din prenumeration.

## Funktioner

- Webbui för publikationer, utgåvor, jobb och schema.
- Schemalagd katalogkontroll (cron) som köar nedladdning av nya utgåvor.
- Jobbkö med förloppsvisning och historik.
- Statusmärkning per publikation när inloggningsuppgifterna slutat gälla,
  i stället för ett dagligt jobb som tyst misslyckas.
- CLI för engångsnedladdning, import och manuell körning.

## Köra med Docker

```bash
cp .env.example .env   # valfritt: sätt PRENLY_PASSWORD och PRENLY_SECRET_KEY
docker compose up -d
```

Webbui:t ligger sedan på port 8000. Volymer:

| Sökväg    | Innehåll                                       |
|-----------|------------------------------------------------|
| `/data`   | SQLite-databas med publikationer och historik  |
| `/output` | Nedladdade PDF:er, en mapp per publikation     |
| `/conf`   | Gamla JSON-konfigurationer, för engångsimport  |

### Migrera från de gamla JSON-konfigurationerna

Databasen är sanningen sedan version 0.2. De gamla conf-filerna importeras
en gång, antingen från Inställningar i webbui:t eller med:

```bash
docker exec prenly-dl prenly-dl import /conf
```

Efter importen behövs inte längre userscriptet som startade containern en
gång per konfigurationsfil - schemaläggaren sköter körningarna.

## Köra lokalt

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[web]"
prenly-dl serve
```

## Kommandon

```bash
prenly-dl serve --port 8000        # webbui och schemaläggare
prenly-dl import /conf             # importera JSON-konfigurationer
prenly-dl poll                     # kör katalogkontroll och nedladdningar nu
prenly-dl poll --publication dn    # bara en publikation
prenly-dl verify                   # testa alla sparade uppgifter
prenly-dl download --json conf.json [--force]
prenly-dl --json=conf.json         # gamla anropsformen fungerar fortfarande
```

## Miljövariabler

| Variabel               | Standard              | Betydelse                                  |
|------------------------|-----------------------|--------------------------------------------|
| `PRENLY_DB`            | `data/prenly.db`      | Sökväg till databasen                      |
| `PRENLY_OUTPUT`        | `output`              | Rotkatalog för PDF:er                      |
| `PRENLY_TZ`            | `Europe/Stockholm`    | Tidszon för schema och tidsstämplar        |
| `PRENLY_SECRET_KEY`    | -                     | Nyckel för sessionscookien                 |
| `PRENLY_PASSWORD`      | tomt                  | Sätts för att kräva inloggning             |

Databasen innehåller inloggningsuppgifter i klartext. Sätt `PRENLY_PASSWORD`
om tjänsten kan nås av någon annan, och lägg databasfilen där bara du kommer
åt den.

## Hämta inloggningsuppgifter

Uppgifterna har begränsad livslängd och behöver förnyas då och då. När en
publikation får statusen "Utloggad" i webbui:t, eller när du vill se vad
kontot faktiskt kan läsa vid "Ingen prenumeration":

1. Logga in på tidningens läsarsida i webbläsaren.
2. Öppna `/uppgifter` i prenly-dl, kopiera konsolsnutten och kör den i
   webbläsarens konsol (F12 → Console) på läsarsidan.
3. Klistra in resultatet i inklistringsytan på samma sida. Ett enskilt
   JSON-block eller hela konsolutskriften fungerar lika bra - alla
   konfigurationer i texten plockas ut, du väljer vilka som ska sparas, och
   publikationer du redan har känns igen på sajt och titel-ID och får de nya
   uppgifterna i stället för att läggas till på nytt.

Snutten läser klientnyckeln ur sidans egen `PrenlyData`-konfiguration och
sessionstoken ur `localStorage`, och frågar sedan Prenly vilka publikationer
kontot kan läsa. Ingen letning i Network-fliken, och publikationens ID
behöver inte vara känt i förväg.

Säger snutten att en publikation inte är läsbar stämmer uppgifterna, men
prenumerationen täcker inte den titeln. Nya uppgifter hjälper alltså inte
där - däremot kan tidningen finnas under ett annat publikations-ID, vilket
snuttens tabell visar.

### Vad arkivet innehåller

Katalogen kapar `limit` vid 100 poster per anrop, men `offset` går djupt -
hur långt bakåt beror på utgivaren. Tidningen Ångermanland ger över 2000
utgåvor tillbaka till 2019, medan Dagens Nyheter tar slut efter 31. Knappen
"Hämta hela katalogen" på publikationssidan hämtar allt som finns och kör
som ett jobb, eftersom det kan bli många anrop.

## Utveckling

```bash
pip install -e ".[web,dev]"
pytest
```
