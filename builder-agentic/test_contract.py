"""Discover the practice ledger and validate generated tests before scoring agents.

No case-specific ledger aliases belong here: the practice server is the authority.
The probe opens a separate, empty session and closes it without calling a bank tool.
"""
import copy
import datetime
import json
import re
import time
import urllib.parse
import urllib.request
import uuid


RESERVED = {"outcome", "reason", "end_reason"}
SEMANTICS = {
    "scalar_types": "strict JSON types (a string is not a number; a boolean is not a number)",
    "objects": "expected object fields are a partial match; extra actual fields are allowed",
    "lists": "exact cardinality, order-free one-to-one matching; each actual item matches at most one expected item",
    "outcome": "the ledger exit when a close_call service records it; otherwise a semantic check against the actual workflow exit and conversation",
    "reason": "detail belonging to outcome; not an independent generic ledger key",
    "end_reason": "exact workflow end function name, not a ledger field; omit when only the semantic exit is known",
    "checks": "conversation assertions; use these for behaviour the mock does not record",
}


def json_type(value):
    if value is None: return "null"
    if isinstance(value, bool): return "boolean"
    if isinstance(value, str): return "string"
    if isinstance(value, (int, float)): return "number"
    if isinstance(value, dict): return "object"
    if isinstance(value, list): return "array"
    return "invalid"


def shape(value):
    # A null in an empty session says nothing about the type after a successful write.
    if value is None: return {"type": "unknown", "observed_null": True}
    out = {"type": json_type(value)}
    if isinstance(value, dict): out["properties"] = {k: shape(v) for k, v in value.items()}
    elif isinstance(value, list) and value:
        # Only observed shapes are authoritative. Empty arrays reveal no record fields.
        out["items"] = shape(value[0])
    return out


def capabilities(api, ledger=None):
    operations = []
    for path, item in (api.get("paths") or {}).items():
        if not isinstance(item, dict): continue
        for method, operation in item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"} or not isinstance(operation, dict): continue
            operations.append(path + " " + str(operation.get("operationId", "")))
    tools = [str(c.get("tool", "")) for c in (ledger or {}).get("calls", []) if isinstance(c, dict)]
    names = operations + tools
    return {"records_close": any(re.search(r"(?:^|[/\s])close_call(?:$|[/\s])", x, re.I) for x in names),
            "records_transfer": any(re.search(r"(?:^|[/\s_])transfer(?:$|[/\s_])", x, re.I) for x in names),
            "operations": operations}


def runtime_template(api):
    return {"version": 1, "status": "pending_probe", "comparator": SEMANTICS,
            "reserved_expected_keys": sorted(RESERVED), "capabilities": capabilities(api),
            "instructions": "Actual ledger keys and shapes will be discovered after tests are written. Read the mock's documented ledger if available; do not invent names or scalar types."}


def profile(api):
    result = {}
    for key, path, field in (("outcomes", "/close_call", "outcome"), ("queues", "/transfer_to_agent", "queue"), ("note_reasons", "/add_note", "reason")):
        try:
            values = api["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"][field]["enum"]
            if values: result[key] = values
        except (KeyError, TypeError): pass
    return result


def request_json(bank_url, path, body=None, timeout=5):
    req = urllib.request.Request(bank_url.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"Content-Type": "application/json", "User-Agent": "freya-builder-test-contract"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def discover(api, first_case, bank_url, request=None):
    """request injection lets offline tests exercise register/session/ledger/end."""
    runtime = runtime_template(api)
    runtime.update(bank_url=bank_url, status="unavailable")
    ask = request or (lambda path, body=None: request_json(bank_url, path, body))
    case = copy.deepcopy(first_case)
    case["id"] = "test-contract-" + uuid.uuid4().hex
    case.setdefault("brief", "test-contract"); case.setdefault("family", "test-contract"); case.setdefault("level", "-")
    customer_id = None
    runtime["probe_case_id"] = case["id"]
    try:
        registered = ask("/v1/practice/register", {"cases": [case], "profile": profile(api)})
        if registered.get("ok") is False: raise ValueError("practice register rejected the probe")
        session = ask("/v1/practice/sessions", {"case": case["id"]})
        customer_id = session["variables"]["customer_id"]
        ledger = ask("/v1/practice/ledger/" + urllib.parse.quote(str(customer_id), safe=""))
        if not isinstance(ledger, dict) or ledger.get("ok") is False:
            raise ValueError("practice ledger did not return an object")
        runtime.update(status="observed", empty_ledger=ledger,
                       ledger_keys=sorted(ledger), ledger_shape={k: shape(v) for k, v in ledger.items()},
                       capabilities=capabilities(api, ledger))
    except Exception as exc:
        runtime["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        if customer_id is not None:
            try: ask("/v1/practice/end", {"customer_id": customer_id})
            except Exception as exc: runtime["cleanup_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    runtime["observed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return runtime


def check_shape(value, observed, path):
    errors = []
    if observed["type"] == "unknown": return errors
    actual_type = json_type(value)
    if actual_type != observed["type"]:
        return [f"{path}: expected {observed['type']} from actual ledger, got {actual_type}"]
    if actual_type == "object":
        fields = observed.get("properties", {})
        for key, item in value.items():
            if key not in fields: errors.append(f"{path}.{key}: unobserved record field")
            else: errors += check_shape(item, fields[key], path + "." + key)
    elif actual_type == "array" and observed.get("items"):
        for index, item in enumerate(value): errors += check_shape(item, observed["items"], f"{path}[{index}]")
    return errors


def structure_errors(case):
    if not isinstance(case, dict): return ["test must be an object"]
    errors = []
    if not isinstance(case.get("id"), str) or not case["id"]: errors.append("id must be a nonempty string")
    for field in ("variables", "identity", "expected"):
        if not isinstance(case.get(field), dict): errors.append(field + " must be an object")
    variables = case.get("variables")
    if isinstance(variables, dict):
        try:
            if not isinstance(variables.get("now"), str): raise ValueError("not a string")
            datetime.datetime.fromisoformat(variables["now"])
        except ValueError: errors.append("variables.now must be an ISO datetime string the simulator can parse")
    if not isinstance(case.get("script"), str): errors.append("script must be a string")
    checks = case.get("checks")
    if not isinstance(checks, list) or any(not isinstance(c, str) for c in checks): errors.append("checks must be a list of strings")
    return errors


def empty_records(value, path):
    errors = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            if item == {}: errors.append(f"{path}[{index}]: empty object record drops every positive field constraint")
            else: errors += empty_records(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items(): errors += empty_records(item, path + "." + key)
    return errors


def validate(tests, runtime):
    """Return valid tests plus per-index errors; never manufacture a ledger alias."""
    if not isinstance(tests, list): return [], [{"index": None, "id": None, "errors": ["tests.json must be a list"]}]
    good, failures, seen = [], [], set()
    known = runtime.get("ledger_shape", {})
    for index, case in enumerate(tests):
        errors = structure_errors(case)
        if not isinstance(case, dict):
            failures.append({"index": index, "id": None, "errors": errors}); continue
        cid = case.get("id")
        if isinstance(cid, str) and cid:
            if cid in seen: errors.append("id must be unique")
            else: seen.add(cid)
        exp = case.get("expected")
        if isinstance(exp, dict):
            errors += empty_records(exp, "expected")
            for key, value in exp.items():
                if key in RESERVED:
                    if value is not None and not isinstance(value, str): errors.append(f"expected.{key}: must be a string or null")
                    continue
                if key not in known:
                    errors.append(f"expected.{key}: {'unknown ledger key' if runtime.get('status') == 'observed' else 'ledger key could not be verified'}; use an actual recorded key or a conversation check")
                else: errors += check_shape(value, known[key], "expected." + key)
                if key == "transfers" and value and not runtime.get("capabilities", {}).get("records_transfer"):
                    errors.append("expected.transfers: positive transfers are not recorded by any exposed transfer API/tool; check the actual semantic exit and conversation instead")
        if errors: failures.append({"index": index, "id": cid, "errors": errors})
        else: good.append(case)
    return good, failures
