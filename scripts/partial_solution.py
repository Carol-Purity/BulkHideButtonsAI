"""
DemoHider - a smaller copy of the solution with only the changed tables
=======================================================================
Importing the whole DemoHider solution can take 15-25 minutes when it holds
more than bare tables (business rules, forms, views ...). A run only changes
the command bars of a few tables, so we pack and import just those.

build(solution_dir, out_dir, tables) makes a copy of the unpacked solution:

  Entities\\<Table>\\          only the tables in `tables` (everything inside kept)
  Other\\Solution.xml         RootComponents trimmed to those tables
                             (+ their own forms/views/columns, + the Application Ribbon)
  Other\\Customizations.xml   Entities trimmed; the Application Ribbon (RibbonDiffXml)
                             and Languages kept; all other sections emptied
  everything else            left out (Workflows, WebResources, Relationships ...)

Importing an UNMANAGED solution never removes things that are not in it, so
the other tables, business rules etc. stay as they are in Dynamics.

  python scripts\\partial_solution.py --solution solutions\\DemoHider --out tmp\\part --tables contact,account
"""

import argparse
import re
import shutil
import sys
import xml.parsers.expat as expat
from pathlib import Path

KEEP_ALWAYS_TYPES = {"50"}          # 50 = Application Ribbon
TABLE_TYPE = "1"                    # 1 = table (entity)
CUSTOMIZATION_KEEP = {"entities", "ribbondiffxml", "languages"}


class PartialError(Exception):
    pass


def _norm_id(s):
    return re.sub(r"[{}]", "", (s or "")).strip().lower()


def entity_folders(solution_dir):
    ents = Path(solution_dir) / "Entities"
    return {d.name.lower(): d for d in ents.iterdir() if d.is_dir()} if ents.is_dir() else {}


def _spans(data, max_depth=2):
    """Byte spans of elements at depth 1..max_depth: [(depth, tag, start, end, parent_index)].
    Only what we cut out changes - the rest of the file stays byte-for-byte the same."""
    out, stack = [], []
    p = expat.ParserCreate()

    def tag_end(i):
        """Index just after the '>' that closes the tag starting at i (quotes respected)."""
        q = None
        while i < len(data):
            c = data[i:i + 1]
            if q:
                if c == q:
                    q = None
            elif c in (b'"', b"'"):
                q = c
            elif c == b">":
                return i + 1
            i += 1
        raise PartialError("could not read the XML (unclosed tag)")

    def start(tag, attrs):
        depth = len(stack)
        parent = stack[-1][1] if stack else None
        idx, selfclosing = None, False
        a = p.CurrentByteIndex
        e = tag_end(a)
        selfclosing = data[e - 2:e] == b"/>"
        if 1 <= depth <= max_depth:
            out.append([depth, tag, a, e if selfclosing else None, parent])
            idx = len(out) - 1
        stack.append((tag, idx, selfclosing))

    def end(tag):
        _, idx, selfclosing = stack.pop()
        if idx is not None and not selfclosing:
            out[idx][3] = tag_end(p.CurrentByteIndex)

    p.StartElementHandler, p.EndElementHandler = start, end
    try:
        p.Parse(data, True)
    except expat.ExpatError as e:
        raise PartialError(f"could not read the XML ({e})")
    return out


def _cut(data, edits):
    """edits: [(start, end, replacement_bytes)] - applied from the end backwards."""
    for a, b, rep in sorted(edits, reverse=True):
        data = data[:a] + rep + data[b:]
    return data


def _text_of(data, span, child):
    m = re.search(rb"<" + child.encode() + rb"\b[^>]*>([^<]*)<", data[span[2]:span[3]])
    return m.group(1).decode("utf-8", "ignore").strip().lower() if m else ""


def build(solution_dir, out_dir, tables):
    """Create the trimmed copy. Returns a dict describing what was kept.
    Raises PartialError if it can't be done safely (the caller then sends the full solution)."""
    src, out = Path(solution_dir), Path(out_dir)
    tables = {t.lower() for t in tables if t}
    if not tables:
        raise PartialError("no tables to send")
    sol_xml, cust_xml = src / "Other" / "Solution.xml", src / "Other" / "Customizations.xml"
    if not sol_xml.is_file() or not cust_xml.is_file():
        raise PartialError("Other\\Solution.xml or Other\\Customizations.xml is missing")

    folders = entity_folders(src)
    missing = sorted(t for t in tables if t not in folders)
    if missing:
        raise PartialError(f"table folder(s) not in the solution: {', '.join(missing)}")

    if out.exists():
        shutil.rmtree(out)
    (out / "Other").mkdir(parents=True)
    (out / "Entities").mkdir()
    kept_text = []
    for t in sorted(tables):
        dst = out / "Entities" / folders[t].name
        shutil.copytree(folders[t], dst)
        for f in dst.rglob("*"):
            kept_text.append(f.name.lower())
            if f.is_file() and f.suffix.lower() in (".xml", ".json", ".xaml"):
                try:
                    kept_text.append(f.read_text(encoding="utf-8-sig", errors="ignore").lower())
                except OSError:
                    pass
    blob = "\n".join(kept_text)

    # ---- Solution.xml: keep only root components that belong to the kept tables
    data = sol_xml.read_bytes()
    comps = list(re.finditer(rb"<RootComponent\b[^>]*?/>", data))
    if not comps or len(comps) != data.count(b"<RootComponent "):
        raise PartialError("unexpected RootComponents layout in Solution.xml")
    kept, dropped, edits = [], [], []
    for m in comps:
        attrs = dict((k.decode(), v.decode()) for k, v in re.findall(rb'(\w+)="([^"]*)"', m.group(0)))
        typ, name, rid = attrs.get("type", ""), attrs.get("schemaName", "").lower(), _norm_id(attrs.get("id"))
        if typ == TABLE_TYPE:
            keep = name in tables
        elif typ in KEEP_ALWAYS_TYPES:
            keep = True
        elif rid:
            keep = rid in blob          # e.g. a form or view that lives in a kept table folder
        else:
            keep = False
        (kept if keep else dropped).append(f"{typ}:{name or rid}")
        if not keep:
            # remove the element and the whitespace/line before it
            a = m.start()
            while a > 0 and data[a - 1:a] in (b" ", b"\t"):
                a -= 1
            if a > 0 and data[a - 1:a] == b"\n":
                a -= 1
                if a > 0 and data[a - 1:a] == b"\r":
                    a -= 1
            edits.append((a, m.end(), b""))
    if not any(k.startswith(TABLE_TYPE + ":") for k in kept):
        raise PartialError("none of the changed tables is a root component in Solution.xml")
    (out / "Other" / "Solution.xml").write_bytes(_cut(data, edits))

    # ---- Customizations.xml: keep the tables, the Application Ribbon and Languages;
    #      empty every other section (they point to files we leave out)
    data = cust_xml.read_bytes()
    spans = _spans(data)
    edits, emptied = [], []
    for i, sp in enumerate(spans):
        depth, tag, a, b, _ = sp
        if depth != 1:
            continue
        low = tag.rsplit(":", 1)[-1].lower()
        if low == "entities":
            for ch in spans:
                if ch[0] == 2 and ch[4] == i:
                    ename = _text_of(data, ch, "Name")
                    if ename and ename not in tables:
                        edits.append((ch[2], ch[3], b""))
        elif low not in CUSTOMIZATION_KEEP:
            inner = data[a:b]
            if inner.rstrip().endswith(b"/>") and inner.count(b"<") == 1:
                continue        # already empty
            if re.fullmatch(rb"<[^>]*>\s*</[^>]*>", inner):
                continue        # already empty
            edits.append((a, b, b"<" + tag.encode() + b" />"))
            emptied.append(tag)
    (out / "Other" / "Customizations.xml").write_bytes(_cut(data, edits))

    return {"tables": sorted(folders[t].name for t in tables),
            "root_components_kept": len(kept), "root_components_left_out": len(dropped),
            "sections_emptied": emptied,
            "left_out": sorted(p.name for p in src.iterdir()
                               if p.name not in ("Entities", "Other"))}


def main():
    ap = argparse.ArgumentParser(description="Copy of the solution with only some tables")
    ap.add_argument("--solution", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tables", required=True, help="comma-separated logical names")
    a = ap.parse_args()
    try:
        info = build(a.solution, a.out, a.tables.split(","))
    except PartialError as e:
        print(f"Could not make the smaller copy: {e}")
        sys.exit(1)
    print(f"Tables: {', '.join(info['tables'])}")
    print(f"Root components kept: {info['root_components_kept']}, "
          f"left out: {info['root_components_left_out']}")
    if info["left_out"]:
        print(f"Folders left out: {', '.join(info['left_out'])}")


if __name__ == "__main__":
    main()
