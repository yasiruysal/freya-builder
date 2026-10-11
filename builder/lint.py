"""Platform-level fixes applied to every workflow the builder writes. Nothing here depends on a brief.

    python3 builder/lint.py <workflow.json>      # fixes in place, prints what it changed
"""
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def tools_of(wf):
    for name, n in list(wf.get("nodes", {}).items()) + list((wf.get("global_nodes") or {}).items()):
        for t in (n.get("tools") or []) + (n.get("pre_actions") or []): yield name, t
    for t in wf.get("tools") or []: yield "(workflow)", t


from contract import EXTRACTION_TYPES          # extraction field types Freya accepts
TYPE_ALIASES = {"number": "float", "decimal": "float", "double": "float", "int": "integer", "str": "string", "text": "string", "bool": "boolean", "time": "string"}


def fix(wf):
    changes = []
    for name, n in list(wf.get("nodes", {}).items()) + list((wf.get("global_nodes") or {}).items()):
        for e in n.get("extraction_schema") or []:
            ty = e.get("type")
            if ty not in EXTRACTION_TYPES:
                e["type"] = TYPE_ALIASES.get(str(ty).lower(), "string")
                changes.append(f"{name}: extraction field {e.get('name')} had type '{ty}', which Freya rejects; now '{e['type']}'")
    for node, t in tools_of(wf):
        fn = t.get("function") or {}
        if fn.get("handler") != "__function__:api_call": continue
        body = ((fn.get("parameters") or {}).get("properties") or {}).get("body") or {}
        changes += leaf_defaults(body, f"{node}/{fn.get('name')}: body", fn.get("name", "tool"))
    return changes


def leaf_defaults(spec, where, tool, path=()):
    """Verified on Freya: a body field without a default is never sent (only defaults reach the request), and nested
    objects are filled leaf by leaf. So every LEAF gets a default; an object with properties never gets one (a string
    default there would stand in for the whole object). A {{var}} default whose variable is unset is filled by the
    model, so a leaf's default names a variable nothing else sets."""
    changes = []
    for field, p in ((spec or {}).get("properties") or {}).items():
        if not isinstance(p, dict): continue
        here = path + (field,)
        if p.get("type") == "object" and p.get("properties"):
            if "default" in p:
                p.pop("default"); changes.append(f"{where}.{'.'.join(here)} is an object with fields: removed its own default")
            changes += leaf_defaults(p, where, tool, here)
        elif "default" not in p:
            p["default"] = "{{m_%s_%s}}" % (tool, "_".join(here))
            changes.append(f"{where}.{'.'.join(here)} had no default, now {p['default']}")
    return changes


if __name__ == "__main__":
    path = sys.argv[1]; raw = json.load(open(path)); wf = raw.get("workflow", raw)
    ch = fix(wf); json.dump(raw, open(path, "w"), indent=1)
    print("\n".join(ch) or "no changes")
