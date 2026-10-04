import os, json, urllib.request

for line in open(".env").read().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

req = urllib.request.Request(
    os.environ["LLM_BASE_URL"].rstrip("/") + "/models",
    headers={"Authorization": "Bearer " + os.environ["LLM_API_KEY"], "User-Agent": "Mozilla/5.0"},
)
for m in json.load(urllib.request.urlopen(req))["data"]:
    print(m["id"])