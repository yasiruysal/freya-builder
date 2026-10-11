"""Deploy our mock bank to Cloudflare Workers. Run it again after you add or change a case.

    source env.sh && python3 mockbank/deploy.py

Customers: public-test-set.json plus every tests/cases/*.json. Profiles: mockbank/profiles.json.
Needs CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID. Prints the bank's base URL.
"""
import glob, json, os, pathlib, urllib.request, urllib.error, uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = "freya-mockbank"
API = f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}"
AUTH = {"Authorization": "Bearer " + os.environ["CLOUDFLARE_API_TOKEN"]}


def cf(method, path, data=None, headers=None):
    req = urllib.request.Request(API + path, data=data, method=method, headers={**AUTH, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}")


def all_cases():
    cases = {}
    for f in [ROOT / "public-test-set.json", *sorted(glob.glob(str(ROOT / "tests" / "cases" / "*.json")))]:
        for c in json.load(open(f)):
            if c["id"] in cases: raise SystemExit(f"duplicate case id {c['id']} in {f}")
            cases[c["id"]] = c
    return cases


def deploy_js(src, script):
    """Deploy a finished worker (ES module with a Durable Object class `Bank` bound as BANK) under the name `script`."""
    exists = cf("GET", f"/workers/scripts/{script}/settings").get("success")
    meta = {"main_module": "worker.js", "compatibility_date": "2025-09-01",
            "bindings": [{"type": "durable_object_namespace", "name": "BANK", "class_name": "Bank"}]}
    if not exists: meta["migrations"] = {"new_tag": "v1", "new_sqlite_classes": ["Bank"]}
    b = uuid.uuid4().hex
    parts = [("metadata", "blob", "application/json", json.dumps(meta)), ("worker.js", "worker.js", "application/javascript+module", src)]
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{n}"; filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n'.encode() + x.encode() + b"\r\n" for n, fn, ct, x in parts) + f"--{b}--\r\n".encode()
    r = cf("PUT", f"/workers/scripts/{script}", body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    if not r.get("success"): raise SystemExit(f"deploy failed: {r.get('errors')}")
    cf("POST", f"/workers/scripts/{script}/subdomain", json.dumps({"enabled": True}).encode(), {"Content-Type": "application/json"})
    sub = cf("GET", "/workers/subdomain")["result"]["subdomain"]
    return f"https://{script}.{sub}.workers.dev"


def main():
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--js":       # python3 mockbank/deploy.py --js <worker.js> [name]
        name = sys.argv[3] if len(sys.argv) > 3 else "freya-final-mock"
        print("MOCK_URL=" + deploy_js(pathlib.Path(sys.argv[2]).read_text(), name)); return
    cases = all_cases()
    profiles = json.load(open(ROOT / "mockbank" / "profiles.json"))
    src = (ROOT / "mockbank" / "worker.template.js").read_text()
    src = src.replace("__CASES__", json.dumps(cases)).replace("__PROFILES__", json.dumps(profiles))
    exists = cf("GET", f"/workers/scripts/{SCRIPT}/settings").get("success")
    meta = {"main_module": "worker.js", "compatibility_date": "2025-09-01",
            "bindings": [{"type": "durable_object_namespace", "name": "BANK", "class_name": "Bank"}]}
    if not exists:
        meta["migrations"] = {"new_tag": "v1", "new_sqlite_classes": ["Bank"]}
    b = uuid.uuid4().hex
    parts = [("metadata", "blob", "application/json", json.dumps(meta)), ("worker.js", "worker.js", "application/javascript+module", src)]
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{n}"; filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n'.encode() + d.encode() + b"\r\n" for n, fn, ct, d in parts) + f"--{b}--\r\n".encode()
    r = cf("PUT", f"/workers/scripts/{SCRIPT}", body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    if not r.get("success"): raise SystemExit(f"deploy failed: {r.get('errors')}")
    cf("POST", f"/workers/scripts/{SCRIPT}/subdomain", json.dumps({"enabled": True}).encode(), {"Content-Type": "application/json"})
    sub = cf("GET", "/workers/subdomain")["result"]["subdomain"]
    url = f"https://{SCRIPT}.{sub}.workers.dev"
    print(f"deployed {len(cases)} cases: {', '.join(cases)}")
    print("MOCK_BANK_URL=" + url)


if __name__ == "__main__":
    main()
