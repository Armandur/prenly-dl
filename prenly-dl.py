import getopt
import json
import os
import sys
from glob import glob
from PIL import Image
import img2pdf

import requests
from PyPDF2 import PdfFileMerger
from PyPDF2.utils import PdfReadError


def getIssueJSON(session, credentials, issue):
    url = "https://content.textalk.se/api/v2/"

    headers = {
        "Accept": "*/*",
        "Accept-Language": "sv-SE,en-US,en",
        "Accept-Encoding": "gzip, deflate, br",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json-rpc",
        "X-Textalk-Content-Client-Authorize": credentials["textalk-auth"],
        "X-Textalk-Content-Product-Type": "webapp",
        "Authorization": f"Bearer {credentials['auth']}",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site"
    }

    data = '{"jsonrpc":"2.0","method":"Issue.get","params":{"title_id":' + \
        str(issue["title"]) + ',"issue_uid":"' + issue["uid"] + '"},"id":1}'
    req = session.post(url, data=data, headers=headers)

    # TODO Check response for API error

    # DEBUG to print json response
    # with open("response.json", 'w') as file:
    #    file.write(req.text)

    # exit(0)

    return req.text


def getPDF(session, issue, hash, h, cdn="https://mediacdn.prenly.com"):
    # Some sites may use another cdn
    # What is this h=23bcv... ??? All but first page works without it, without it the first page gets as webp

    url = f"{cdn}/api/v2/media/get/{issue['title']}/{hash}?h={h}"

    headers = {
        "Origin": issue["site"],
        "Referer": f"{issue['site']}/",
        "Accept": "application/pdf",
        "Accept-Language": "sv-SE,sv;q=0.8,en-US;q=0.5,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site"
    }

    # TODO Check for response error, wrong CDN, h, auth etc
    response = session.get(url, headers=headers)
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
        print("You can try to merge the files yourself, we won't delete them", file=sys.stderr)
    else:
        with open(f"{title}.pdf", "wb") as merged:
            merger.write(merged)

        try:
            for pdf in allpdfs:
                # TODO Why doesn't this work? WinError 32, can't access file, in use by another process, can't find any process with procexp.exe...
                os.remove(pdf)
        except OSError as error:
            # At least we have som error handling for it...
            print(repr(error), file=sys.stderr)
            # exit(1)


def main(conf):
    # Support for multiple uids
    for uid in conf["issue"]["uids"]:
        issue = {
            "title": conf["issue"]["title"],
            "uid": uid,
            "site": conf["issue"]["site"]
        }

        session = requests.Session()
        session.headers.update(
            {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:98.0) Gecko/20100101 Firefox/98.0"})

        JSON = json.loads(getIssueJSON(session, conf["credentials"], issue))
        hashes = getHashes(JSON)  # Extract the hashes for individual pages

        # Get all PDFs and write them to files.
        for page_num in hashes:
            if "cdn" in conf["issue"] and conf["issue"]["cdn"] != "":  # Custom CDN supplied
                req = getPDF(
                    session, issue, hashes[page_num], conf["credentials"]["h"], conf["issue"]["cdn"])
            else:
                req = getPDF(session, issue,
                             hashes[page_num], conf["credentials"]["h"])
            name = JSON['result']['name']

            content = req.content

            # Sometimes we might just not get a pdf, convert response to pdf instead.
            if req.headers["content-type"] in ("image/webp", "image/jpeg", "image/png", "image/gif", "image/svg"):
                print(f"{name} - page {page_num} is image/, not application/pdf, we convert file to pdf", file=sys.stderr)
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

    config = {}

    try:
        # title issue site, cdn, textalk, auth, h=23ob...
        # TODO Custom title?
        opts, args = getopt.getopt(argv, "t:i:s:c:u:a:h:", [
                                   "title=", "issue=", "site=", "cdn=", "textalk=", "auth=", "json=", "help"])
    except getopt.GetoptError as error:
        print(repr(error), file=sys.stderr)
        print(usage)
        sys.exit(2)
    for opt, arg in opts:
        if opt == "--json":
            with open(arg, "r") as file:
                config = json.loads(file.read())
            break
        # TODO rest of options, build config with supplied options

    main(config)


if __name__ == '__main__':
    opts(sys.argv[1:])
    sys.exit(0)
