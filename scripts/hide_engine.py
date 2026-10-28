"""
DemoHider - Customization Engine + Solution Validator
=====================================================
Applies a hide/unhide plan to an UNPACKED solution folder, safely.

Commands
--------
  preview   Checks every plan item and shows exactly what would change.
            Nothing in the solution folder is modified.
  apply     Applies a previewed run. Refuses if the files changed since preview.
  rollback  Restores the original files from a run (byte-for-byte).
  validate  Checks a solution folder: valid XML, duplicates, unknown controls,
            and whether it matches the manifest of what DemoHider has hidden.

Typical flow (PowerShell, from the DemoHider folder)
----------------------------------------------------
  pac solution export --name DemoHider --path solutions\\DemoHider.zip
  pac solution unpack --zipfile solutions\\DemoHider.zip --folder solutions\\DemoHider --packagetype Unmanaged

  python scripts\\hide_engine.py preview --catalog catalog\\CUS-Dev\\catalog.json --plan plans\\lean-sales.json --solution solutions\\DemoHider
  python scripts\\hide_engine.py apply --run runs\\<run-id>
  python scripts\\hide_engine.py validate --catalog catalog\\CUS-Dev\\catalog.json --solution solutions\\DemoHider

  pac solution pack --zipfile solutions\\DemoHider.zip --folder solutions\\DemoHider --packagetype Unmanaged
  pac solution import --path solutions\\DemoHider.zip --publish-changes

  # undo
  python scripts\\hide_engine.py rollback --run runs\\<run-id>
  (then pack + import again)

Plan file format (plans\\<name>.json)
-------------------------------------
  {
    "environment_name": "CUS-Dev",
    "preset": "lean-sales",
    "description": "Hide sharing, reporting and template buttons",
    "hide":   [ {"record_key": "<from catalog>", "reason": "..."} ],
    "unhide": [ {"record_key": "<from catalog>", "reason": "..."} ]
  }
  Instead of record_key an item may give {"table": "...", "control_id": "...",
  "command_bar_location": "..."} - it is only accepted if it matches exactly one control.

Safety rules
------------
  * Only controls present in the catalogue AND in its source ribbon XML can be hidden.
  * Tables that are not customizable are blocked.
  * DemoHider only removes hides it created (HideActionId starts with the prefix).
  * Hides made by anyone else are never touched.
  * Original files are copied into the run folder before anything changes.
"""

import argparse
import difflib
import os
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import quoteattr, escape
import html
import xml.etree.ElementTree as ET

CONTROL_TAGS = {"Button", "SplitButton", "FlyoutAnchor", "ToggleButton"}
DEFAULT_PREFIX = "demohider"
STALE_DAYS = 30
BOM = b"\xef\xbb\xbf"

EMPTY_RIBBON_DIFF = (
    '<RibbonDiffXml xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
    "  <CustomActions />\n"
    "  <Templates>\n"
    '    <RibbonTemplates Id="Mscrm.Templates"></RibbonTemplates>\n'
    "  </Templates>\n"
    "  <CommandDefinitions />\n"
    "  <RuleDefinitions>\n"
    "    <TabDisplayRules />\n"
    "    <DisplayRules />\n"
    "    <EnableRules />\n"
    "  </RuleDefinitions>\n"
    "  <LocLabels />\n"
    "</RibbonDiffXml>\n"
)


class EngineError(Exception):
    pass


def die(msg: str):
    print(f"ERROR: {msg}")
    sys.exit(1)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def local(tag: str) -> str:
    return tag.split("}")[-1]


def load_json(path: Path) -> dict:
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        die(f"file not found: {path}")
    if not raw.strip():
        die(f"{path} is empty - save the file contents (Ctrl+S) or download it again")
    # Windows tools sometimes save as UTF-16
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        first = text.strip().splitlines()[0][:60] if text.strip() else ""
        die(f"{path} is not valid JSON ({e}). It starts with: {first!r}")


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def decode(raw: bytes) -> tuple[bool, str]:
    return raw.startswith(BOM), raw.decode("utf-8-sig")


def encode(bom: bool, text: str) -> bytes:
    return (BOM if bom else b"") + text.encode("utf-8")


def hide_target(rec: dict) -> str:
    """What a HideCustomAction actually hides: one control id in one ribbon."""
    return f"{rec['scope']}|{rec['table'] or '*'}|{rec['control_id']}"


def make_hide_id(prefix: str, rec: dict) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.]", "", rec["control_id"])
    return f"{prefix}.{safe}.Hide"


def describe(rec: dict) -> str:
    name = rec.get("label") or rec.get("label_hint") or rec["control_id"]
    where = "ALL tables" if rec["scope"] == "application" else rec["table"]
    return f"{name} [{where} / {rec['location_name']}]"


# ================================================================ catalogue

class Catalog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.dir = self.path.parent
        data = load_json(self.path)
        if data.get("schema_version") != 2:
            die("this catalogue was made by an older Catalogue Builder - rebuild it first")
        self.data = data
        self.env = data["environment_name"]
        self.records = data["records"]
        self.by_key = {r["record_key"]: r for r in self.records}
        self.sources = data.get("sources", {})
        self._xml_ids = {}

    def age_days(self) -> float | None:
        try:
            ts = datetime.fromisoformat(self.data.get("updated_utc") or self.data["created_utc"])
            return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
        except Exception:
            return None

    def control_in_source(self, rec: dict):
        """True/False = found or not in the raw ribbon XML; None = XML file missing."""
        f = self.dir / rec["source_xml_file"]
        if f not in self._xml_ids:
            if not f.exists():
                self._xml_ids[f] = None
            else:
                root = ET.parse(f).getroot()
                self._xml_ids[f] = {el.get("Id") for el in root.iter()
                                    if local(el.tag) in CONTROL_TAGS and el.get("Id")}
        ids = self._xml_ids[f]
        return None if ids is None else rec["control_id"] in ids

    def resolve(self, item):
        """Returns (record, error)."""
        if isinstance(item, str):
            item = {"record_key": item}
        if not isinstance(item, dict):
            return None, "plan item is not an object"
        key = item.get("record_key")
        if key:
            rec = self.by_key.get(key)
            return (rec, None) if rec else (None, f"record_key not found in catalogue: {key}")
        cid = item.get("control_id")
        if not cid:
            return None, "plan item has neither record_key nor control_id"
        table = (item.get("table") or "").lower()
        scope = item.get("scope") or ("table" if table else "application")
        loc = item.get("command_bar_location")
        matches = [r for r in self.records
                   if r["control_id"] == cid and r["scope"] == scope and r["table"] == table
                   and (not loc or r["command_bar_location"] == loc)]
        if not matches:
            return None, f"control not found in catalogue: {cid} ({scope} {table})"
        if len({hide_target(r) for r in matches}) > 1:
            return None, f"ambiguous - {len(matches)} matches, use record_key"
        return matches[0], None


def saved_id_record(raw):
    """Minimal record built from a plan item's saved IDs (used only for unhiding)."""
    if isinstance(raw, str):
        raw = {"record_key": raw}
    if not isinstance(raw, dict):
        return None
    if raw.get("record_key"):
        parts = raw["record_key"].split("|")
        if len(parts) < 4:
            return None
        scope, table, loc, cid = parts[:4]
        cmd = "|".join(parts[4:])
    elif raw.get("control_id"):
        cid = raw["control_id"]
        table = (raw.get("table") or "").lower()
        scope = raw.get("scope") or ("table" if table else "application")
        loc, cmd = raw.get("command_bar_location", ""), raw.get("command_id", "")
    else:
        return None
    if scope not in ("table", "application"):
        return None
    table = "" if table == "*" else table.lower()
    return {"record_key": "|".join([scope, table or "*", loc or "?", cid, cmd]),
            "scope": scope, "table": table, "control_id": cid, "command_id": cmd,
            "command_bar_location": loc or "?", "location_name": loc or "?",
            "label": raw.get("label") or cid, "label_hint": "",
            "source": "(application)" if scope == "application" else table,
            "uci_relevant": True, "parent_control_id": "", "defined_in": ""}


# =========================================================== solution files

RIBBON_OPEN = re.compile(r"<RibbonDiffXml\b[^>]*?(/?)>")
HIDE_TAG = re.compile(r"<HideCustomAction\b([^>]*?)/?>")
ATTR = re.compile(r'([\w:]+)\s*=\s*"([^"]*)"')


def find_region(text: str, kind: str):
    """Returns (open_match, close_index) of the RibbonDiffXml to edit, or None."""
    if kind == "application":
        spans = [(m.start(), m.end()) for m in re.finditer(r"<Entities\b[^/>]*>.*?</Entities>",
                                                           text, re.S)]
        match = next((m for m in RIBBON_OPEN.finditer(text)
                      if not any(a <= m.start() < b for a, b in spans)), None)
    else:
        match = RIBBON_OPEN.search(text)
    if not match:
        return None
    if match.group(1) == "/":
        return match, None
    close = text.find("</RibbonDiffXml>", match.end())
    return (match, close) if close >= 0 else None


def expand_self_closing(text: str, kind: str) -> str:
    region = find_region(text, kind)
    if region and region[1] is None:
        m = region[0]
        nl = "\r\n" if "\r\n" in text else "\n"
        opened = m.group(0)[:-2].rstrip() + ">"
        text = text[:m.start()] + opened + nl + "</RibbonDiffXml>" + text[m.end():]
    return text


def read_hides(text: str, kind: str) -> list[tuple[str, str]]:
    """[(HideActionId, Location)] inside the ribbon region."""
    region = find_region(text, kind)
    if not region or region[1] is None:
        return []
    m, close = region
    out = []
    for tag in HIDE_TAG.finditer(text, m.end(), close):
        attrs = {k: html.unescape(v) for k, v in ATTR.findall(tag.group(1))}
        out.append((attrs.get("HideActionId", ""), attrs.get("Location", "")))
    return out


def line_indent(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    before = text[start:pos]
    return before if before.strip() == "" else ""


def insert_hides(text: str, kind: str, entries: list[tuple[str, str]]) -> str:
    if not entries:
        return text
    nl = "\r\n" if "\r\n" in text else "\n"
    text = expand_self_closing(text, kind)
    m, close = find_region(text, kind)
    region = text[m.end():close]

    def lines(indent):
        return "".join(f"{indent}<HideCustomAction HideActionId={quoteattr(h)} "
                       f"Location={quoteattr(loc)} />{nl}" for h, loc in entries)

    sc = re.search(r"<CustomActions\s*/>", region)
    if sc:
        ind = line_indent(region, sc.start())
        new = f"<CustomActions>{nl}{lines(ind + '  ')}{ind}</CustomActions>"
        region = region[:sc.start()] + new + region[sc.end():]
    else:
        end_tag = region.find("</CustomActions>")
        if end_tag >= 0:
            ind = line_indent(region, end_tag)
            if ind:   # closing tag is on its own line: insert above it
                line_start = end_tag - len(ind)
                region = region[:line_start] + lines(ind + "  ") + region[line_start:]
            else:     # closing tag shares a line with other content
                region = region[:end_tag] + nl + lines("    ") + region[end_tag:]
        else:
            ind = "  "
            region = (f"{nl}{ind}<CustomActions>{nl}{lines(ind + '  ')}{ind}</CustomActions>"
                      + region)
    return text[:m.end()] + region + text[close:]


def remove_hides(text: str, kind: str, hide_ids: list[str]) -> str:
    if not hide_ids:
        return text
    m, close = find_region(text, kind)
    region = text[m.end():close]
    for hid in hide_ids:
        pat = re.compile(r'[ \t]*<HideCustomAction\b[^>]*\bHideActionId="'
                         + re.escape(escape(hid, {'"': "&quot;"})) + r'"[^>]*/>[ \t]*(\r?\n)?')
        region = pat.sub("", region, count=1)
    region = re.sub(r"<CustomActions>\s*</CustomActions>", "<CustomActions />", region)
    return text[:m.end()] + region + text[close:]


def find_table_folder(solution_dir: Path, table: str, schema_name: str | None):
    ents = solution_dir / "Entities"
    if not ents.is_dir():
        return None
    names = {table.lower(), (schema_name or "").lower()} - {""}
    return next((d for d in ents.iterdir() if d.is_dir() and d.name.lower() in names), None)


class FileState:
    def __init__(self, solution_dir: Path, path: Path, kind: str):
        self.path = path
        self.rel = path.relative_to(solution_dir).as_posix()
        self.kind = kind
        self.existed = path.exists()
        self.original = path.read_bytes() if self.existed else None
        if self.existed:
            self.bom, self.text = decode(self.original)
        else:
            self.bom, self.text = False, EMPTY_RIBBON_DIFF
        try:
            ET.fromstring(self.text.encode("utf-8"))
        except ET.ParseError as e:
            raise EngineError(f"{self.rel} is not valid XML ({e})")
        if find_region(self.text, kind) is None:
            if kind == "application":
                raise EngineError("the Application Ribbon is not in this solution - add it "
                                  "(classic solution explorer: Client Extensions > Application "
                                  "Ribbons), then export and unpack again")
            raise EngineError(f"{self.rel} has no <RibbonDiffXml> element")
        self.hides = read_hides(self.text, kind)
        self.to_add: list[tuple[str, str]] = []
        self.to_remove: list[str] = []

    def hidden_by(self, control_id: str):
        return next((h for h, loc in self.hides if loc == control_id), None)

    def proposed_text(self) -> str:
        text = remove_hides(self.text, self.kind, self.to_remove)
        return insert_hides(text, self.kind, self.to_add)


def file_for(solution_dir: Path, rec: dict, catalog: Catalog, cache: dict) -> FileState:
    if rec["scope"] == "application":
        path, kind = solution_dir / "Other" / "Customizations.xml", "application"
        if not path.exists():
            raise EngineError("Other/Customizations.xml not found - is this an unpacked "
                              "solution folder? (pac solution unpack)")
    else:
        meta = catalog.sources.get(rec["table"], {})
        folder = find_table_folder(solution_dir, rec["table"], meta.get("schema_name"))
        if folder is None:
            raise EngineError(f"table '{rec['table']}' is not in the solution - add it "
                              "(no subcomponents needed), then export and unpack again")
        path, kind = folder / "RibbonDiff.xml", "table"
    key = str(path)
    if key not in cache:
        cache[key] = FileState(solution_dir, path, kind)
    return cache[key]


# ================================================================== preview

def related_notes(rec: dict, catalog: Catalog) -> list[str]:
    notes = []
    same_scope = [r for r in catalog.records
                  if r["scope"] == rec["scope"] and r["table"] == rec["table"]]
    others_same_control = [r for r in same_scope if r["control_id"] == rec["control_id"]
                           and r["record_key"] != rec["record_key"]]
    if others_same_control:
        locs = sorted({r["location_name"] for r in others_same_control})
        notes.append("The same control also appears in: " + ", ".join(locs)
                     + " - it is hidden there too.")
    if rec["command_id"]:
        shared = [r for r in same_scope if r["command_id"] == rec["command_id"]
                  and r["control_id"] != rec["control_id"]]
        if shared:
            names = sorted({f"{r.get('label') or r['label_hint'] or r['control_id']} "
                            f"[{r['location_name']}]" for r in shared})
            more = f" (+{len(names) - 4} more)" if len(names) > 4 else ""
            notes.append(f"{len(shared)} other control(s) run the same command "
                         f"({rec['command_id']}) and stay visible: "
                         + ", ".join(names[:4]) + more)
    children = [r for r in same_scope if r["parent_control_id"] == rec["control_id"]]
    if children:
        notes.append(f"This is a menu with {len(children)} item(s); they are hidden with it.")
    if rec["scope"] == "table" and rec.get("defined_in") == "application ribbon":
        notes.append(f"Added by the Application Ribbon - this hides it on '{rec['table']}' only.")
    if rec["scope"] == "application":
        notes.append("Application-level - hides this button on EVERY table.")
    return notes


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "plan").lower()).strip("-")[:40] or "plan"


def cmd_preview(args):
    catalog = Catalog(args.catalog)
    plan_path = Path(args.plan)
    plan = load_json(plan_path)
    solution_dir = Path(args.solution).resolve()
    if not (solution_dir / "Other" / "Solution.xml").exists():
        die(f"{solution_dir} does not look like an unpacked solution (Other/Solution.xml missing)")
    prefix = args.prefix

    global_warnings = []
    if plan.get("environment_name") and plan["environment_name"] != catalog.env:
        die(f"plan is for '{plan['environment_name']}' but the catalogue is for '{catalog.env}'")
    age = catalog.age_days()
    if age is not None and age > STALE_DAYS:
        global_warnings.append(f"Catalogue is {age:.0f} days old - consider rebuilding it.")

    files: dict[str, FileState] = {}
    entries, targets = [], {"hide": set(), "unhide": set()}

    for action in ("hide", "unhide"):
        for raw in plan.get(action, []) or []:
            e = {"action": action, "input": raw, "status": "", "errors": [], "warnings": [],
                 "notes": [], "reason": raw.get("reason", "") if isinstance(raw, dict) else ""}
            entries.append(e)
            rec, err = catalog.resolve(raw)
            if err and action == "unhide":
                # Hidden buttons can be missing from a catalogue rebuilt after hiding.
                rec = saved_id_record(raw)
                if rec:
                    err = None
                    e["notes"].append("Not in the current catalogue (it is probably hidden) - "
                                      "unhiding by its saved ID.")
            if err:
                e["errors"].append(err)
                e["status"] = "BLOCKED"
                continue
            e["record"] = rec
            target = hide_target(rec)
            other = "unhide" if action == "hide" else "hide"
            if target in targets[action]:
                e["status"] = "skip"
                e["notes"].append("Duplicate of an earlier line in the plan.")
                continue
            if target in targets[other]:
                e["status"] = "BLOCKED"
                e["errors"].append(f"Same control is also in the '{other}' list.")
                continue
            targets[action].add(target)

            if action == "hide":   # unhiding only removes our own line, no checks needed
                found = catalog.control_in_source(rec)
                if found is None:
                    e["errors"].append(f"Source ribbon XML {rec['source_xml_file']} is missing - "
                                       "cannot verify the control. Rebuild the catalogue.")
                elif not found:
                    e["errors"].append("Control not found in the source ribbon XML - catalogue "
                                       "may be out of date.")
                src = catalog.sources.get(rec["source"], {})
                if src.get("is_customizable") is False:
                    e["errors"].append(f"Table '{rec['table']}' is not customizable.")
                elif src.get("is_customizable") is None and rec["scope"] == "table":
                    e["warnings"].append("Could not confirm the table is customizable.")
                if src.get("status") not in (None, "ok"):
                    e["warnings"].append("Last catalogue refresh of this table failed - "
                                         "using older data.")
                if not rec.get("uci_relevant"):
                    e["warnings"].append("This location is not normally shown in model-driven "
                                         "apps; hiding it may have no visible effect.")
                elif rec.get("modern") == "hidden":
                    e["warnings"].append("Never shown in model-driven apps ("
                                         f"{rec.get('modern_reason', '')}) - hiding it has no "
                                         "visible effect.")
            try:
                fs = file_for(solution_dir, rec, catalog, files)
            except EngineError as ex:
                e["errors"].append(str(ex))
                fs = None
            if e["errors"]:
                e["status"] = "BLOCKED"
                continue

            e["file"] = fs.rel
            existing = fs.hidden_by(rec["control_id"])
            if action == "hide":
                hide_id = make_hide_id(prefix, rec)
                if existing and existing.startswith(prefix + "."):
                    e["status"] = "skip"
                    e["notes"].append("Already hidden by DemoHider.")
                elif existing:
                    e["status"] = "skip"
                    e["warnings"].append(f"Already hidden by another customization "
                                         f"(HideActionId={existing}) - left untouched.")
                else:
                    fs.to_add.append((hide_id, rec["control_id"]))
                    fs.hides.append((hide_id, rec["control_id"]))
                    e["status"] = "hide"
                    e["hide_action_id"] = hide_id
                    e["notes"] += related_notes(rec, catalog)
            else:
                if existing and existing.startswith(prefix + "."):
                    fs.to_remove.append(existing)
                    e["status"] = "unhide"
                    e["hide_action_id"] = existing
                elif existing:
                    e["status"] = "BLOCKED"
                    e["errors"].append(f"Hidden by another customization (HideActionId={existing}); "
                                       "DemoHider only removes hides it created.")
                else:
                    e["status"] = "skip"
                    e["notes"].append("Not currently hidden in this solution.")

    # ---- write the run folder
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slug(plan.get("preset") or plan_path.stem)
    run_dir = Path(args.runs) / run_id
    (run_dir / "original").mkdir(parents=True, exist_ok=True)
    (run_dir / "proposed").mkdir(parents=True, exist_ok=True)
    shutil.copy2(plan_path, run_dir / "plan.json")

    file_records, diffs = [], []
    for fs in files.values():
        if not fs.to_add and not fs.to_remove:
            continue
        proposed = encode(fs.bom, fs.proposed_text())
        if fs.existed:
            dest = run_dir / "original" / fs.rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(fs.original)
        dest = run_dir / "proposed" / fs.rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(proposed)
        try:
            ET.fromstring(proposed)
        except ET.ParseError as ex:
            die(f"internal check failed - proposed {fs.rel} is not valid XML ({ex}). Nothing changed.")
        before = fs.text if fs.existed else ""
        diffs.append("".join(difflib.unified_diff(
            before.splitlines(keepends=True), proposed.decode("utf-8-sig").splitlines(keepends=True),
            fromfile=f"original/{fs.rel}" if fs.existed else "/dev/null",
            tofile=f"proposed/{fs.rel}")))
        file_records.append({"rel": fs.rel, "existed_before": fs.existed,
                             "original_sha": sha(fs.original), "proposed_sha": sha(proposed),
                             "added": len(fs.to_add), "removed": len(fs.to_remove)})

    (run_dir / "changes.diff").write_text("\n".join(diffs), encoding="utf-8")

    def slim(e):
        r = e.get("record") or {}
        if not r:   # not found in catalogue: show what the plan asked for
            raw = e["input"] if isinstance(e["input"], dict) else {"record_key": e["input"]}
            r = {"label": raw.get("record_key") or raw.get("control_id") or str(raw)[:80],
                 "table": raw.get("table")}
        return {k: v for k, v in {
            "action": e["action"], "status": e["status"], "reason": e["reason"],
            "record_key": r.get("record_key"), "scope": r.get("scope"), "table": r.get("table"),
            "command_bar_location": r.get("command_bar_location"),
            "control_id": r.get("control_id"), "command_id": r.get("command_id"),
            "label": ((e["input"].get("label") if isinstance(e["input"], dict) else None)
                      or r.get("label") or r.get("label_hint")),
            "source_element": r.get("source_element"), "file": e.get("file"),
            "hide_action_id": e.get("hide_action_id"), "input": None if r else e["input"],
            "errors": e["errors"], "warnings": e["warnings"], "notes": e["notes"]}.items()
            if v not in (None, [], "")}

    items = [slim(e) for e in entries]
    blocked = sum(1 for e in entries if e["status"] == "BLOCKED")
    changes = sum(1 for e in entries if e["status"] in ("hide", "unhide"))
    run = {"run_id": run_id, "created_utc": now_utc(), "status": "previewed",
           "environment_name": catalog.env, "catalog": str(Path(args.catalog).resolve()),
           "catalog_updated_utc": catalog.data.get("updated_utc"),
           "solution_dir": str(solution_dir), "plan_file": str(plan_path.resolve()),
           "preset": plan.get("preset", ""), "prefix": prefix,
           "global_warnings": global_warnings, "change_count": changes,
           "blocked_count": blocked, "files": file_records, "items": items}
    save_json(run_dir / "run.json", run)
    write_preview_md(run_dir, run, diffs)

    # ---- console summary
    print(f"\nPreview  : {run_dir / 'preview.md'}")
    print(f"Plan     : {plan.get('preset') or plan_path.name}   Environment: {catalog.env}")
    for w in global_warnings:
        print(f"WARNING  : {w}")
    counts = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    print("Result   : " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    for e in entries:
        if e["status"] == "BLOCKED":
            what = describe(e["record"]) if e.get("record") else str(e["input"])[:80]
            print(f"  BLOCKED  {what}\n           " + "\n           ".join(e["errors"]))
    if os.environ.get("DEMOHIDER_RUNNER"):
        return   # demohider.ps1 shows its own summary and next steps
    if changes == 0:
        print("\nNothing to change.")
    elif blocked:
        print(f"\n{blocked} item(s) blocked. Fix the plan and preview again, or apply the rest with:")
        print(f"  python scripts\\hide_engine.py apply --run {run_dir} --allow-partial")
    else:
        print("\nReview preview.md, then apply with:")
        print(f"  python scripts\\hide_engine.py apply --run {run_dir}")


PLACE_NAMES = {"HomepageGrid": "Main grid", "Form": "Form", "SubGrid": "Subgrid"}


def place_name(loc):
    """Name users see for a command bar location (HomepageGrid -> Main grid)."""
    return PLACE_NAMES.get(loc or "", loc or "")


def write_preview_md(run_dir: Path, run: dict, diffs: list[str]) -> None:
    L = [f"# DemoHider preview - {run['preset'] or run['run_id']}", "",
         f"- Environment: **{run['environment_name']}**",
         f"- Catalogue updated: {run['catalog_updated_utc']}",
         f"- Solution folder: `{run['solution_dir']}`",
         f"- Changes: **{run['change_count']}**   Blocked: **{run['blocked_count']}**", ""]
    for w in run["global_warnings"]:
        L.append(f"> WARNING: {w}")
    groups = [("hide", "Will be hidden"), ("unhide", "Will be shown again"),
              ("BLOCKED", "Blocked (will NOT be applied)"), ("skip", "Skipped")]
    for status, title in groups:
        rows = [i for i in run["items"] if i["status"] == status]
        if not rows:
            continue
        L += [f"## {title} ({len(rows)})", "",
              "| Button | Where | Location | Control ID | Command |",
              "|---|---|---|---|---|"]
        for i in rows:
            where = "ALL tables" if i.get("scope") == "application" else (i.get("table") or "?")
            L.append(f"| {i.get('label', str(i.get('input', ''))[:60])} | {where} | "
                     f"{place_name(i.get('command_bar_location'))} | `{i.get('control_id', '')}` | "
                     f"`{i.get('command_id', '')}` |")
        L.append("")
        details = [i for i in rows if i.get("errors") or i.get("warnings") or i.get("notes")]
        for i in details:
            L.append(f"- **{i.get('label', 'item')}**")
            for kind in ("errors", "warnings", "notes"):
                for msg in i.get(kind, []):
                    L.append(f"  - {kind[:-1].upper()}: {msg}")
        if details:
            L.append("")
    L += ["## File changes", ""]
    for f in run["files"]:
        L.append(f"- `{f['rel']}`: +{f['added']} / -{f['removed']}"
                 + ("" if f["existed_before"] else " (new file)"))
    L += ["", "```diff", *("\n".join(diffs).splitlines() or ["(no changes)"]), "```", ""]
    (run_dir / "preview.md").write_text("\n".join(L), encoding="utf-8")


# ============================================================ apply / rollback

def manifest_path(args, env: str) -> Path:
    return Path(args.manifests) / f"{env}.json"


def cmd_apply(args):
    run_dir = Path(args.run)
    run = load_json(run_dir / "run.json")
    if run["status"] != "previewed":
        die(f"this run is '{run['status']}' - only a previewed run can be applied")
    if run["change_count"] == 0:
        die("this run has nothing to change")
    if run["blocked_count"] and not args.allow_partial:
        die(f"{run['blocked_count']} item(s) are blocked. Fix the plan and preview again, "
            "or add --allow-partial to apply only the valid items.")
    sol = Path(run["solution_dir"])

    for f in run["files"]:
        p = sol / f["rel"]
        current = p.read_bytes() if p.exists() else None
        if sha(current) != f["original_sha"]:
            die(f"{f['rel']} changed since the preview. Run preview again.")

    mpath = manifest_path(args, run["environment_name"])
    manifest = load_json(mpath) if mpath.exists() else {
        "environment_name": run["environment_name"], "hidden": {}}
    save_json(run_dir / "manifest_before.json", manifest)

    for f in run["files"]:
        p = sol / f["rel"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((run_dir / "proposed" / f["rel"]).read_bytes())

    for i in run["items"]:
        key = f"{i.get('scope')}|{i.get('table') or '*'}|{i.get('control_id')}"
        if i["status"] == "hide":
            manifest["hidden"][key] = {
                "label": i.get("label"), "scope": i["scope"], "table": i.get("table", ""),
                "control_id": i["control_id"], "command_id": i.get("command_id", ""),
                "command_bar_location": i.get("command_bar_location", ""),
                "hide_action_id": i["hide_action_id"], "file": i["file"],
                "reason": i.get("reason", ""), "run_id": run["run_id"], "applied_utc": now_utc()}
        elif i["status"] == "unhide":
            manifest["hidden"].pop(key, None)
    manifest["solution_dir"] = run["solution_dir"]
    manifest["updated_utc"] = now_utc()
    save_json(mpath, manifest)

    run.update(status="applied", applied_utc=now_utc(),
               manifest_after_sha=sha(mpath.read_bytes()), manifest_file=str(mpath.resolve()))
    save_json(run_dir / "run.json", run)

    print(f"Applied {run['change_count']} change(s) to {sol}")
    print(f"Manifest: {mpath}")
    if os.environ.get("DEMOHIDER_RUNNER"):
        return
    print("\nNext:")
    print(f"  python scripts\\hide_engine.py validate --catalog \"{run['catalog']}\" --solution \"{sol}\"")
    print(f"  pac solution pack --zipfile <zip> --folder \"{sol}\" --packagetype Unmanaged")
    print("  pac solution import --path <zip> --publish-changes")


def cmd_rollback(args):
    run_dir = Path(args.run)
    run = load_json(run_dir / "run.json")
    if run["status"] != "applied":
        die(f"this run is '{run['status']}' - only an applied run can be rolled back")
    sol = Path(run["solution_dir"])

    for f in run["files"]:
        p = sol / f["rel"]
        current = p.read_bytes() if p.exists() else None
        if sha(current) != f["proposed_sha"] and not args.force:
            die(f"{f['rel']} was changed after this run was applied. Rolling back would discard "
                "those changes. Use --force if you are sure.")

    for f in run["files"]:
        p = sol / f["rel"]
        if f["existed_before"]:
            p.write_bytes((run_dir / "original" / f["rel"]).read_bytes())
        elif p.exists():
            p.unlink()   # file was created by this run

    mpath = Path(run["manifest_file"])
    if mpath.exists() and sha(mpath.read_bytes()) != run.get("manifest_after_sha") and not args.force:
        print("WARNING: the manifest changed after this run (another run was applied). "
              "Files were restored but the manifest was left as is - run validate to check.")
    else:
        save_json(mpath, load_json(run_dir / "manifest_before.json"))

    run.update(status="rolled_back", rolled_back_utc=now_utc())
    save_json(run_dir / "run.json", run)
    print(f"Rolled back {len(run['files'])} file(s) in {sol} to their original content.")
    print("To undo it in Dataverse, pack and import the solution again:")
    print(f"  pac solution pack --zipfile <zip> --folder \"{sol}\" --packagetype Unmanaged")
    print("  pac solution import --path <zip> --publish-changes")


# ================================================================== validate

def cmd_validate(args):
    sol = Path(args.solution).resolve()
    catalog = Catalog(args.catalog) if args.catalog else None
    prefix = args.prefix
    errors, warnings, info = [], [], []

    if not (sol / "Other" / "Solution.xml").exists():
        die(f"{sol} does not look like an unpacked solution (Other/Solution.xml missing)")

    targets = []
    ents = sol / "Entities"
    if ents.is_dir():
        for d in sorted(ents.iterdir()):
            if d.is_dir():
                info.append(f"Table in solution: {d.name}")
                if (d / "RibbonDiff.xml").exists():
                    targets.append((d / "RibbonDiff.xml", "table", d.name.lower()))
    cust = sol / "Other" / "Customizations.xml"
    if cust.exists():
        targets.append((cust, "application", ""))

    ours_found = {}
    for path, kind, table in targets:
        rel = path.relative_to(sol).as_posix()
        raw = path.read_bytes()
        _, text = decode(raw)
        try:
            ET.fromstring(text.encode("utf-8"))
        except ET.ParseError as e:
            errors.append(f"{rel}: not valid XML ({e})")
            continue
        if kind == "application" and find_region(text, kind) is None:
            continue
        hides = read_hides(text, kind)
        ids = [h for h, _ in hides]
        for dup in sorted({h for h in ids if ids.count(h) > 1}):
            errors.append(f"{rel}: duplicate HideActionId {dup}")
        locs = [loc for _, loc in hides]
        for dup in sorted({loc for loc in locs if locs.count(loc) > 1}):
            warnings.append(f"{rel}: control hidden more than once: {dup}")
        scope = "application" if kind == "application" else "table"
        if catalog and catalog.sources:
            if scope == "table":
                schema_map = {(m.get("schema_name") or "").lower(): n
                              for n, m in catalog.sources.items()}
                table = schema_map.get(table, table)
            known = {r["control_id"] for r in catalog.records
                     if r["scope"] == scope and r["table"] == table}
        else:
            known = None
        for h, loc in hides:
            if not h.startswith(prefix + "."):
                info.append(f"{rel}: hide not made by DemoHider (left alone): {loc}")
                continue
            ours_found[f"{scope}|{table or '*'}|{loc}"] = rel
            if known is not None and loc not in known:
                if known:
                    warnings.append(f"{rel}: hidden control not in the catalogue "
                                    f"(stale catalogue or wrong environment?): {loc}")
                else:
                    warnings.append(f"{rel}: table '{table}' is not in the catalogue - cannot "
                                    f"verify {loc}")

    if catalog:
        mpath = manifest_path(args, catalog.env)
        if mpath.exists():
            manifest = load_json(mpath)
            if manifest.get("solution_dir") and Path(manifest["solution_dir"]).resolve() != sol:
                info.append(f"Manifest belongs to another solution folder: {manifest['solution_dir']}")
            else:
                for key in sorted(set(manifest.get("hidden", {})) - set(ours_found)):
                    warnings.append(f"In manifest but not in the solution: {key}")
                for key in sorted(set(ours_found) - set(manifest.get("hidden", {}))):
                    warnings.append(f"In the solution but not in the manifest: {key}")

    print(f"Validating {sol}\n")
    print(f"DemoHider hides found: {len(ours_found)}")
    for msg in info:
        print(f"  info     {msg}")
    for msg in warnings:
        print(f"  WARNING  {msg}")
    for msg in errors:
        print(f"  ERROR    {msg}")
    if errors:
        print(f"\nFAILED with {len(errors)} error(s) - do not import this solution.")
        sys.exit(1)
    print("\nOK - safe to pack and import." + (f" ({len(warnings)} warning(s) to review)"
                                               if warnings else ""))


# ====================================================================== main

def main():
    ap = argparse.ArgumentParser(description="DemoHider customization engine")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX,
                    help=f"HideActionId prefix that marks DemoHider's own hides (default {DEFAULT_PREFIX})")
    ap.add_argument("--runs", default="runs", help="Folder for run records (default: runs)")
    ap.add_argument("--manifests", default="manifests", help="Folder for manifests (default: manifests)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preview", help="Check a plan and show what would change")
    p.add_argument("--catalog", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--solution", required=True, help="Unpacked solution folder")

    p = sub.add_parser("apply", help="Apply a previewed run")
    p.add_argument("--run", required=True)
    p.add_argument("--allow-partial", action="store_true",
                   help="Apply valid items even if some were blocked")

    p = sub.add_parser("rollback", help="Restore the files changed by a run")
    p.add_argument("--run", required=True)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("validate", help="Check a solution folder before import")
    p.add_argument("--solution", required=True)
    p.add_argument("--catalog")

    args = ap.parse_args()
    {"preview": cmd_preview, "apply": cmd_apply, "rollback": cmd_rollback,
     "validate": cmd_validate}[args.cmd](args)


if __name__ == "__main__":
    main()
