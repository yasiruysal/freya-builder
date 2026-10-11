"""The agentic builder: a fixed control loop in Python, rule-based decisions, LLM sessions only as steps.

    python3 builder-agentic/build.py <brief.md> <tool_api.json> [--slot NAME] [--minutes 25] [--runner cli|manual] [--no-save]

Phases (state in builder-agentic/work/<slot>/state.json, so a run can be resumed):
  features   deterministic: raw material? several products? third parties? identity fields (from the tool API)
  reconcile  raw briefs only: one session writes RECONCILED.md (every rule with its source, every override)
  build      3 candidates whose strategies are chosen by rules from the features, + 1 session writing test customers
             from the raw brief (adversarial); each candidate gets the library patterns that match the features
  validate_tests discover the real practice ledger, validate expectations, and repair the tests once if needed
  style      Python adds style variants of simple tests (pieces, correction, in words, question back, bundled)
  select     simulator on every candidate; best = most passes, no style failure preferred, contract clean, fewer nodes
  fix        one session fixes the largest cluster of failures; kept only if failures drop and no passed test regresses
             (re-runs the failed tests + 4 passed ones); a second round only if the first one helped and time is left
  finish     lint, contract check, save to Freya, export, trace
--runner cli: LLM steps run as `claude -p` sessions (the final, one-command run).
--runner manual: the script stops before each LLM step, prints the folders that need a session (each has PROMPT.txt),
  and continues from there when it is run again (used while testing, when the sessions run elsewhere).
"""
import argparse, hashlib, json, os, pathlib, random, re, shutil, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
HERE = ROOT / "builder-agentic"
sys.path[:0] = [str(ROOT / "builder"), str(ROOT / "sim")]
import build as base                      # the pipeline builder: prepare(), prompt(), run_claude(), summarise(), redact()
_base_prepare = base.prepare
import contract, lint
sys.path.insert(0, str(HERE))
import test_contract

STYLES = {
    "pieces": "HOW YOU TALK: give your {field} in two parts: say the first half, stop, and give the rest in your next turn.",
    "correct": "HOW YOU TALK: the first time you say a day or an amount, say a slightly different one and correct yourself in the same sentence ('the 21st, sorry, I mean the 20th'); the corrected value is the one in the script. Never do this with a security answer.",
    "words": "HOW YOU TALK: say every number and date fully in words ('four hundred and twenty', 'June third, nineteen eighty-seven'), digits one by one in words.",
    "question_back": "HOW YOU TALK: before your first security answer, ask 'who is calling, and why do you need that?'; whatever the answer, then give it.",
    "bundle": "HOW YOU TALK: when asked the first security question, answer it and give the next security answer in the same sentence.",
}


# ------------------------------------------------------------------ features and policy (rules, no LLM)

def features(brief, api):
    t = brief.lower()
    raw = sum(bool(re.search(p, t, re.M)) for p in [r"^\**from:\**", r"\bmemo\b", r"code list", r"policy (extract|v|version)", r"^\**subject:\**", r"^#+ document"]) >= 2
    multi = bool(re.search(r"(up to|one to|one or|1 to|1 or)\s+(two|three|2|3)\s+\w*\s*(products|facilities|policies|accounts|loans|cards|items)", t)) \
        or bool(re.search(r"(two|three|several|multiple) (products|facilities|policies|accounts)", t))
    third = bool(re.search(r"(someone else|third part|relative|colleague|employee|office manager|answers the phone)", t))
    try:
        idf = api["paths"]["/verify_identity"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["field"]["enum"]
    except Exception:
        idf = []
    tr = len(re.findall(r"[çğışöüÇĞİŞÖÜ]", brief)) > 50
    return {"raw": raw, "multi_product": multi, "third_party": third, "identity_fields": idf, "language": "Turkish" if tr else "English"}


def policy(f):
    """Three different strategies, chosen by rules from the features (diversity is guaranteed, not left to chance)."""
    extra_multi = "Several products: implement the multi-product loop recipe of the core skill exactly (P05 in LIBRARY.md)."
    extra_raw = "This brief is raw material: build from RECONCILED.md (the reconciled rules with sources); where it and the brief differ, re-read the cited source."
    c = [
        {"skills": ["core", "spec-first"], "note": ""},
        {"skills": ["core", "spec-first"], "note": ""},          # a second spec-first build: on the dev set core+library was weakest (11/20 vs 14/20)
        {"skills": ["core", "spec-first", "primitives"], "note": ""},
    ]
    for x in c:   # the detector can miss several items; the recipe only applies where the call handles items one by one
        x["note"] += " " + ("If the call handles several items (products, accounts, cards...) one by one, with a decision or a write per item, build "
                            "the per-item loop recipe of the core skill (P05 in LIBRARY.md); with one item it costs nothing. If the call only "
                            "picks one item among several (e.g. which card), a selection step is enough.")
    if f["raw"]:
        for x in c: x["note"] += " " + extra_raw
    lang = f.get("language", "English")
    for x in c:
        x["note"] += (f" The call is in {lang}: every sentence the agent says (fixed lines, what_to_do_next sentences to quote, "
                      f"required sentences, the system prompt's language rule) is in {lang}, with the documents' exact wording where they give it; "
                      "use the example conversations in the case folder, if any, for wording and tone. Customers say dates, amounts and digits "
                      f"in {lang} words: convert them with lib/normalize.py (it reads English and Turkish).")
    return c


def library_for(f):
    pats = json.loads((HERE / "library" / "patterns.json").read_text())
    on = {"always", "multi_product"} | {k for k in ("raw", "third_party") if f.get(k)}     # multi-product patterns always: the detector can miss them
    pick = [p for p in pats if set(p["features"]) & on]
    return "# Library: patterns that apply to this brief\n\nEach pattern comes from a failure on another company's brief. They are shapes, not values.\n\n" + \
        "\n\n".join(f"## {p['id']} ({p['type']})\nProblem: {p['problem']}\nShape: {p['shape']}\nSymptom when missing: {p['symptom']}" for p in pick)


TEXT_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".tsv", ".mmd", ".mermaid", ".puml", ".html", ".xml", ".py", ".js", ".ts"}


def office_text(p):
    """Text of a .docx (paragraphs) or .xlsx (every sheet as tab-separated rows), with the standard library only."""
    import zipfile, re as _re, html
    z = zipfile.ZipFile(p)
    if p.suffix.lower() == ".docx":
        x = z.read("word/document.xml").decode("utf-8", "ignore")
        x = _re.sub(r"</w:p>", "\n", x); x = _re.sub(r"<w:tab/>", "\t", x)
        return html.unescape(_re.sub(r"<[^>]+>", "", x))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        sx = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
        shared = [html.unescape(_re.sub(r"<[^>]+>", "", si)) for si in _re.findall(r"<si>(.*?)</si>", sx, _re.S)]
    names = {}
    if "xl/workbook.xml" in z.namelist():
        wx = z.read("xl/workbook.xml").decode("utf-8", "ignore")
        for i, nm in enumerate(_re.findall(r'<sheet [^>]*name="([^"]+)"', wx)): names[i + 1] = html.unescape(nm)
    out = []
    for f in sorted(n for n in z.namelist() if _re.match(r"xl/worksheets/sheet\d+\.xml", n)):
        k = int(_re.findall(r"\d+", f)[-1]); out.append(f"## Sheet: {names.get(k, f)}")
        sx = z.read(f).decode("utf-8", "ignore")
        for row in _re.findall(r"<row[^>]*>(.*?)</row>", sx, _re.S):
            cells = []
            for attrs, body in _re.findall(r"<c([^>]*)>(.*?)</c>", row, _re.S):
                v = _re.search(r"<v>(.*?)</v>", body, _re.S); t = _re.search(r'\bt="(\w+)"', attrs)
                if t and t.group(1) == "s" and v: cells.append(shared[int(v.group(1))] if int(v.group(1)) < len(shared) else "")
                elif t and t.group(1) == "inlineStr": cells.append(html.unescape(_re.sub(r"<[^>]+>", "", body)))
                else: cells.append(v.group(1) if v else "")
            out.append("\t".join(cells))
    return "\n".join(out)


def load_case(case_dir, mock_url, out_dir):
    """A case folder (the bank's documents) -> one CASE.md with every text document (BRIEF.md and README first), and an
    OpenAPI file whose server is our mock. Images and other files stay in the folder, which every session gets a copy of."""
    case_dir = pathlib.Path(case_dir); out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in case_dir.rglob("*") if p.is_file() and not p.name.startswith("."))
    first = [p for p in files if p.name.lower() in ("brief.md", "readme.md")]
    first.sort(key=lambda p: 0 if p.name.lower() == "brief.md" else 1)
    rest = [p for p in files if p not in first]
    parts, others, api = [], [], None
    for p in first + rest:
        rel = p.relative_to(case_dir)
        if p.suffix.lower() in TEXT_EXT:
            try: t = p.read_text(encoding="utf-8")
            except Exception: others.append(str(rel)); continue
            if len(t) > 50000:            # a big data file (e.g. thousands of records): the start here, the whole file in the folder
                t = t[:6000] + f"\n\n[... {len(t) - 6000} more characters: open brief/case/{rel} for the whole file ...]"
            parts.append(f"\n\n---\n\n# Document: {rel}\n\n{t}")
            if api is None and p.suffix.lower() in (".json", ".yaml", ".yml") and ("openapi" in t[:2000].lower() or '"paths"' in t or "\npaths:" in t):
                api = p
        elif p.suffix.lower() in (".docx", ".xlsx"):
            try: parts.append(f"\n\n---\n\n# Document: {rel} (converted to text)\n\n{office_text(p)}")
            except Exception: others.append(str(rel))
        else:
            others.append(str(rel))
    head = (f"# Case documents (all text files of {case_dir.name}, BRIEF.md and README.md first)\n\n"
            f"The original folder is in brief/case/. Word and Excel files are converted below. Files that are not text: open each one yourself with Read (PDFs and images can be read): {', '.join(others) or 'none'}.\n"
            f"The bank's services are reached at the mock: {mock_url} (every endpoint path in the documents goes after this base URL).\n"
            + ("No machine-readable service contract (OpenAPI) was found: take the endpoints, fields, authentication and errors from the documents themselves.\n" if api is None else ""))
    (out_dir / "CASE.md").write_text(head + "".join(parts))
    spec = {"openapi": "3.0.3", "info": {"title": "service contract (not found as OpenAPI)"}, "servers": [{"url": mock_url}], "paths": {}}
    if api is not None:
        try:
            if api.suffix.lower() == ".json": spec = json.load(open(api))
            else:
                try:
                    import yaml; spec = yaml.safe_load(open(api))
                except Exception: pass
        except Exception: pass
    base_path = ""
    try:
        from urllib.parse import urlparse
        base_path = urlparse((spec.get("servers") or [{}])[0].get("url", "")).path.rstrip("/")
    except Exception: pass
    spec["servers"] = [{"url": mock_url.rstrip("/") + base_path}]
    json.dump(spec, open(out_dir / "service-contract.openapi.json", "w"), indent=1)
    return out_dir / "CASE.md", out_dir / "service-contract.openapi.json", str(api.relative_to(case_dir)) if api else None


def test_bank_notes(case_dir, bank_url):
    """Only local documentation naming this exact practice bank is runtime authority."""
    docs = set(ROOT.glob("*/NOTES.md"))
    if case_dir: docs.update(pathlib.Path(case_dir).rglob("*.md"))
    target = bank_url.rstrip("/")
    matched = []
    for path in sorted(docs):
        try: text = path.read_text()
        except (OSError, UnicodeError): continue
        urls = {url.rstrip(".,;/") for url in re.findall(r"https?://[^\s`<>\"')\]]+", text)}
        if target in urls: matched.append(f"# Runtime bank documentation: {path.name}\n\n{text}")
    return "\n\n".join(matched)


def feedback_transcript(call):
    if isinstance(call, str): return call
    if not isinstance(call, dict): return "Native call transcript unavailable."
    if isinstance(call.get("transcript"), str) and call["transcript"]: return call["transcript"]
    lines = []
    for message in call.get("messages", []):
        if not isinstance(message, dict) or message.get("role") not in {"assistant", "agent", "user", "customer", "human"}: continue
        content = message.get("message", message.get("text", message.get("content", "")))
        if isinstance(content, list): content = " ".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
        if isinstance(content, str) and content: lines.append(message["role"].upper() + ": " + content)
    return "\n".join(lines) or "Native call transcript unavailable."

# ------------------------------------------------------------------ prompts

RECONCILE_PROMPT = ("Read the brief ({brief}) and the tool API (tools/{api}). If brief/case/ exists, open every file there that the brief "
                    "does not already contain as text (PDFs, images, flow drawings) with Read and put what they say into your result; a flow "
                    "drawing is probably the call flow. The material may be raw (mails, analyses in several versions, meeting notes, memos, "
                    "code lists) and written months apart. Write out/RECONCILED.md: a clean, numbered specification of the call. For every "
                    "rule give: the rule; its source in brackets (document and section or quoted line); the older rule it replaces, if any; "
                    "and the data the agent needs at run time to apply it (which call variable or which service field). Later and more specific "
                    "documents win; a decisions recap or memo changes what it names. Add the sections 'Overrides applied', 'Ignored' (superseded, "
                    "irrelevant, informal details) and 'Unresolved' (conflicts you could not settle from the documents, or rules that need data no "
                    "service returns): for each, the safest behaviour that does not invent anything, and why. Take every code and every sentence "
                    "the agent must say exactly from the documents. "
                    "If tools/{api} has no paths (the service contract is a document, not OpenAPI), also write out/contract.openapi.json: an "
                    "OpenAPI 3 description of every service the documents define (method, path after the base URL, request body with every field, "
                    "nested objects such as an envelope, required fields, enums, response shapes, error responses and status codes), and of any "
                    "authentication call. Do not build an agent. You have {minutes} minutes. Finish with: DONE <number of rules>.")

FIX_PROMPT = ("You are fixing a phone agent that fails some test customers. Read .claude/skills/core/SKILL.md and LIBRARY.md, the brief ({brief}), "
              "then out/agent.workflow.json (and out/build.py or the generator script if there is one). The failing customers and the reasons are in "
              "FAILURES.md, with the path of each transcript. Fix the cause of the largest group of failures first, then the others, in the generator "
              "(regenerate) or in the JSON. Keep everything that works; do not rewrite the agent. Prefer routes and code over prompt text. "
              "If a transcript contains an external grader and native call evidence, use the actual failure and latest raw sources to identify "
              "the general cause: preserve required date precision and required choice/options wording for all customers; never hardcode "
              "a test id, customer id, fixture date or preferred option to pass that example. Audit every early-answer shortcut as a "
              "separate graph path: a stored choice must still pass through every mandatory availability/options announcement, "
              "preview and confirmation required by the brief. Track completion of these steps independently from whether an answer "
              "was extracted. Count every retry-eligible invalid customer reply according to the brief, even when extraction returns "
              "none; a transition that only fires on a successfully extracted answer cannot enforce that limit. Implement counters "
              "and limit routes in code; exempt repeats or other cases only when the source says they are exempt. Run "
              "`python3 tools/contract.py out/agent.workflow.json tools/{api}` and fix every ERROR. You have {minutes} minutes. Finish with: DONE.")

TESTS_REPAIR_PROMPT = (
    "Repair the generated test set, not an agent. Read the raw brief ({brief}), every latest source relevant to the tests "
    "in brief/case/ if present, tools/{api}, TEST_RUNTIME.json (the actual empty ledger and comparator semantics), "
    "TEST_BANK.md if supplied (the actual matching mock's record names, shapes and scalar types), "
    "TEST_ERRORS.json, TESTS_INPUT.json (original raw constraints), and PREVIOUS_TESTS.json if supplied. "
    "Write the complete corrected set to out/tests.json. "
    "Revisit expected results and conversation checks against the raw sources and their precedence; a summary or an old "
    "example does not override a later explicit decision. Keep valid customer identities and service inputs. Compute fixture "
    "and calendar values with Python rather than guessing; when a scripted choice is ambiguous, make it deterministic and "
    "compute the expected value from that exact choice. Add missing boundary coverage for the latest rules if needed. "
    "Never invent ledger key aliases. Only actual recorded fields belong in expected. Positive transfer records require a "
    "recording transfer tool/API: otherwise use an outcome and/or conversation check that checks the actual hand-off. "
    "Scalar JSON types are strict. Expected objects match only the fields specified (partial objects are allowed). "
    "Lists have exact cardinality and order-free one-to-one matching. Empty observed lists do not reveal record field "
    "types: inspect TEST_BANK.md and the service/fixture response types. Empty object records such as [{{}}] are "
    "invalid: they erase the original positive constraints and prove only that some write occurred. Preserve original "
    "record constraints wherever the raw brief and actual bank documentation prove them; use the correct typed values. "
    "Revisit every conversation check, including failure exits, against the latest explicit sources; a technical failure "
    "must use the exit those sources require. Do not weaken required success or security checks just to pass validation. "
    "Do not build, edit, run or save any agent; do not call models or run live tests. "
    "You have {minutes} minutes. Finish with: DONE <number> corrected tests."
)


# ------------------------------------------------------------------ the control loop

# Time plan (minutes). The whole run must stay under --minutes (default 25; the rules allow 30): every phase takes its
# budget from the time left, and at the end the best valid workflow is finished whatever happened before.
RESERVE = 1.5            # finish (lint, contract, save, export, trace)
SELECT_PICK = 5          # coverage tests per candidate in selection (+ SELECT_STYLE style variants)
SELECT_STYLE = 1


CASE_DIR = None


def _prepare_with_case(work, briefs, api, skills):
    _base_prepare(work, briefs, api, skills)
    if CASE_DIR: shutil.copytree(CASE_DIR, work / "brief" / "case")


base.prepare = _prepare_with_case


class Run:
    def __init__(self, a):
        self.a = a; self.t0 = time.time()
        self.brief, self.api = a.brief, a.api
        self.case_dir = None
        if pathlib.Path(a.brief).is_dir():                     # final format: <case folder> <mock url>
            self.case_dir = pathlib.Path(a.brief).resolve()
            # The positional mock URL is the authority for both service calls and the practice harness.
            # Synchronise the live environment and the subprocess environment snapshot made by base.
            os.environ["MOCK_BANK_URL"] = a.api.rstrip("/")
            base.ENV["MOCK_BANK_URL"] = a.api.rstrip("/")
            cm, ap_, found = load_case(self.case_dir, a.api, HERE / "work" / a.slot / "case")
            self.brief, self.api = str(cm), str(ap_)
            global CASE_DIR; CASE_DIR = self.case_dir
            print(f"case folder {self.case_dir.name}: service contract {found or 'NOT FOUND (empty paths)'}; mock {a.api}", flush=True)
        self.work = HERE / "work" / a.slot; self.work.mkdir(parents=True, exist_ok=True)
        self.sf = self.work / "state.json"
        self.s = json.loads(self.sf.read_text()) if self.sf.exists() else {"phase": "features", "log": [], "started": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                                                                     "phase_secs": {}, "calls": [], "selftests": []}
        for k, v in (("phase_secs", {}), ("calls", []), ("selftests", [])): self.s.setdefault(k, v)   # state written by an older version
        if a.runner == "cli" and "t0" in self.s: self.t0 = self.s["t0"]      # a resumed cli run keeps its clock
        self.s.setdefault("t0", self.t0)
        self.api_json = json.loads(pathlib.Path(self.api).read_text())
        # load_case writes an empty placeholder again on resume when the source contract is prose.
        # Preserve the machine contract that the completed reconcile phase already produced.
        if self.case_dir is not None and not self.api_json.get("paths") and (self.work / "reconcile" / "DONE").exists():
            for saved in (self.work / "tools" / pathlib.Path(self.api).name,
                          self.work / "reconcile" / "out" / "contract.openapi.json"):
                try:
                    spec = json.load(open(saved))
                    if not isinstance(spec.get("paths"), dict) or not spec["paths"]: continue
                    spec["servers"] = self.api_json.get("servers") or spec.get("servers")
                    json.dump(spec, open(self.api, "w"), indent=1); self.api_json = spec
                    self.log(f"resume: restored reconciled contract ({len(spec['paths'])} paths) from {saved.relative_to(self.work)}")
                    break
                except (OSError, ValueError, TypeError): continue
        if self.s["phase"] in ("style", "select", "fix", "finish") and not self.s.get("tests_validated"):
            self.s["phase"] = "validate_tests"
            for key in ("best", "best_results", "ranking", "round", "round1_helped"): self.s.pop(key, None)
            self.log("resume: generated tests must be validated before candidate scores are used")
        if getattr(a, "feedback", None): self.ingest_feedback(a.feedback)

    def save(self): self.sf.write_text(json.dumps(self.s, indent=1))
    def log(self, m): self.s["log"].append(time.strftime("%H:%M:%S ") + f"[{self.used():4.1f} min] " + m); print(m, flush=True); self.save()
    def used(self): return (time.time() - self.t0) / 60
    def left(self): return self.a.minutes - self.used()

    def ingest_feedback(self, source):
        """Consume explicit external evidence once; never infer a pass or restart without the CLI flag."""
        path = pathlib.Path(source).resolve()
        try:
            raw = path.read_bytes(); document = json.loads(raw)
            rows = document.get("rows", []) if isinstance(document, dict) else document
            if not isinstance(rows, list) or not rows: raise ValueError("feedback needs a nonempty rows list")
            digest = hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if digest in self.s.get("external_feedback_hashes", []):
                self.log(f"external feedback unchanged ({digest[:12]}): already consumed; phase unchanged"); return False
            if not self.s.get("best"): raise ValueError("feedback requires an existing selected workflow")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not re.fullmatch(r"[\w.-]+", row["id"]):
                    raise ValueError("each feedback row needs a safe testcase id")
                if not isinstance(row.get("pass"), bool): raise ValueError("each feedback row needs an explicit boolean pass")
                if not isinstance(row.get("failed", []), list) or any(not isinstance(x, str) for x in row.get("failed", [])):
                    raise ValueError("failed must be a list of grader reasons")
        except (OSError, ValueError, TypeError) as exc:
            self.log(f"external feedback rejected: {str(exc)[:240]}"); return False
        archive = self.work / "external-feedback"; archive.mkdir(exist_ok=True)
        saved = archive / (digest[:16] + ".json"); saved.write_bytes(raw)
        best = pathlib.Path(self.s["best"])
        evidence_dir = best.parent / "selftest-out"; evidence_dir.mkdir(exist_ok=True)
        stamp = f"{int(time.time())}-{digest[:12]}"
        results = dict(self.s.get("best_results", {}))
        failed_ids = []
        for row in rows:
            cid = row["id"]; results[cid] = row["pass"]
            if row["pass"]: continue
            failed_ids.append(cid)
            transcript = feedback_transcript(row.get("call"))
            ledger = row.get("ledger")
            if isinstance(ledger, dict):
                ledger = {key: value for key, value in ledger.items() if key != "calls"}
                ledger["calls"] = [{key: call[key] for key in ("n", "tool", "args", "status") if key in call}
                                   for call in row["ledger"].get("calls", []) if isinstance(call, dict)]
            grader = row.get("freya_run")
            if isinstance(grader, dict): grader = {key: grader[key] for key in ("id", "callId", "outcome", "reason", "agentVersionId", "agentVersionNumber") if key in grader}
            reasons = row.get("failed") or ([row["freya_run"]["reason"]] if isinstance(row.get("freya_run"), dict) and row["freya_run"].get("reason") else ["External grader reported failure without a reason"])
            evidence = (f"# External grader evidence\nsource: {path}\narchived: {saved}\nsha256: {digest}\n"
                + f"testcase: {cid}; reported pass: false\n\n# Native call transcript\n{transcript}\n\n"
                + "# Actual ledger (tool responses in archived source)\n" + json.dumps(ledger, ensure_ascii=False, indent=1)
                + "\n\n# External grader result\n" + json.dumps(grader, ensure_ascii=False, indent=1)
                + "\n\n# failed\n" + "\n".join(reasons) + "\n")
            (evidence_dir / f"{cid}@external-{stamp}.txt").write_text(evidence)
        self.s.setdefault("external_feedback_hashes", []).append(digest)
        self.s["best_results"] = results
        self.s["external_feedback_pending"] = bool(failed_ids)
        self.s["external_feedback_source"] = str(saved)
        self.s["external_feedback_current_hash"] = digest
        self.s["phase"] = "fix" if failed_ids else ("finish" if self.s["phase"] != "done" else "done")
        grader = "native Freya" if any(row.get("freya_run") for row in rows) else "external"
        self.log(f"external feedback ({grader} grader): {sum(row['pass'] for row in rows)}/{len(rows)} passed; failed {failed_ids}; "
                 f"source {path}; archived {saved.relative_to(self.work)}; sha256 {digest[:12]}; fresh feedback {'pending' if failed_ids else 'no failures'}")
        return True

    def sessions(self, dirs, minutes, purpose, hard_limit_sec=None):
        """Run (cli) or hand over (manual) one LLM session per folder; each folder has PROMPT.txt. Returns False when waiting."""
        todo = [d for d in dirs if not (d / "DONE").exists()]
        if todo and self.a.runner == "manual":
            self.log("WAITING for sessions in: " + ", ".join(str(d) for d in todo) + " (run PROMPT.txt in each, then touch DONE)")
            return False
        t = time.time(); procs = []
        for d in todo:
            f = open(d / "session.jsonl", "w")
            procs.append((d, f, base.run_claude(d, (d / "PROMPT.txt").read_text(), self.a.model, f)))
        for d, f, p in procs:
            try: p.wait(timeout=max(1, (hard_limit_sec if hard_limit_sec is not None else minutes * 60 + 45) - (time.time() - t)))
            except subprocess.TimeoutExpired: p.kill(); p.wait(); self.log(f"session {d.name} stopped at its time limit ({minutes} min)")
            f.close(); (d / "DONE").touch()
        for d in dirs:
            if any(c["session"] == d.name for c in self.s["calls"]): continue
            entry = {"session": d.name, "purpose": purpose(d) if callable(purpose) else purpose, "model": self.a.model, "lines": []}
            if (d / "session.jsonl").exists() and (d / "session.jsonl").stat().st_size:
                base.summarise(d / "session.jsonl", entry["lines"], self.t0, time.time())
            else:
                entry["model"] = self.a.manual_model; entry["lines"] = [f"session ran outside this process (manual runner), model {self.a.manual_model}; its result is in {d.name}/out"]
            self.s["calls"].append(entry)
            self.log(f"session {d.name} ({entry['model']}): {entry['purpose']}; " + " | ".join(x for x in entry["lines"] if not x.startswith("  "))[:200])
        return True

    def selftests(self, runs, deadline, label):
        """Run the simulator on several workflows at once, with one global deadline (seconds). runs: [(name, workflow, tests file, extra args)]."""
        # One candidate after the other, each with the whole parallel budget: candidates must not test the same bank
        # customer at the same time (the mock files calls without a session id by customer or card).
        out = {}; share = max(30, deadline / max(1, len(runs)))
        for name, wf, tests, extra in runs:
            cmd = [sys.executable, str(ROOT / "sim" / "selftest.py"), str(wf), str(tests), "--api", str(self.work / "tools" / pathlib.Path(self.api).name),
                   "--parallel", str(self.a.parallel), "--deadline", str(int(share))] + extra
            p = subprocess.Popen(cmd, cwd=wf.parent, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=base.ENV, start_new_session=True)
            try: txt = p.communicate(timeout=share + 60)[0]
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, 9); txt = p.communicate()[0] + "\nSKIP (selftest process killed at the deadline)"
            with (wf.parent.parent / "selftest.txt").open("a") as fh: fh.write(txt)
            res = {}
            if not re.search(r"^(PASS|FAIL|SKIP) ", txt, re.M):          # the simulator itself failed (no network, no bank, ...)
                self.log(f"self-test {label} {name}: NO RESULTS, the simulator failed: " + " / ".join(txt.strip().splitlines()[-2:])[:300])
            for l in txt.splitlines():
                m = re.match(r"(PASS|FAIL) (\S+)", l)
                if m: res[m.group(2)] = m.group(1) == "PASS"
            self.s["selftests"].append({"label": f"{label} {name}", "workflow": str(wf.relative_to(ROOT)),
                                        "lines": [l for l in txt.splitlines() if l.startswith(("PASS", "FAIL", "SKIP", "    -", "simulator model calls")) or " passed in " in l]})
            out[name] = res
        return out

    # phases

    def p_features(self):
        f = features(pathlib.Path(self.brief).read_text(), self.api_json)
        if self.case_dir is not None and sum(1 for x in self.case_dir.rglob("*") if x.is_file() and not x.name.startswith(".")) >= 3:
            f["raw"] = True                 # several documents of different kinds: always reconcile them first (any language)
        self.s["features"] = f; self.log(f"features: {f}")
        (self.work / "tools").mkdir(exist_ok=True); shutil.copy(self.api, self.work / "tools" / pathlib.Path(self.api).name)
        self.s["phase"] = "reconcile" if f["raw"] else "build"

    def p_reconcile(self):
        d = self.work / "reconcile"; mins = int(max(3, min(8, self.left() - 18)))
        if not d.exists():
            base.prepare(d, [self.brief], self.api, ["core"])
            (d / "PROMPT.txt").write_text(RECONCILE_PROMPT.format(brief="brief/" + pathlib.Path(self.brief).name, api=pathlib.Path(self.api).name, minutes=mins))
        if not self.sessions([d], mins, "reconcile the raw brief into RECONCILED.md (rules with sources, overrides)"): return False
        ok = (d / "out" / "RECONCILED.md").exists(); self.log(f"reconcile: RECONCILED.md {'written' if ok else 'MISSING'}")
        gen = d / "out" / "contract.openapi.json"
        if not (self.api_json.get("paths") or {}) and gen.exists():       # the contract was a document: use the machine version from now on
            try:
                spec = json.load(open(gen)); assert spec.get("paths")
                spec["servers"] = self.api_json.get("servers") or spec.get("servers")
                json.dump(spec, open(self.api, "w"), indent=1); self.api_json = spec
                shutil.copy(self.api, self.work / "tools" / pathlib.Path(self.api).name)
                self.log(f"reconcile: machine-readable contract written from the documents ({len(spec['paths'])} paths); the contract check uses it")
            except Exception as e:
                self.log(f"reconcile: contract.openapi.json unusable ({str(e)[:100]}); contract check stays generic")
        self.s["phase"] = "build"

    def p_build(self):
        f = self.s["features"]; strat = policy(f); lib = library_for(f)
        mins = int(max(6, min(9, self.left() - 12)))          # leave ~12 min for selection, a fix round and finishing
        dirs = []
        for i, st in enumerate(strat):
            d = self.work / f"cand-{i + 1}"; dirs.append(d)
            if d.exists(): continue
            base.prepare(d, [self.brief], self.api, st["skills"])
            (d / "LIBRARY.md").write_text(lib)
            rec = self.work / "reconcile" / "out" / "RECONCILED.md"
            if rec.exists(): shutil.copy(rec, d / "RECONCILED.md")
            text = base.prompt([self.brief], self.api, st["skills"], mins) + "\nAlso read LIBRARY.md (patterns from failures on other briefs)." + st["note"]
            (d / "PROMPT.txt").write_text(text); (d / "STRATEGY.txt").write_text(json.dumps(st))
        t = self.work / "tests"; dirs.append(t)
        if not t.exists():
            base.prepare(t, [self.brief], self.api, ["core", "redteam"])
            (t / "TEST_RUNTIME.json").write_text(json.dumps(test_contract.runtime_template(self.api_json), indent=1))
            bank_docs = test_bank_notes(self.case_dir, base.ENV.get("MOCK_BANK_URL", "https://freya-mockbank.yasir-freya.workers.dev"))
            if bank_docs: (t / "TEST_BANK.md").write_text(bank_docs)
            (t / "PROMPT.txt").write_text(base.TESTS_PROMPT.format(briefs="brief/" + pathlib.Path(self.brief).name, api=pathlib.Path(self.api).name, minutes=mins - 2)
                                          + " Read the brief itself, not anybody's summary of it, and look for the rules that are easiest to get wrong."
                                          + " Read TEST_RUNTIME.json for comparator semantics; expected scalar JSON types are strict, objects allow partial fields,"
                                          " and lists match with exact cardinality and order-free one-to-one pairing. Use only ledger keys the mock actually records;"
                                          " behaviour without a recording API belongs in checks or a semantic outcome. Read TEST_BANK.md if supplied for actual"
                                          " record shapes and value types. Expected positive records must specify the required effect; never use empty {} wildcard records."
                                          + " If the case folder (brief/case/) has test customers or example conversations, turn every example conversation into a"
                                          " test (the customer's lines become the script, what the conversation records becomes the expected result) and use the"
                                          " bank's own test customers for identity and accounts; then add your own edge cases.")
        purpose = lambda d: ("write test customers from the brief (adversarial)" if d.name == "tests" else
                             "build candidate workflow, skills " + "+".join(json.loads((d / "STRATEGY.txt").read_text())["skills"]))
        if not self.sessions(dirs, mins, purpose): return False
        self.log(f"build: strategies {[json.loads((d / 'STRATEGY.txt').read_text())['skills'] for d in dirs[:-1]]}; candidates written: {[(d / 'out' / 'agent.workflow.json').exists() for d in dirs[:-1]]}")
        self.s["phase"] = "validate_tests"

    def p_validate_tests(self):
        tf = self.work / "tests" / "out" / "tests.json"
        raw = tf.with_name("tests.raw.json")
        if tf.exists() and not raw.exists(): shutil.copy(tf, raw)
        try:
            tests = json.loads(tf.read_text()); parse_error = None
        except (OSError, ValueError) as exc:
            tests = []; parse_error = str(exc)
        runtime_path = self.work / "TEST_RUNTIME.json"
        try: runtime = json.loads(runtime_path.read_text())
        except (OSError, ValueError): runtime = None
        if not runtime:
            first = next((c for c in tests if not test_contract.structure_errors(c)), None) if isinstance(tests, list) else None
            bank_url = base.ENV.get("MOCK_BANK_URL", "https://freya-mockbank.yasir-freya.workers.dev")
            runtime = test_contract.discover(self.api_json, first, bank_url) if first is not None else test_contract.runtime_template(self.api_json)
            runtime_path.write_text(json.dumps(runtime, indent=1))
            self.log(f"test runtime: {runtime['status']}; actual ledger keys {runtime.get('ledger_keys', [])}"
                     + (f"; {runtime['error']}" if runtime.get("error") else ""))
            if runtime.get("cleanup_error"): self.log("TEST ERRORS: practice probe cleanup failed: " + runtime["cleanup_error"])
        (self.work / "tests").mkdir(exist_ok=True)
        shutil.copy(runtime_path, self.work / "tests" / "TEST_RUNTIME.json")
        bank_docs = test_bank_notes(self.case_dir, base.ENV.get("MOCK_BANK_URL", "https://freya-mockbank.yasir-freya.workers.dev"))
        if bank_docs:
            (self.work / "TEST_BANK.md").write_text(bank_docs)
            (self.work / "tests" / "TEST_BANK.md").write_text(bank_docs)
        good, errors = test_contract.validate(tests, runtime)
        if parse_error: errors = [{"index": None, "id": None, "errors": ["tests.json could not be read: " + parse_error]}]
        initial_errors = errors
        if errors:
            self.log(f"TEST ERRORS: {len(errors)} invalid generated testcases; validating the harness before scoring agents")
            for error in errors: self.log(f"  test {error['id'] or error['index']}: " + "; ".join(error["errors"])[:500])
            for attempt in (1, 2):
                if not errors: break
                d = self.work / ("tests-repair" if attempt == 1 else "tests-repair-2")
                mins = max(0.0, min(2.0, self.left() - RESERVE - 0.25))
                if mins < 0.5 and not d.exists():
                    self.log(f"tests-repair-{attempt}: skipped ({self.left():.1f} min left); invalid tests will not score candidates")
                    break
                if not d.exists():
                    base.prepare(d, [self.brief], self.api, ["core", "redteam"])
                    shutil.copy(runtime_path, d / "TEST_RUNTIME.json")
                    if bank_docs: (d / "TEST_BANK.md").write_text(bank_docs)
                    (d / "TEST_ERRORS.json").write_text(json.dumps(errors, indent=1))
                    (d / "PREVIOUS_TESTS.json").write_text(json.dumps(tests, indent=1))
                    if raw.exists():
                        shutil.copy(raw, d / "TESTS_INPUT.json"); shutil.copy(raw, d / "out" / "tests.json")
                    else: (d / "TESTS_INPUT.json").write_text("null")
                    (d / "PROMPT.txt").write_text(TESTS_REPAIR_PROMPT.format(brief="brief/" + pathlib.Path(self.brief).name,
                        api=pathlib.Path(self.api).name, minutes=round(mins, 1)))
                if not self.sessions([d], mins, f"repair generated test expectations (attempt {attempt}) against the actual ledger and raw sources", hard_limit_sec=min(120, max(1, mins * 60))): return False
                try:
                    corrected = json.loads((d / "out" / "tests.json").read_text())
                    good, errors = test_contract.validate(corrected, runtime)
                    tests = corrected
                    self.log(f"tests-repair-{attempt}: {len(good)} valid, {len(errors)} still invalid (originally {len(initial_errors)} invalid)")
                except (OSError, ValueError) as exc:
                    self.log(f"tests-repair-{attempt}: unusable output ({str(exc)[:160]}); keeping the previously valid tests")
        report = {"runtime_status": runtime["status"], "initial_errors": initial_errors, "remaining_errors": errors,
                  "valid_ids": [c["id"] for c in good], "excluded_ids": [e["id"] for e in errors]}
        (self.work / "TEST_ERRORS.json").write_text(json.dumps(report, indent=1))
        tf.parent.mkdir(parents=True, exist_ok=True); tf.write_text(json.dumps(good, indent=1))
        if errors:
            self.log(f"TEST ERRORS: excluded {len(errors)} invalid testcases; these are harness errors, not candidate failures")
            for error in errors: self.log(f"  excluded test {error['id'] or error['index']}: " + "; ".join(error["errors"])[:500])
        self.s["tests_validated"] = {"valid": len(good), "excluded": len(errors), "runtime_status": runtime["status"]}
        self.log(f"validate_tests: {len(good)} valid tests; raw input preserved at {raw.relative_to(self.work)}; details in TEST_ERRORS.json")
        self.s["phase"] = "style"

    def p_style(self):
        tf = self.work / "tests" / "out" / "tests.json"
        try: tests = json.load(open(tf))
        except Exception: self.log("style: no valid tests were written"); self.s["phase"] = "select"; return
        simple = [c for c in tests if not c.get("faults") and len(c.get("products") or []) <= 1 and not c.get("third_party")]
        rnd = random.Random(7); out = []
        for k, (name, text) in enumerate(STYLES.items()):
            for c in simple[k % max(1, len(simple)):][:2]:
                v = json.loads(json.dumps(c)); v["id"] = f"S-{name}-{c['id']}"
                field = rnd.choice(list(c["identity"].keys())) if c.get("identity") else "answer"
                v["script"] = c["script"] + "\n\n" + text.format(field=field.replace("_", " ")); v["style_of"] = c["id"]; out.append(v)
        json.dump(tests + out, open(self.work / "tests.all.json", "w"), indent=1)
        # the selection set: coverage tests that differ in their expected result, plus a style variant
        import selftest as st
        promised = [c for c in tests if (c.get("expected") or {}).get("promises")]       # the core of the call: at least 3 promise cases,
        promised.sort(key=lambda c: -len(c.get("products") or []))                       # the multi-product ones first
        sel = st.pick(promised, min(3, len(promised)))
        sel += st.pick([c for c in tests if c not in sel], SELECT_PICK - len(sel)) + rnd.sample(out, min(SELECT_STYLE, len(out)))
        json.dump(sel, open(self.work / "tests.select.json", "w"), indent=1)
        self.log(f"style: {len(tests)} coverage tests + {len(out)} style variants; selection set {[c['id'] for c in sel]}"); self.s["phase"] = "select"

    def valid(self, c):
        try: wf = contract.load(c); return wf if wf.get("nodes") else None
        except Exception: return None

    def p_select(self):
        tests = self.work / "tests.select.json"
        cands = [c for c in (self.work / f"cand-{i}" / "out" / "agent.workflow.json" for i in (1, 2, 3)) if c.exists() and self.valid(c)]
        if not cands: self.log("select: no valid candidate workflow"); self.s["phase"] = "done"; self.trace(None); return
        if self.a.take:                                                     # testing: keep a named candidate, no simulator
            best = self.work / self.a.take / "out" / "agent.workflow.json"
            self.s.update(best=str(best), best_results={}, round=0, phase="fix"); self.log(f"kept {self.a.take} (--take, no selection)"); return
        deadline = 60 * min(max(4, min(15, self.left() - RESERVE - 9)), max(1, self.left() - RESERVE - 0.5))   # keep ~9 min for a fix round when we can
        # Generated expectations and the simulator can change while a run is repaired/resumed.
        # Re-score the current tests; old PASS files are not evidence for the new contract.
        res = self.selftests([(c.parent.parent.name, c, tests, []) for c in cands], deadline, "select") if tests.exists() and json.loads(tests.read_text()) else {}
        score = {}
        done = [set(res.get(c.parent.parent.name, {})) for c in cands]
        common = set.intersection(*done) if done and all(done) else set()     # compare candidates on the tests every one completed
        for c in cands:
            r_all = res.get(c.parent.parent.name, {}); errs, _ = contract.check(self.valid(c), self.api_json)
            r = {k: v for k, v in r_all.items() if k in common} if len(common) >= 3 else r_all
            p = sum(r.values()); style_fail = sum(1 for k, v in r.items() if k.startswith("S-") and not v); nodes = len(self.valid(c)["nodes"])
            _, warns_c = contract.check(contract.load(c), self.api_json)
            model_writes = sum(1 for w in warns_c if 'is a tool the model must call' in w)   # prefer the graph that leaves less to the weak model
            score[str(c)] = [p - (100 if errs else 0), -style_fail, -len(errs), -model_writes, -nodes]
            self.log(f"select {c.parent.parent.name}: {p}/{len(r)} passed (on {len(common)} tests every candidate completed; {sum(r_all.values())}/{len(r_all)} of its own), "
                     f"{style_fail} style failures, {len(errs)} contract errors, {nodes} nodes")
        best = max(cands, key=lambda c: score[str(c)]); self.s["ranking"] = [str(c) for c in sorted(cands, key=lambda c: score[str(c)], reverse=True)]
        self.s["best"] = str(best); self.s["best_results"] = res.get(best.parent.parent.name, {}); self.s["round"] = 0; self.s["phase"] = "fix"
        self.log(f"kept {best.parent.parent.name}")

    def p_fix(self):
        best = pathlib.Path(self.s["best"]); tests = self.work / "tests.all.json"
        last = self.s.get("best_results", {})
        failed = [k for k, v in last.items() if not v]
        fresh_feedback = self.s.get("external_feedback_pending", False)
        if not failed or self.s["round"] >= self.a.fix_rounds or self.left() < RESERVE + 7.5 or (self.s["round"] == 1 and not self.s.get("round1_helped") and not fresh_feedback):
            self.s["external_feedback_pending"] = False
            self.log(f"fix: stop (failed {len(failed)}, round {self.s['round']}, {self.left():.1f} min left)"); self.s["phase"] = "finish"; return
        n = self.s["round"] + 1; d = self.work / f"fix-{n}"
        mins = int(max(3, min(6, self.left() - RESERVE - 4.5)))      # the session, then ~4 min to re-test
        if not d.exists():
            shutil.copytree(best.parent.parent, d, ignore=shutil.ignore_patterns("selftest-out", "session.jsonl", "DONE", "selftest.txt"))
            shutil.copytree(ROOT / "builder" / "skills" / "core", d / ".claude" / "skills" / "core", dirs_exist_ok=True)
            (d / "LIBRARY.md").write_text(library_for(self.s.get("features", {})))
            (d / "out" / "selftest-out").mkdir(exist_ok=True)
            (d / "failures").mkdir(exist_ok=True)                                   # copies, so the session needs nothing outside its folder
            for k in failed:
                tr = sorted((best.parent / "selftest-out").glob(f"{k}@*.txt"), key=lambda f: f.stat().st_mtime)
                if tr: shutil.copy(tr[-1], d / "failures" / f"{k}.txt")
            fl = "\n".join(f"- {k}: transcript failures/{k}.txt" for k in failed)
            _, warns = contract.check(contract.load(best), self.api_json)
            unused = "\n".join(f"- {w}" for w in warns)
            (d / "FAILURES.md").write_text(f"# Failing test customers ({len(failed)})\n\n{fl}\n\nThe reasons are at the end of each transcript, under '# failed'.\n"
                                           + (f"\n# Allowed values no call can send (contract check)\n\n{unused}\n\nFor each one, check whether the brief uses it "
                                              "(an outcome, reason or code the agent must be able to send). If it does, add the missing route; if it does not, ignore it.\n" if unused else ""))
            (d / "PROMPT.txt").write_text(FIX_PROMPT.format(brief="brief/" + pathlib.Path(self.brief).name, api=pathlib.Path(self.api).name, minutes=mins))
        if not self.sessions([d], mins, f"fix round {n}: fix the largest cluster of {len(failed)} failing tests"): return False
        if fresh_feedback:
            self.s["external_feedback_pending"] = False
            self.log(f"fix round {n}: consumed fresh external feedback {self.s.get('external_feedback_current_hash', '')[:12]} (fix-round limit unchanged)")
        new = d / "out" / "agent.workflow.json"
        if not self.valid(new):
            self.log(f"fix round {n}: no valid workflow written -> reverted"); helped = False
        else:
            passed = [k for k, v in last.items() if v]
            check = failed + random.Random(n).sample(passed, min(4, len(passed)))    # the failures + 4 passed tests (regression veto)
            sub = d / "tests.check.json"
            sub.write_text(json.dumps([c for c in json.loads(tests.read_text()) if c["id"] in check], indent=1))
            res = self.selftests([(d.name, new, sub, [])], 60 * max(1, self.left() - RESERVE - 0.3), f"fix round {n}").get(d.name, {})
            fixed = [k for k in failed if res.get(k)]; regressed = [k for k in check if k not in failed and not res.get(k, False)]
            helped = bool(fixed) and not regressed
            self.log(f"fix round {n}: fixed {fixed} of {failed}; regressions {regressed} -> {'kept' if helped else 'reverted (veto)' if regressed else 'reverted (nothing fixed)'}")
            if helped:
                self.s["best_results"] = {k: res.get(k, last[k]) for k in last}; self.s["best"] = str(new)
        self.s["round"] = n
        if n == 1: self.s["round1_helped"] = helped

    def p_finish(self):
        best = pathlib.Path(self.s["best"]); wf = self.valid(best); wf.pop("agent_config", None)
        for c in lint.fix(wf): self.log("lint: " + c)
        errs, warns = contract.check(wf, self.api_json); self.log(f"contract: {len(errs)} errors, {len(warns)} warnings")
        if errs:                      # never ship a contract error when another tested candidate is clean: take the next one in the ranking
            for alt in [pathlib.Path(x) for x in self.s.get("ranking", []) if x != str(best)]:
                w2 = self.valid(alt)
                if not w2: continue
                w2.pop("agent_config", None); lint.fix(w2); e2, w2warns = contract.check(w2, self.api_json)
                if not e2:
                    self.log(f"contract errors in {best.relative_to(ROOT)}; finishing with {alt.relative_to(ROOT)} instead (next in the selection ranking, 0 errors)")
                    best, wf, errs, warns = alt, w2, e2, w2warns; break
            else:
                self.log("CONTRACT ERRORS and no clean candidate: finishing with the best one anyway, errors listed below")
        for x in errs + warns: self.log("  " + x)
        (HERE / "out").mkdir(exist_ok=True); json.dump(wf, open(HERE / "out" / f"{self.a.slot}.workflow.json", "w"), indent=1)
        tools = sorted({t["function"]["name"] for n in wf["nodes"].values() for t in (n.get("tools") or []) + (n.get("pre_actions") or []) if t.get("function")})
        self.log(f"workflow: {len(wf['nodes'])} nodes, tools used: {', '.join(tools)}; written to builder-agentic/out/{self.a.slot}.workflow.json")
        if not self.a.no_save:
            try:
                os.chdir(ROOT); import freya
                ids = freya.save_workflow(self.a.slot, wf); (ROOT / "submission").mkdir(exist_ok=True)
                freya.export_workflow(ids["workflow_id"], str(ROOT / "submission" / f"{self.a.slot}.workflow.json"))
                self.log(f"created on Freya: workflow_id {ids['workflow_id']}, agent_id {ids['agent_id']}, {len(wf['nodes'])} nodes, tools {', '.join(tools)}; "
                         f"exported to submission/{self.a.slot}.workflow.json")
            except Exception as e:
                self.log(f"Freya save failed: {str(e)[:200]}")
        else:
            self.log("not saved to Freya (--no-save)")
        self.log(f"done: {self.a.slot}, kept {best.relative_to(ROOT)}"); self.s["phase"] = "done"; self.trace(best)

    def trace(self, best):
        """The trace the rules ask for: start/end, every model call (model + purpose), what was created on Freya, every self-test."""
        self.s["ended"] = time.strftime("%Y-%m-%d %H:%M:%S %Z")
        L = [f"agentic builder run {self.a.slot}", f"start: {self.s['started']}", f"end:   {self.s['ended']}",
             f"brief: {self.brief}", f"tool API: {self.api}", f"runner: {self.a.runner}; builder model: {self.a.model if self.a.runner == 'cli' else self.a.manual_model}; "
             f"simulator model: {os.environ.get('SIM_MODEL', 'haiku')}; time limit {self.a.minutes} min", "",
             "## Phase times (seconds, this process" + ("; LLM sessions in manual mode ran outside it" if self.a.runner == "manual" else "") + ")"]
        L += [f"  {k:<10} {v:7.0f}" for k, v in self.s["phase_secs"].items()] + [f"  {'total':<10} {sum(self.s['phase_secs'].values()):7.0f}", "", "## Log"] + self.s["log"]
        L += ["", "## Model calls (builder sessions)"]
        for c in self.s["calls"]: L += [f"### {c['session']} ({c['model']}): {c['purpose']}"] + c["lines"]
        L += ["", "## Self-tests (local simulator)"]
        for t in self.s["selftests"]: L += [f"### {t['label']} ({t['workflow']})"] + t["lines"]
        (ROOT / "traces").mkdir(exist_ok=True); stamp = time.strftime("%Y%m%d-%H%M%S")
        tr = ROOT / "traces" / f"{self.a.slot}_{stamp}.agentic.log"; tr.write_text("\n".join(L) + "\n"); out = [tr]
        for c in self.s["calls"]:                                   # every message of every session, next to the summary
            j = self.work / c["session"] / "session.jsonl"
            if j.exists() and j.stat().st_size: out.append(ROOT / "traces" / f"{self.a.slot}_{stamp}.{c['session']}.jsonl"); shutil.copy(j, out[-1])
        base.redact(*out); print(f"trace: {tr.relative_to(ROOT)}", flush=True)

    def go(self):
        phases = {"features": self.p_features, "reconcile": self.p_reconcile, "build": self.p_build, "validate_tests": self.p_validate_tests, "style": self.p_style,
                  "select": self.p_select, "fix": self.p_fix, "finish": self.p_finish}
        while self.s["phase"] != "done":
            ph = self.s["phase"]
            if ph not in ("finish",) and self.left() < RESERVE and self.s.get("best"):
                self.log(f"time rail: {self.left():.1f} min left, finishing with the best valid workflow"); self.s["phase"] = ph = "finish"
            t = time.time(); r = phases[ph]()
            self.s["phase_secs"][ph] = self.s["phase_secs"].get(ph, 0) + time.time() - t
            self.save()
            if r is False: return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brief"); ap.add_argument("api"); ap.add_argument("--slot"); ap.add_argument("--minutes", type=float, default=25)
    ap.add_argument("--runner", choices=["cli", "manual"], default="cli"); ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--model", default="claude-opus-5-5"); ap.add_argument("--parallel", type=int, default=8); ap.add_argument("--fix-rounds", type=int, default=2)
    ap.add_argument("--take", help="testing only: skip selection and keep this candidate folder (e.g. cand-1)")
    ap.add_argument("--feedback", help="optional external results JSON (rows with id, pass, failed, call, ledger, freya_run); consumed once per content hash")
    ap.add_argument("--manual-model", default="claude-opus-5-5 (subagent)", help="the model named in the trace for sessions run outside (manual runner)")
    a = ap.parse_args(); a.slot = a.slot or "agentic_" + pathlib.Path(a.brief.rstrip("/")).stem
    Run(a).go()


if __name__ == "__main__":
    main()
