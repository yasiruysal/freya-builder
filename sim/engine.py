"""A local stand-in for Freya's workflow engine, so a phone agent can be tested without the platform.

It follows docs/workflow-format.md and docs/tools-and-code.md: nodes, transitions (variable checks in code,
intents by one routing call), extraction, the dialogue call with tools, api_call tools with baked defaults,
pre/post hooks, response_mapping and flattened result keys, code nodes, passthrough and end_call nodes.
Model calls go through the `claude` CLI (the user's Claude login), so no API key is needed.

Differences from the platform are expected. Calibrate against Freya results before trusting a number.
"""
import copy, datetime, json, re, subprocess, tempfile, time, urllib.error, urllib.request

OFFICIAL_BANK = "https://freya-bank-mock.freyavoice.workers.dev"
NEUTRAL_CWD = tempfile.mkdtemp(prefix="freya-sim-")       # no project settings or memory leak into model calls
MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
STATS = {"calls": 0, "seconds": 0.0}


# ---------------------------------------------------------------- model calls

def llm(model, system, prompt, retries=2):
    cmd = ["claude", "-p", "--model", MODELS.get(model, model), "--tools", "", "--setting-sources", "",
           "--no-session-persistence", "--system-prompt", system, "--output-format", "json"]
    for attempt in range(retries + 1):
        t = time.time()
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=180, cwd=NEUTRAL_CWD)
            STATS["calls"] += 1; STATS["seconds"] += time.time() - t
            out = json.loads(p.stdout)
            if not out.get("is_error"): return out.get("result") or ""
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("model call failed")


def parse_json(text, default=None):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m: return default
    try: return json.loads(m.group(0))
    except Exception: return default


# ---------------------------------------------------------------- variables and templates

def to_str(v):
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, (dict, list)): return json.dumps(v)
    return str(v)


def render(text, vars):
    def sub(m):
        name, fmt = m.group(1).strip(), m.group(2)
        if name not in vars or vars[name] is None: return m.group(0)
        return json.dumps(vars[name]) if fmt == "json" else to_str(vars[name])
    return re.sub(r"\{\{\s*([^}:]+?)\s*(?::(json))?\s*\}\}", sub, text) if isinstance(text, str) else text


def flatten(prefix, value, out):
    """Like the platform: nested objects and lists become dotted keys, lists by index (x.products.0.type)."""
    if isinstance(value, dict):
        for k, v in value.items(): flatten(f"{prefix}.{k}", v, out)
    elif isinstance(value, list):
        for i, v in enumerate(value): flatten(f"{prefix}.{i}", v, out)
    else:
        out[prefix] = to_str(value) if isinstance(value, bool) else value


# ---------------------------------------------------------------- the engine

class Call:
    def __init__(self, workflow, variables, agent_model="haiku", bank_url=None, log=None, bank_from=OFFICIAL_BANK):
        self.wf = json.loads(json.dumps(workflow).replace(bank_from.rstrip("/"), bank_url.rstrip("/"))) if bank_url else workflow
        self.vars = dict(self.wf.get("test_variables") or {}); self.vars.update(variables)
        self.model = agent_model
        self.history = []          # dicts: {"role": "agent"|"customer"|"tool", ...}
        self.events = log if log is not None else []
        self.node = None; self.prev = None; self.ended = False; self.end_reason = None
        self.node_turns = {}

    # -- public

    def start(self):
        """Run the initial node. Returns what the agent says first (may be empty)."""
        for t in self.wf.get("tools") or []:
            if "start_of_call" in ((t.get("config") or {}).get("alwaysRunsAt") or []): self.run_tool(t, {}, engine=True)
        return self.enter(self.wf["initial_node"], "start")

    def user(self, text):
        """One customer message in, the agent's reply out."""
        self.history.append({"role": "customer", "text": text})
        self.node_turns[self.node] = self.node_turns.get(self.node, 0) + 1
        self.vars[f"{self.node}.current_turns"] = int(self.vars.get(f"{self.node}.current_turns") or 0) + 1   # 1 on the first routed message
        self.vars[f"{self.node}.total_turns"] = int(self.vars.get(f"{self.node}.total_turns") or 0) + 1
        node = self.cur()
        self.extract(node)
        said = []
        cap = node.get("max_node_turns")
        if cap and self.node_turns[self.node] > cap and node.get("max_turn_transition"):
            said.append(self.enter(node["max_turn_transition"], "max turns"))
        else:
            target = self.route(node, after="user")
            said.append(self.enter(target, "route") if target else self.dialogue())
        return " ".join(s for s in said if s).strip()

    def finish(self):
        for t in self.wf.get("tools") or []:
            if "end_of_call" in ((t.get("config") or {}).get("alwaysRunsAt") or []): self.run_tool(t, {}, engine=True)

    # -- nodes

    def cur(self):
        return self.wf["nodes"].get(self.node) or (self.wf.get("global_nodes") or {}).get(self.node) or {}

    def enter(self, target, reason, depth=0):
        if target == "__previous__": target = self.prev
        if depth > 12 or target is None: return ""
        if target != self.node: self.prev = self.node
        first = target not in self.node_turns
        self.node = target; self.node_turns.setdefault(target, 0)
        self.vars[f"{target}.current_turns"] = 0                       # reset on every entry, also a self-transition
        self.vars.setdefault(f"{target}.total_turns", 0)
        self.events.append({"event": "node_enter", "node": target, "reason": reason})
        n = self.cur()
        kind = n.get("node_type", "conversation")
        if kind == "end_call":
            self.ended = True
            fn = (n.get("terminal") or {}).get("function") or {}
            self.end_reason = fn.get("name")
            props = (fn.get("parameters") or {}).get("properties") or {}
            mode = (props.get("message_mode") or {}).get("default", "none")
            if mode == "custom":
                line = render((props.get("spoken_message") or {}).get("default", ""), self.vars)
                if line: self.history.append({"role": "agent", "text": line})
                return line
            if mode == "llm": return self.dialogue(extra=render((props.get("llm_prompt") or {}).get("default", "Say goodbye."), self.vars), allow_tools=False)
            return ""
        oe = n.get("on_enter") or ({"type": "silent"} if kind in ("code", "api_request") else {"type": "llm"})
        if kind in ("code", "api_request") or n.get("passthrough"):
            line = ""                                  # a fixed line on a passthrough node is said before its actions run
            if oe.get("type") == "tts" and not (n.get("on_enter_first_entry_only") and not first):
                line = render(oe.get("text", ""), self.vars); self.history.append({"role": "agent", "text": line})
            for a in n.get("pre_actions") or []:
                self.run_tool(a, {}, engine=True, node_name=target, node_mapping=n.get("response_mapping"))
            nxt = self.route(n, after="entry") or n.get("passthrough_default_transition")
            rest = self.enter(nxt, "passthrough", depth + 1) if nxt else ""
            return " ".join(x for x in [line, rest] if x)
        if n.get("on_enter_first_entry_only") and not first: return self.dialogue()
        if oe.get("type") == "tts":
            line = render(oe.get("text", ""), self.vars); self.history.append({"role": "agent", "text": line}); return line
        if oe.get("type") == "silent": return ""
        return self.dialogue(extra=oe.get("prompt"))

    # -- extraction

    def extract(self, node):
        schema = node.get("extraction_schema") or []
        if not schema: return
        spec = "\n".join(f"- {e['name']} ({e.get('type', 'string')}{', one of: ' + ', '.join(map(str, e['enum_values'])) if e.get('enum_values') else ''}): {e.get('description', '')}" for e in schema)
        today = str(self.vars.get("now", ""))[:10]
        out = parse_json(llm(self.model, "You extract structured values from a phone conversation. Reply with one JSON object only.",
                             f"Today is {today}. Recent conversation:\n{self.transcript(10)}\n\nVariables to fill from what the customer said:\n{spec}\n\n"
                             "Return {\"name\": value, ...}. Use null when the conversation does not give a value. Dates as YYYY-MM-DD."), {}) or {}
        for e in schema:
            v = out.get(e["name"])
            if v is None or v == "": continue
            if e.get("enum_values") and str(v) not in map(str, e["enum_values"]): continue
            if e.get("validation_pattern") and not re.fullmatch(e["validation_pattern"], str(v)): continue
            self.vars[e["name"]] = v
            self.events.append({"event": "variable_extracted", "name": e["name"], "value": v})

    # -- routing

    def route(self, node, after):
        cands = [("loop", node["loop_condition"], self.node)] if node.get("loop_condition") and after == "user" else []
        cands += [("t", t.get("condition") or {"operator": "always"}, t["target_node"]) for t in node.get("transitions") or [] if not t.get("is_tool_call")]
        for name, g in (self.wf.get("global_nodes") or {}).items():
            if not g.get("is_tool_call") and self.node not in (g.get("excluded_nodes") or []) and name != self.node and after == "user":
                cands.append(("g", g.get("condition") or {}, name))
        intents, judged = [], None
        for kind, c, target in cands:
            if isinstance(c, str): c = {"operator": "matches_intent", "description": c}
            r = self.cond(c, intents, judged)
            if r is None:                         # an intent decides it: ask the routing model once for all intents
                if judged is None:
                    judged = self.judge_intents(node, self.collect_intents([x[1] for x in cands]))
                r = self.cond(c, intents, judged)
            if r:
                if kind == "loop": return None
                self.events.append({"event": "route", "from": self.node, "to": target})
                return target                       # a self-transition is a re-entry on Freya (on_enter runs again)
        return None

    def collect_intents(self, conds):
        out = []
        def walk(c):
            if isinstance(c, str): out.append(c); return
            if c.get("operator") == "matches_intent": out.append(c.get("description", ""))
            for x in c.get("conditions") or []: walk(x)
        for c in conds: walk(c)
        return list(dict.fromkeys(out))

    def judge_intents(self, node, intents):
        if not intents or not any(h["role"] == "customer" for h in self.history): return set()
        listing = "\n".join(f"{i + 1}. {d}" for i, d in enumerate(intents))
        extra = ("\n\nInstruction for this step: " + render(node["routing_prompt"], self.vars)) if node.get("routing_prompt") else ""
        out = parse_json(llm(self.model, "You decide which intent descriptions match the customer's LAST message in a phone call. Reply with one JSON object only.",
                             f"Conversation:\n{self.transcript(10)}{extra}\n\nIntent descriptions:\n{listing}\n\nReturn {{\"matched\": [numbers of every description the customer's last message matches]}}."), {}) or {}
        return {intents[i - 1] for i in out.get("matched", []) if isinstance(i, int) and 0 < i <= len(intents)}

    def cond(self, c, intents, judged):
        if isinstance(c, str): c = {"operator": "matches_intent", "description": c}
        if "conditions" in c:
            rs = [self.cond(x, intents, judged) for x in c["conditions"]]
            if (c.get("logic") or "or") == "and":
                if any(r is False for r in rs): return False
                return None if any(r is None for r in rs) else True
            if any(r is True for r in rs): return True
            return None if any(r is None for r in rs) else False
        op = c.get("operator")
        if op == "always": return True
        if op == "matches_intent": return None if judged is None else c.get("description", "") in judged
        name = c.get("variable"); v = self.vars.get(name)
        if op == "is_set": return v is not None and v != ""
        if op == "is_not_set": return v is None or v == ""
        if v is None: return False if op != "not_equals" else True
        sv, so = to_str(v), to_str(c.get("operand"))
        try:
            if op == "equals": return sv == so
            if op == "not_equals": return sv != so
            if op == "contains": return so.lower() in sv.lower()
            if op == "greater_than": return float(sv) > float(so)
            if op == "less_than": return float(sv) < float(so)
        except ValueError:
            return False
        return False

    # -- dialogue

    def tools_here(self):
        n = self.cur(); tools = []
        for t in n.get("tools") or []:
            if not ((t.get("config") or {}).get("alwaysRunsAt")): tools.append(("tool", t))
        for t in n.get("transitions") or []:
            if t.get("is_tool_call"): tools.append(("transition", t))
        for name, g in (self.wf.get("global_nodes") or {}).items():
            if g.get("is_tool_call") and self.node not in (g.get("excluded_nodes") or []): tools.append(("global", {"target_node": name, "description": g.get("description", "")}))
        return tools

    def model_params(self, tool):
        """Parameters the model fills: no default, or a {{var}} default whose variable is unset."""
        fn = tool["function"]; props = (fn.get("parameters") or {}).get("properties") or {}
        if fn.get("handler") == "__function__:api_call":
            flat = {}
            def walk(sp):
                for k, p in ((sp or {}).get("properties") or {}).items():
                    if isinstance(p, dict) and p.get("type") == "object" and p.get("properties"): walk(p)
                    elif isinstance(p, dict): flat[k] = p
            walk(props.get("body") or {}); props = flat
        out = {}
        for k, p in props.items():
            d = p.get("default")
            if d is None:
                out[k] = p
            elif isinstance(d, str):
                m = re.fullmatch(r"\{\{\s*([^}:]+?)\s*\}\}", d)
                if m and self.vars.get(m.group(1)) is None: out[m.group(1)] = p
        return out

    def dialogue(self, extra=None, allow_tools=True, depth=0):
        n = self.cur()
        task = n.get("task_message", "")
        if n.get("loop_prompt") and self.node_turns.get(self.node, 0) > 1: task = n["loop_prompt"]
        system = "\n\n".join(x for x in [render(self.wf.get("system_prompt", ""), self.vars), render(n.get("role_message", ""), self.vars),
                                          "## Current Task\n" + render(task, self.vars), render(extra or "", self.vars)] if x)
        tools = self.tools_here() if allow_tools else []
        spec = []
        for kind, t in tools:
            if kind == "tool":
                fn = t["function"]
                if fn.get("handler") == "__function__:end_call":
                    spec.append({"name": fn["name"], "description": fn.get("description", "End the call."), "parameters": {}})
                else:
                    spec.append({"name": fn["name"], "description": fn.get("description", ""), "parameters": {k: {x: p[x] for x in ("type", "description", "enum") if x in p} for k, p in self.model_params(t).items()}})
            else:
                spec.append({"name": "transition_to_" + t["target_node"], "description": t.get("description", ""), "parameters": {}})
        rules = ("You are the phone agent in a live call. Write only the words you say, one or two short sentences.\n"
                 "Reply with ONE JSON object: {\"say\": \"...\"} to speak, or {\"tool\": \"name\", \"args\": {...}} to call a tool, "
                 "or both {\"say\": \"...\", \"tool\": ..., \"args\": ...} to say something and then call the tool.\n"
                 + ("Tools you may call now:\n" + json.dumps(spec, indent=1) if spec else "No tool call is allowed now."))
        out = parse_json(llm(self.model, system + "\n\n" + rules, "Conversation so far (tool calls and results included):\n" + self.transcript() + "\n\nYour next turn as JSON:"), {"say": ""}) or {"say": ""}
        said = (out.get("say") or "").strip()
        if said: self.history.append({"role": "agent", "text": said})
        name = out.get("tool")
        if not name or depth > 5: return said
        for kind, t in tools:
            if kind == "tool" and t["function"]["name"] == name:
                if t["function"].get("handler") == "__function__:end_call":
                    self.ended = True; self.end_reason = name; return said
                self.run_tool(t, out.get("args") or {})
                target = self.route(self.cur(), after="tool")
                nxt = self.enter(target, "tool result") if target else self.dialogue(depth=depth + 1)
                return " ".join(x for x in [said, nxt] if x)
            if kind != "tool" and "transition_to_" + t["target_node"] == name:
                nxt = self.enter(t["target_node"], "tool-call transition")
                return " ".join(x for x in [said, nxt] if x)
        self.history.append({"role": "tool", "name": name, "result": f"The function `{name}` is not currently available."})
        return " ".join(x for x in [said, self.dialogue(depth=depth + 1)] if x)

    # -- tools

    def run_tool(self, tool, args, engine=False, node_name=None, node_mapping=None):
        fn = tool["function"]; name = fn.get("name", "tool")
        key = node_name or name          # Freya names the result keys and counters of pre_actions after the node
        props = (fn.get("parameters") or {}).get("properties") or {}
        if fn.get("handler") == "__function__:code_exec":
            code = (props.get("code") or {}).get("default") or args.get("code", "")
            ok = True; before = dict(self.vars)
            try:
                exec(code, {"vars": self.vars, "json": json, "re": re, "datetime": datetime})
            except Exception as e:
                ok = False; self.events.append({"event": "code_error", "node": node_name, "error": repr(e)})
            rejected = self.reject_schema_violations(before)
            result = {"status": "success" if ok else "error", "data": None, "message": "Code executed successfully" if ok else "Code failed", "status_code": 200 if ok else 500}
            if rejected: result["validation_errors"] = [f"variable '{k}' rejected by schema validation" for k in rejected]
            self.after_result(key, result, ok, tool, node_mapping)
            return result
        if fn.get("handler") != "__function__:api_call": return None
        url = render((props.get("url") or {}).get("default", ""), self.vars)

        def fill(spec):
            """Like the platform (verified on Freya): nested objects are filled leaf by leaf; a leaf without a default is
            not sent; a {{var}} default with an unset variable is filled by the model (args by variable or field name)."""
            out = {}
            for k, p in ((spec or {}).get("properties") or {}).items():
                if not isinstance(p, dict): continue
                if p.get("type") == "object" and p.get("properties"):
                    sub = fill(p)
                    if sub: out[k] = sub
                    continue
                d = p.get("default")
                m = re.fullmatch(r"\{\{\s*([^}:]+?)\s*\}\}", d) if isinstance(d, str) else None
                if m and self.vars.get(m.group(1)) is None:
                    if m.group(1) in args: out[k] = args[m.group(1)]
                    elif k in args: out[k] = args[k]
                elif d is not None:
                    out[k] = render(d, self.vars) if isinstance(d, str) else d
            return out

        body = fill(props.get("body") or {})
        for k, v in args.items(): self.vars[f"{name}_args.{k}"] = v
        config = {"url": url, "method": render((props.get("method") or {}).get("default", "POST"), self.vars), "headers": {"Content-Type": "application/json"}, "body": body}
        if tool.get("pre_process_code", "").strip():
            try: exec(tool["pre_process_code"], {"vars": self.vars, "config": config, "json": json, "re": re, "datetime": datetime})
            except Exception as e: self.events.append({"event": "hook_error", "tool": name, "error": repr(e)})
        self.history.append({"role": "tool_call", "name": name, "args": config["body"]})
        retries = int((tool.get("config") or {}).get("maxRetries") or 0)
        for attempt in range(retries + 1):
            status, data = self.http(config)
            if status < 400: break
        ok = status < 400
        result = {"status": "success", "data": data, "message": f"Successfully executed {config['method']} request", "status_code": status} if ok \
            else {"status": "error", "error": f"API request failed: {data}", "status_code": status}
        if ok and tool.get("post_process_code", "").strip():
            try: exec(tool["post_process_code"], {"result": result, "vars": self.vars, "json": json, "re": re, "datetime": datetime})
            except Exception as e: self.events.append({"event": "hook_error", "tool": name, "error": repr(e)})
        self.after_result(key, result, ok, tool, node_mapping)
        shown = {"status": "success"} if self.cur().get("suppress_tool_results") else {k: v for k, v in result.items() if k != "header"}
        self.history.append({"role": "tool", "name": name, "result": shown})
        return result

    def reject_schema_violations(self, before):
        """Seen on Freya: a variable written by code that is also declared in an extraction_schema is rejected
        (the old value stays) when the new value does not fit that schema: not in enum_values, empty, or not a date."""
        schema = {}
        for n in list(self.wf["nodes"].values()) + list((self.wf.get("global_nodes") or {}).values()):
            for e in n.get("extraction_schema") or []: schema[e["name"]] = e
        rejected = []
        for k, v in list(self.vars.items()):
            if k not in schema or before.get(k, object()) == v: continue
            e = schema[k]; sv = to_str(v)
            bad = sv == "" or (e.get("enum_values") and sv not in map(str, e["enum_values"])) \
                or (e.get("type") == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", sv)) \
                or (e.get("type") == "integer" and not re.fullmatch(r"-?\d+", sv))
            if bad:
                rejected.append(k)
                if k in before: self.vars[k] = before[k]
                else: self.vars.pop(k, None)
        return rejected

    def after_result(self, key, result, ok, tool, node_mapping):
        c = f"{key}_success_count" if ok else f"{key}_failure_count"
        self.vars[c] = int(self.vars.get(c) or 0) + 1
        if not ok:                                   # seen on Freya: an error envelope; earlier data.* keys stay
            code = result.get("status_code")
            self.vars.update({f"{key}_response.status": "error", f"{key}_response.error": f"HTTP {code}",
                              f"{key}_response.message": result.get("error", ""), f"{key}_response.status_code": code, f"{key}_status_code": code})
            return
        flat = {}; flatten(f"{key}_response", result, flat); self.vars.update(flat)
        self.vars[f"{key}_status_code"] = result.get("status_code")
        for m in (tool.get("response_mapping") or []) + (node_mapping or []):
            v = result
            for part in m["path"].split("."):
                v = v.get(part) if isinstance(v, dict) else None
            # Seen on Freya: a mapping to a list (data.products) leaves the variable unset; only scalars are mapped.
            if v is not None and not isinstance(v, (list, dict)): self.vars[m["variable"]] = to_str(v) if isinstance(v, bool) else v

    def http(self, config):
        req = urllib.request.Request(config["url"], data=json.dumps(config["body"]).encode() if not isinstance(config["body"], str) else config["body"].encode(),
                                     method=config["method"], headers={**config["headers"], "User-Agent": "Mozilla/5.0 (freya-sim)"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r: return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            try: return e.code, json.loads(e.read() or b"{}")
            except Exception: return e.code, {}
        except Exception as e:
            return 599, {"error": repr(e)}

    # -- text

    def transcript(self, last=None):
        lines = []
        for h in self.history:
            if h["role"] == "agent": lines.append("AGENT: " + h["text"])
            elif h["role"] == "customer": lines.append("CUSTOMER: " + h["text"])
            elif h["role"] == "tool_call": lines.append(f"[tool call {h['name']} {json.dumps(h['args'])}]")
            else: lines.append(f"[tool result {h['name']}: {json.dumps(h['result'])}]")
        return "\n".join(lines[-last:] if last else lines)
