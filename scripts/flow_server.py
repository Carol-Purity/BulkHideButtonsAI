"""
DemoHider - confirm and send, in the browser
============================================
Shows what will change and the environment URL, with "Confirm and send" and
"Cancel" buttons. After you confirm it shows live progress (update files,
safety check, round 1 and round 2 of pack -> import -> publish) and, at the
end, buttons to Publish again, Send again or Undo this change.

demohider.ps1 starts it for you. The tick-box page (picker_server.py) uses it
too, in the same browser tab, after you click Apply.

  python scripts\\flow_server.py --catalog <catalog.json> --solution <folder> --plan <plan.json>
  python scripts\\flow_server.py --catalog <catalog.json> --solution <folder> --run <run> --action undo|publish|send

Runs only on your own computer (127.0.0.1).
Exit codes: 0 = done, 2 = NOT LIVE YET, 5 = round 2 failed (round 1 sent),
            1 = stopped / blocked / error, 3 = cancelled, 4 = nothing to change
"""

import argparse
import json
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sender  # noqa: E402

PLACES = {"HomepageGrid": "Main grid", "Form": "Form", "SubGrid": "Subgrid"}
IDLE_LIMIT = 300   # seconds without the page asking for news before we stop waiting


def place(loc):
    return PLACES.get(loc or "", loc or "")


def items_from_run(run, kind):
    """Rows for the confirmation list."""
    out, skipped = [], 0
    for i in run.get("items", []):
        st = i.get("status")
        if kind == "undo":
            a = {"hide": "show", "unhide": "hide"}.get(st)
        else:
            a = {"hide": "hide", "unhide": "show", "BLOCKED": "blocked"}.get(st)
        if not a:
            skipped += st == "skip"
            continue
        out.append({"a": a,
                    "t": "ALL tables" if i.get("scope") == "application" else (i.get("table") or "?"),
                    "w": place(i.get("command_bar_location")),
                    "n": i.get("label") or i.get("control_id") or "?",
                    "id": i.get("control_id") or "",
                    "why": " ".join(i.get("errors", []))})
    order = {"blocked": 0, "hide": 1, "show": 2}
    out.sort(key=lambda r: (order[r["a"]], r["t"], r["w"], r["n"].lower()))
    return out, skipped


class Flow:
    def __init__(self, catalog, solution, rounds=2, round_wait=60):
        self.catalog, self.solution = str(catalog), str(solution)
        self.rounds, self.round_wait = rounds, round_wait
        cat = json.loads(Path(catalog).read_text(encoding="utf-8-sig"))
        self.env = cat.get("environment_name", "")
        self.url = cat.get("environment_url", "")
        self.phase = "idle"   # checking confirm nothing blocked error running finished cancelled closed
        self.kind = None      # change | undo | publish | send
        self.run_dir = None
        self.confirm = None
        self.error = ""
        self.job = None
        self.exit_code = None
        self.done = threading.Event()
        self.last_seen = time.time()
        self.lock = threading.Lock()

    # ------------------------------------------------------------ starting points
    def start_plan(self, plan_file):
        """Check a plan (in the background), then ask for confirmation."""
        self.phase, self.kind = "checking", "change"

        def work():
            run_dir, log = sender.preview(self.catalog, plan_file, self.solution)
            if not run_dir:
                self.error = "\n".join(log[-40:]) or "The check stopped."
                self.phase, self.exit_code = "error", sender.STOPPED
                return
            self.run_dir = run_dir
            self._build_confirm()
        threading.Thread(target=work, daemon=True).start()

    def start_existing(self, run_dir, action):
        self.run_dir, self.kind = str(run_dir), action
        self._build_confirm()

    def _build_confirm(self):
        run = sender.read_run(self.run_dir)
        rows, skipped = items_from_run(run, self.kind)
        self.confirm = {
            "kind": self.kind, "run": Path(self.run_dir).name,
            "items": rows, "skipped": skipped,
            "hide": sum(r["a"] == "hide" for r in rows),
            "show": sum(r["a"] == "show" for r in rows),
            "blocked": sum(r["a"] == "blocked" for r in rows),
            "warnings": run.get("global_warnings", []),
            "preview": str(Path(self.run_dir) / "preview.md"),
        }
        if self.kind == "change" and self.confirm["blocked"]:
            self.phase, self.exit_code = "blocked", sender.STOPPED
        elif self.kind == "change" and run.get("change_count", 0) == 0:
            self.phase, self.exit_code = "nothing", 4
        elif self.kind == "undo" and run.get("status") != "applied":
            self.error = f"Run {self.confirm['run']} is '{run.get('status')}' - only an applied run can be undone."
            self.phase, self.exit_code = "error", sender.STOPPED
        else:
            self.phase = "confirm"

    # ------------------------------------------------------------------ jobs
    def _start_job(self, what):
        titles = {"change": "Sending your change", "undo": "Undoing the change",
                  "send": "Sending again", "publish": "Publishing again"}
        job = sender.Job(titles[what])
        if what == "publish":
            target = lambda: sender.do_publish(job, self.run_dir)            # noqa: E731
        else:
            target = lambda: sender.do_send(                                 # noqa: E731
                job, self.run_dir, self.catalog, self.solution, self.rounds, self.round_wait,
                apply=(what == "change"), undo=(what == "undo"))

        def work():
            try:
                target()
            except Exception as e:   # never leave the page spinning
                job.write(f"Unexpected error: {e}")
                job.finish("failed", f"Stopped by an unexpected error: {e}", sender.STOPPED)
            self.exit_code = job.code
            self.phase = "finished"
        self.job, self.phase, self.last_action = job, "running", what
        threading.Thread(target=work, daemon=True).start()

    def actions(self):
        if self.phase != "finished" or not self.run_dir:
            return []
        acts = ["publish", "send"]
        try:
            if sender.read_run(self.run_dir).get("status") == "applied":
                acts.append("undo")
        except Exception:
            pass
        return acts

    # ------------------------------------------------------------------- web
    def state(self):
        self.last_seen = time.time()
        return {"phase": self.phase, "kind": self.kind, "env": self.env, "url": self.url,
                "rounds": self.rounds, "round_wait": self.round_wait,
                "confirm": self.confirm, "error": self.error,
                "job": self.job.snapshot() if self.job else None,
                "actions": self.actions(), "run": Path(self.run_dir).name if self.run_dir else ""}

    def handle(self, method, path, body):
        """Returns (status, json-able) or None if the path is not ours."""
        if method == "GET" and path == "/flow/state":
            return 200, self.state()
        if method != "POST":
            return None
        with self.lock:
            if path == "/flow/confirm":
                if self.phase != "confirm":
                    return 409, {"error": "Nothing waiting for confirmation."}
                self._start_job(self.kind)
                return 200, {"ok": True}
            if path == "/flow/cancel":
                if self.phase == "running":
                    return 409, {"error": "Already sending - it can't be cancelled now."}
                if self.phase in ("confirm", "checking"):
                    self.phase, self.exit_code = "cancelled", 3
                    if self.run_dir and self.kind == "change":
                        self._mark_cancelled()
                self.done.set()
                return 200, {"ok": True}
            if path == "/flow/again":
                what = body.get("what")
                if what not in self.actions():
                    return 409, {"error": "That is not available right now."}
                self._start_job(what)
                return 200, {"ok": True}
            if path == "/flow/close":
                if self.phase == "running":
                    return 409, {"error": "Still sending - wait until it finishes."}
                if self.phase == "confirm":
                    self.phase, self.exit_code = "cancelled", 3
                self.done.set()
                return 200, {"ok": True}
        return None

    def _mark_cancelled(self):
        try:
            p = Path(self.run_dir) / "run.json"
            run = json.loads(p.read_text(encoding="utf-8"))
            if run.get("status") == "previewed":
                run["status"] = "cancelled"
                p.write_text(json.dumps(run, indent=2), encoding="utf-8")
        except Exception:
            pass

    def idle(self):
        """True when the page has been gone for a long time and nothing is running."""
        return (self.phase not in ("running", "checking", "idle")
                and time.time() - self.last_seen > IDLE_LIMIT)

    def final_message(self):
        code = self.exit_code
        if self.phase == "cancelled" or code == 3:
            return "Cancelled - nothing was changed."
        if code == 4:
            return "Nothing to change - it's already like that."
        if self.job and self.job.message:
            return self.job.message
        if self.phase == "blocked":
            return "Some items are BLOCKED - nothing was changed. See the reasons on the page."
        if self.error:
            return self.error
        return "Closed - nothing was sent."


def page_html(token):
    return FLOW_PAGE.replace("__TOKEN__", token)


def wait_until_done(flow, server):
    try:
        while not flow.done.wait(0.5):
            if flow.idle():
                print("\n(The page was closed - stopping.)")
                break
    except KeyboardInterrupt:
        if flow.phase == "running":
            print("\nStopped while sending - check the result with  .\\demohider.ps1 -Publish")
            flow.exit_code = sender.NOT_LIVE
        elif flow.exit_code is None:
            flow.exit_code = 3
    server.shutdown()
    if flow.exit_code is None:
        flow.exit_code = 3


def main():
    ap = argparse.ArgumentParser(description="DemoHider - confirm and send in the browser")
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--solution", required=True)
    ap.add_argument("--plan")
    ap.add_argument("--run")
    ap.add_argument("--action", choices=["undo", "publish", "send"])
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--round-wait", type=int, default=60)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if not a.plan and not (a.run and a.action):
        ap.error("give --plan, or --run with --action")

    flow = Flow(a.catalog, a.solution, max(1, a.rounds), a.round_wait)
    if a.plan:
        flow.start_plan(a.plan)
    else:
        flow.start_existing(a.run, a.action)

    token = secrets.token_urlsafe(16)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *x):
            pass

        def send_body(self, code, body, ctype="application/json"):
            data = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def route(self, method):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if q.get("token", [""])[0] != token:
                return self.send_body(403, "Open the link shown in the terminal.", "text/plain")
            if method == "GET" and u.path in ("/", "/flow"):
                return self.send_body(200, page_html(token), "text/html")
            body = {}
            if method == "POST":
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except json.JSONDecodeError:
                    return self.send_body(400, {"error": "bad request"})
            r = flow.handle(method, u.path, body)
            if r is None:
                return self.send_body(404, {"error": "not found"})
            return self.send_body(*r)

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/flow?token={token}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Confirm in your browser ({flow.env}: {flow.url}).")
    print("If it doesn't open, open this link:")
    print(f"  {url}")
    print("Progress is shown here too. (Ctrl+C here to cancel before you confirm.)")
    if not a.no_browser:
        webbrowser.open(url)
    wait_until_done(flow, server)
    if not flow.job:   # a job already printed its own result
        print("\n" + flow.final_message())
    sys.exit(flow.exit_code)


FLOW_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DemoHider</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1d2330;--muted:#647089;--line:#e2e6ee;--accent:#2f5ee6;
--accent-ink:#fff;--hide:#c2410c;--hide-bg:#fff4ed;--show:#15803d;--show-bg:#effbf2;--chip:#eef2fb;
--bad:#b91c1c;--bad-bg:#fdecec;--warn:#a16207;--warn-bg:#fdf6e3}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--panel:#1a1f29;--ink:#e6e9f0;--muted:#97a1b6;
--line:#2a3140;--accent:#6b8dff;--accent-ink:#0b1020;--hide:#fb923c;--hide-bg:#2a1a10;--show:#4ade80;
--show-bg:#10241a;--chip:#232a38;--bad:#f87171;--bad-bg:#2b1414;--warn:#facc15;--warn-bg:#2a2410}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 "Segoe UI",system-ui,sans-serif}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:12px 20px}
header .row{max-width:900px;margin:0 auto}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font-size:17px;margin:0 16px 0 0}
.muted{color:var(--muted)}
main{max-width:900px;margin:0 auto;padding:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:16px}
h2{font-size:18px;margin:0 0 6px}
.target{border:2px solid var(--accent);border-radius:10px;padding:10px 14px;margin:12px 0}
.target b{font-size:15px;word-break:break-all}
.counts b.h{color:var(--hide)} .counts b.s{color:var(--show)} .counts b.x{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px}
td .id{display:block;color:var(--muted);font-size:11px;word-break:break-all}
.tag{font-size:11px;border-radius:4px;padding:1px 6px;font-weight:600;white-space:nowrap}
.tag.hide{background:var(--hide-bg);color:var(--hide)}
.tag.show{background:var(--show-bg);color:var(--show)}
.tag.blocked{background:var(--bad-bg);color:var(--bad)}
.listwrap{max-height:46vh;overflow:auto;border:1px solid var(--line);border-radius:8px;margin-top:10px}
.note{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:8px 12px;margin-top:10px}
.bar{display:flex;flex-wrap:wrap;gap:10px;justify-content:flex-end;margin-top:16px}
.btn{font:inherit;border-radius:8px;padding:9px 18px;cursor:pointer;border:1px solid var(--line);
background:var(--panel);color:var(--ink)}
.btn.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
.btn.danger{color:var(--bad);border-color:var(--bad)}
.btn:disabled{opacity:.5;cursor:default}
ol.steps{list-style:none;padding:0;margin:8px 0 0}
ol.steps li{display:flex;gap:10px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line)}
ol.steps li:last-child{border-bottom:0}
.ic{width:22px;height:22px;flex:none;border-radius:50%;display:grid;place-items:center;font-size:12px;
font-weight:700;border:2px solid var(--line);color:var(--muted)}
li.run .ic{border-color:var(--accent);border-top-color:transparent;animation:spin .9s linear infinite}
li.ok .ic{background:var(--show);border-color:var(--show);color:var(--panel)}
li.fail .ic{background:var(--bad);border-color:var(--bad);color:var(--panel)}
li.skip{color:var(--muted)} li.wait{color:var(--muted)}
li.run{font-weight:600}
li .det{color:var(--muted);font-size:12px;margin-left:auto}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){li.run .ic{animation:none;border-top-color:var(--accent)}}
.result{border-radius:10px;padding:12px 14px;margin-top:14px;font-weight:600}
.result.ok{background:var(--show-bg);color:var(--show)}
.result.warning{background:var(--warn-bg);color:var(--warn)}
.result.notlive,.result.failed{background:var(--bad-bg);color:var(--bad)}
details{margin-top:12px}
summary{cursor:pointer;color:var(--muted)}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:320px;
overflow:auto;font:12px/1.4 Consolas,ui-monospace,monospace;white-space:pre-wrap;word-break:break-all}
.center{text-align:center;padding:40px 20px}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--panel);
padding:8px 14px;border-radius:8px;display:none}
@media (max-width:600px){main{padding:12px}.card{padding:14px}.bar .btn{flex:1}}
</style>
</head>
<body>
<header><div class="row"><h1>DemoHider</h1><span class="muted" id="env"></span></div></header>
<main id="main"><div class="card center muted">Loading…</div></main>
<div id="toast"></div>
<script>
const TOKEN = "__TOKEN__";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const api = (p, body) => fetch(p + "?token=" + TOKEN, body === undefined ? {cache: "no-store"} :
  {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
  .then(r => r.json().then(j => { if (!r.ok) throw new Error(j.error || r.status); return j; }));
function toast(m) { const t = $("#toast"); t.textContent = m; t.style.display = "block";
  clearTimeout(toast.h); toast.h = setTimeout(() => t.style.display = "none", 3000); }

let S = null, lastKey = "", stopped = false, logOpen = false;
const ICON = {ok: "✓", fail: "✕", skip: "–", wait: "", run: ""};
const VERB = {
  change: ["Confirm and send", "Check before sending"],
  undo: ["Confirm undo", "Undo this change?"],
  publish: ["Publish again", "Publish this run again?"],
  send: ["Send again", "Send this run again?"]};

function target() {
  return `<div class="target">You are about to change:<br><b>${esc(S.url)}</b> <span class="muted">(${esc(S.env)})</span></div>`;
}
function itemsTable(rows) {
  const lab = {hide: "HIDE", show: "SHOW", blocked: "BLOCKED"};
  return `<div class="listwrap"><table><thead><tr><th></th><th>Table</th><th>Where</th><th>Button</th></tr></thead><tbody>` +
    rows.map(r => `<tr><td><span class="tag ${r.a}">${lab[r.a]}</span></td><td>${esc(r.t)}</td><td>${esc(r.w)}</td>
      <td>${esc(r.n)}<span class="id">${esc(r.id)}</span>${r.why ? `<span class="id" style="color:var(--bad)">${esc(r.why)}</span>` : ""}</td></tr>`).join("") +
    `</tbody></table></div>`;
}
function sendNote() {
  if (S.kind === "publish") return "Publishes the changed tables, waits, and publishes again. Takes a minute or two.";
  const r = S.rounds > 1 ? `in ${S.rounds} rounds (pack → import → publish, wait ${S.round_wait} s, then again)` : "once (pack → import → publish)";
  return `Sends ${r}. This usually takes 5–20 minutes – keep the terminal window open.`;
}

function viewConfirm() {
  const c = S.confirm, [btn, title] = VERB[S.kind];
  const what = S.kind === "change" ? "" : `<p class="muted">Run <b>${esc(c.run)}</b></p>`;
  const counts = `<p class="counts"><b class="h">${c.hide}</b> to hide · <b class="s">${c.show}</b> to show again` +
    (c.skipped ? ` · <span class="muted">${c.skipped} already done (skipped)</span>` : "") + `</p>`;
  const listTitle = S.kind === "publish" || S.kind === "send" ? `<p class="muted">Buttons changed in this run:</p>` : "";
  return `<div class="card"><h2>${title}</h2>${what}${target()}${counts}
    ${c.warnings.map(w => `<div class="note">${esc(w)}</div>`).join("")}
    ${listTitle}${itemsTable(c.items)}
    <p class="muted">${sendNote()}<br>Full details: ${esc(c.preview)}</p>
    <div class="bar"><button class="btn" data-do="cancel">Cancel</button>
    <button class="btn primary" data-do="confirm">${btn}</button></div></div>`;
}
function viewBlocked() {
  const c = S.confirm;
  return `<div class="card"><h2>Some buttons can't be changed</h2>${target()}
    <p class="counts"><b class="x">${c.blocked}</b> blocked – <b>nothing was changed</b>. The reasons are shown in red below.</p>
    ${itemsTable(c.items)}<div class="bar"><button class="btn" data-do="close">Close</button></div></div>`;
}
function stepsHtml(j) {
  return `<ol class="steps">` + j.steps.map(s => `<li class="${s.status}"><span class="ic">${ICON[s.status]}</span>
    <span>${esc(s.label)}</span><span class="det">${esc([s.detail, s.took].filter(Boolean).join(" · "))}</span></li>`).join("") + `</ol>`;
}
function viewJob() {
  const j = S.job, running = S.phase === "running";
  const res = running ? `<p class="muted">Working… Closing this tab does not stop it; the terminal shows the same progress.</p>`
    : `<div class="result ${j.state}">${esc(j.message)}</div>`;
  const acts = S.actions.map(a => a === "undo"
    ? `<button class="btn danger" data-again="undo">Undo this change</button>`
    : `<button class="btn" data-again="${a}">${a === "publish" ? "Publish again" : "Send again"}</button>`).join("");
  const bar = running ? "" : `<div class="bar">${acts}<button class="btn primary" data-do="close">Close</button></div>`;
  return `<div class="card"><h2>${esc(j.title)}</h2><p class="muted">${esc(S.url)} · run ${esc(S.run)}</p>
    ${stepsHtml(j)}${res}${bar}
    <details id="log"${logOpen ? " open" : ""}><summary>Show details</summary><pre id="pre">${esc(j.log.join("\n"))}</pre></details></div>`;
}
function simple(title, text, close) {
  return `<div class="card center"><h2>${esc(title)}</h2><p class="muted" style="white-space:pre-wrap">${esc(text)}</p>
    ${close ? `<div class="bar" style="justify-content:center"><button class="btn primary" data-do="close">Close</button></div>` : ""}</div>`;
}

function render() {
  $("#env").textContent = S.env ? `Environment: ${S.env} · ${S.url}` : "";
  let html;
  switch (S.phase) {
    case "checking": html = simple("Checking…", "Nothing is changed yet."); break;
    case "confirm": html = viewConfirm(); break;
    case "blocked": html = viewBlocked(); break;
    case "nothing": html = simple("Nothing to change", "It's already like that.", true); break;
    case "error": html = simple("Stopped", S.error, true); break;
    case "running": case "finished": html = viewJob(); break;
    case "cancelled": html = simple("Cancelled", "Nothing was changed. You can close this tab."); break;
    default: html = simple("…", "");
  }
  // only redraw when something changed (keeps scroll position and the open log)
  const key = S.phase + JSON.stringify(S.job && [S.job.steps, S.job.state, S.job.log.length]) + JSON.stringify(S.actions);
  if (key === lastKey) return;
  lastKey = key;
  const pre = $("#pre"), atBottom = !pre || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  $("#main").innerHTML = html;
  const p2 = $("#pre"); if (p2 && atBottom) p2.scrollTop = p2.scrollHeight;
  const d = $("#log"); if (d) d.addEventListener("toggle", () => logOpen = d.open);
}

function poll() {
  if (stopped) return;
  api("/flow/state").then(s => { S = s; render(); })
    .catch(() => { if (S && S.phase !== "running") return; toast("Lost contact with DemoHider - is the terminal still open?"); })
    .finally(() => setTimeout(poll, S && (S.phase === "running" || S.phase === "checking") ? 1000 : 2000));
}
function closed(title, text) { stopped = true; $("#main").innerHTML = simple(title, text); }

document.addEventListener("click", e => {
  const d = e.target.closest("[data-do]"), a = e.target.closest("[data-again]");
  if (d) {
    const what = d.dataset.do; d.disabled = true;
    if (what === "confirm") api("/flow/confirm", {}).then(poll0).catch(err => { toast(err.message); d.disabled = false; });
    if (what === "cancel") api("/flow/cancel", {}).then(() => closed("Cancelled", "Nothing was changed. You can close this tab.")).catch(err => { toast(err.message); d.disabled = false; });
    if (what === "close") api("/flow/close", {}).then(() => closed("All done", "You can close this tab.")).catch(err => { toast(err.message); d.disabled = false; });
  }
  if (a) {
    const what = a.dataset.again;
    const ask = {undo: `Undo this change? The buttons go back to how they were before this run, and it is sent to Dynamics again (${S.rounds} round(s)).`,
      send: "Pack, import and publish this run again?", publish: "Publish this run again?"}[what];
    if (!confirm(ask)) return;
    a.disabled = true;
    api("/flow/again", {what}).then(poll0).catch(err => { toast(err.message); a.disabled = false; });
  }
});
function poll0() { lastKey = ""; api("/flow/state").then(s => { S = s; render(); }); }
poll();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
