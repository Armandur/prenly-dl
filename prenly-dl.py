import getopt
import json
import os
import sys
from glob import glob
import img2pdf

import requests
from PyPDF2 import PdfFileMerger
from PyPDF2.errors import PdfReadError


def getContextToken(session, conf):
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
        "Sec-Fetch-Site": "cross-site"
    }
    token = json.loads(session.get(url, headers=headers).text)["token"]

    return token


def getCatalogueIssues(session, conf, contextToken, limit=50):
    issues = []
    url = f"https://apicdn.prenly.com/api/web-reader/v1/issues?title_ids[]={conf['publication']['title']}&limit={limit}&context_token={contextToken}"
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
    JSON = json.loads(response.text)

    for issue in JSON:
        issues.append(issue["uid"])
    return issues


def getIssueJSON(session, issue, conf):
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
        "Sec-Fetch-Site": "cross-site"
    }

    data = json.dumps({
        "jsonrpc": "2.0",
        "method": "Issue.get",
        "params": {
            "title_id": str(issue["title"]),
            "issue_uid": issue["uid"]
        },
        "id": 1
    })

    req = session.post(url, data=data, headers=headers)

    JSON = json.loads(req.text)

    if "error" in JSON:
        print(f"POST {url} - {JSON['error']}", file=sys.stderr)
        sys.exit(1)

    return JSON


def getPDF(session, conf, hash, cdn="https://mediacdn.prenly.com"):
    # Some sites may use another cdn

    url = f"{cdn}/api/v2/media/get/{conf['publication']['title']}/{hash}?h=abc"

    headers = {
        "Origin": conf["credentials"]["site"],
        "Referer": f"{conf['credentials']['site']}/",
        "Accept": "application/pdf",
        "Accept-Language": "sv-SE,sv;q=0.8,en-US;q=0.5,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site"
    }

    response = session.get(url, headers=headers)
    if response.status_code != 200:
        print(f"GET {url} Error {response.status_code}", file=sys.stderr)
        sys.exit(1)

    return response


def getHashes(JSON):
    hashes = {}

    for spread in JSON["result"]["replica_spreads"]:
        for page in spread["pages"]:
            number = int(page["page_no"])
            # pad the numbers to get them in order when globbing pdfs for merging
            page_num = str(number).zfill(3)
            hashes[page_num] = page["media"][0]["checksum"]

    return hashes


def pdfMerge(title):
    merger = PdfFileMerger()
    allpdfs = [a for a in glob(f"{title}*.pdf")]
    currentpdf = allpdfs[0]
    try:
        for pdf in allpdfs:
            currentpdf = pdf
            merger.append(pdf)
    except PdfReadError as error:
        print(f"{repr(error)} - File: {currentpdf}", file=sys.stderr)
        print(
            "You can try to merge the files yourself, we won't delete them", file=sys.stderr)
    else:
        with open(f"{title}.pdf", "wb") as merged:
            merger.write(merged)
        merger.close()

        try:
            for pdf in allpdfs:
                os.remove(pdf)
        except OSError as error:
            print(repr(error), file=sys.stderr)


def main(conf):
    if "folder" in conf and conf["folder"] != "":
        if not os.path.exists(conf["folder"]):
            os.makedirs(conf["folder"])
        os.chdir(conf["folder"])

    session = requests.Session()
    session.headers.update(
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:98.0) Gecko/20100101 Firefox/98.0"})

    # download newest to limit number of issues of publication
    if "limit" in conf["publication"]:
        uids = getCatalogueIssues(session, conf, getContextToken(
            session, conf), conf["publication"]["limit"])
        conf["publication"]["uids"] = uids

    if len(conf["publication"]["uids"]) == 0 or "uids" not in conf["publication"]:
        print("No uids supplied", file=sys.stderr)
        exit(1)

    if "prefix" not in conf:
        conf["prefix"] = ""

    for uid in conf["publication"]["uids"]:
        issue = {
            "title": conf["publication"]["title"],
            "uid": uid,
            "site": conf["credentials"]["site"]
        }

        JSON = getIssueJSON(session, issue, conf)
        hashes = getHashes(JSON)  # Extract the hashes for individual pages

        # Get all PDFs and write them to files.
        for page_num in hashes:
            # Custom CDN supplied
            if "cdn" in conf["credentials"] and conf["credentials"]["cdn"] != "":
                req = getPDF(
                    session, conf, hashes[page_num], conf["credentials"]["cdn"])
            else:
                req = getPDF(session, conf, hashes[page_num])

            name = f"{conf['prefix']}{JSON['result']['name']}"

            content = req.content

            # Sometimes we might just not get a pdf, convert response to pdf instead.
            if req.headers["content-type"] in ("image/webp", "image/jpeg", "image/png", "image/gif", "image/svg"):
                print(
                    f"{name} - page {page_num} is image/, not application/pdf, converting file to pdf", file=sys.stderr)
                image = req.content
                content = img2pdf.convert(image)

            with open(f"{name} - {page_num}.pdf", "wb") as file:
                file.write(content)

        pdfMerge(name)

    return


def opts(argv):
    usage = "test"  # TODO Put a helpful text here
    if len(argv) == 0:
        print("No parameters given")
        print(usage)
        sys.exit(1)

    conf = {}

    try:
        # publication-id, issue, site, cdn, textalk, auth, prefix
        opts, args = getopt.getopt(argv, "p:i:s:c:u:a:o:", [
                                   "publication=", "issue=", "site=", "cdn=", "textalk=", "auth=", "json=", "help"])
    except getopt.GetoptError as error:
        print(repr(error), file=sys.stderr)
        print(usage)
        sys.exit(2)

    for opt, arg in opts:
        if opt == "--json":
            with open(arg, "r", encoding="utf-8") as file:
                conf = json.loads(file.read())
            break
        # TODO rest of options, build config python with supplied options

    main(conf)


if __name__ == '__main__':
    opts(sys.argv[1:])
    sys.exit(0)
