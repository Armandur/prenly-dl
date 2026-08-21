# prenly-dl

Laddar ner tidningsnummer som PDF från Prenly-plattformen (Textalk), med
webbgränssnitt, schemaläggning och jobbkö.

> Verktyget är till för personligt bruk, för tidningar du redan har laglig
> tillgång till via din prenumeration.

## Funktioner

- Katalog över allt dina inloggningsuppgifter ger tillgång till, med en
  växel per titel för vad som ska bevakas.
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
2. Öppna `/credentials` i prenly-dl, kopiera konsolsnutten och kör den i
   webbläsarens konsol (F12 → Console) på läsarsidan.
3. Klistra in resultatet i rutan högst upp på samma sida. Alla titlar sajten
   exponerar sparas i katalogen, och de du kryssar i bevakas.

Snutten läser fyra värden: sajtens adress, klientnyckeln ur sidans egen
`PrenlyData`-konfiguration, sessionstoken ur `localStorage` och sajtens CDN.
Den kopierar dem till urklipp. Vilka publikationer uppgifterna ger tillgång
till frågar prenly-dl om när du klistrar in dem, så ingen letning i
Network-fliken och inget publikations-ID behöver vara känt.

Uppgifterna hör till sajten och delas av alla dess titlar: en inklistring
räcker för att förnya hela DN eller hela Ångermanland.

Står en titel som "Ingen läsrätt" i katalogen stämmer uppgifterna, men
prenumerationen täcker inte just den titeln.

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
