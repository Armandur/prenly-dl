import getopt
import json
import os
import sys

import img2pdf
import requests

from prenly_dl import (
    download_pdf,
    get_catalogue_issues,
    get_context_token,
    get_hashes,
    get_issue_json,
    pdf_merge,
)


def run(conf):
    if "folder" in conf and conf["folder"] != "":
        if not os.path.exists(conf["folder"]):
            os.makedirs(conf["folder"])
        os.chdir(conf["folder"])

    session = requests.Session()
    session.headers.update(
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:98.0) Gecko/20100101 Firefox/98.0"}
    )

    if "limit" in conf["publication"]:
        uids = get_catalogue_issues(
            session, conf, get_context_token(session, conf), conf["publication"]["limit"]
        )
        conf["publication"]["uids"] = uids

    if "uids" not in conf["publication"] or len(conf["publication"]["uids"]) == 0:
        print("No uids supplied", file=sys.stderr)
        sys.exit(1)

    if "prefix" not in conf:
        conf["prefix"] = ""

    for uid in conf["publication"]["uids"]:
        issue = {
            "title": conf["publication"]["title"],
            "uid": uid,
            "site": conf["credentials"]["site"],
        }

        data = get_issue_json(session, issue, conf)
        hashes = get_hashes(data)

        file_name = f"{conf['prefix']}{data['result']['name']}.pdf"
        if os.path.isfile(file_name):
            if conf["force"] != "True":
                print(
                    f"File {file_name} already exists. Use --force to redownload",
                    file=sys.stderr,
                )
                sys.exit(1)
            else:
                print(f"Forcing redownload of {file_name}", file=sys.stderr)
                os.remove(file_name)

        for page_num in hashes:
            if "cdn" in conf["credentials"] and conf["credentials"]["cdn"] != "":
                req = download_pdf(session, conf, hashes[page_num], conf["credentials"]["cdn"])
            else:
                req = download_pdf(session, conf, hashes[page_num])

            name = f"{conf['prefix']}{data['result']['name']}"
            content = req.content

            if req.headers["content-type"] in (
                "image/webp", "image/jpeg", "image/png", "image/gif", "image/svg"
            ):
                print(
                    f"{name} - page {page_num} is image/, not application/pdf, converting file to pdf",
                    file=sys.stderr,
                )
                content = img2pdf.convert(req.content)

            with open(f"{name} - {page_num}.pdf", "wb") as f:
                f.write(content)

        pdf_merge(name)


def main():
    argv = sys.argv[1:]
    usage = "Usage: prenly-dl --json <config.json> [--force]"

    if len(argv) == 0:
        print("No parameters given")
        print(usage)
        sys.exit(1)

    conf = {}
    force = "False"

    try:
        opts, _ = getopt.getopt(
            argv,
            "p:i:s:c:u:a:o:F",
            ["publication=", "issue=", "site=", "cdn=", "textalk=", "auth=", "json=", "force", "help"],
        )
    except getopt.GetoptError as error:
        print(repr(error), file=sys.stderr)
        print(usage)
        sys.exit(2)

    for opt, arg in opts:
        if opt == "--json":
            try:
                with open(arg, "r", encoding="utf-8") as f:
                    conf = json.loads(f.read())
                break
            except OSError as error:
                print(f"Configurationfile {arg} not found!\n{repr(error)}", file=sys.stderr)
                sys.exit(1)
            except json.decoder.JSONDecodeError as error:
                print(f"Malformed JSON-file!\n{repr(error)}")
                sys.exit(1)

        if opt == "--force":
            print("Forcing redownload", file=sys.stderr)
            force = "True"

    if "force" not in conf:
        conf["force"] = force

    run(conf)


if __name__ == "__main__":
    main()
    sys.exit(0)
