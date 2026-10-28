"""
DemoHider - Plan Maker (no AI needed)
=====================================
Creates the plan file that hide_engine.py / demohider.ps1 use, in three ways:

  1. MANUAL  - tick buttons in a spreadsheet (picker)
  2. PRESET  - a reusable file of rules, e.g. "sales-demo"
  3. RULES   - the rules inside a preset: which buttons, tables and places

Key idea: every button has a short BUTTON NAME that is the same on every table.
  Mscrm.HomepageGrid.contact.RunReport  -> RunReport
  Mscrm.Form.lead.WordTemplate          -> WordTemplate
So a preset can say "hide RunReport on account, contact and lead" and it works
in any environment.

Commands (PowerShell, from the DemoHider folder)
------------------------------------------------
  # Search the catalogue for buttons and their short names
  python scripts\\make_plan.py find --catalog catalog\\CUS-Dev\\catalog.json --text report
  python scripts\\make_plan.py find --catalog catalog\\CUS-Dev\\catalog.json --text excel --table contact --where "main grid"

  # 1. MANUAL: create a picker spreadsheet, put an x in the "hide" column, save
  python scripts\\make_plan.py picker --catalog catalog\\CUS-Dev\\catalog.json --out pickers\\CUS-Dev.csv
  python scripts\\make_plan.py from-picker --catalog catalog\\CUS-Dev\\catalog.json --picker pickers\\CUS-Dev.csv --out plans\\my-pick.json

  # 2/3. PRESET with rules
  python scripts\\make_plan.py from-preset --catalog catalog\\CUS-Dev\\catalog.json --preset presets\\sales-demo.json --out plans\\sales-demo.json
  python scripts\\make_plan.py from-preset ... --unhide        (bring a preset's buttons back)

  # Bring back EVERYTHING DemoHider has hidden in an environment
  python scripts\\make_plan.py unhide-all --catalog catalog\\CUS-Dev\\catalog.json --out plans\\unhide-all.json

You normally don't run these yourself - demohider.ps1 does it for you.

Preset file format (presets\\<name>.json)
-----------------------------------------
  {
    "name": "sales-demo",
    "description": "What this preset is for",
    "rules": [
      {
        "description": "Reports and templates",          (shown in the preview as the reason)
        "tables":  ["account", "contact"]  or  "*"        (* = every table in the catalogue)
        "where":   ["main grid", "form", "subgrid"]       (optional; default = all three;
                                                   "list" also works for main grid)
        "buttons": ["RunReport", "WordTemplate"]          (short button names - use 'find')
      },
      {
        "description": "Power Pages buttons",
        "tables": "*",
        "id_starts_with": ["adx.", "Microsoft.PowerPages."]
      }
    ],
    "keep": [                                             (optional exceptions - never hidden)
      { "tables": ["opportunity"], "buttons": ["RunReport"] }
    ]
  }
  Inside one rule, every condition must match. A button is hidden if ANY rule matches
  and no "keep" rule matches. Other conditions you can use in a rule:
    "id_contains":      ["Excel"]           text inside the control id
    "label_contains":   ["Template"]        text inside the button name / label
    "commands":         ["Mscrm.ExportToExcel"]
    "scope":            "application"       hide on ALL tables at once (needs the
                                            Application Ribbon in your solution)
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

WHERE = {
    "main grid": "HomepageGrid", "maingrid": "HomepageGrid", "main_grid": "HomepageGrid",
    "list": "HomepageGrid", "grid": "HomepageGrid",
    "homepagegrid": "HomepageGrid", "form": "Form", "subgrid": "SubGrid",
    "dashboard": "Dashboard", "other": "Other",
}
WHERE_NAMES = {"HomepageGrid": "main grid", "Form": "form", "SubGrid": "subgrid",
               "Dashboard": "dashboard", "Grid": "grid", "Other": "other"}
DEFAULT_WHERE = ["HomepageGrid", "Form", "SubGrid"]
RULE_KEYS = {"description", "tables", "where", "buttons", "id_starts_with", "id_contains",
             "label_contains", "commands", "scope"}
PICKER_FIELDS = ["hide", "table", "where", "button", "name", "in_menu", "control_id",
                 "command_id", "scope", "record_key"]


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        die(f"file not found: {path}")
    except json.JSONDecodeError as e:
        die(f"{path} is not valid JSON: {e}\n(check for a missing comma or quote near that line)")


def save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


# ================================================================ catalogue

def button_name(control_id, table, scope):
    """The part of the control id after the table name - same on every table."""
    marker = "{!entitylogicalname}" if scope == "application" else (table or "").lower()
    segs = control_id.split(".")
    for i, s in enumerate(segs):
        if s.lower() == marker and i < len(segs) - 1:
            return ".".join(segs[i + 1:])
    return control_id


def location_from_id(control_id):
    for seg in control_id.split("."):
        seg = {"HomeGrid": "HomepageGrid"}.get(seg, seg)
        if seg in WHERE_NAMES:
            return seg
    return "Other"


def display(r):
    return r.get("label") or r.get("label_hint") or r["control_id"]


def target(r):
    return f"{r['scope']}|{r['table'] or '*'}|{r['control_id']}"


def load_catalog(path):
    cat = load_json(path)
    if cat.get("schema_version") != 2:
        die("this catalogue is an older format - rebuild it with build_catalog.py")
    for r in cat["records"]:
        r["button"] = button_name(r["control_id"], r["table"], r["scope"])
    return cat


def manifest_records(manifest_path, cat):
    """Hidden buttons from the manifest, as catalogue-like records (for unhiding)."""
    p = Path(manifest_path)
    if not p.exists():
        return []
    m = load_json(p)
    known = {target(r) for r in cat["records"]}
    out = []
    for key, h in m.get("hidden", {}).items():
        if key in known:
            continue
        loc = h.get("command_bar_location") or location_from_id(h["control_id"])
        if loc == "Other" and not h.get("command_bar_location"):
            loc = "?"     # unknown: match any place
        out.append({"scope": h["scope"], "table": h.get("table", ""),
                    "control_id": h["control_id"], "command_id": h.get("command_id", ""),
                    "command_bar_location": loc, "label": h.get("label", ""),
                    "label_hint": "", "parent_control_id": "",
                    "button": button_name(h["control_id"], h.get("table", ""), h["scope"]),
                    "record_key": "|".join([h["scope"], h.get("table") or "*", loc,
                                            h["control_id"], h.get("command_id", "")])})
    return out


# ==================================================================== rules

def as_list(v):
    if v is None:
        return None
    return [v] if isinstance(v, str) else list(v)


def norm_where(values, where_from):
    out = []
    for v in values:
        w = WHERE.get(str(v).strip().lower())
        if not w:
            die(f"{where_from}: unknown place '{v}' - use main grid, form or subgrid")
        out.append(w)
    return out


def compile_rule(rule, all_tables, label):
    unknown = set(rule) - RULE_KEYS
    if unknown:
        die(f"{label}: unknown setting(s) {sorted(unknown)}. Allowed: {sorted(RULE_KEYS)}")
    scope = rule.get("scope", "table")
    if scope not in ("table", "application"):
        die(f"{label}: scope must be 'table' or 'application'")
    tables = rule.get("tables", "*")
    tables = all_tables if tables == "*" or tables == ["*"] else [t.lower() for t in as_list(tables)]
    where = norm_where(as_list(rule["where"]), label) if rule.get("where") else DEFAULT_WHERE
    c = {"label": label, "description": rule.get("description", ""), "scope": scope,
         "tables": set(tables), "where": set(where),
         "buttons": {b.lower() for b in as_list(rule.get("buttons")) or []},
         "id_starts_with": [s.lower() for s in as_list(rule.get("id_starts_with")) or []],
         "id_contains": [s.lower() for s in as_list(rule.get("id_contains")) or []],
         "label_contains": [s.lower() for s in as_list(rule.get("label_contains")) or []],
         "commands": {s.lower() for s in as_list(rule.get("commands")) or []}}
    if not any([c["buttons"], c["id_starts_with"], c["id_contains"], c["label_contains"],
                c["commands"]]):
        die(f"{label}: says where to look but not which buttons - add 'buttons' "
            "(or id_starts_with / id_contains / label_contains / commands)")
    return c


def matches(c, r):
    if r["scope"] != c["scope"]:
        return False
    if c["scope"] == "table" and r["table"] not in c["tables"]:
        return False
    if r["command_bar_location"] != "?" and r["command_bar_location"] not in c["where"]:
        return False
    cid = r["control_id"].lower()
    if c["buttons"] and r["button"].lower() not in c["buttons"]:
        return False
    if c["id_starts_with"] and not any(cid.startswith(s) for s in c["id_starts_with"]):
        return False
    if c["id_contains"] and not any(s in cid for s in c["id_contains"]):
        return False
    if c["label_contains"]:
        text = f"{r.get('label', '')} {r.get('label_hint', '')}".lower()
        if not any(s in text for s in c["label_contains"]):
            return False
    if c["commands"] and r["command_id"].lower() not in c["commands"]:
        return False
    return True


# ============================================================== plan output

def drop_menu_items(selected, all_records):
    """If a menu is hidden, its items disappear with it - no need to list them."""
    chosen = {target(r) for r in selected}
    by_target = {}
    for r in all_records:
        by_target.setdefault(f"{r['scope']}|{r['table'] or '*'}|{r['control_id']}", r)
    kept, dropped = [], 0
    for r in selected:
        parent, inside_hidden = r.get("parent_control_id"), False
        while parent:
            pt = f"{r['scope']}|{r['table'] or '*'}|{parent}"
            if pt in chosen:
                inside_hidden = True
                break
            parent = (by_target.get(pt) or {}).get("parent_control_id")
        if inside_hidden:
            dropped += 1
        else:
            kept.append(r)
    return kept, dropped


def write_plan(cat, out, preset, description, chosen, action, reasons):
    seen, items = set(), []
    for r in chosen:
        t = target(r)
        if t in seen:
            continue      # same control in the same ribbon = one hide
        seen.add(t)
        items.append({"label": display(r), "record_key": r["record_key"],
                      "reason": reasons.get(r["record_key"], "")})
    plan = {"environment_name": cat["environment_name"], "preset": preset,
            "description": description, "generated_by": "make_plan.py",
            "hide": items if action == "hide" else [],
            "unhide": items if action == "unhide" else []}
    save_json(out, plan)
    return items


def print_summary(items, records_by_key, action):
    by = Counter()
    for i in items:
        r = records_by_key[i["record_key"]]
        where = "ALL tables" if r["scope"] == "application" else r["table"]
        by[(where, WHERE_NAMES.get(r["command_bar_location"], "saved"))] += 1
    verb = "hide" if action == "hide" else "show again"
    print(f"\n{len(items)} button(s) to {verb}:")
    for (t, w), n in sorted(by.items()):
        print(f"  {t:15} {w:10} {n}")


# ================================================================= commands

def cmd_find(args):
    cat = load_catalog(args.catalog)
    text = (args.text or "").lower()
    where = set(norm_where([args.where], "--where")) if args.where else None
    rows = defaultdict(lambda: {"tables": set(), "where": set(), "names": set(), "scope": ""})
    for r in cat["records"]:
        if args.table and r["table"] != args.table.lower():
            continue
        if where and r["command_bar_location"] not in where:
            continue
        if not args.include_menu_items and r["parent_control_id"]:
            continue
        hay = f"{r['button']} {r['control_id']} {r['command_id']} {display(r)}".lower()
        if text and text not in hay:
            continue
        k = (r["scope"], r["button"])
        rows[k].setdefault("modern", set()).add(r.get("modern", "unknown"))
        rows[k]["tables"].add(r["table"] or "ALL")
        rows[k]["where"].add(WHERE_NAMES.get(r["command_bar_location"], "other"))
        rows[k]["names"].add(display(r))
    if not rows:
        print("No buttons found.")
        return
    print(f"{'BUTTON (use this in presets)':45} {'NAME':32} {'WHERE':24} TABLES")
    for (scope, b), v in sorted(rows.items(), key=lambda x: (x[0][0] != "table", x[0][1].lower())):
        tag = " [application]" if scope == "application" else ""
        if v.get("modern") == {"hidden"}:
            tag += " [not in modern apps]"
        print(f"{(b + tag)[:45]:45} {sorted(v['names'])[0][:32]:32} "
              f"{','.join(sorted(v['where']))[:24]:24} {','.join(sorted(v['tables']))}")
    print(f"\n{len(rows)} button name(s).")


def cmd_picker(args):
    cat = load_catalog(args.catalog)
    where = set(DEFAULT_WHERE)
    rows = [r for r in cat["records"]
            if r["scope"] == "table" and r["command_bar_location"] in where
            and (args.table is None or r["table"] == args.table.lower())]
    if args.include_application:
        rows += [r for r in cat["records"]
                 if r["scope"] == "application" and r["command_bar_location"] in where]
    order = {"HomepageGrid": 0, "Form": 1, "SubGrid": 2}
    rows.sort(key=lambda r: (r["scope"] != "table", r["table"],
                             order.get(r["command_bar_location"], 9),
                             bool(r["parent_control_id"]), display(r).lower()))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=PICKER_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({"hide": "", "table": r["table"] or "ALL TABLES",
                        "where": WHERE_NAMES.get(r["command_bar_location"]),
                        "button": r["button"], "name": display(r),
                        "in_menu": "yes" if r["parent_control_id"] else "",
                        "control_id": r["control_id"], "command_id": r["command_id"],
                        "scope": r["scope"], "record_key": r["record_key"]})
    print(f"Picker written: {out}  ({len(rows)} buttons)")
    print("Open it in Excel, type x in the 'hide' column for each button to hide, "
          "and save it (keep the CSV format).")


def read_picker(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    first = text.splitlines()[0] if text else ""
    delim = ";" if first.count(";") > first.count(",") else ","
    return list(csv.DictReader(text.splitlines(), delimiter=delim))


def cmd_from_picker(args):
    cat = load_catalog(args.catalog)
    by_key = {r["record_key"]: r for r in cat["records"]}
    rows = read_picker(args.picker)
    if rows and "record_key" not in rows[0]:
        die("the picker has no 'record_key' column - create it again with the 'picker' command")
    chosen, missing = [], 0
    for row in rows:
        if not (row.get("hide") or "").strip():
            continue
        r = by_key.get((row.get("record_key") or "").strip())
        if r:
            chosen.append(r)
        else:
            missing += 1
    if missing:
        print(f"WARNING: {missing} ticked row(s) are not in this catalogue (picker from another "
              "environment or an older catalogue?) - skipped.")
    if not chosen:
        die("no buttons ticked - put an x in the 'hide' column and save the file")
    chosen, dropped = drop_menu_items(chosen, cat["records"])
    name = args.name or Path(args.picker).stem
    reasons = {r["record_key"]: "Picked manually" for r in chosen}
    items = write_plan(cat, args.out, name, f"Picked manually in {Path(args.picker).name}",
                       chosen, "unhide" if args.unhide else "hide", reasons)
    print_summary(items, by_key, "unhide" if args.unhide else "hide")
    if dropped:
        print(f"  ({dropped} menu item(s) left out - their menu is hidden, so they go with it)")
    print(f"\nPlan written: {args.out}")


def cmd_from_preset(args):
    cat = load_catalog(args.catalog)
    preset = load_json(args.preset)
    name = preset.get("name") or Path(args.preset).stem
    records = list(cat["records"])
    if args.unhide:   # hidden buttons may no longer be in a refreshed catalogue
        records += manifest_records(Path(args.manifests) / f"{cat['environment_name']}.json", cat)
    by_key = {r["record_key"]: r for r in records}

    all_tables = sorted({r["table"] for r in records if r["scope"] == "table"})
    rules = [compile_rule(r, all_tables, f"rule {i + 1}")
             for i, r in enumerate(preset.get("rules", []))]
    keeps = [compile_rule(r, all_tables, f"keep {i + 1}")
             for i, r in enumerate(preset.get("keep", []))]
    if not rules:
        die(f"{args.preset} has no rules")

    wanted_tables = set().union(*(c["tables"] for c in rules if c["scope"] == "table")) \
        if any(c["scope"] == "table" for c in rules) else set()
    missing_tables = sorted(wanted_tables - set(all_tables))

    chosen, reasons, kept_back = [], {}, 0
    found = defaultdict(set)          # rule label -> button names found
    for r in records:
        hit = next((c for c in rules if matches(c, r)), None)
        if not hit:
            continue
        if any(matches(k, r) for k in keeps):
            kept_back += 1
            continue
        chosen.append(r)
        reasons[r["record_key"]] = hit["description"] or f"preset {name}, {hit['label']}"
        found[hit["label"]].add(r["button"].lower())

    chosen, dropped = drop_menu_items(chosen, records)
    action = "unhide" if args.unhide else "hide"
    items = write_plan(cat, args.out, name, preset.get("description", ""), chosen, action, reasons)
    print(f"Preset: {name}   Environment: {cat['environment_name']}")
    print_summary(items, by_key, action)
    if dropped:
        print(f"  ({dropped} menu item(s) left out - their menu is hidden, so they go with it)")
    if kept_back:
        print(f"  ({kept_back} match(es) kept visible by 'keep' rules)")

    for c in rules:
        not_found = sorted(c["buttons"] - found[c["label"]])
        if not_found:
            print(f"NOTE: {c['label']}: no match in this environment for: {', '.join(not_found)}")
    if missing_tables:
        print(f"NOTE: table(s) not in the catalogue, skipped: {', '.join(missing_tables)} "
              "(add them with build_catalog.py --tables ...)")
    print(f"\nPlan written: {args.out}")


def cmd_unhide_all(args):
    cat = load_catalog(args.catalog)
    mpath = Path(args.manifests) / f"{cat['environment_name']}.json"
    if not mpath.exists():
        die(f"no manifest at {mpath} - nothing has been hidden in this environment yet")
    m = load_json(mpath)
    items = [{"label": h.get("label") or h["control_id"], "scope": h["scope"],
              "table": h.get("table", ""), "control_id": h["control_id"],
              "reason": "Unhide all"} for h in m.get("hidden", {}).values()]
    if not items:
        die("the manifest is empty - nothing is hidden")
    save_json(args.out, {"environment_name": cat["environment_name"], "preset": "unhide-all",
                         "description": "Bring back everything DemoHider has hidden",
                         "generated_by": "make_plan.py", "hide": [], "unhide": items})
    print(f"{len(items)} button(s) to show again.\nPlan written: {args.out}")


def main():
    ap = argparse.ArgumentParser(description="DemoHider plan maker (no AI needed)")
    ap.add_argument("--manifests", default="manifests")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("find", help="Search the catalogue for buttons")
    p.add_argument("--catalog", required=True)
    p.add_argument("--text", help="Text to look for, e.g. report")
    p.add_argument("--table")
    p.add_argument("--where", help="main grid, form or subgrid")
    p.add_argument("--include-menu-items", action="store_true")

    p = sub.add_parser("picker", help="Create a spreadsheet to tick buttons in")
    p.add_argument("--catalog", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--table", help="Only one table")
    p.add_argument("--include-application", action="store_true",
                   help="Also list application-level buttons (hide on ALL tables)")

    p = sub.add_parser("from-picker", help="Make a plan from a ticked picker")
    p.add_argument("--catalog", required=True)
    p.add_argument("--picker", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--name")
    p.add_argument("--unhide", action="store_true")

    p = sub.add_parser("from-preset", help="Make a plan from a preset")
    p.add_argument("--catalog", required=True)
    p.add_argument("--preset", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--unhide", action="store_true")

    p = sub.add_parser("unhide-all", help="Make a plan that brings everything back")
    p.add_argument("--catalog", required=True)
    p.add_argument("--out", required=True)

    args = ap.parse_args()
    {"find": cmd_find, "picker": cmd_picker, "from-picker": cmd_from_picker,
     "from-preset": cmd_from_preset, "unhide-all": cmd_unhide_all}[args.cmd](args)


if __name__ == "__main__":
    main()
