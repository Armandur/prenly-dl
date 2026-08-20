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
publikation får statusen "Utloggad" i webbui:t:

1. Logga in på tidningens läsarsida i webbläsaren.
2. Kör bookmarkleten i `bookmarklet.html`, eller hämta värdena för hand:
   - `auth`: `localStorage['prenlyreadersessiontoken']`, utan `Bearer `.
   - `textalk-auth`: DevTools → Network → en `api/v2`-request →
     Request Headers → `X-Textalk-Content-Client-Authorize`.
   - `cdn`: behövs bara om sajten inte använder `https://mediacdn.prenly.com`.
3. Klistra in värdena på publikationens sida och tryck "Testa uppgifter".

## Utveckling

```bash
pip install -e ".[web,dev]"
pytest
```
