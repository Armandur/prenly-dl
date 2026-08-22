"""Hitta Prenly-läsare bland Bonnier News varumärken.

Åtkomsten är sajtbunden men prenumerationen är global: uppmätt att kontot
har bn_global_epaper och bnlo_global_epaper, alltså e-tidning över hela
Bonnier News och Bonnier News Local. Ändå måste man känna till sajten och
logga in där för att se dess titlar - Dalademokraten upptäcktes av en
slump, och gav tillgång till Expressen.

Sonderingen kostar inga inloggningar. En anonym session räcker för att
fråga vilka titlar en sajt har; först när användaren väljer att lägga
till sajten sker en inloggning.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import requests

from .api import (
    Credentials,
    create_session,
    fetch_client_key,
    get_context_token,
    get_titles,
)
from .errors import PrenlyError

logger = logging.getLogger(__name__)

# Bonniers egen förteckning. Skrapas vid varje körning så nya varumärken
# fångas, till priset av att sonderingen slutar hitta något om de gör om
# sidan - vilket syns som noll kandidater, inte som fel data.
VARUMARKEN_URL = "https://www.bonniernews.se/titlar-och-varumarken"
DOMAN = re.compile(r"https?://(?:www\.)?([a-z0-9åäö-]+\.se)")

# Bonniers egna bolagssidor, inte tidningar.
EJ_TIDNINGAR = {
    "bonniernews.se",
    "bonniernewsbrandstudio.se",
    "boldprinting.se",
    "bonway.se",
    "arcy.se",
}

# Läsarna bor på etidning.<domän> hos samtliga som hittats.
LASARPREFIX = "etidning."

# Paus mellan anropen. Sonderingen rör någon annans servrar, och 52
# domäner i följd utan paus vore oförskämt.
PAUS = 0.4


@dataclass
class Fynd:
    doman: str
    url: str
    klientnyckel: str
    titlar: list[dict] = field(default_factory=list)
    fel: str = ""

    @property
    def antal(self) -> int:
        return len(self.titlar)


def fetch_brand_domains(session: requests.Session) -> list[str]:
    """Domänerna på Bonniers varumärkessida, utan deras egna bolagssidor."""
    try:
        svar = session.get(VARUMARKEN_URL, timeout=30)
        svar.raise_for_status()
    except requests.RequestException as error:
        raise PrenlyError(f"Kunde inte läsa {VARUMARKEN_URL}: {error}") from error
    domaner = {d for d in DOMAN.findall(svar.text) if d not in EJ_TIDNINGAR}
    logger.info("Hittade %d varumärkesdomäner", len(domaner))
    return sorted(domaner)


def probe_site(session: requests.Session, doman: str) -> Fynd | None:
    """Titlarna på en sajt, via en anonym session. None om ingen läsare finns."""
    url = f"https://{LASARPREFIX}{doman}"
    try:
        nyckel = fetch_client_key(session, url)
    except PrenlyError:
        # Ingen läsare på den adressen, vilket är det vanliga svaret.
        return None

    fynd = Fynd(doman=doman, url=url, klientnyckel=nyckel)
    try:
        token = create_session(session, url, nyckel)
        creds = Credentials(site=url, textalk_auth=nyckel, auth=token)
        titlar = get_titles(session, creds, get_context_token(session, creds))
    except PrenlyError as error:
        # Läsaren finns men svarade inte. Värd att visa ändå: den går att
        # lägga till manuellt.
        fynd.fel = str(error)
        return fynd
    fynd.titlar = [
        {"id": t.get("id"), "name": t.get("name"), "readable": bool(t.get("user_readable"))}
        for t in titlar
    ]
    return fynd


def discover(
    session: requests.Session, progress=None
) -> list[Fynd]:
    """Sondera alla varumärken och returnera de som har en läsare."""
    domaner = fetch_brand_domains(session)
    fynd: list[Fynd] = []
    for gjort, doman in enumerate(domaner, start=1):
        traff = probe_site(session, doman)
        if traff is not None:
            fynd.append(traff)
        if progress:
            progress(gjort, len(domaner))
        time.sleep(PAUS)
    logger.info("%d av %d domäner har en Prenly-läsare", len(fynd), len(domaner))
    return fynd
