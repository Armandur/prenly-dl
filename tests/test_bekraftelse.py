"""Destruktiva formulär måste fråga innan de skickar.

Bakgrund: bekräftelsen bands en gång vid sidladdning, men htmx byter ut
sajtraden - efter första swappen togs sajten bort utan att någon dialog
visades. Delegeringen fixade det, men ett formulär med hx-verb behöver
dessutom hx-confirm: htmx skickar sin egen request och bryr sig inte om
att submit stoppades.
"""

import re
from pathlib import Path

import pytest

MALLAR = Path(__file__).resolve().parent.parent / "prenly_dl" / "web" / "templates"
DESTRUKTIVT = re.compile(r"action=\"[^\"]*/(delete|remove|purge|clear|forget)\b")
HX_VERB = re.compile(r"\bhx-(post|put|delete|patch|get)=")


def _formular():
    for fil in sorted(MALLAR.glob("*.html")):
        for tagg in re.findall(r"<form\b[^>]*>", fil.read_text(), re.DOTALL):
            yield fil.name, " ".join(tagg.split())


def _destruktiva():
    return [(f, t) for f, t in _formular() if DESTRUKTIVT.search(t)]


def test_hittar_destruktiva_formular():
    # Utan den här faller testet tyst igenom om mallarna flyttas.
    assert _destruktiva(), "inga destruktiva formulär hittades i mallarna"


@pytest.mark.parametrize("fil,tagg", _destruktiva(), ids=lambda v: v[:40])
def test_destruktivt_formular_fragar(fil, tagg):
    assert "data-confirm=" in tagg or "hx-confirm=" in tagg, f"{fil}: {tagg}"
    if HX_VERB.search(tagg):
        assert "hx-confirm=" in tagg, (
            f"{fil}: htmx skickar sin egen request, data-confirm stoppar den inte: {tagg}"
        )
