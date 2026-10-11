"""Check a workflow against the bank's tool API, deterministically. Nothing here depends on a brief.

    python3 tools/contract.py out/agent.workflow.json tools/<tool api>.json

Reads every tool of the API (path, body fields, required fields, enums) and every api_call in the workflow, then reports:
  ERROR  a call to a URL that is not one of the API's tools (only the bank's tool API may be called)
  ERROR  a body field the tool does not have, or a required field the call never sends
  ERROR  a fixed (baked) value that is not one of the field's allowed values (e.g. an outcome name of another bank)
  WARN   an allowed value that no call can ever send (an outcome, reason, queue or identity field left unused)
Exit code 1 when there is an ERROR. Fix every ERROR; read every WARN and decide whether the brief really never uses it.
"""
import json, re, sys


def load(path):
    raw = json.load(open(path)); return raw.get("workflow", raw)


def contract(api):
    server = (api.get("servers") or [{}])[0].get("url", "").rstrip("/")
    tools = {}
    for path, ops in (api.get("paths") or {}).items():
        for method, op in ops.items():
            schema = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
            schema = resolve(api, schema); props = {k: resolve(api, v) for k, v in (schema.get("properties") or {}).items()}
            tools[path.strip("/").split("/")[-1]] = {
                "url": server + path, "method": method.upper(), "required": set(schema.get("required") or []),
                "fields": {k: {"enum": v.get("enum"), "type": v.get("type"), "schema": v} for k, v in props.items()}}
    return server, tools


def resolve(api, s):
    """Follow a local $ref (#/components/schemas/X), recursively for nested properties."""
    for _ in range(10):
        if not isinstance(s, dict) or "$ref" not in s: break
        node = api
        for part in s["$ref"].lstrip("#/").split("/"): node = (node or {}).get(part, {})
        s = node
    if isinstance(s, dict) and s.get("properties"):
        s = {**s, "properties": {k: resolve(api, v) for k, v in s["properties"].items()}}
    return s


def nested(where, path, wspec, aspec, errors):
    """A nested object of the body against the contract: unknown fields, missing required fields, fixed values outside an enum."""
    aprops = aspec.get("properties") or {}; wprops = (wspec or {}).get("properties") or {}
    for k, w in wprops.items():
        here = f"{path}.{k}"
        if k not in aprops: errors.append(f"{where}: sends {here}, which the contract does not have (fields: {', '.join(aprops)})"); continue
        a = aprops[k]
        if a.get("type") == "object" and a.get("properties"):
            if isinstance(w, dict) and w.get("properties"): nested(where, here, w, a, errors)
            continue
        d = (w or {}).get("default") if isinstance(w, dict) else None
        if a.get("enum") and isinstance(d, str) and "{{" not in d and d not in a["enum"]:
            errors.append(f"{where}: {here} is fixed to '{d}', allowed: {', '.join(map(str, a['enum']))}")
    for req in aspec.get("required") or []:
        if req not in wprops: errors.append(f"{where}: never sends required field {path}.{req}")


def calls(wf):
    for name, n in list(wf.get("nodes", {}).items()) + list((wf.get("global_nodes") or {}).items()):
        for t in (n.get("tools") or []) + (n.get("pre_actions") or []): yield name, t
    for t in wf.get("tools") or []: yield "(workflow)", t


def strings_in_code(wf):
    """Every quoted string in code nodes and hooks: values written there can reach a body through {{var}}."""
    out = set()
    for _, t in calls(wf):
        for k in ("pre_process_code", "post_process_code"):
            out |= set(re.findall(r"['\"]([A-Za-z0-9_ .:-]{1,40})['\"]", t.get(k) or ""))
        code = (((t.get("function") or {}).get("parameters") or {}).get("properties") or {}).get("code", {}).get("default", "")
        out |= set(re.findall(r"['\"]([A-Za-z0-9_ .:-]{1,40})['\"]", code or ""))
    for n in wf.get("nodes", {}).values():
        for e in n.get("extraction_schema") or []: out |= {str(v) for v in e.get("enum_values") or []}
    return out


EXTRACTION_TYPES = {"string", "integer", "float", "boolean", "date", "datetime", "enum", "name", "phone_number", "object"}   # verified: Freya rejects any other


def pair_rules(api):
    """From the tool API's descriptions: "Required for every outcome except A and B" -> {(tool, field): (sibling field, {A, B})}."""
    out = {}
    for path, ops in (api.get("paths") or {}).items():
        for op in ops.values():
            props = ((((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}).get("properties") or {}
            for f, s in props.items():
                d = (s.get("description") or "").lower()
                if "except" not in d: continue
                for g, gs in props.items():
                    vals = {v for v in gs.get("enum") or [] if re.search(r"\b" + re.escape(str(v).lower()) + r"\b", d)}
                    if g != f and vals: out[(path.strip("/").split("/")[-1], f)] = (g, vals)
    return out


def close_code_errors(wf, api):
    """Close codes must be fixed per close node, chosen by routes or code; never taken from a variable a model extracts,
    and every close node must send or omit the reason as the tool API requires for its outcome."""
    errors = []
    extracted = {e.get("name") for n in list(wf.get("nodes", {}).values()) + list((wf.get("global_nodes") or {}).values()) for e in n.get("extraction_schema") or []}
    rules = pair_rules(api)
    for node, t in calls(wf):
        props = ((t.get("function") or {}).get("parameters") or {}).get("properties") or {}
        tool = str((props.get("url") or {}).get("default", "")).rstrip("/").split("/")[-1]
        body = {k: str((v or {}).get("default", "")) for k, v in ((props.get("body") or {}).get("properties") or {}).items() if isinstance(v, dict)}
        if tool == "close_call":
            for k, d in body.items():
                m = re.fullmatch(r"\{\{\s*([^}:]+?)\s*\}\}", d)
                if k != "customer_id" and m and m.group(1) in extracted:
                    errors.append(f"{node}: close_call.{k} comes from the extracted variable {m.group(1)}; use one close node per outcome/reason with fixed values, chosen by routes or a code node")
        for (tl, f), (g, exempt) in rules.items():
            if tl != tool or g not in body: continue
            gv, sends = body[g], f in body and body[f] != ""
            if "{{" in gv and f in body and "{{" not in body[f]:
                errors.append(f"{node}: {tool}.{g} is a variable but {f} is always '{body[f]}'; {sorted(exempt)} take no {f}: one node per {g}")
            elif "{{" not in gv and gv in exempt and sends and "{{" not in body[f]:
                errors.append(f"{node}: {tool}.{g}={gv} takes no {f}, but the node sends '{body[f]}'")
            elif "{{" not in gv and gv not in exempt and not sends:
                errors.append(f"{node}: {tool}.{g}={gv} requires {f}, but the node sends none")
    return errors


def check(wf, api):
    server, tools = contract(api)
    errors, warns, used = [], [], {}
    if not tools:                     # no machine-readable contract (e.g. a Markdown service description): only the generic checks
        return close_code_errors(wf, api), ["no machine-readable service contract: request fields were NOT checked against the services"]
    for node, t in calls(wf):
        fn = t.get("function") or {}
        if fn.get("handler") != "__function__:api_call": continue
        props = (fn.get("parameters") or {}).get("properties") or {}
        url = str((props.get("url") or {}).get("default", ""))
        name = url.rstrip("/").split("/")[-1]
        where = f"{node}/{fn.get('name')}"
        if name not in tools or not url.startswith(server):
            if "{{" not in url: errors.append(f"{where}: calls {url}, which is not a tool of the API ({server}/...)")
            continue
        spec = tools[name]
        body = ((props.get("body") or {}).get("properties")) or {}
        for field, leaf in body.items():
            if field not in spec["fields"]: errors.append(f"{where}: sends body.{field}, which {name} does not have (fields: {', '.join(spec['fields'])})"); continue
            sch = spec["fields"][field].get("schema") or {}
            if sch.get("type") == "object" and sch.get("properties"):          # nested request bodies ({data, call}, ...)
                if isinstance(leaf, dict) and leaf.get("properties"): nested(where, f"body.{field}", leaf, sch, errors)
                else: errors.append(f"{where}: body.{field} must be an object with fields {', '.join(sch['properties'])}; the workflow sends it as one value")
                continue
            d = (leaf or {}).get("default") if isinstance(leaf, dict) else None
            enum = spec["fields"][field]["enum"]
            if enum and isinstance(d, str) and "{{" not in d:
                if d not in enum: errors.append(f"{where}: body.{field} is fixed to '{d}', allowed: {', '.join(enum)}")
                used.setdefault((name, field), set()).add(d)
            elif enum:
                used.setdefault((name, field), set()).add("*model or variable*")
                if isinstance(leaf, dict) and leaf.get("enum") and set(leaf["enum"]) - set(enum):
                    errors.append(f"{where}: body.{field} offers the model {sorted(set(leaf['enum']) - set(enum))}, not allowed by the API")
        for req in spec["required"] - set(body):
            if req != "customer_id" or "customer_id" not in body: errors.append(f"{where}: never sends required field {req}")
    for nname, n in wf.get("nodes", {}).items():
        for e in n.get("extraction_schema") or []:
            if e.get("type") not in EXTRACTION_TYPES: errors.append(f"{nname}: extraction field {e.get('name')} has type '{e.get('type')}', which Freya rejects")
    # Seen on real Freya: when a write or the close is a tool the weak model must call, it sometimes says nothing and
    # calls nothing, and the call hangs until the customer gives up (no promise recorded, no close_call).
    for name, n in list(wf.get("nodes", {}).items()) + list((wf.get("global_nodes") or {}).items()):
        for t in n.get("tools") or []:
            url = str((((t.get("function") or {}).get("parameters") or {}).get("properties") or {}).get("url", {}).get("default", ""))
            tool = url.rstrip("/").split("/")[-1]
            if tool == "close_call":
                errors.append(f"{name}: close_call is a tool the model must call; close in an api_request node (pre_actions) reached by a route")
            elif tool in ("create_promise", "add_note", "transfer_to_agent", "schedule_callback"):
                warns.append(f"{name}: {tool} is a tool the model must call; prefer an api_request node with values validated by code")
    errors += close_code_errors(wf, api)
    code_strings = strings_in_code(wf)
    for name, spec in tools.items():
        for field, f in spec["fields"].items():
            if not f["enum"]: continue
            u = used.get((name, field), set())
            if not u: continue
            dynamic = "*model or variable*" in u
            for v in f["enum"]:
                if v not in u and not (dynamic and v in code_strings) and not (dynamic and any(v in str(x) for x in u)):
                    warns.append(f"{name}.{field} = '{v}' is allowed but no call can send it")
    return errors, warns


if __name__ == "__main__":
    errs, warns = check(load(sys.argv[1]), json.load(open(sys.argv[2])))
    for e in errs: print("ERROR", e)
    for w in warns: print("WARN ", w)
    print(f"{len(errs)} errors, {len(warns)} warnings")
    sys.exit(1 if errs else 0)
