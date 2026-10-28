"""
DemoHider - Tick-box picker
===========================
Opens a page in your browser that lists every button by table and place
(Main grid / Form / Subgrid). Ticked = hidden. Click "Apply" and the plan file is
written. With --flow the same tab then shows the check, asks you to confirm and
shows the progress of sending (flow_server.py). Without --flow, demohider.ps1
asks Y/N in the terminal.

You normally start it with:   .\\demohider.ps1 -Pick

Runs only on your own computer (127.0.0.1). Nothing is sent anywhere else.

Exit codes: 0 = plan written (or, with --flow, sent), 3 = cancelled, 4 = no changes,
            1 = error / blocked, 2 = NOT LIVE YET, 5 = round 2 failed
"""

import argparse
import json
import re
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_plan as mp  # noqa: E402
import flow_server  # noqa: E402

WHERE_ORDER = {"HomepageGrid": 0, "Form": 1, "SubGrid": 2}
WHERE_LABEL = {"HomepageGrid": "Main grid", "Form": "Form", "SubGrid": "Subgrid"}

# Names users see on screen for well-known buttons (short button name -> label)
FRIENDLY = {
    "maintab.quickpowerbi.button": "Visualize this view",
    "send": "Email a Link",
    "sendselected": "Email a Link",
    "flows.refreshcommandbar": "Flow",
    "flows.refreshcommandbar.flows": "Flow (menu)",
    "documenttemplate": "Excel Templates",
    "wordtemplate": "Word Templates",
    "runreport": "Run Report",
    "reports": "Run Report",
    "sharing": "Share",
    "permissions.sharing": "Share",
    "deletemenu": "Delete",
    "delete": "Delete",
    "newrecord": "New",
    "exporttoexcel": "Export to Excel",
    "importdatafromexcel": "Import from Excel",
    "quickcampaign": "Quick Campaign",
    "modernquickcampaign": "Quick Campaign (new)",
    "addtolist": "Add to Marketing List",
    "followbutton": "Follow",
    "unfollowbutton": "Unfollow",
    "newunfollowbutton": "Unfollow (new)",
    "followbuttonforyammer": "Follow in Yammer",
    "mailmerge": "Mail Merge",
    "runworkflow": "Run Workflow",
    "assign": "Assign",
    "addconnection": "Connect",
    "actionbuttonformsteams": "Collaborate (Teams)",
    "mergerecords": "Merge",
    "saveandclose": "Save & Close",
    "saveandnew": "Save & New",
}

# Quick-select groups shown as buttons on the page
GROUPS = [
    {"id": "reports", "label": "Reports", "buttons": ["runreport", "reports"]},
    {"id": "templates", "label": "Templates", "buttons": ["wordtemplate", "documenttemplate"]},
    {"id": "flows", "label": "Flows & workflows",
     "buttons": ["flows.refreshcommandbar", "runworkflow"]},
    {"id": "email", "label": "Email a Link", "buttons": ["send", "sendselected"]},
    {"id": "visualize", "label": "Visualize this view", "buttons": ["maintab.quickpowerbi.button"]},
    {"id": "marketing", "label": "Marketing",
     "buttons": ["quickcampaign", "modernquickcampaign", "addtolist"]},
    {"id": "follow", "label": "Follow",
     "buttons": ["followbutton", "unfollowbutton", "newunfollowbutton", "followbuttonforyammer"]},
    {"id": "mailmerge", "label": "Mail Merge", "buttons": ["mailmerge"]},
    {"id": "portal", "label": "Power Pages", "prefixes": ["adx.", "microsoft.powerpages."]},
]

RARE = re.compile(r"\.VisualizationTab\.|\.Chart\.|\.Jewel\.", re.I)


def solution_hides(solution_dir, cat, prefix):
    """DemoHider hides actually present in the solution files: {target: control_id}."""
    import hide_engine as he
    out = {}
    ents = Path(solution_dir) / "Entities"
    if not ents.is_dir():
        return out
    schema_map = {(s.get("schema_name") or "").lower(): n for n, s in cat["sources"].items()}
    for d in ents.iterdir():
        f = d / "RibbonDiff.xml"
        if not f.is_file():
            continue
        table = schema_map.get(d.name.lower(), d.name.lower())
        _, text = he.decode(f.read_bytes())
        for hid, loc in he.read_hides(text, "table"):
            if hid.startswith(prefix + "."):
                out[f"table|{table}|{loc}"] = loc
    return out


def build_items(cat, manifest_path, solution_dir=None, prefix="demohider"):
    """One item per button (table + control id), including hidden ones.
    Returns (items, hidden_targets, warnings)."""
    warnings = []
    manifest_hidden = {}
    if manifest_path.exists():
        m = mp.load_json(manifest_path)
        manifest_hidden = {k: h for k, h in m.get("hidden", {}).items() if h.get("scope") == "table"}

    if solution_dir:
        in_solution = solution_hides(solution_dir, cat, prefix)
        hidden_targets = set(in_solution)
        only_manifest = sorted(set(manifest_hidden) - hidden_targets)
        only_solution = sorted(hidden_targets - set(manifest_hidden))
        if only_manifest:
            warnings.append(f"{len(only_manifest)} button(s) are in DemoHider's records but not in "
                            "the solution files, so they are not shown as hidden.")
        if only_solution:
            warnings.append(f"{len(only_solution)} DemoHider hide(s) are in the solution files but "
                            "missing from its records; they are shown as hidden and can be restored.")
    else:
        in_solution = {}
        hidden_targets = set(manifest_hidden)

    records = [r for r in cat["records"] if r["scope"] == "table"]
    records += [r for r in mp.manifest_records(manifest_path, cat) if r["scope"] == "table"]
    known = {mp.target(r) for r in records}
    for t, cid in in_solution.items():   # hidden, but not in catalogue or manifest
        if t not in known:
            table = t.split("|")[1]
            loc = mp.location_from_id(cid)
            loc = loc if loc in WHERE_ORDER else "HomepageGrid"
            records.append({"scope": "table", "table": table, "control_id": cid, "command_id": "",
                            "command_bar_location": loc, "label": "", "label_hint": "",
                            "parent_control_id": "", "button": mp.button_name(cid, table, "table"),
                            "record_key": "|".join(["table", table, loc, cid, ""])})

    items, seen = [], {}
    for r in records:
        loc = r["command_bar_location"]
        if loc not in WHERE_ORDER and loc != "?":
            continue
        t = mp.target(r)
        if t in seen:
            continue
        b = r["button"]
        friendly = FRIENDLY.get(b.lower())
        name = friendly or mp.display(r)
        parent = f"table|{r['table']}|{r['parent_control_id']}" if r.get("parent_control_id") else ""
        seen[t] = len(items)
        items.append({"k": t, "rk": r["record_key"], "t": r["table"],
                      "w": loc if loc in WHERE_ORDER else "HomepageGrid",
                      "n": name, "b": b, "id": r["control_id"], "p": parent,
                      "h": t in hidden_targets,
                      "mh": r.get("modern") == "hidden",
                      "mr": r.get("modern_reason", ""),
                      "rare": bool(RARE.search(r["control_id"]))})
    items.sort(key=lambda i: (i["t"], WHERE_ORDER.get(i["w"], 9), i["n"].lower(), i["id"]))
    return items, hidden_targets, warnings


def items_for_preset(preset_path, cat, manifest_path, items_by_k):
    preset = mp.load_json(preset_path)
    records = list(cat["records"]) + mp.manifest_records(manifest_path, cat)
    all_tables = sorted({r["table"] for r in records if r["scope"] == "table"})
    rules = [mp.compile_rule(r, all_tables, f"rule {i + 1}")
             for i, r in enumerate(preset.get("rules", []))]
    keeps = [mp.compile_rule(r, all_tables, f"keep {i + 1}")
             for i, r in enumerate(preset.get("keep", []))]
    out = []
    for r in records:
        if any(mp.matches(c, r) for c in rules) and not any(mp.matches(k, r) for k in keeps):
            t = mp.target(r)
            if t in items_by_k:
                out.append(t)
    return sorted(set(out))


def preset_from_ticks(name, ticked_items):
    groups = {}
    for i in ticked_items:
        groups.setdefault((i["t"], i["w"]), []).append(i["b"])
    rules = []
    for (table, where), buttons in sorted(groups.items()):
        rules.append({"description": f"Picked on {table} {WHERE_LABEL[where].lower()}",
                      "tables": [table], "where": [mp.WHERE_NAMES[where]],
                      "buttons": sorted(set(buttons))})
    return {"name": name, "description": "Saved from the tick-box picker",
            "rules": rules, "keep": []}


def main():
    ap = argparse.ArgumentParser(description="DemoHider tick-box picker")
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--out", required=True, help="Plan file to write")
    ap.add_argument("--manifests", default="manifests")
    ap.add_argument("--presets", default="presets")
    ap.add_argument("--solution", help="Unpacked solution folder (used to confirm what is hidden)")
    ap.add_argument("--prefix", default="demohider")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--flow", action="store_true",
                    help="After Apply: check, confirm and send in the same browser tab")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--round-wait", type=int, default=60)
    ap.add_argument("--full", action="store_true",
                    help="send the whole solution, not only the changed tables")
    args = ap.parse_args()
    if args.flow and not args.solution:
        ap.error("--flow needs --solution")

    cat = mp.load_catalog(args.catalog)
    env = cat["environment_name"]
    manifest_path = Path(args.manifests) / f"{env}.json"
    presets_dir = Path(args.presets)
    items, hidden, warnings = build_items(cat, manifest_path, args.solution, args.prefix)
    by_k = {i["k"]: i for i in items}
    token = secrets.token_urlsafe(16)
    result = {"code": None}
    done = threading.Event()
    flow = flow_server.Flow(args.catalog, args.solution, max(1, args.rounds),
                            args.round_wait, args.full) if args.flow else None

    page = (PAGE.replace("__DATA__", json.dumps({
        "env": env, "url": cat.get("environment_url", ""), "items": items, "groups": GROUPS,
        "token": token, "updated": cat.get("updated_utc", ""), "warnings": warnings,
        "has_modern_info": any("modern" in r for r in cat["records"])}, separators=(",", ":"))
        .replace("</", "<\\/")))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def ok_token(self, q):
            return q.get("token", [""])[0] == token

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/flow"):
                if not self.ok_token(q):
                    return self.send(403, "Open the link shown in the terminal.", "text/plain")
                if u.path == "/flow" and flow:
                    return self.send(200, flow_server.page_html(token), "text/html")
                return self.send(200, page, "text/html")
            if not self.ok_token(q):
                return self.send(403, '{"error":"bad token"}')
            if flow and u.path.startswith("/flow/"):
                r = flow.handle("GET", u.path, {})
                if r:
                    return self.send(r[0], json.dumps(r[1]))
            if u.path == "/presets":
                names = sorted(p.stem for p in presets_dir.glob("*.json")) if presets_dir.is_dir() else []
                return self.send(200, json.dumps(names))
            if u.path == "/preset":
                name = q.get("name", [""])[0]
                path = presets_dir / f"{name}.json"
                if not re.fullmatch(r"[\w.-]+", name or "") or not path.exists():
                    return self.send(404, '{"error":"preset not found"}')
                try:
                    keys = items_for_preset(path, cat, manifest_path, by_k)
                except SystemExit:
                    return self.send(400, json.dumps({"error": f"{name}.json has a mistake - "
                                                      "run it with -Preset to see the details"}))
                return self.send(200, json.dumps(keys))
            return self.send(404, '{"error":"not found"}')

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self.ok_token(q):
                return self.send(403, '{"error":"bad token"}')
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self.send(400, '{"error":"bad request"}')

            if flow and u.path.startswith("/flow/"):
                r = flow.handle("POST", u.path, body)
                if r:
                    return self.send(r[0], json.dumps(r[1]))
                return self.send(404, '{"error":"not found"}')

            if u.path == "/cancel":
                result["code"] = 3
                self.send(200, '{"ok":true}')
                done.set()
                return

            if u.path == "/save-set":
                name = (body.get("name") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
                    return self.send(400, json.dumps({"error": "Use letters, numbers, - or _ "
                                                      "(no spaces), up to 40 characters."}))
                ticked = [by_k[k] for k in body.get("checked", []) if k in by_k]
                if not ticked:
                    return self.send(400, '{"error":"Tick at least one button first."}')
                presets_dir.mkdir(parents=True, exist_ok=True)
                mp.save_json(presets_dir / f"{name}.json", preset_from_ticks(name, ticked))
                return self.send(200, json.dumps({"ok": True, "name": name}))

            if u.path == "/apply":
                checked = {k for k in body.get("checked", []) if k in by_k}
                # a hidden menu takes its items with it - don't list those separately
                def under_checked(i):
                    p, guard = i["p"], 0
                    while p and guard < 10:
                        if p in checked:
                            return True
                        p = (by_k.get(p) or {}).get("p", "")
                        guard += 1
                    return False
                # Items inside a ticked menu get no action of their own: the menu hides
                # them, and whatever state they had is kept (never hide + restore together).
                to_hide = [by_k[k] for k in sorted(checked - hidden) if not under_checked(by_k[k])]
                to_show = [by_k[k] for k in sorted(hidden - checked) if not under_checked(by_k[k])]
                if not to_hide and not to_show:
                    self.send(200, json.dumps({"ok": True, "changes": 0}))
                    result["code"] = 4
                    done.set()
                    return
                plan = {"environment_name": env, "preset": "picked",
                        "description": "Chosen in the tick-box picker",
                        "generated_by": "picker_server.py",
                        "hide": [{"label": i["n"], "record_key": i["rk"],
                                  "reason": "Ticked in the picker"} for i in to_hide],
                        "unhide": [{"label": i["n"], "record_key": i["rk"],
                                    "reason": "Unticked in the picker"} for i in to_show]}
                mp.save_json(args.out, plan)
                result["code"] = 0
                result["summary"] = (len(to_hide), len(to_show))
                if flow:
                    if flow.phase != "idle":
                        return self.send(409, '{"error":"Already applied - see the other page."}')
                    print(f"\nSaved your choices: {len(to_hide)} to hide, {len(to_show)} to show "
                          f"again -> {args.out}. Checking ...")
                    flow.start_plan(args.out)
                    return self.send(200, json.dumps({"ok": True, "flow": True}))
                self.send(200, json.dumps({"ok": True, "hide": len(to_hide), "show": len(to_show)}))
                done.set()
                return
            return self.send(404, '{"error":"not found"}')

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"Picker for {env} ({cat.get('environment_url', '')}): {len(items)} buttons, "
          f"{len(hidden)} currently hidden by DemoHider.")
    for w in warnings:
        print(f"NOTE: {w}")
    print("Your browser should open now. If it doesn't, open this link:")
    print(f"  {url}")
    print("Tick the buttons to hide, then click 'Apply'.  (Ctrl+C here to cancel)")
    if not args.no_browser:
        webbrowser.open(url)

    if flow:
        # done = cancelled in the picker or no changes; otherwise the flow decides
        try:
            while not done.wait(0.5):
                if flow.done.is_set() or flow.idle():
                    break
        except KeyboardInterrupt:
            if flow.phase == "running":
                print("\nStopped while sending - check the result with  .\\demohider.ps1 -Publish")
                flow.exit_code = 2
            elif flow.exit_code is None:
                flow.exit_code = 3
        server.shutdown()
        if not done.is_set():
            if flow.exit_code is None:
                flow.exit_code = 3
            if not flow.job:   # a job already printed its own result
                print("\n" + flow.final_message())
            sys.exit(flow.exit_code)
    else:
        try:
            while not done.wait(0.5):
                pass
        except KeyboardInterrupt:
            result["code"] = 3
        server.shutdown()

    code = result["code"]
    if code == 0:
        h, s = result["summary"]
        print(f"\nSaved your choices: {h} to hide, {s} to show again -> {args.out}")
    elif code == 4:
        print("\nNo changes - your ticks match what is already hidden.")
    else:
        print("\nCancelled - nothing was changed.")
    sys.exit(code)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DemoHider Picker</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1d2330;--muted:#647089;--line:#e2e6ee;--accent:#2f5ee6;
--accent-ink:#fff;--hide:#c2410c;--hide-bg:#fff4ed;--show:#15803d;--show-bg:#effbf2;--chip:#eef2fb}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--panel:#1a1f29;--ink:#e6e9f0;--muted:#97a1b6;
--line:#2a3140;--accent:#6b8dff;--accent-ink:#0b1020;--hide:#fb923c;--hide-bg:#2a1a10;--show:#4ade80;
--show-bg:#10241a;--chip:#232a38}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 "Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);
padding:12px 20px}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font-size:17px;margin:0 16px 0 0}
.env{color:var(--muted)}
input[type=search],input[type=text],select{font:inherit;color:var(--ink);background:var(--bg);
border:1px solid var(--line);border-radius:8px;padding:7px 10px}
input[type=search]{flex:1;min-width:220px}
.chips{margin-top:10px}
.chip{border:1px solid var(--line);background:var(--chip);color:var(--ink);border-radius:999px;
padding:4px 12px;font:inherit;cursor:pointer}
.chip:hover{border-color:var(--accent)}
.lbl{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-right:4px}
label.opt{display:inline-flex;gap:6px;align-items:center;cursor:pointer;color:var(--muted)}
main{display:grid;grid-template-columns:200px 1fr;gap:20px;padding:20px;max-width:1200px;margin:0 auto}
nav{position:sticky;top:150px;align-self:start}
nav button{display:flex;justify-content:space-between;width:100%;text-align:left;font:inherit;
color:var(--ink);background:none;border:0;border-radius:8px;padding:8px 10px;cursor:pointer}
nav button.on{background:var(--chip);font-weight:600}
nav .count{color:var(--hide);font-weight:600}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin-bottom:16px}
section h2{font-size:15px;margin:0;padding:12px 16px;border-bottom:1px solid var(--line);
display:flex;justify-content:space-between;align-items:center}
section h2 small{color:var(--muted);font-weight:400}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:2px 16px;padding:10px 16px}
.item{display:flex;gap:8px;align-items:flex-start;padding:5px 6px;border-radius:6px;cursor:pointer}
.item:hover{background:var(--bg)}
.item input{margin-top:3px;accent-color:var(--hide)}
.item .id{display:block;color:var(--muted);font-size:11px;word-break:break-all}
.item.child{margin-left:22px}
.item.dis{opacity:.5;cursor:default}
.tag{font-size:11px;border-radius:4px;padding:0 5px;margin-left:4px}
.tag.h{background:var(--hide-bg);color:var(--hide)}
.tag.s{background:var(--show-bg);color:var(--show)}
.tag.m{background:var(--chip);color:var(--muted)}
.legend{margin-top:8px;color:var(--muted);font-size:12px}
.legend b{color:var(--ink);font-weight:600}
.note{margin-top:8px;background:var(--hide-bg);color:var(--hide);border-radius:8px;padding:6px 10px;font-size:13px}
.url{color:var(--muted);font-size:12px}
.more{font:inherit;font-size:12px;color:var(--accent);background:none;border:0;cursor:pointer;padding:0 0 0 22px}
footer{position:sticky;bottom:0;background:var(--panel);border-top:1px solid var(--line);padding:12px 20px}
footer .row{max-width:1200px;margin:0 auto;justify-content:space-between}
.btn{font:inherit;border-radius:8px;padding:8px 16px;cursor:pointer;border:1px solid var(--line);
background:var(--panel);color:var(--ink)}
.btn.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
.btn:disabled{opacity:.5;cursor:default}
.summary b.h{color:var(--hide)} .summary b.s{color:var(--show)}
.empty{padding:16px;color:var(--muted)}
.done{max-width:560px;margin:80px auto;text-align:center;background:var(--panel);border:1px solid var(--line);
border-radius:12px;padding:32px}
#toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--panel);
padding:8px 14px;border-radius:8px;display:none}
@media (max-width:760px){main{grid-template-columns:1fr}nav{position:static;display:flex;flex-wrap:wrap}
nav button{width:auto}}
</style>
</head>
<body>
<header>
  <div class="row">
    <h1>DemoHider</h1><span class="env" id="env"></span><span class="url" id="url"></span>
    <input type="search" id="q" placeholder="Search buttons, e.g. report, excel, flow">
  </div>
  <div class="row chips" id="chips"><span class="lbl">Quick select</span></div>
  <div class="row chips">
    <span class="lbl">Show</span>
    <label class="opt"><input type="checkbox" class="wf" value="HomepageGrid" checked> Main grid</label>
    <label class="opt"><input type="checkbox" class="wf" value="Form" checked> Form</label>
    <label class="opt"><input type="checkbox" class="wf" value="SubGrid" checked> Subgrid</label>
    <label class="opt"><input type="checkbox" id="onlyTicked"> Only ticked</label>
    <label class="opt" title="Chart-designer buttons and buttons whose rules say they are never shown in model-driven apps"><input type="checkbox" id="rare"> Buttons not shown in modern apps</label>
    <span class="lbl" style="margin-left:12px">When I tick</span>
    <label class="opt"><input type="checkbox" id="allTables"> tick it on every table</label>
  </div>
  <div class="legend"><b>Ticked = hidden.</b> Untick a hidden button to bring it back.
    Ticking a <b>menu</b> hides everything in it. Hiding applies to that table in every app of this environment.</div>
  <div id="notes"></div>
</header>
<main>
  <nav id="tables"></nav>
  <div id="list"></div>
</main>
<footer>
  <div class="row">
    <div class="row">
      <select id="sets"><option value="">Load a saved set…</option></select>
      <input type="text" id="setName" placeholder="Name for this set" size="18">
      <button class="btn" id="saveSet">Save set</button>
      <button class="btn" id="clear">Untick all</button>
    </div>
    <div class="row">
      <span class="summary" id="summary"></span>
      <button class="btn" id="cancel">Cancel</button>
      <button class="btn primary" id="apply">Apply</button>
    </div>
  </div>
</footer>
<div id="toast"></div>
<script>
const D = __DATA__;
const items = D.items, byK = new Map(items.map(i => [i.k, i]));
const checked = new Set(items.filter(i => i.h).map(i => i.k));
const children = new Map();
items.forEach(i => { if (i.p && byK.has(i.p)) { if (!children.has(i.p)) children.set(i.p, []); children.get(i.p).push(i); } });
const tables = [...new Set(items.map(i => i.t))];
const WL = {HomepageGrid: "Main grid", Form: "Form", SubGrid: "Subgrid"};
let current = "*", open = new Set();
const $ = s => document.querySelector(s);
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
const api = (path, body) => fetch(path + (path.includes("?") ? "&" : "?") + "token=" + D.token,
  body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
  .then(r => r.json().then(j => { if (!r.ok) throw new Error(j.error || r.status); return j; }));
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.style.display = "block";
  clearTimeout(toast.h); toast.h = setTimeout(() => t.style.display = "none", 2600); }
function parentChecked(i) { let p = i.p, n = 0; while (p && n++ < 10) { if (checked.has(p)) return true; p = (byK.get(p) || {}).p; } return false; }
function wheres() { return [...document.querySelectorAll(".wf:checked")].map(e => e.value); }

$("#env").textContent = "Environment: " + D.env;
$("#url").textContent = D.url;
$("#notes").innerHTML = D.warnings.map(w => `<div class="note">${esc(w)}</div>`).join("");
D.groups.forEach(g => { const b = document.createElement("button"); b.className = "chip"; b.textContent = g.label;
  b.title = "Tick these on the tables and places shown"; b.onclick = () => quick(g); $("#chips").appendChild(b); });

function inGroup(g, i) { const b = i.b.toLowerCase(), id = i.id.toLowerCase();
  return (g.buttons || []).includes(b) || (g.prefixes || []).some(p => id.startsWith(p)); }
function quick(g) {
  const ws = wheres(); let n = 0;
  const all = $("#rare").checked;
  items.forEach(i => { if ((current === "*" || i.t === current) && ws.includes(i.w) && inGroup(g, i) && (all || !i.mh) && !checked.has(i.k)) { checked.add(i.k); n++; } });
  render(); toast(n ? `Ticked ${n} "${g.label}" button(s)` : `No more "${g.label}" buttons here`);
}
function setChecked(i, on) {
  const targets = $("#allTables").checked ? items.filter(x => x.b.toLowerCase() === i.b.toLowerCase() && x.w === i.w) : [i];
  targets.forEach(x => on ? checked.add(x.k) : checked.delete(x.k));
  if (targets.length > 1) toast(`${on ? "Ticked" : "Unticked"} on ${targets.length} tables`);
}
function visible(i, q, ws) {
  if (!ws.includes(i.w)) return false;
  if (current !== "*" && i.t !== current) return false;
  if ((i.rare || i.mh) && !$("#rare").checked && !checked.has(i.k) && !i.h) return false;
  if ($("#onlyTicked").checked && !checked.has(i.k) && !i.h) return false;
  if (q && !(i.n + " " + i.b + " " + i.id).toLowerCase().includes(q)) return false;
  return true;
}
function itemHtml(i, child) {
  const on = checked.has(i.k), byParent = parentChecked(i);
  let tag = "";
  if (byParent) tag = '<span class="tag m">hidden with its menu</span>';
  else if (on && !i.h) tag = '<span class="tag h">will hide</span>';
  else if (!on && i.h) tag = '<span class="tag s">will show</span>';
  else if (on && i.h) tag = '<span class="tag h">hidden</span>';
  if (i.mh) tag += '<span class="tag m">not in modern apps</span>';
  const tip = i.id + (i.mh ? "\nNever shown in model-driven apps (" + i.mr + ") - hiding it changes nothing on screen." : "");
  return `<label class="item${child ? " child" : ""}${byParent ? " dis" : ""}" title="${esc(tip)}">
    <input type="checkbox" data-k="${esc(i.k)}" ${on || byParent ? "checked" : ""} ${byParent ? "disabled" : ""}>
    <span>${esc(i.n)}${tag}<span class="id">${esc(i.b)}</span></span></label>`;
}
function render() {
  const q = $("#q").value.trim().toLowerCase(), ws = wheres();
  // table nav
  const counts = {}; items.forEach(i => { if (checked.has(i.k)) counts[i.t] = (counts[i.t] || 0) + 1; });
  $("#tables").innerHTML = [["*", "All tables"], ...tables.map(t => [t, t])].map(([t, label]) => {
    const c = t === "*" ? checked.size : (counts[t] || 0);
    return `<button data-t="${esc(t)}" class="${current === t ? "on" : ""}"><span>${esc(label)}</span><span class="count">${c || ""}</span></button>`;
  }).join("");
  // sections
  let html = "", shown = 0;
  for (const t of tables) {
    if (current !== "*" && t !== current) continue;
    for (const w of ["HomepageGrid", "Form", "SubGrid"]) {
      const top = items.filter(i => i.t === t && i.w === w && !(i.p && byK.has(i.p)) && (visible(i, q, ws) || (children.get(i.k) || []).some(c => visible(c, q, ws))));
      if (!top.length) continue;
      shown += top.length;
      const n = items.filter(i => i.t === t && i.w === w && checked.has(i.k)).length;
      html += `<section><h2><span>${esc(t)} · ${WL[w]}</span><small>${n ? n + " ticked" : ""}</small></h2><div class="grid">`;
      for (const i of top) {
        html += "<div>" + itemHtml(i, false);
        const kids = (children.get(i.k) || []).filter(c => q ? visible(c, q, ws) : true);
        if (kids.length) {
          if (open.has(i.k) || q) html += kids.map(c => itemHtml(c, true)).join("");
          else html += `<button class="more" data-open="${esc(i.k)}">▸ ${kids.length} item(s) in this menu</button>`;
        }
        html += "</div>";
      }
      html += "</div></section>";
    }
  }
  $("#list").innerHTML = html || '<div class="empty">No buttons match. Clear the search or tick more places under “Show”.</div>';
  // summary
  const hide = items.filter(i => checked.has(i.k) && !i.h && !parentChecked(i)).length;
  const show = items.filter(i => i.h && !checked.has(i.k) && !parentChecked(i)).length;
  $("#summary").innerHTML = hide || show ? `<b class="h">${hide}</b> to hide · <b class="s">${show}</b> to show again` : "No changes yet";
  $("#apply").disabled = !(hide || show);
}
document.addEventListener("change", e => {
  const k = e.target.dataset && e.target.dataset.k;
  if (k) { setChecked(byK.get(k), e.target.checked); render(); }
  else if (e.target.matches(".wf,#onlyTicked,#rare")) render();
});
document.addEventListener("click", e => {
  const t = e.target.closest("[data-t]"); if (t) { current = t.dataset.t; render(); window.scrollTo(0, 0); return; }
  const o = e.target.closest("[data-open]"); if (o) { open.add(o.dataset.open); render(); }
});
let qt; $("#q").addEventListener("input", () => { clearTimeout(qt); qt = setTimeout(render, 120); });
$("#clear").onclick = () => { if (confirm("Untick every button? (Buttons hidden now will be shown again when you apply.)")) { checked.clear(); render(); } };
function loadSets(select) {
  api("/presets").then(names => { $("#sets").innerHTML = '<option value="">Load a saved set…</option>' +
    names.map(n => `<option${n === select ? " selected" : ""}>${esc(n)}</option>`).join(""); }).catch(() => {});
}
$("#sets").onchange = e => { const n = e.target.value; if (!n) return;
  api("/preset?name=" + encodeURIComponent(n)).then(keys => { let a = 0; keys.forEach(k => { if (!checked.has(k)) { checked.add(k); a++; } });
    render(); toast(`Loaded "${n}": ticked ${a} more button(s)`); }).catch(err => toast(err.message)); };
$("#saveSet").onclick = () => { const name = $("#setName").value.trim();
  api("/save-set", {name, checked: [...checked]}).then(r => { toast(`Saved set "${r.name}"`); loadSets(r.name); }).catch(err => toast(err.message)); };
function finish(title, text) { document.body.innerHTML = `<div class="done"><h2>${title}</h2><p>${text}</p></div>`; }
$("#apply").onclick = () => { $("#apply").disabled = true;
  api("/apply", {checked: [...checked]}).then(r => r.flow ? (location.href = "/flow?token=" + D.token) : r.changes === 0
    ? finish("No changes", "You can close this tab.")
    : finish("Choices saved ✓", `${r.hide} to hide, ${r.show} to show again.<br>Go back to the terminal to review and confirm with <b>Y</b>.<br><br>You can close this tab.`))
    .catch(err => { toast(err.message); $("#apply").disabled = false; }); };
$("#cancel").onclick = () => api("/cancel", {}).then(() => finish("Cancelled", "Nothing was changed. You can close this tab."));
loadSets(); render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
