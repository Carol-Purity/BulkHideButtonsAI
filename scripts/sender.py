"""
DemoHider - sending a run to Dynamics
=====================================
Runs the steps after you have confirmed a change, in this order:

  (apply)   update the solution files            hide_engine.py apply
  (undo)    or put them back                     hide_engine.py rollback
            safety check                         hide_engine.py validate
  Round 1   pack -> import -> publish            pac + publish_tables.py
  Round 2   the same three again, same run       (makes the change show in the app)

Only the tables the run changed are packed and imported (a trimmed copy of the
solution in runs\\<run>\\send-solution) - much faster than the whole solution.
Use --full to send the whole solution instead. If the trimmed copy can't be
made or packed, the whole solution is sent automatically.

Used by the browser page (flow_server.py) and by demohider.ps1 -NoBrowser.
You normally never run it yourself, but you can:

  python scripts\\sender.py send    --run runs\\<run> --catalog <catalog.json> --solution solutions\\DemoHider
  python scripts\\sender.py send    --run runs\\<run> ... --apply      (apply a previewed run first)
  python scripts\\sender.py send    --run runs\\<run> ... --undo       (undo an applied run first)
  python scripts\\sender.py publish --run runs\\<run>

Exit codes: 0 = sent and published, 2 = NOT LIVE YET (files changed, sending failed),
            1 = stopped before anything was sent, 5 = round 1 sent, round 2 failed
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import partial_solution  # noqa: E402

OK, NOT_LIVE, STOPPED, ROUND2_FAILED = 0, 2, 1, 5


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Job:
    """Progress of one sending job. Read by the web page, printed to the terminal."""

    def __init__(self, title, echo=True):
        self.title = title
        self.steps = []          # {"id","label","status": wait|run|ok|fail|skip,"detail"}
        self.log = []            # output lines
        self.state = "running"   # running | ok | warning | notlive | failed
        self.message = ""
        self.code = None
        self.started = now_utc()
        self.echo = echo
        self.lock = threading.Lock()

    def add_step(self, sid, label):
        self.steps.append({"id": sid, "label": label, "status": "wait", "detail": "",
                           "t0": None, "t1": None})

    def step(self, sid):
        return next(s for s in self.steps if s["id"] == sid)

    def set(self, sid, status, detail=""):
        with self.lock:
            s = self.step(sid)
            s["status"], s["detail"] = status, detail
            if status == "run":
                s["t0"] = time.time()
            elif s["t0"] and not s["t1"]:
                s["t1"] = time.time()
        if status == "run" and self.echo:
            print(f"\n==> {s['label']}", flush=True)

    def write(self, line):
        line = line.rstrip()
        if not line.strip():
            return
        with self.lock:
            self.log.append(line)
            if len(self.log) > 2000:
                del self.log[:500]
        if self.echo:
            print(line, flush=True)

    def finish(self, state, message, code):
        with self.lock:
            self.state, self.message, self.code = state, message, code
            for s in self.steps:
                if s["status"] in ("wait", "run"):
                    s["status"] = "skip"
        if self.echo:
            print(f"\n{message}", flush=True)

    def snapshot(self):
        def took(s):
            if not s["t0"]:
                return ""
            sec = int((s["t1"] or time.time()) - s["t0"])
            return f"{sec // 60} min {sec % 60:02d} s" if sec >= 60 else f"{sec} s"
        with self.lock:
            steps = [{"id": s["id"], "label": s["label"], "status": s["status"],
                      "detail": s["detail"], "took": took(s)} for s in self.steps]
            return {"title": self.title, "steps": steps,
                    "log": self.log[-300:], "state": self.state, "message": self.message,
                    "code": self.code, "started": self.started}


def run_cmd(job, argv):
    """Run a command, stream its output into the job. Returns the exit code."""
    env = dict(os.environ, DEMOHIDER_RUNNER="1", PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                             errors="replace", env=env)
    except OSError as e:
        job.write(f"Could not start {argv[0]}: {e}")
        return 127
    for line in p.stdout:
        job.write(line)
    return p.wait()


def py(script, *args):
    return [sys.executable, "-u", str(SCRIPTS / script), *map(str, args)]


def pac():
    exe = shutil.which("pac")
    if not exe:
        raise FileNotFoundError("The Power Platform CLI ('pac') was not found.")
    return exe


def preview(catalog, plan, solution, echo=True):
    """Run the check (nothing changes). Returns (run_dir or None, output lines)."""
    job = Job("Checking", echo=echo)
    code = run_cmd(job, py("hide_engine.py", "preview", "--catalog", catalog, "--plan", plan,
                           "--solution", solution))
    run_dir = None
    for line in job.log:
        m = re.match(r"Preview\s*:\s*(.+preview\.md)\s*$", line)
        if m:
            run_dir = str(Path(m.group(1).strip()).parent)
    return (run_dir if code == 0 else None), job.log


def read_run(run_dir):
    return json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ jobs

def build_send(job, rounds, apply=False, undo=False):
    if apply:
        job.add_step("apply", "Updating the solution files")
    if undo:
        job.add_step("undo", "Putting the solution files back (undo)")
    if apply or undo:
        job.add_step("validate", "Final safety check")
    for r in range(1, rounds + 1):
        tag = f"Round {r} of {rounds}" if rounds > 1 else "Sending"
        if r > 1:
            job.add_step(f"wait{r}", f"Waiting before round {r}")
        job.add_step(f"pack{r}", f"{tag} - 1/3 Packing the solution")
        job.add_step(f"import{r}", f"{tag} - 2/3 Importing into Dynamics (a few minutes)")
        job.add_step(f"publish{r}", f"{tag} - 3/3 Publishing the changed tables")


def run_tables(run_dir):
    """(tables, uses_application_ribbon) changed by a run."""
    tables, app = set(), False
    for i in read_run(run_dir).get("items", []):
        if i.get("status") not in ("hide", "unhide"):
            continue
        if i.get("scope") == "application":
            app = True
        elif i.get("table"):
            tables.add(i["table"].lower())
    return sorted(tables), app


def choose_folder(job, run_dir, solution, full):
    """The folder to pack: a trimmed copy with only the changed tables, or the full solution."""
    if full:
        job.write("Sending the whole solution (-FullSolution).")
        return solution, "whole solution"
    tables, app = run_tables(run_dir)
    if not tables:
        job.write("This run only changes the Application Ribbon - sending the whole solution.")
        return solution, "whole solution"
    out = Path(run_dir) / "send-solution"
    try:
        info = partial_solution.build(solution, out, tables)
    except partial_solution.PartialError as e:
        job.write(f"Could not make the smaller copy ({e}) - sending the whole solution instead.")
        return solution, "whole solution"
    job.write(f"Sending only the changed table(s): {', '.join(info['tables'])}"
              + (" + Application Ribbon" if app else ""))
    if info["left_out"]:
        job.write(f"  (left out of this import, unchanged in Dynamics: {', '.join(info['left_out'])})")
    return str(out), ", ".join(info["tables"])


def do_send(job, run_dir, catalog, solution, rounds=2, round_wait=60, apply=False, undo=False,
            full=False):
    """Apply/undo (optional), check, then send in `rounds` rounds. Sets job result."""
    run_dir = str(run_dir)
    name = Path(run_dir).name
    if not job.steps:
        build_send(job, rounds, apply, undo)
    job.write(f"Run: {name}")

    if apply:
        job.set("apply", "run")
        if run_cmd(job, py("hide_engine.py", "apply", "--run", run_dir)) != 0:
            job.set("apply", "fail")
            return job.finish("failed", "Updating the files stopped - nothing was sent to "
                              "Dynamics. See the details below.", STOPPED)
        job.set("apply", "ok")
    if undo:
        job.set("undo", "run")
        if run_cmd(job, py("hide_engine.py", "rollback", "--run", run_dir)) != 0:
            job.set("undo", "fail")
            return job.finish("failed", "Undo stopped - nothing was changed. See the details "
                              "below.", STOPPED)
        job.set("undo", "ok")
    if apply or undo:
        job.set("validate", "run")
        if run_cmd(job, py("hide_engine.py", "validate", "--catalog", catalog,
                           "--solution", solution)) != 0:
            job.set("validate", "fail")
            return job.finish("failed", "The safety check failed - nothing was sent to "
                              "Dynamics. Use Undo to reset the files.", STOPPED)
        job.set("validate", "ok")

    try:
        pac_exe = pac()
    except FileNotFoundError as e:
        job.write(str(e))
        return job.finish("notlive", "NOT LIVE YET - pac was not found, so nothing was sent.",
                          NOT_LIVE)

    folder, what = choose_folder(job, run_dir, solution, full)

    for r in range(1, rounds + 1):
        last = r == rounds
        if r > 1:
            job.set(f"wait{r}", "run", f"{round_wait} seconds")
            job.write(f"Round {r - 1} done. Waiting {round_wait} seconds before round {r} "
                      "(makes the change show in the app) ...")
            time.sleep(round_wait)
            job.set(f"wait{r}", "ok")

        def fail(sid, why):
            job.set(sid, "fail")
            if r == 1:
                return job.finish("notlive", f"NOT LIVE YET - {why}. Your files are updated; "
                                  "use Publish again or Send again.", NOT_LIVE)
            return job.finish("warning", f"Round {r} did not complete ({why}). Round 1 was "
                              "sent. If the buttons have not changed in a few minutes, use "
                              "Send again.", ROUND2_FAILED)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        zip_path = str(Path(solution).parent / f"DemoHider_{stamp}.zip")
        job.set(f"pack{r}", "run", what)
        packed = run_cmd(job, [pac_exe, "solution", "pack", "--zipfile", zip_path,
                               "--folder", folder, "--packagetype", "Unmanaged"]) == 0
        if not packed and folder != solution:
            job.write("Packing the smaller copy failed - packing the whole solution instead.")
            folder, what = solution, "whole solution"
            job.set(f"pack{r}", "run", what)
            packed = run_cmd(job, [pac_exe, "solution", "pack", "--zipfile", zip_path,
                                   "--folder", folder, "--packagetype", "Unmanaged"]) == 0
        if not packed:
            if r == 1:
                job.set("pack1", "fail")
                return job.finish("failed", "Packing failed - nothing was sent to Dynamics.",
                                  STOPPED if not (apply or undo) else NOT_LIVE)
            return fail(f"pack{r}", "packing failed")
        job.set(f"pack{r}", "ok")

        job.set(f"import{r}", "run", what)
        if run_cmd(job, [pac_exe, "solution", "import", "--path", zip_path]) != 0:
            return fail(f"import{r}", "the import did not finish")
        job.set(f"import{r}", "ok")

        job.set(f"publish{r}", "run")
        args = ["--run", run_dir, "--round", r]
        if not last:
            args.append("--no-confirm")   # the next round publishes again anyway
        code = run_cmd(job, py("publish_tables.py", *args))
        if code != 0:
            return fail(f"publish{r}", "the connection kept dropping while publishing"
                        if code == 2 else "publishing was rejected")
        job.set(f"publish{r}", "ok")

    done = {1: "", 2: " twice (2 rounds)"}.get(rounds, f" {rounds} times")
    first = "Undone! The buttons are back to how they were before this run." if undo else "Done!"
    return job.finish("ok", f"{first} Imported and published{done}. Wait a minute, then check "
                      "in a NEW InPrivate/Incognito window.", OK)


def do_publish(job, run_dir):
    if not job.steps:
        job.add_step("publish", "Publishing the changed tables (with confirmation)")
    job.write(f"Run: {Path(run_dir).name}")
    job.set("publish", "run")
    code = run_cmd(job, py("publish_tables.py", "--run", str(run_dir)))
    if code != 0:
        job.set("publish", "fail")
        return job.finish("notlive", "NOT LIVE YET - publishing did not complete. Try "
                          "Publish again, or Send again.", NOT_LIVE)
    job.set("publish", "ok")
    return job.finish("ok", "Published. Wait a minute, then check in a NEW InPrivate/Incognito "
                      "window.", OK)


def main():
    ap = argparse.ArgumentParser(description="Send a DemoHider run to Dynamics")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("send")
    p.add_argument("--run", required=True)
    p.add_argument("--catalog", required=True)
    p.add_argument("--solution", required=True)
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--round-wait", type=int, default=60)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="apply the previewed run first")
    g.add_argument("--undo", action="store_true", help="undo the applied run first")
    p.add_argument("--full", action="store_true",
                   help="send the whole solution, not only the changed tables")
    p = sub.add_parser("publish")
    p.add_argument("--run", required=True)
    a = ap.parse_args()

    if not (Path(a.run) / "run.json").exists():
        print(f"ERROR: run folder not found: {a.run}")
        sys.exit(STOPPED)
    job = Job(a.cmd)
    if a.cmd == "publish":
        do_publish(job, a.run)
    else:
        do_send(job, a.run, a.catalog, a.solution, max(1, a.rounds), a.round_wait,
                a.apply, a.undo, a.full)
    sys.exit(job.code)


if __name__ == "__main__":
    main()
