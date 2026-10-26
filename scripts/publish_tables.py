"""
DemoHider - publish only what a run changed
===========================================
"Publish all customizations" can take very long in a busy environment and the
connection may drop. This publishes ONLY the tables (and the Application
Ribbon, if used) that a DemoHider run touched, which is much faster.

Usage (PowerShell, from the DemoHider folder):
  python scripts\\publish_tables.py --run runs\\<run-folder>
  python scripts\\publish_tables.py --env-url https://org.crm4.dynamics.com --tables contact,lead
"""

import argparse
import json
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape


def get_token(env_url: str) -> str:
    az = shutil.which("az")
    if not az:
        sys.exit("ERROR: Azure CLI ('az') not found.")
    r = subprocess.run([az, "account", "get-access-token", "--resource", env_url,
                        "--query", "accessToken", "-o", "tsv"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit("ERROR: Could not get a token. Run 'az login --tenant <tenant> "
                 "--allow-no-subscriptions' and try again.\n" + r.stderr)
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


def main():
    ap = argparse.ArgumentParser(description="Publish only the changed tables")
    ap.add_argument("--run", help="Run folder created by hide_engine.py")
    ap.add_argument("--env-url")
    ap.add_argument("--tables", help="Comma-separated logical names")
    ap.add_argument("--app-ribbon", action="store_true", help="Also publish the Application Ribbon")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    if args.run:
        env_url, tables, app = from_run(Path(args.run))
    else:
        if not (args.env_url and args.tables):
            sys.exit("Give --run, or --env-url with --tables.")
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
    print(f"Publishing: {what} ...", flush=True)

    req = urllib.request.Request(
        env_url.rstrip("/") + "/api/data/v9.2/PublishXml",
        data=json.dumps({"ParameterXml": xml}).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {get_token(env_url)}",
                 "Content-Type": "application/json", "Accept": "application/json",
                 "OData-MaxVersion": "4.0", "OData-Version": "4.0"})
    try:
        with urllib.request.urlopen(req, timeout=args.timeout):
            pass
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:800]
        sys.exit(f"Publish failed: HTTP {e.code}\n{body}")
    except (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionError, OSError) as e:
        print(f"The connection dropped while publishing ({e}).")
        print("Dynamics usually finishes the publish anyway - wait a few minutes and press Ctrl+F5.")
        print("If the change still isn't visible, run this command again.")
        sys.exit(2)
    print("Published.")


if __name__ == "__main__":
    main()
