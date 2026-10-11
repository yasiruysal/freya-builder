"""Generate mockbank-final/worker.js from worker.src.js + the case's test customers (nothing typed by hand).

    python3 mockbank-final/build.py
"""
import json, pathlib

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE.parent / "cases" / "FreyaBank-Final" / "test-environment" / "test-customers.json"

data = json.loads(DATA.read_text(encoding="utf-8"))
src = (HERE / "worker.src.js").read_text(encoding="utf-8")
assert src.count("const DATA = __CUSTOMERS__;") == 1
out = src.replace("const DATA = __CUSTOMERS__;", "const DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";")
(HERE / "worker.js").write_text(out, encoding="utf-8")
print(f"worker.js: {len(data['customers'])} customers, {len(out)} bytes")
