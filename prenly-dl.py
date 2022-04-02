import requests
import json

def getIssueJSON(session, credentials, issue):
    url = "https://content.textalk.se/api/v2/"
    
    headers = {
        "Host": "content.textalk.se",
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

    data = '{"jsonrpc":"2.0","method":"Issue.get","params":{"title_id":'+str(issue["title"])+',"issue_uid":"'+issue["uid"]+'"},"id":1}'
    req = session.post(url, data=data, headers=headers)

    return req.text

def getPDF(session, title, hash):
    url = f"https://mediacdn.prenly.com/api/v2/media/get/{title}/{hash}?h=23bcb3b0f0a0d49bb18803b189f4a61b" #What is this h=23bcv... ??? All but first page works without it, without it the first page gets as webp

    headers = {
        "Host": "mediacdn.prenly.com",
        "Accept": "application/pdf",
        "Accept-Language": "sv-SE,sv;q=0.8,en-US;q=0.5,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site"
    }

    return session.get(url, headers=headers)

def getHashes(JSON):
    hashes = {}

    for spread in JSON["result"]["replica_spreads"]:
        for page in spread["pages"]:
            hashes[page["page_no"]] = page["media"][0]["checksum"]

    return hashes

def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:98.0) Gecko/20100101 Firefox/98.0"})
    
    credentials = {
        "textalk-auth": "YOUR OWN", #Look in api request content
        "auth": "YOUR OWN" #Look in Storage/Local Storage/prenlyreadersessiontoken
    }

    issue = {
        "title": 4019,
        "uid": "490377" #uid of a specific issue
    }

    #TODO Add getopts to send title, issueid and auth as arguments, custom title instead of "title: 4019"?

    JSON = json.loads(getIssueJSON(session, credentials, issue))
    hashes = getHashes(JSON) #Extract the hashes for individual pages

    #Get all PDFs and write them to files.
    for page_num in hashes:
        req = getPDF(session, issue["title"], hashes[page_num])
        with open(f"{JSON['result']['name']} - {page_num}.pdf", 'wb') as file:
            file.write(req.content)

    #TODO Add support for writing this to a folder as well as concatenating pdf files
    #TODO Add support to supply list of issue uids

    return


if __name__ == '__main__':
	main()