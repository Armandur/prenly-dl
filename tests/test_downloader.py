import io

import pytest
from pypdf import PdfReader, PdfWriter

from prenly_dl import downloader
from prenly_dl.downloader import download_issue, merge_pdfs, safe_filename
from prenly_dl.errors import PrenlyError


def _skriv_pdf(path, text_bredd):
    writer = PdfWriter()
    writer.add_blank_page(width=text_bredd, height=100)
    with open(path, "wb") as handle:
        writer.write(handle)


def test_safe_filename_behaller_svenska_tecken():
    assert safe_filename("Tidningen Hemslöjd - 2026/01") == "Tidningen Hemslöjd - 2026-01"


@pytest.mark.parametrize(
    "name",
    ["CON", "con.pdf", "PRN.txt", "AUX", "NUL", "COM1", "LPT9.pdf"],
)
def test_safe_filename_prefixar_windows_reserverade_namn(name):
    assert safe_filename(name).startswith("_")


def test_safe_filename_tar_bort_avslutande_punkter_och_mellanslag():
    assert safe_filename("Nr 5 . ") == "Nr 5"


def test_safe_filename_faller_tillbaka_nar_namnet_blir_tomt():
    assert safe_filename(" . ") == "utgava"


def test_safe_filename_normaliserar_unicode_till_nfc():
    assert safe_filename("Ra\u0308ven") == safe_filename("Räven") == "Räven"


def test_langa_sokvagar_kortas_med_stabil_unik_hash(tmp_path):
    folder = downloader.safe_output_directory(tmp_path, "Publikation" * 30)
    path_a = downloader._safe_pdf_path(folder, "Nummer" * 50 + "A")
    path_b = downloader._safe_pdf_path(folder, "Nummer" * 50 + "B")

    assert len(str(path_a.absolute())) <= 259
    assert len(str(path_b.absolute())) <= 259
    assert path_a != path_b
    assert path_a.suffix == ".pdf"
    assert downloader._safe_pdf_path(folder, "Nummer" * 50 + "A") == path_a


def test_merge_pdfs_behaller_ordningen(tmp_path):
    for index, bredd in enumerate([10, 20, 30], start=1):
        _skriv_pdf(tmp_path / f"{index:05d}.pdf", bredd)
    target = tmp_path / "ihop.pdf"
    merge_pdfs(sorted(tmp_path.glob("0*.pdf")), target)
    breddar = [round(float(page.mediabox.width)) for page in PdfReader(target).pages]
    assert breddar == [10, 20, 30]


def test_merge_pdfs_utan_sidor_ger_fel(tmp_path):
    with pytest.raises(PrenlyError):
        merge_pdfs([], tmp_path / "tom.pdf")


class _Svar:
    def __init__(self, content, content_type="application/pdf"):
        self.content = content
        self.headers = {"content-type": content_type}


def _pdf_bytes(bredd):
    writer = PdfWriter()
    writer.add_blank_page(width=bredd, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_download_issue_skriver_sidorna_i_ratt_ordning(tmp_path, monkeypatch):
    # Uppslagen levereras i fel ordning: sidan 10 först, sedan 2 och 1.
    # Utan position_number är sidnumret det som bestämmer.
    issue_data = {
        "name": "2026-01-01",
        "replica_spreads": [
            {"pages": [{"page_no": 10, "media": [{"checksum": "c10"}]}]},
            {"pages": [{"page_no": 2, "media": [{"checksum": "c2"}]}]},
            {"pages": [{"page_no": 1, "media": [{"checksum": "c1"}]}]},
        ],
    }
    breddar = {"c1": 11, "c2": 22, "c10": 33}
    monkeypatch.setattr(downloader, "get_issue", lambda *a, **k: issue_data)
    monkeypatch.setattr(
        downloader, "download_media",
        lambda session, creds, title_id, checksum: _Svar(_pdf_bytes(breddar[checksum])),
    )

    result = download_issue(None, None, 1, "42", tmp_path, prefix="Tidningen - ")
    assert result.page_count == 3
    assert result.path.name == "Tidningen - 2026-01-01.pdf"
    sidbreddar = [round(float(p.mediabox.width)) for p in PdfReader(result.path).pages]
    assert sidbreddar == [11, 22, 33]
    # Tempkatalogen ska vara borta efteråt.
    assert list(tmp_path.iterdir()) == [result.path]


def test_download_issue_hoppar_over_befintlig_fil(tmp_path, monkeypatch):
    issue_data = {"name": "2026-01-01", "replica_spreads": []}
    monkeypatch.setattr(downloader, "get_issue", lambda *a, **k: issue_data)
    (tmp_path / "2026-01-01.pdf").write_bytes(b"finns")

    result = download_issue(None, None, 1, "42", tmp_path)
    assert result.skipped is True


def test_download_issue_utan_sidor_ger_fel(tmp_path, monkeypatch):
    monkeypatch.setattr(
        downloader, "get_issue", lambda *a, **k: {"name": "tom", "replica_spreads": []}
    )
    with pytest.raises(PrenlyError):
        download_issue(None, None, 1, "42", tmp_path)


def test_download_issue_tar_med_bilagorna(tmp_path, monkeypatch):
    """Uppmätt: DN:s PDF hade 24 sidor när utgåvan har 64.

    Del B och Del C ligger som egna utgåvor i insert_issues.
    """
    huvud = {
        "name": "2026-08-21",
        "replica_spreads": [
            {"position_number": 1, "pages": [{"page_no": 1, "media": [{"checksum": "a1"}]}]},
        ],
        "insert_issues": [{"title_id": 2359, "issue_uid": "b"}],
    }
    bilaga = {
        "replica_spreads": [
            {"position_number": 1, "pages": [{"page_no": 1, "media": [{"checksum": "b1"}]}]},
        ]
    }
    breddar = {"a1": 11, "b1": 22}
    hamtade = []

    import prenly_dl.api as api

    def _get_issue(session, creds, title_id, uid):
        return bilaga if str(uid) == "b" else huvud

    monkeypatch.setattr(downloader, "get_issue", _get_issue)
    monkeypatch.setattr(api, "get_issue", _get_issue)

    def _media(session, creds, title_id, checksum):
        hamtade.append((title_id, checksum))
        return _Svar(_pdf_bytes(breddar[checksum]))

    monkeypatch.setattr(downloader, "download_media", _media)

    result = download_issue(None, None, 2357, "42", tmp_path)

    assert result.page_count == 2, "bilagans sida ska med"
    sidbreddar = [round(float(p.mediabox.width)) for p in PdfReader(result.path).pages]
    assert sidbreddar == [11, 22], "huvuddelen först, sedan bilagan"
    # Bilagans media hämtas under dess egen titel.
    assert hamtade == [(2357, "a1"), (2359, "b1")]
