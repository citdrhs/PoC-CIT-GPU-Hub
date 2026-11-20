"""Plain-text and ANSI views. All untrusted values pass through clean()."""
import json
import os
import shutil
import sys
import unicodedata
from datetime import datetime, timezone


def clean(value):
    return "".join(ch for ch in str(value) if not unicodedata.category(ch).startswith("C"))


def gib(value):
    return "unavailable" if value is None else f"{value / 2 ** 30:g} GiB"


def date(value):
    if value is None:
        return "unavailable"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def label(value):
    return clean(value or "unavailable").replace("_", " ").lower()


class Screen:
    INK = "1;97"
    ACCENT = "38;5;215"
    DIM = "38;5;248"
    GREEN = "38;5;114"
    BLOCK = "38;5;204"

    def __init__(self, stream=None):
        self.out = stream or sys.stdout
        self.color = self.out.isatty() and "NO_COLOR" not in os.environ
        self.width = min(110, max(36, shutil.get_terminal_size((100, 40)).columns - 2))
        self.unicode = True
        try:
            "─●".encode(self.out.encoding or "utf-8")
        except UnicodeError:
            self.unicode = False

    def paint(self, text, tone):
        return f"\x1b[{tone}m{text}\x1b[0m" if self.color else text

    def line(self, text=""):
        print(text, file=self.out)

    def rule(self):
        self.line(self.paint(("─" if self.unicode else "-") * self.width, self.DIM))

    def title(self, name, context):
        self.line()
        self.line(self.paint("  CIT", self.ACCENT) + self.paint("  /  GPU HUB", self.INK)
                  + "    " + clean(name))
        self.line("  " + self.paint(clean(context), self.DIM))
        self.rule()

    def section(self, title, detail=""):
        self.line()
        self.line("  " + self.paint(clean(title).upper(), self.ACCENT)
                  + ("   " + self.paint(clean(detail), self.DIM) if detail else ""))

    def fields(self, data):
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            self.line(f"    {self.paint(clean(k), self.DIM):s}  {clean(v) if v is not None else 'unavailable'}")

    def table(self, headers, rows, sizes=None):
        if not rows:
            self.line("    " + self.paint("None", self.DIM))
            return
        rows = [[clean(value) if value is not None else "—" if self.unicode else "-" for value in row]
                for row in rows]
        if self.width < 75 and len(headers) >= 3:
            for row in rows:
                self.line("    " + self.paint(row[0], self.INK) + "  " + row[1])
                for title, value in zip(headers[2:], row[2:]):
                    self.line(f"      {title.lower()}  {value}")
            return
        widths = [max([len(h)] + [len(row[i]) for row in rows]) for i, h in enumerate(headers)]
        if sizes:
            widths = [min(w, m) for w, m in zip(widths, sizes)]
        def row_line(row):
            cells = []
            for i, val in enumerate(row):
                if i == len(row) - 1:
                    cells.append(val)
                    break
                if len(val) > widths[i]:
                    val = val[:max(1, widths[i]-1)] + ("…" if self.unicode else "~")
                cells.append(val.ljust(widths[i]))
            return "    " + "  ".join(cells)
        self.line(self.paint(row_line(headers), self.DIM))
        for row in rows:
            self.line(row_line(row))

    def dashboard(self, data):
        me, nodes, queue = data["me"], data["nodes"], data["queue"]
        who = me["user"]
        context = "  /  ".join([who.get("alias") or who["id"], who["role"],
                                 "development auth" if me.get("dev_auth") else "authenticated",
                                 date(data["observed_at"])])
        self.title("Cluster overview", context)
        slots = [slot for node in nodes for slot in node["slots"]]
        waiting = [j for j in queue if j.get("state") in ("QUEUED", "PREPARING")]
        self.section("Capacity", f"{len(nodes)} nodes   /   {len(slots)} fixed slots   /   "
                     f"{sum(s['state'] == 'OCCUPIED' for s in slots)} occupied   /   {len(waiting)} waiting")
        for node in nodes:
            self.node(node)
        self.section("Queue", "waiting for admission")
        self.table(["PROJECT", "OWNER", "STATE", "WAITING REASON"], [
            [j["name"], j.get("alias") or j.get("user_id"), label(j["state"]), label(j.get("waiting_reason"))]
            for j in waiting[:8]], [25, 14, 12, 42])
        if len(waiting) > 8:
            self.line(f"    {len(waiting)-8} more in gpuhub queue")
        self.line()
        self.rule()
        self.line("  " + self.paint("gpuhub jobs", self.INK) + "   ·   "
                  + self.paint("gpuhub status JOB_ID", self.INK) + "   ·   "
                  + self.paint("gpuhub logs JOB_ID --follow", self.INK))

    def node(self, node):
        env = node.get("envelope") or {}
        qual = str(node.get("qualification_status") or node.get("status") or "unknown")
        backend = node.get("backend", "unknown")
        is_blocked = any(x["state"] in ("BLOCKED", "QUARANTINED") for x in node["slots"])
        tag = "SIMULATED" if backend == "simulated" else qual
        self.line()
        color = self.BLOCK if is_blocked else self.GREEN
        self.line("  " + self.paint(clean(node["name"]), self.INK) + "   " + self.paint(clean(tag), color)
                  + "   " + self.paint(label(node.get("status")), self.DIM))
        self.line("    " + self.paint("host reserve", self.DIM) + " " + gib(node.get("reserve_bytes")) + "   "
                  + self.paint("slot budget", self.DIM) + " " + gib(env.get("memory_bytes"))
                  + ("   " + self.paint("GPU share / slot", self.DIM) + " " + clean(env["gpu_fraction"])
                     if env.get("gpu_fraction") else ""))
        for slot in node["slots"]:
            detail = slot.get("job_name")
            if detail and (slot.get("alias") or slot.get("user_id")):
                detail += "  /  " + (slot.get("alias") or slot["user_id"])
            if not detail:
                detail = {"BLOCKED": "GPU enforcement unverified", "AVAILABLE": "Ready for admission",
                          "QUARANTINED": "Awaiting reconciliation"}.get(slot["state"], "")
            self.line("    " + self.paint("[" + clean(slot["id"]) + "]", self.ACCENT)
                      + f"  {label(slot['state']):<12}" + "  " + clean(detail))

    def detail(self, job, artifacts, checkpoints):
        self.title(job["name"], f"Job {job['id']}  /  {label(job['state'])}")
        self.fields({"Owner": job["user_id"], "Profile": job["profile"], "Revision": job["revision_id"],
                     "Submitted": date(job.get("created_at")), "Waiting reason": job.get("waiting_reason")})
        for attempt in job.get("attempts", []):
            self.section("Attempt", attempt["id"])
            self.fields({"Worker / slot": f"{attempt['node_id']} / {attempt['slot']}",
                         "Observed state": attempt["state"], "Hard deadline": date(attempt.get("deadline")),
                         "Allocation": attempt.get("envelope"), "Observed metrics": attempt.get("metrics") or None,
                         "Outcome": attempt.get("reason"), "Stop / cleanup": attempt.get("metadata")})
        self.section("Checkpoints")
        self.records(checkpoints)
        self.section("Artifacts")
        self.records(artifacts)

    def records(self, items):
        if not items:
            self.line("    " + self.paint("None", self.DIM))
        for item in items:
            self.fields(item)
            self.line()
