"""
DemoHider - Catalogue Builder (v2)
==================================
Reads the real, merged command bar (ribbon) definitions from a Dataverse /
Dynamics 365 environment and builds a verified catalogue of buttons that the
Customization Engine (hide_engine.py) can hide.

Key terms used in the catalogue
-------------------------------
  control_id            The Id of the button / menu control. This is the exact value
                        that goes into <HideCustomAction Location="..."/>.
  command_id            The action the control runs (e.g. Mscrm.NewRecordFromGrid).
                        Several controls can share one command.
  command_bar_location  Where the control appears: Form, HomepageGrid (main list),
                        SubGrid, Dashboard, or Other.
  scope                 "table"       -> read from that table's ribbon, hide affects that table
                        "application" -> read from the Application Ribbon, hide affects ALL tables
  defined_in            For table-scope rows: whether the control is the table's own
                        ("table ribbon") or inherited from the Application Ribbon.
  record_key            Unique key = scope|table|location|control_id|command_id.
                        The AI plan refers to rows by this key.

Output (catalog/<env-name>/)
----------------------------
  catalog.json              Full catalogue (used by hide_engine.py)
  catalog.csv               Everything, for Excel
  catalog_tables.csv        Table-scope rows only
  catalog_application.csv   Application-scope rows only
  raw/*.xml                 Source ribbon XML (used for verification before hiding)

Usage (PowerShell, from the DemoHider folder)
---------------------------------------------
  # Build / refresh the default tables + application ribbon
  python scripts\\build_catalog.py --env-url https://org.crm4.dynamics.com --env-name CUS-Dev

  # Retry only what failed last time (keeps everything else)
  python scripts\\build_catalog.py --env-url https://org.crm4.dynamics.com --env-name CUS-Dev --retry-failed

  # Add or refresh specific tables only (others are kept)
  python scripts\\build_catalog.py --env-url ... --env-name CUS-Dev --tables quote,salesorder --no-app-ribbon

  # A table that keeps timing out: fetch Form / main grid / subgrid separately
  python scripts\\build_catalog.py --env-url ... --env-name CUS-Dev --tables account --no-app-ribbon --split

Requirements: Python 3.10+, Azure CLI signed in (az login --tenant <tenant> --allow-no-subscriptions).
No extra Python packages needed.
"""

import argparse
import base64
import csv
import http.client
import io
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

SCHEMA_VERSION = 2
DEFAULT_TABLES = ["account", "contact", "lead", "opportunity"]
APP = "(application)"

# Ribbon elements that are clickable controls and can be hidden.
CONTROL_TAGS = {"Button", "SplitButton", "FlyoutAnchor", "ToggleButton"}

# When a full ribbon times out, it is fetched in these parts instead.
SPLIT_FILTERS = ["Form", "HomepageGrid", "SubGrid"]

LOCATION_NAMES = {
    "HomepageGrid": "Main grid (list view)",
    "Form": "Form",
    "SubGrid": "Subgrid / associated view",
    "Dashboard": "Dashboard",
    "Grid": "Grid (other)",
}
LOCATION_ALIASES = {"HomeGrid": "HomepageGrid", "SubGridStandard": "SubGrid",
                    "SubGridAssociated": "SubGrid"}
# Command bars that exist in the Unified Interface (model-driven apps).
UCI_LOCATIONS = {"HomepageGrid", "Form", "SubGrid", "Dashboard"}

# Segments that never describe what a button does.
LABEL_STOP_WORDS = {
    "Ribbon", "Mscrm", "LabelText", "Label", "Button", "Btn", "Alt", "ToolTipTitle",
    "ToolTipDescription", "Command", "Cmd", "HomepageGrid", "HomeGrid", "Form",
    "SubGrid", "SubGridStandard", "SubGridAssociated", "MainTab", "ApplicationRibbon",
    "Record", "Management", "Status", "Actions", "Data", "Tools", "Grid", "Group",
    "Controls", "Flyout", "Templates",
}
# Internal names whose on-screen name differs a lot. Shown as hints, not facts.
COMMON_NAMES = {
    "NewRecord": "New", "DeleteMenu": "Delete", "Sharing": "Share",
    "SendShortcut": "Email a Link", "ReportsMenu": "Run Report",
    "AddToList": "Add to Marketing List", "SaveAndClose": "Save & Close",
    "SaveAndNew": "Save & New", "RunWorkflow": "Run Workflow",
}

CSV_FIELDS = ["scope", "table", "command_bar_location", "location_name", "uci_relevant",
              "label", "label_hint", "control_id", "command_id", "control_type",
              "parent_control_id", "defined_in", "publisher_prefix", "label_key",
              "tab_id", "group_id", "record_key"]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ================================================================ connection

class Token:
    """Gets an access token from the Azure CLI and refreshes it when old."""

    def __init__(self, env_url: str):
        self.env_url = env_url
        self.value = None
        self.fetched_at = 0.0

    def get(self) -> str:
        if not self.value or time.time() - self.fetched_at > 40 * 60:
            self.value = self._from_az()
            self.fetched_at = time.time()
        return self.value

    def _from_az(self) -> str:
        az = shutil.which("az")
        if not az:
            sys.exit("ERROR: Azure CLI ('az') not found. Install it and reopen the terminal.")
        result = subprocess.run(
            [az, "account", "get-access-token", "--resource", self.env_url,
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip():
            sys.exit("ERROR: Could not get a token. Run 'az login --tenant <tenant> "
                     "--allow-no-subscriptions' and try again.\n" + result.stderr)
        return result.stdout.strip()


class ApiError(Exception):
    def __init__(self, kind: str, message: str, code: int | None = None):
        super().__init__(message)
        self.kind = kind      # "http", "timeout" or "network"
        self.code = code


def _error_text(body: str) -> str:
    try:
        return json.loads(body)["error"]["message"][:500]
    except Exception:
        return body[:500]


def call_api(env_url, token, path, timeout, attempts=1, retry_timeouts=True) -> dict:
    url = env_url.rstrip("/") + "/api/data/v9.2/" + path
    err = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token.get()}",
            "Accept": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            err = ApiError("http", f"HTTP {e.code}: {_error_text(body)}", e.code)
            if e.code != 429 and e.code < 500:      # request problem: retry won't help
                raise err from None
        except (TimeoutError, socket.timeout):
            err = ApiError("timeout", f"timed out after {timeout} seconds")
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                err = ApiError("timeout", f"timed out after {timeout} seconds")
            else:
                err = ApiError("network", f"network error: {e.reason}")
        except (http.client.HTTPException, ConnectionError, OSError) as e:
            err = ApiError("network", f"connection problem: {type(e).__name__}: {e}")

        if err.kind == "timeout" and not retry_timeouts:
            raise err
        if attempt < attempts:
            wait = 10 * attempt
            print(f"\n      {err} - retrying in {wait}s (attempt {attempt + 1} of {attempts}) ...",
                  end=" ", flush=True)
            time.sleep(wait)
    raise err


def decompress(b64: str) -> str:
    """Ribbon XML comes back base64-encoded and zipped."""
    data = base64.b64decode(b64)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".xml")] or z.namelist()
            return z.read(names[0]).decode("utf-8-sig")
    except zipfile.BadZipFile:
        return data.decode("utf-8-sig")


def fetch_entity_ribbon(env_url, token, table, location_filter, timeout, attempts,
                        retry_timeouts=True) -> str:
    enum = f"Microsoft.Dynamics.CRM.RibbonLocationFilters'{location_filter}'"
    variants = [
        f"RetrieveEntityRibbon(EntityName=@p1,RibbonLocationFilter=@p2)?@p1='{table}'&@p2={enum}",
        f"RetrieveEntityRibbon(EntityName='{table}',RibbonLocationFilter={enum})",
    ]
    for i, path in enumerate(variants):
        try:
            data = call_api(env_url, token, path, timeout, attempts, retry_timeouts)
            return decompress(data["CompressedEntityXml"])
        except ApiError as e:
            if e.kind == "http" and e.code == 400 and i == 0:
                continue          # try the other request format
            raise


def fetch_app_ribbon(env_url, token, timeout, attempts) -> str:
    data = call_api(env_url, token, "RetrieveApplicationRibbon()", timeout, attempts)
    return decompress(data["CompressedApplicationRibbonXml"])


def fetch_table_ribbons(env_url, token, table, args):
    """Returns (mode, [(filter_name, xml)]). Falls back to fetching in parts on timeout."""
    attempts = args.retries + 1
    if not args.split:
        try:
            xml = fetch_entity_ribbon(env_url, token, table, "All", args.timeout,
                                      attempts, retry_timeouts=False)
            return "all", [("All", xml)]
        except ApiError as e:
            if e.kind != "timeout":
                raise
            print(f"\n      full ribbon {e}; fetching Form / main grid / subgrid separately ...",
                  end=" ", flush=True)
    parts = []
    for f in SPLIT_FILTERS:
        parts.append((f, fetch_entity_ribbon(env_url, token, table, f, args.timeout, attempts)))
    return "split", parts


def get_table_meta(env_url, token, table) -> dict:
    path = (f"EntityDefinitions(LogicalName='{table}')"
            "?$select=LogicalName,SchemaName,IsCustomizable,IsManaged")
    try:
        d = call_api(env_url, token, path, timeout=60, attempts=2)
        return {"schema_name": d.get("SchemaName", ""),
                "is_customizable": (d.get("IsCustomizable") or {}).get("Value"),
                "is_managed": d.get("IsManaged")}
    except ApiError as e:
        if e.kind == "http" and e.code == 404:
            raise ApiError("http", f"table '{table}' was not found - check the logical name", 404)
        return {"schema_name": "", "is_customizable": None, "is_managed": None,
                "meta_error": str(e)}


# =================================================================== parsing

def local(tag: str) -> str:
    return tag.split("}")[-1]


def camel_words(s: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return re.sub(r"\s+", " ", s.replace("_", " ")).strip()


def derive_name(text: str, table: str) -> str:
    """Best-guess readable name from a label key or control id."""
    if not text:
        return ""
    if text.startswith("$"):
        text = text.split(":", 1)[-1]
    useful = [s for s in text.split(".")
              if s and s not in LABEL_STOP_WORDS and not s.islower()
              and not s.startswith("{") and s.lower() != (table or "").lower()]
    if not useful:
        return ""
    last = useful[-1]
    return COMMON_NAMES.get(last) or camel_words(last)


def label_hint_for(label_key: str, control_id: str, table: str) -> str:
    a, b = derive_name(label_key, table), derive_name(control_id, table)
    norm = lambda s: s.lower().replace(" ", "")
    if a and b and norm(a) != norm(b):
        return f"{a} ({b})"
    return a or b


def collect_loclabels(root, lcid: int) -> dict:
    labels = {}
    for el in root.iter():
        if local(el.tag) == "LocLabel" and el.get("Id"):
            titles = [t for t in el.iter() if local(t.tag) == "Title"]
            chosen = next((t for t in titles if t.get("languagecode") == str(lcid)),
                          titles[0] if titles else None)
            if chosen is not None and chosen.get("description"):
                labels[el.get("Id")] = chosen.get("description")
    return labels


def location_of(tab: str, group: str, control_id: str) -> str:
    for source in (tab, group, control_id):
        for seg in (source or "").split("."):
            seg = LOCATION_ALIASES.get(seg, seg)
            if seg in LOCATION_NAMES:
                return seg
    return "Other"


def parse_ribbon(xml_text, source, scope, table, source_file, lcid) -> list[dict]:
    root = ET.fromstring(xml_text)
    loclabels = collect_loclabels(root, lcid)
    ui = next((el for el in root.iter() if local(el.tag) == "UI"), None)
    if ui is None:
        return []
    records = []

    def walk(el, tab, group, parent):
        tag = local(el.tag)
        if tag == "Tab":
            tab = el.get("Id") or tab
        elif tag == "Group":
            group = el.get("Id") or group

        if tag in CONTROL_TAGS and el.get("Id"):
            cid = el.get("Id")
            key = el.get("LabelText") or el.get("Alt") or el.get("ToolTipTitle") or ""
            label, label_source = "", ""
            if key.startswith("$LocLabels:") and key.split(":", 1)[1] in loclabels:
                label, label_source = loclabels[key.split(":", 1)[1]], "loclabel"
            elif key and not key.startswith("$"):
                label, label_source = key, "plain text"
            loc = location_of(tab, group, cid)
            command = el.get("Command", "")
            records.append({
                "record_key": "|".join([scope, table or "*", loc, cid, command]),
                "source": source,
                "scope": scope,
                "table": table,
                "command_bar_location": loc,
                "location_name": LOCATION_NAMES.get(loc, "Other"),
                "uci_relevant": loc in UCI_LOCATIONS,
                "control_id": cid,
                "command_id": command,
                "control_type": tag,
                "parent_control_id": parent or "",
                "label": label,
                "label_source": label_source,
                "label_hint": label_hint_for(key, cid, table),
                "label_key": key,
                "defined_in": "",
                "publisher_prefix": cid.split(".")[0],
                "tab_id": tab or "",
                "group_id": group or "",
                "source_xml_file": source_file,
                "source_element": ET.tostring(ET.Element(el.tag, dict(el.attrib)),
                                              encoding="unicode"),
            })
            parent = cid

        for child in el:
            walk(child, tab, group, parent)

    walk(ui, "", "", "")
    return records


def dedupe(records: list[dict]) -> list[dict]:
    """Remove exact duplicates only: same scope, table, location, control AND command."""
    seen, out = set(), []
    for r in records:
        if r["record_key"] not in seen:
            seen.add(r["record_key"])
            out.append(r)
    return out


def mark_origin(records: list[dict]) -> None:
    app_ids = {r["control_id"] for r in records if r["scope"] == "application"}
    for r in records:
        if r["scope"] == "application":
            r["defined_in"] = "application ribbon"
            continue
        if not app_ids:
            r["defined_in"] = "unknown (application ribbon not loaded)"
            continue
        generic = ".".join("{!EntityLogicalName}" if s.lower() == r["table"].lower() else s
                           for s in r["control_id"].split("."))
        r["defined_in"] = ("application ribbon" if r["control_id"] in app_ids or generic in app_ids
                           else "table ribbon")


# ================================================================== catalogue

def load_catalog(path: Path):
    if not path.exists():
        return None
    try:
        cat = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        print("  (existing catalog.json could not be read - starting a new one)")
        return None
    if cat.get("schema_version") != SCHEMA_VERSION:
        print("  (existing catalogue is an older format - rebuilding)")
        return None
    return cat


def write_outputs(catalog: dict, out_dir: Path) -> None:
    catalog["updated_utc"] = now_utc()
    catalog["records"].sort(key=lambda r: (r["scope"] != "table", r["table"],
                                           r["command_bar_location"], r["control_id"],
                                           r["command_id"]))
    (out_dir / "catalog.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")

    def write_csv(name, rows):
        with open(out_dir / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    recs = catalog["records"]
    write_csv("catalog.csv", recs)
    write_csv("catalog_tables.csv", [r for r in recs if r["scope"] == "table"])
    write_csv("catalog_application.csv", [r for r in recs if r["scope"] == "application"])


def print_summary(catalog: dict) -> list[str]:
    print("\nSummary")
    failed = []
    for name, src in catalog["sources"].items():
        recs = [r for r in catalog["records"] if r["source"] == name]
        counts = {}
        for r in recs:
            counts[r["location_name"]] = counts.get(r["location_name"], 0) + 1
        detail = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no buttons"
        status = src.get("status", "?")
        if status != "ok":
            failed.append(name)
            detail = f"FAILED ({len(recs)} buttons kept from an earlier run)" if recs else "FAILED"
        mode = " [fetched in parts]" if src.get("fetch_mode") == "split" else ""
        print(f"  {name:15} {detail}{mode}")
    t = sum(1 for r in catalog["records"] if r["scope"] == "table")
    a = len(catalog["records"]) - t
    print(f"\nTable-level buttons: {t}   Application-level buttons: {a}")
    return failed


def main():
    ap = argparse.ArgumentParser(description="Build the DemoHider button catalogue.")
    ap.add_argument("--env-url", required=True, help="e.g. https://contoso.crm4.dynamics.com")
    ap.add_argument("--env-name", help="Short folder name, e.g. CUS-Dev")
    ap.add_argument("--tables", help="Comma-separated logical names "
                    f"(default: {','.join(DEFAULT_TABLES)})")
    ap.add_argument("--no-app-ribbon", action="store_true", help="Skip the Application Ribbon")
    ap.add_argument("--retry-failed", action="store_true",
                    help="Only fetch the tables that failed last time")
    ap.add_argument("--fresh", action="store_true",
                    help="Discard the existing catalogue instead of updating it")
    ap.add_argument("--split", action="store_true",
                    help="Fetch Form / main grid / subgrid separately (for very large ribbons)")
    ap.add_argument("--timeout", type=int, default=300, help="Seconds per request (default 300)")
    ap.add_argument("--retries", type=int, default=2, help="Retries per request (default 2)")
    ap.add_argument("--lcid", type=int, default=1033, help="Label language (default 1033 English)")
    ap.add_argument("--out", default="catalog", help="Output folder (default: catalog)")
    args = ap.parse_args()

    env_url = args.env_url.rstrip("/")
    env_name = args.env_name or env_url.split("//")[-1].split(".")[0]
    out_dir = Path(args.out) / env_name
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    catalog = None if args.fresh else load_catalog(out_dir / "catalog.json")
    if catalog and catalog["environment_url"].lower() != env_url.lower():
        sys.exit(f"ERROR: {out_dir} holds a catalogue for {catalog['environment_url']}. "
                 "Use a different --env-name, or --fresh to replace it.")
    if catalog is None:
        catalog = {"schema_version": SCHEMA_VERSION, "environment_url": env_url,
                   "environment_name": env_name, "created_utc": now_utc(),
                   "updated_utc": "", "lcid": args.lcid,
                   "note": "Merged ribbon at time of export. Buttons already hidden are not "
                           "listed. Modern (command designer) commands are not included.",
                   "sources": {}, "records": []}

    if args.retry_failed:
        targets = [n for n, s in catalog["sources"].items() if s.get("status") != "ok"]
        if not targets:
            print("Nothing to retry - every table in the catalogue was read successfully.")
            return
    else:
        tables = ([t.strip().lower() for t in args.tables.split(",") if t.strip()]
                  if args.tables else DEFAULT_TABLES)
        targets = tables + ([] if args.no_app_ribbon else [APP])

    print(f"Environment : {env_url}")
    print(f"Saving to   : {out_dir.resolve()}")
    print(f"Reading     : {', '.join(targets)}\n")

    token = Token(env_url)
    token.get()  # fail early if not signed in

    for name in targets:
        print(f"  Reading {name} ...", end=" ", flush=True)
        try:
            if name == APP:
                xml = fetch_app_ribbon(env_url, token, args.timeout, args.retries + 1)
                files, mode, meta = [("_application.xml", xml)], "all", {"is_customizable": True}
                scope, table = "application", ""
            else:
                meta = get_table_meta(env_url, token, name)
                mode, parts = fetch_table_ribbons(env_url, token, name, args)
                files = [(f"{name}.xml" if f == "All" else f"{name}.{f}.xml", x) for f, x in parts]
                scope, table = "table", name

            new_records = []
            for fname, xml in files:
                (raw_dir / fname).write_text(xml, encoding="utf-8")
                new_records += parse_ribbon(xml, name, scope, table, f"raw/{fname}", args.lcid)
            new_records = dedupe(new_records)

            catalog["records"] = [r for r in catalog["records"] if r["source"] != name] + new_records
            catalog["sources"][name] = {"status": "ok", "fetched_utc": now_utc(),
                                        "fetch_mode": mode,
                                        "raw_files": [f"raw/{f}" for f, _ in files],
                                        "record_count": len(new_records), **meta}
            print(f"{len(new_records)} buttons" + (" (fetched in parts)" if mode == "split" else ""))
        except Exception as e:
            print("FAILED")
            print(f"      reason: {str(e)[:600]}")
            kept = sum(1 for r in catalog["records"] if r["source"] == name)
            if kept:
                print(f"      keeping {kept} buttons from the previous successful read")
            prev = catalog["sources"].get(name, {})
            catalog["sources"][name] = {**prev, "status": "failed", "error": str(e)[:1000],
                                        "failed_utc": now_utc()}
        mark_origin(catalog["records"])
        write_outputs(catalog, out_dir)   # save progress after every table

    failed = print_summary(catalog)
    print(f"Files: {out_dir / 'catalog.json'}, catalog.csv, catalog_tables.csv, "
          "catalog_application.csv")
    if failed:
        print(f"\nFailed: {', '.join(failed)}")
        print("Retry only those with:  add --retry-failed to the same command"
              " (add --split if it timed out again)")
        sys.exit(1)


if __name__ == "__main__":
    main()
