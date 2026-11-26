import http.client
import json
import urllib.parse

conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=20)
target = "BROWSER_EXTENSION.md"
for q in [
    "browser extension",
    "name:browser extension",
    'name:"browser extension"',
    "browser_extension",
    "extension browser",
    "path:chess-ai browser",
]:
    path = "/api/search?q=" + urllib.parse.quote(q, safe="") + "&limit=10"
    conn.request("GET", path)
    obj = json.loads(conn.getresponse().read())
    hits = obj.get("hits", [])
    ranks = [i for i, h in enumerate(hits, 1) if h["name"].casefold() == target.casefold()]
    print(f"{q:28s} total={obj.get('total')} mode={obj.get('mode')} "
          f"rank={ranks[0] if ranks else '-'} top={[h['name'][:28] for h in hits[:3]]}")
conn.close()
