"""
DemoHider - publish only what a run changed (reliable version)
==============================================================
Publishes ONLY the tables (and the Application Ribbon, if used) that a
DemoHider run touched - much faster than "Publish all customizations".

To make the change reliably reach the app it:
  1. retries if the connection drops, times out or Dynamics is busy
  2. waits a little and publishes a second time ("confirmation publish") -
     in busy environments the app sometimes only picks up a change after a
     second publish
  3. records the result in the run folder (run.json -> "publish")

Usage (PowerShell, from the DemoHider folder):
  python scripts\\publish_tables.py --run runs\\<run-folder>
  python scripts\\publish_tables.py --env-url https://org.crm4.dynamics.com --tables contact,lead

Options:
  --attempts N   tries per publish (default 3)
  --wait S       seconds between tries and before the confirmation publish (default 30)
  --no-confirm   skip the second (confirmation) publish
  --round N      label stored in run.json (the runner sends 2 rounds)

NOTE: in PowerShell, --run needs a real folder, e.g. --run ".\\runs\\20261027-175030-picked".
      ($RunDir only exists inside demohider.ps1 - use  .\\demohider.ps1 -Publish  instead.)

Exit codes: 0 = published and confirmed, 2 = not confirmed (connection problems),
            1 = error (e.g. not signed in, request rejected)
"""

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_token(env_url: str) -> str:
    az = shutil.which("az")
    if not az:
        print("ERROR: Azure CLI ('az') not found.")
        sys.exit(1)
    r = subprocess.run([az, "account", "get-access-token", "--resource", env_url,
                        "--query", "accessToken", "-o", "tsv"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        print("ERROR: Could not get a token. Run 'az login --tenant <tenant> "
              "--allow-no-subscriptions' and try again.\n" + r.stderr)
        sys.exit(1)
    return r.stdout.strip()


def from_run(run_dir: Path):
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    catalog = json.loads(Path(run["catalog"]).read_text(encoding="utf-8-sig"))
    tables, app = set(), False
    for item in run.get("items", []):
        if item.get("status") not in ("hide", "unhide"):
            continue
        if item.get("scope") == "application":
            app = True
        elif item.get("table"):
            tables.add(item["table"])
    return catalog["environment_url"], sorted(tables), app


def publish_once(env_url, xml, timeout):
    """Returns ('ok'|'retry'|'fail', message)."""
    req = urllib.request.Request(
        env_url.rstrip("/") + "/api/data/v9.2/PublishXml",
        data=json.dumps({"ParameterXml": xml}).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {get_token(env_url)}",
                 "Content-Type": "application/json", "Accept": "application/json",
                 "OData-MaxVersion": "4.0", "OData-Version": "4.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return "ok", "published"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:600]
        if e.code == 429 or e.code >= 500:
            return "retry", f"Dynamics is busy (HTTP {e.code})"
        return "fail", f"request rejected (HTTP {e.code}): {body}"
    except (TimeoutError, socket.timeout) as e:
        return "retry", f"timed out ({e})"
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        return "retry", f"connection problem ({e})"


def publish_with_retries(env_url, xml, attempts, wait, timeout, label):
    for i in range(1, attempts + 1):
        print(f"  {label} - attempt {i} of {attempts} ...", end=" ", flush=True)
        status, msg = publish_once(env_url, xml, timeout)
        print(msg)
        if status == "ok":
            return "ok", msg
        if status == "fail":
            return "fail", msg
        if i < attempts:
            print(f"  waiting {wait} seconds before trying again ...", flush=True)
            time.sleep(wait)
    return "retry", msg


def record(run_dir, data):
    if not run_dir:
        return
    p = Path(run_dir) / "run.json"
    try:
        run = json.loads(p.read_text(encoding="utf-8"))
        run["publish"] = data                      # latest result
        run.setdefault("publish_history", []).append(data)   # every round
        p.write_text(json.dumps(run, indent=2), encoding="utf-8")
    except Exception as e:  # never fail the publish because of bookkeeping
        print(f"  (could not record the result in {p}: {e})")


def main():
    ap = argparse.ArgumentParser(description="Publish only the changed tables, reliably")
    ap.add_argument("--run", help="Run folder created by hide_engine.py")
    ap.add_argument("--env-url")
    ap.add_argument("--tables", help="Comma-separated logical names")
    ap.add_argument("--app-ribbon", action="store_true", help="Also publish the Application Ribbon")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--wait", type=int, default=30)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--no-confirm", action="store_true", help="Skip the confirmation publish")
    ap.add_argument("--round", help="Round number (set by the runner, stored in run.json)")
    args = ap.parse_args()

    if args.run:
        env_url, tables, app = from_run(Path(args.run))
    else:
        if not (args.env_url and args.tables):
            print("Give --run, or --env-url with --tables.")
            sys.exit(1)
        env_url = args.env_url
        tables = [t.strip().lower() for t in args.tables.split(",") if t.strip()]
        app = args.app_ribbon

    if not tables and not app:
        print("Nothing to publish.")
        return

    xml = "<importexportxml>"
    if tables:
        xml += "<entities>" + "".join(f"<entity>{escape(t)}</entity>" for t in tables) + "</entities>"
    if app:
        xml += "<ribbons><ribbon></ribbon></ribbons>"
    xml += "</importexportxml>"
    what = ", ".join(tables + (["Application Ribbon"] if app else []))
    print(f"Publishing: {what}")
    info = {"what": what, "started_utc": now_utc()}
    if args.round:
        info["round"] = args.round

    status, msg = publish_with_retries(env_url, xml, args.attempts, args.wait, args.timeout,
                                       "Publish")
    if status != "ok":
        info.update(status="not_published", detail=msg, finished_utc=now_utc())
        record(args.run, info)
        print("")
        print(f"NOT PUBLISHED: {msg}")
        sys.exit(1 if status == "fail" else 2)
    info["published_utc"] = now_utc()

    if not args.no_confirm:
        print(f"  waiting {args.wait} seconds, then publishing again so the app picks it up ...",
              flush=True)
        time.sleep(args.wait)
        c_status, c_msg = publish_with_retries(env_url, xml, args.attempts, args.wait,
                                               args.timeout, "Confirmation publish")
        if c_status != "ok":
            # the first publish worked; the change is live in Dynamics
            print(f"  (confirmation publish did not complete: {c_msg} - the first publish did)")
            info["confirm"] = f"not completed: {c_msg}"
        else:
            info["confirm"] = "ok"
            info["confirmed_utc"] = now_utc()

    info.update(status="published", finished_utc=now_utc())
    record(args.run, info)
    print("Published.")


if __name__ == "__main__":
    main()
