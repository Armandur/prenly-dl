import json
import os
import sys
from glob import glob

import img2pdf
import requests
from pypdf import PdfWriter
from pypdf.errors import PdfReadError


def get_context_token(session, conf):
    url = "https://content.textalk.se/api/web-reader/v1/context-token"
    headers = {
        "Accept": "*/*",
        "Accept-Language": "sv-SE,en-US,en",
        "Accept-Encoding": "gzip, deflate, br",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json-rpc",
        "X-Textalk-Content-Client-Authorize": conf["credentials"]["textalk-auth"],
        "X-Textalk-Content-Product-Type": "webapp",
        "Authorization": f"Bearer {conf['credentials']['auth']}",
        "Origin": conf["credentials"]["site"],
        "Referer": f"{conf['credentials']['site']}/",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    token = json.loads(session.get(url, headers=headers).text)["token"]
    return token


def get_catalogue_issues(session, conf, context_token, limit=50):
    issues = []
    url = (
        f"https://apicdn.prenly.com/api/web-reader/v1/issues"
        f"?title_ids[]={conf['publication']['title']}&limit={limit}&context_token={context_token}"
    )
    headers = {
        "Accept": "*/*",
        "Accept-Language": "sv-SE,en-US,en",
        "Accept-Encoding": "gzip",
        "Origin": conf["credentials"]["site"],
        "Referer": f"{conf['credentials']['site']}/",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    response = session.get(url, headers=headers)
    data = json.loads(response.text)
    for issue in data:
        issues.append(issue["uid"])
    return issues


def get_issue_json(session, issue, conf):
    url = "https://content.textalk.se/api/v2/"
    headers = {
        "Accept": "*/*",
        "Accept-Language": "sv-SE,en-US,en",
        "Accept-Encoding": "gzip, deflate, br",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json-rpc",
        "X-Textalk-Content-Client-Authorize": conf["credentials"]["textalk-auth"],
        "X-Textalk-Content-Product-Type": "webapp",
        "Authorization": f"Bearer {conf['credentials']['auth']}",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "Issue.get",
        "params": {
            "title_id": str(issue["title"]),
            "issue_uid": issue["uid"],
        },
        "id": 1,
    })
    req = session.post(url, data=payload, headers=headers)
    result = json.loads(req.text)
    if "error" in result:
        print(f"POST {url} - {result['error']}", file=sys.stderr)
        sys.exit(1)
    return result


def get_hashes(data):
    hashes = {}
    for spread in data["result"]["replica_spreads"]:
        for page in spread["pages"]:
            number = int(page["page_no"])
            # Pad page numbers so lexical sort matches numeric sort for >9999 pages.
            page_num = str(number).zfill(5)
            hashes[page_num] = page["media"][0]["checksum"]
    # The API may deliver replica_spreads out of reading order; sort explicitly.
    return dict(sorted(hashes.items(), key=lambda kv: int(kv[0])))


def download_pdf(session, conf, checksum, cdn="https://mediacdn.prenly.com"):
    url = f"{cdn}/api/v2/media/get/{conf['publication']['title']}/{checksum}?h=abc"
    headers = {
        "Origin": conf["credentials"]["site"],
        "Referer": f"{conf['credentials']['site']}/",
        "Accept": "application/pdf",
        "Accept-Language": "sv-SE,sv;q=0.8,en-US;q=0.5,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    response = session.get(url, headers=headers)
    if response.status_code != 200:
        print(f"GET {url} Error {response.status_code}", file=sys.stderr)
        sys.exit(1)
    return response


def pdf_merge(title):
    merger = PdfWriter()
    # glob() does not guarantee any particular order, so sort explicitly.
    # Per-page files are named "{title} - {page_num}.pdf" where page_num is
    # zero-padded, so plain sorted() gives correct reading order.
    allpdfs = sorted(
        p for p in glob(f"{title} - *.pdf") if p != f"{title}.pdf"
    )
    if not allpdfs:
        print(f"No page PDFs found to merge for {title}", file=sys.stderr)
        return
    currentpdf = allpdfs[0]
    try:
        for pdf in allpdfs:
            currentpdf = pdf
            merger.append(pdf)
    except PdfReadError as error:
        print(f"{repr(error)} - File: {currentpdf}", file=sys.stderr)
        print(
            "You can try to merge the files yourself, we won't delete them",
            file=sys.stderr,
        )
    else:
        with open(f"{title}.pdf", "wb") as merged:
            merger.write(merged)
        merger.close()
        print(f'Downloaded "{title}.pdf"', file=sys.stderr)
        try:
            for pdf in allpdfs:
                os.remove(pdf)
        except OSError as error:
            print(repr(error), file=sys.stderr)
