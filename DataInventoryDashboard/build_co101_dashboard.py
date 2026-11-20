#!/usr/bin/env python3
"""
2000CO101 (CO101) Goldstone DSS-14, Sep 2009 -- per-day, per-type inventory + HTML dashboard.

Scope: Goldstone data only. Florence is a different object and is NOT used here.
Excluded by the user: day folders sep26, sep28, sep29, sep30 (no Goldstone data).

Read-only over the data tree. Writes only to ./out/ next to this script:
  out/co101_inventory.json   machine-readable inventory (for later .obs comparison)
  out/co101_dashboard.html   self-contained page (works offline)

Usage:
  python3 build_co101_dashboard.py [data_root]
  default data_root = ../Goldstone/2009 (relative to this script)

Type identification (also shown in the page's Legend):
  IMAGING   raw maps.<ts>_1 run whose start time falls in a masterlog 'imaging(...)' window
  RANGING   raw maps.<ts>_1 run whose start time falls in a masterlog 'ranging(...)' window
  LPBK      raw maps.<ts>_1-lpbk  (loopback calibration, filename suffix)
  SKY       raw maps.<ts>_1-sky   (sky calibration, filename suffix)
  NO_LOG    science-type run on a DOY with no masterlog rows
  UNMATCHED science-type run on a logged DOY whose start time is outside every logged window
  CW        planned from masterlog 'CW, 4 hops (10 s)' rows
  DAS       GSSR DAS spectrum/voltage RDF records (PSD*/VOLT*); a PSD with nspec == planned CW
            runs and target 2000CO101 is flagged as a CW record (not verified)
"""
import datetime as dt
import html
import json
import re
import struct  # noqa: F401  (reserved for future binary checks)
import sys
import zipfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (HERE.parent / "Goldstone" / "2009")
OUT_DIR = HERE / "out"
YEAR = 2009
MONTH_FOR_SEP_FOLDERS = 9
MASTERLOG = DATA_ROOT / "2000CO101_Goldstone_masterlog.txt"

# User decision: these day folders hold no Goldstone data. Skipped entirely.
EXCLUDED_DAYS = {"sep26", "sep28", "sep29", "sep30"}

RAW_RE = re.compile(r"^maps\.(\d{14})_1(-lpbk|-sky)?(\.txt)?$")
DAY_DIR_RE = re.compile(r"^sep(\d{2})$")
HEADER_RE = re.compile(r"^([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2})\s+DOY\s+(\d{3})")
SETUP_RE = re.compile(r"^(CW|ranging|imaging)\b")
TIME_RE = re.compile(r"(\d+)\s+(\d{6})-(\d{6})\s*$")
CODE_RE = re.compile(r"x\s+([\d.]+)\s+us")
CAT_OF = {"CW": "CW", "ranging": "RANGING", "imaging": "IMAGING"}
RDF_KEYS = {"type", "ndata", "height", "target", "dasid", "width", "size", "polarization",
            "machine", "nchan", "ntags", "nspec", "format"}

SAMPLE_RECS = []
SCIENCE_CATS = ["IMAGING", "RANGING", "NO_LOG", "UNMATCHED"]
RAW_ORDER = ["IMAGING", "RANGING", "LPBK", "SKY", "NO_LOG", "UNMATCHED"]
PLAN_ORDER = ["IMAGING", "RANGING", "CW"]
COLORS = {
    "IMAGING": "#2563eb", "RANGING": "#0d9488", "CW": "#7c3aed",
    "LPBK": "#9ca3af", "SKY": "#cbd5e1", "NO_LOG": "#f59e0b", "UNMATCHED": "#ef4444",
    "CW_RECORD": "#db2777",
}


def display_root():
    """Path shown in outputs: relative to the project, so no local user paths are published."""
    try:
        return "./" + str(DATA_ROOT.relative_to(HERE.parent))
    except ValueError:
        return "<external data root>"


def doy_to_date(doy):
    return dt.date(YEAR, 1, 1) + dt.timedelta(days=doy - 1)


def fmt_time(hhmmss):
    return f"{hhmmss[0:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}"


# ---------------------------------------------------------------- masterlog
def parse_masterlog(path):
    """One dict per 'Setup' row of the masterlog."""
    blocks = []
    if not path.exists():
        return blocks
    doy = None
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        h = HEADER_RE.match(line)
        if h:
            doy = int(h.group(4))
            continue
        s = SETUP_RE.match(line)
        if not (s and doy is not None):
            continue
        t = TIME_RE.search(line)
        if not t:
            continue
        cat = CAT_OF[s.group(1)]
        code = CODE_RE.search(line)
        if cat == "CW":
            label = "CW (4 hops x 10 s)"
        else:
            label = f"{cat} {code.group(1)} us" if code else cat
        blocks.append({
            "doy": doy,
            "date": doy_to_date(doy).isoformat(),
            "category": cat,
            "label": label,
            "runs": int(t.group(1)),
            "start": t.group(2),
            "end": t.group(3),
            "raw_line": line,
        })
    return blocks


def match_block(blocks, date_iso, hhmmss):
    for b in blocks:
        if b["date"] == date_iso and b["start"] <= hhmmss <= b["end"]:
            return b
    return None


# ---------------------------------------------------------------- raw runs
def classify_top(name):
    low = name.lower()
    if name.startswith(("PSD", "VOLT")) and low.endswith(".rdf"):
        return "CW"  # per JPL: PSD*/VOLT* *.rdf are the CW files
    if name.startswith("drv"):
        return "CONTROL"
    if low.startswith("readme"):
        return "README"
    if low.endswith(".log") or low.startswith("txrrcv"):
        return "LOG"
    return "OTHER"


def parse_rdf_header(path):
    """Key/value lines at the start of a GSSR DAS RDF file (text part only)."""
    with open(path, "rb") as fh:
        head = fh.read(2048).decode("latin-1")
    fields = {}
    for line in head.splitlines():
        m = re.match(r"^([si])\s+(\w+)\s+(.+?)\s*$", line)
        if m and m.group(2) in RDF_KEYS and m.group(2) not in fields:
            val = m.group(3)
            fields[m.group(2)] = int(val) if m.group(1) == "i" and val.isdigit() else val
    return fields


def scan_raw(day_path, day_blocks, has_log_rows):
    runs = {}
    top_files = []
    for f in sorted(day_path.iterdir()):
        if not f.is_file():
            continue
        m = RAW_RE.match(f.name)
        if not m:
            entry = {"name": f.name, "category": classify_top(f.name), "size": f.stat().st_size}
            if entry["category"] == "CW":
                entry["header"] = parse_rdf_header(f)
                entry["kind"] = "PSD spectrum" if f.name.startswith("PSD") else "VOLT record"
            top_files.append(entry)
            continue
        ts, suffix, txt = m.group(1), m.group(2) or "", m.group(3)
        rec = runs.setdefault((ts, suffix), {"ts": ts, "suffix": suffix, "bin": None,
                                             "bin_size": 0, "txt": False})
        if txt:
            rec["txt"] = True
        else:
            rec["bin"] = f.name
            rec["bin_size"] = f.stat().st_size

    recs = []
    for (ts, suffix), r in sorted(runs.items()):
        if r["bin"] is None:
            continue  # .txt without binary is reported separately
        date_iso = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
        hhmmss = ts[8:14]
        matched = None
        if suffix == "-lpbk":
            cat, label = "LPBK", "loopback cal"
        elif suffix == "-sky":
            cat, label = "SKY", "sky cal"
        elif not has_log_rows:
            cat, label = "NO_LOG", "no masterlog rows for this DOY"
        else:
            matched = match_block(day_blocks, date_iso, hhmmss)
            if matched:
                cat, label = matched["category"], matched["label"]
            else:
                cat, label = "UNMATCHED", "start time outside logged windows"
        recs.append({
            "file": r["bin"],
            "time_utc": fmt_time(hhmmss),
            "category": cat,
            "label": label,
            "size": r["bin_size"],
            "has_txt": r["txt"],
            "matched_setup": matched["raw_line"] if matched else None,
        })
    orphan_txt = sorted({f"maps.{ts}_1{suffix}.txt" for (ts, suffix), r in runs.items()
                         if r["bin"] is None})
    return recs, top_files, orphan_txt


def make_notes(rec):
    notes = []
    if rec["in_masterlog"] and not rec["folder_present"]:
        notes.append("Masterlog has runs for this day but the folder is not downloaded yet.")
    if rec["folder_present"] and not rec["in_masterlog"]:
        notes.append("No masterlog rows for this DOY.")
    planned_sci = sum(rec["planned"].get(c, 0) for c in ("IMAGING", "RANGING"))
    found_sci = sum(rec["raw"].get(c, 0) for c in SCIENCE_CATS)
    if rec["in_masterlog"] and rec["folder_present"] and found_sci != planned_sci:
        notes.append(f"Science runs on disk {found_sci} vs {planned_sci} planned.")
    if rec["planned"].get("CW"):
        cw_das = [d for d in rec["das"] if d.get("kind") == "PSD spectrum"
                  and d.get("header", {}).get("nspec") == rec["planned"]["CW"]]
        if cw_das:
            notes.append(f"CW record: {cw_das[0]['name']} has nspec={rec['planned']['CW']} "
                         f"(one per planned CW run), target {cw_das[0]['header'].get('target')}, "
                         f"pol {cw_das[0]['header'].get('polarization')}. Not verified against raw CW data.")
        else:
            notes.append(f"CW planned ({rec['planned']['CW']} runs), no CW data on disk.")
    zero = sum(1 for r in rec["raw_files"] if r["size"] == 0)
    if zero:
        notes.append(f"{zero} zero-byte run file(s).")
    if rec["orphan_txt"]:
        notes.append(f"{len(rec['orphan_txt'])} .txt without matching binary.")
    no_txt = sum(1 for r in rec["raw_files"] if not r["has_txt"])
    if no_txt:
        notes.append(f"{no_txt} run(s) without .txt companion.")
    return notes


def build_days(blocks):
    folders = {}
    for d in DATA_ROOT.iterdir():
        m = DAY_DIR_RE.match(d.name)
        if d.is_dir() and m and d.name not in EXCLUDED_DAYS:
            iso = dt.date(YEAR, MONTH_FOR_SEP_FOLDERS, int(m.group(1))).isoformat()
            folders[iso] = d

    dates = sorted((set(folders) | {b["date"] for b in blocks}))
    days = []
    for iso in dates:
        day_blocks = [b for b in blocks if b["date"] == iso]
        has_log = bool(day_blocks)
        planned = Counter()
        for b in day_blocks:
            planned[b["category"]] += b["runs"]
        path = folders.get(iso)
        rec = {
            "date": iso,
            "doy": dt.date.fromisoformat(iso).timetuple().tm_yday,
            "folder": path.name if path else None,
            "folder_present": path is not None,
            "in_masterlog": has_log,
            "planned": dict(planned),
            "planned_blocks": [{k: b[k] for k in ("category", "label", "runs", "start", "end")}
                               for b in day_blocks],
            "raw": {},
            "raw_files": [],
            "top_files": [],  # filled below when the folder exists
            "das": [],
            "orphan_txt": [],
            "notes": [],
        }
        if path:
            raw_recs, top_files, orphan = scan_raw(path, day_blocks, has_log)
            rec["raw_files"] = raw_recs
            rec["raw"] = dict(Counter(r["category"] for r in raw_recs))
            rec["top_files"] = top_files
            rec["das"] = [t for t in top_files if t["category"] == "CW"]
            rec["orphan_txt"] = orphan
        rec["notes"] = make_notes(rec)
        days.append(rec)
    return days


# ---------------------------------------------------------------- 2009-level files
def scan_2009_level(root):
    """Everything in the 2009 folder that is not a day folder, with a plain-language note."""
    items = []
    for p in sorted(root.iterdir()):
        name = p.name
        if DAY_DIR_RE.match(name):
            if name in EXCLUDED_DAYS:
                items.append({"name": name, "category": "DAY FOLDER", "status": "EXCLUDED",
                              "note": "no Goldstone data (user)"})
            else:
                items.append({"name": name, "category": "DAY FOLDER", "status": "included",
                              "note": ""})
            continue
        status, note = "included", ""
        if p == MASTERLOG:
            cat, note = "MASTERLOG", "Goldstone observing log (DOY 261-264 only)"
        elif name in ("goldobsfiles", "obsfiles"):
            cat, note = "OBS (existing)", "existing SHAPE obsfile(s); comparison target"
        elif name == "logs":
            cat, note = "LOGBOOK", "2000CO101 logbook (co.tex / .pdf) and sbinit logfile"
        elif re.match(r"^TXRRCV\d+\.log$", name):
            cat = "TX/RX LOG"
        elif name.startswith("PRDX.OUT") or name == "newephem":
            cat, note = "EPHEMERIS", "JPL/OSOD predictor or ephemeris output"
        elif name.endswith(".msg"):
            text = p.read_text(errors="replace")
            subj = sorted(set(re.findall(r"^Subject:\s*(.*)$", text, re.M)))
            cat = "EMAIL"
            note = "subjects: " + "; ".join(subj[:3])
            if "CO101" not in text.upper():
                status = "NOT CO101"
                note += " -- no CO101 mention; about a different object"
        elif name.endswith(".zip"):
            with zipfile.ZipFile(p) as z:
                names = [n for n in z.namelist() if not n.endswith("/")]
            cat = "ARCHIVE"
            note = f"{len(names)} FITS files"
            if any("20090926" in n for n in names):
                note += " named run20090926*: that is Sep 26 (an excluded day), check"
        elif name.endswith(".zip"):
            continue  # user: ignore (Arecibo images)
        else:
            cat = "OTHER"
        items.append({"name": name, "category": cat, "status": status, "note": note})
    return items


# ---------------------------------------------------------------- obs files
def obs_summary(path, root):
    text = path.read_text(errors="replace")
    sets = re.search(r"(\d+)\s*\{number of sets\}", text)
    frames = [int(x) for x in re.findall(r"(\d+)\s*\{number of frames\}", text)]
    set_types = re.findall(r"^\s*(delay-doppler|doppler)\s*\{set type\}", text, re.M)
    data_dir = re.findall(r"^(\S.*?)\s*\{data directory\}", text, re.M)
    frame_names = re.findall(r"^\s*(\S+\.rdf)\s+(\d{4})\s+(\d+)\s+(\d+)", text, re.M)
    return {
        "file": str(path.relative_to(root)),
        "sets": int(sets.group(1)) if sets else None,
        "set_types": set_types,
        "frames_per_set": frames,
        "data_directory": data_dir,
        "frame_lines": len(frame_names),
    }


# ---------------------------------------------------------------- HTML
TAB_SCRIPT = """<script>
function showTab(id){
  for (const t of ['tab-inv','tab-samp']) document.getElementById(t).style.display = (t===id)?'block':'none';
  document.getElementById('b-inv').classList.toggle('on', id==='tab-inv');
  document.getElementById('b-samp').classList.toggle('on', id==='tab-samp');
}
</script>"""
CSS = """
.tabs{margin:14px 0 6px} .tabbtn{font:inherit;font-size:13px;padding:6px 14px;border:1px solid #d1d5db;background:#fff;border-radius:6px 6px 0 0;cursor:pointer;margin-right:4px}
.tabbtn.on{background:#111;color:#fff;border-color:#111}
body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:24px;color:#111;background:#fafafa}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}
.sub{color:#555;font-size:13px} .cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:10px 14px;min-width:140px}
.card .n{font-size:22px;font-weight:600} .card .l{font-size:12px;color:#666}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;margin:6px 0}
th,td{border:1px solid #e5e7eb;padding:5px 8px;text-align:right;vertical-align:top}
th{background:#f3f4f6;text-align:center} td.l,th.l{text-align:left}
.warn{color:#b45309;font-weight:600} .bad{color:#b91c1c;font-weight:600} .ok{color:#15803d;font-weight:600}
.dim{color:#9ca3af}
.tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;color:#fff;margin-right:2px;white-space:nowrap}
.note{font-size:12px;color:#555;text-align:left} ul{margin:4px 0 4px 18px;font-size:13px}
details{background:#fff;border:1px solid #e5e7eb;border-radius:6px;margin:6px 0;padding:6px 10px}
summary{cursor:pointer;font-weight:600} code{font-size:12px}
.legend span.sw{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:middle;margin:0 4px 0 12px}
"""


def tag(cat):
    color = COLORS.get(cat, "#6b7280")
    return f'<span class="tag" style="background:{color}">{html.escape(cat)}</span>'


def cell_found_planned(found, planned):
    if not planned and not found:
        return '<td class="dim">–</td>'
    if planned and found == planned:
        cls = "ok"
    elif planned and found < planned:
        cls = "bad"
    else:
        cls = "warn"
    return f'<td class="{cls}">{found} / {planned}</td>'


def dash_or(n):
    return n if n else "<span class='dim'>–</span>"


def svg_chart(days):
    base_y, top_y = 220, 20
    max_total = 1
    for d in days:
        max_total = max(max_total, sum(d["planned"].get(c, 0) for c in PLAN_ORDER),
                        sum(d["raw"].get(c, 0) for c in RAW_ORDER))
    scale = (base_y - top_y) / max_total
    group_w = 120
    width = max(len(days), 1) * group_w + 60
    parts = [f'<svg width="{width}" height="270" xmlns="http://www.w3.org/2000/svg" '
             f'font-family="Helvetica,Arial,sans-serif" font-size="11">',
             f'<line x1="40" y1="{base_y}" x2="{width}" y2="{base_y}" stroke="#999"/>']
    for i, d in enumerate(days):
        x0 = 50 + i * group_w
        for offset, counts, order in ((0, d["planned"], PLAN_ORDER), (36, d["raw"], RAW_ORDER)):
            y = base_y
            total = 0
            for cat in order:
                v = counts.get(cat, 0)
                if not v:
                    continue
                total += v
                h = v * scale
                y -= h
                parts.append(f'<rect x="{x0 + offset}" y="{y:.1f}" width="30" height="{h:.1f}" '
                             f'fill="{COLORS[cat]}"><title>{cat}: {v}</title></rect>')
            if total:
                parts.append(f'<text x="{x0 + offset + 15}" y="{y - 4:.1f}" text-anchor="middle">{total}</text>')
        dd = dt.date.fromisoformat(d["date"]).strftime("%b %d")
        parts.append(f'<text x="{x0 + 33}" y="{base_y + 16}" text-anchor="middle">{dd}</text>')
        parts.append(f'<text x="{x0 + 15}" y="{base_y + 30}" text-anchor="middle" fill="#666">planned</text>')
        parts.append(f'<text x="{x0 + 51}" y="{base_y + 30}" text-anchor="middle" fill="#666">on disk</text>')
    parts.append("</svg>")
    return "".join(parts)


def planned_table(blocks):
    rows = ["<table><tr><th class='l'>Setup (masterlog)</th><th>Runs</th><th class='l'>UTC window</th></tr>"]
    for b in blocks:
        rows.append(f"<tr><td class='l'>{tag(b['category'])} {html.escape(b['label'])}</td>"
                    f"<td>{b['runs']}</td>"
                    f"<td class='l'>{fmt_time(b['start'])} &ndash; {fmt_time(b['end'])}</td></tr>")
    rows.append("</table>")
    return "".join(rows)


def render_html(days, blocks, obs_list, top_items, generated):
    days_with_folder = sum(1 for d in days if d["folder_present"])
    total_sci = sum(d["raw"].get(c, 0) for d in days for c in SCIENCE_CATS)
    total_lpbk = sum(d["raw"].get("LPBK", 0) for d in days)
    total_sky = sum(d["raw"].get("SKY", 0) for d in days)
    planned_sci = sum(d["planned"].get(c, 0) for d in days for c in ("IMAGING", "RANGING"))
    planned_cw = sum(d["planned"].get("CW", 0) for d in days)
    das_n = sum(len(d["das"]) for d in days)
    cw_candidates = sum(1 for d in days for x in d["das"] if x.get("kind") == "PSD spectrum"
                        and d["planned"].get("CW") and x.get("header", {}).get("nspec") == d["planned"]["CW"])

    out = ["<!doctype html><html><head><meta charset='utf-8'>"
           "<title>2000CO101 Goldstone 2009 - data dashboard</title>"
           f"<style>{CSS}</style></head><body>",
           "<h1>2000CO101 (CO101) &mdash; Goldstone DSS-14, September 2009</h1>",
           f"<div class='sub'>Snapshot {html.escape(generated)} from <code>{html.escape(display_root())}</code>. "
           "Read-only scan. Goldstone data only. Excluded folders: sep26, sep28, sep29, sep30 (no Goldstone "
           "data). Downloads are still in progress; re-run the script to refresh.</div>",
           "<div class='tabs'><button id='b-inv' class='tabbtn on' onclick=\"showTab('tab-inv')\">Inventory</button>"
           "<button id='b-samp' class='tabbtn' onclick=\"showTab('tab-samp')\">Sample images</button></div>",
           "<div id='tab-inv'>", "<div class='cards'>"]
    for n, l in [
        (days_with_folder, "day folders on disk (included)"),
        (total_sci, "science runs on disk (IMAGING + RANGING + other)"),
        (planned_sci, "science runs planned in masterlog"),
        (total_lpbk, "loopback cal runs on disk"),
        (total_sky, "sky cal runs on disk"),
        (planned_cw, "CW runs planned in masterlog"),
        (das_n, "CW records on disk (PSD/VOLT)"),
        (cw_candidates, "CW record records (unverified)"),
    ]:
        out.append(f"<div class='card'><div class='n'>{n}</div><div class='l'>{l}</div></div>")
    out.append("</div>")

    out.append("<h2>Per day, per type</h2>")
    out.append("<div class='note'>Science cells show <b>on disk / planned</b> (planned = masterlog Runs column). "
               "Green = match, red = fewer on disk than planned.</div>")
    out.append("<table><tr><th class='l'>Date</th><th>DOY</th><th class='l'>Folder</th>"
               "<th>IMAGING<br>on disk / planned</th><th>RANGING<br>on disk / planned</th>"
               "<th>CW<br>planned runs</th><th>CW on disk</th>"
               "<th>LPBK</th><th>SKY</th><th>NO_LOG / UNMATCHED</th><th>CW records (PSD/VOLT)</th>"
               "<th class='l'>Notes</th></tr>")
    for d in days:
        cw_on_disk = ""
        if d["planned"].get("CW"):
            cw_on_disk = "<span class='ok'>present</span>" if any(
                x.get("kind") == "PSD spectrum" and x.get("header", {}).get("nspec") == d["planned"]["CW"]
                for x in d["das"]) else "<span class='bad'>none</span>"
        das_str = ", ".join(html.escape(x["name"]) for x in d["das"]) or "<span class='dim'>–</span>"
        folder = html.escape(d["folder"]) if d["folder"] else "<span class='dim'>not downloaded</span>"
        out.append(
            "<tr>"
            f"<td class='l'>{dt.date.fromisoformat(d['date']).strftime('%a %b %d')}</td>"
            f"<td>{d['doy']}</td><td class='l'>{folder}</td>"
            f"{cell_found_planned(d['raw'].get('IMAGING', 0), d['planned'].get('IMAGING', 0))}"
            f"{cell_found_planned(d['raw'].get('RANGING', 0), d['planned'].get('RANGING', 0))}"
            f"<td>{dash_or(d['planned'].get('CW', 0))}</td>"
            f"<td>{cw_on_disk or '<span class=dim>–</span>'}</td>"
            f"<td>{dash_or(d['raw'].get('LPBK', 0))}</td>"
            f"<td>{dash_or(d['raw'].get('SKY', 0))}</td>"
            f"<td>{dash_or(d['raw'].get('NO_LOG', 0) + d['raw'].get('UNMATCHED', 0))}</td>"
            f"<td class='l'>{das_str}</td>"
            f"<td class='note'>{'<br>'.join(html.escape(n) for n in d['notes'])}</td>"
            "</tr>")
    out.append("</table>")

    out.append("<h2>Chart: planned (masterlog) vs on disk</h2><div class='legend'>")
    for cat in ["IMAGING", "RANGING", "CW", "LPBK", "SKY", "NO_LOG", "UNMATCHED"]:
        out.append(f"<span class='sw' style='background:{COLORS[cat]}'></span>{cat}")
    out.append("</div>")
    out.append("<div style='overflow-x:auto;background:#fff;border:1px solid #e5e7eb;"
               f"border-radius:6px;padding:8px'>{svg_chart(days)}</div>")

    out.append("<h2>Day details</h2>")
    for d in days:
        if not d["folder_present"]:
            out.append(f"<details><summary>{d['date']} (DOY {d['doy']}) &mdash; folder not downloaded yet</summary>")
            if d["planned_blocks"]:
                out.append(planned_table(d["planned_blocks"]))
            out.append("</details>")
            continue
        out.append(f"<details open><summary>{d['date']} (DOY {d['doy']}) &mdash; "
                   f"{len(d['raw_files'])} run file(s) on disk</summary>")
        if d["planned_blocks"]:
            out.append(planned_table(d["planned_blocks"]))
        if d["raw_files"]:
            out.append("<table><tr><th class='l'>Start UTC</th><th class='l'>Run file</th>"
                       "<th class='l'>Type</th><th class='l'>Setup / note</th><th>Size KB</th>"
                       "<th>.txt</th></tr>")
            for r in d["raw_files"]:
                out.append(
                    f"<tr><td class='l'>{r['time_utc']}</td><td class='l'><code>{html.escape(r['file'])}</code></td>"
                    f"<td class='l'>{tag(r['category'])}</td>"
                    f"<td class='l'>{html.escape(r['label'])}</td>"
                    f"<td>{r['size'] // 1024}</td>"
                    f"<td>{'yes' if r['has_txt'] else '<span class=warn>no</span>'}</td></tr>")
            out.append("</table>")
        else:
            out.append("<div class='note'>No raw run files in this folder yet.</div>")
        if d["das"]:
            out.append("<div class='note'><b>CW records (PSD/VOLT):</b></div><ul>")
            for x in d["das"]:
                h = x.get("header", {})
                out.append(f"<li>{tag('CW')} &mdash; CW data: GSSR RDF records <code>PSD*.rdf</code> (OC) and <code>VOLT*.rdf</code> (SC). Headers give target, polarization and number of spectra (nspec). Per the JPL data notes, these are the only CW files.</li>")
            out.append("</ul>")
        other = [t for t in d["top_files"] if t["category"] not in ("CW",)]
        if other:
            out.append("<div class='note'><b>Other files:</b> " +
                       ", ".join(f"<code>{html.escape(t['name'])}</code> ({t['category']})" for t in other)
                       + "</div>")
        out.append("</details>")

    out.append("<h2>Masterlog (planned runs)</h2>")
    out.append(planned_table(blocks))

    out.append("<h2>Other files in the 2009 folder</h2>")
    out.append("<table><tr><th class='l'>Name</th><th class='l'>Category</th><th class='l'>Status</th>"
               "<th class='l'>Note</th></tr>")
    for it in top_items:
        status_cls = "bad" if it["status"] in ("EXCLUDED", "NOT CO101") else "ok"
        out.append(f"<tr><td class='l'><code>{html.escape(it['name'])}</code></td>"
                   f"<td class='l'>{html.escape(it['category'])}</td>"
                   f"<td class='l {status_cls}'>{html.escape(it['status'])}</td>"
                   f"<td class='l note'>{html.escape(it['note'])}</td></tr>")
    out.append("</table>")

    out.append("<h2>Existing SHAPE .obs files (comparison later)</h2>")
    if obs_list:
        out.append("<table><tr><th class='l'>File</th><th>Sets</th><th class='l'>Set types</th>"
                   "<th class='l'>Frames per set</th><th class='l'>Data directory</th></tr>")
        for o in obs_list:
            out.append(f"<tr><td class='l'><code>{html.escape(o['file'])}</code></td>"
                       f"<td>{o['sets']}</td>"
                       f"<td class='l'>{html.escape(', '.join(sorted(set(o['set_types']))))}</td>"
                       f"<td class='l'>{html.escape(str(o['frames_per_set']))}</td>"
                       f"<td class='l'><code>{html.escape(', '.join(o['data_directory']))}</code></td></tr>")
        out.append("</table>")
    else:
        out.append("<div class='note'>No .obs files found.</div>")

    out.append("<h2>Legend: how the types were identified</h2><ul>")
    out.append(f"<li>{tag('IMAGING')} / {tag('RANGING')} &mdash; raw <code>maps.&lt;YYYYMMDDHHMMSS&gt;_1</code> "
               "delay-Doppler map runs. The masterlog setup (imaging or ranging, with its code length) is "
               "matched by start time UTC.</li>")
    out.append(f"<li>{tag('LPBK')} / {tag('SKY')} &mdash; calibration runs, identified by the "
               "<code>-lpbk</code> / <code>-sky</code> filename suffix.</li>")
    out.append(f"<li>{tag('CW')} &mdash; the masterlog's <code>CW, 4 hops (10 s)</code> rows. No raw CW run "
               "files are on disk for the included days.</li>")
    out.append(f"<li>{tag('CW')} &mdash; CW data: GSSR RDF records <code>PSD*.rdf</code> (OC) and <code>VOLT*.rdf</code> (SC). Headers give target, polarization and number of spectra (nspec). Per the JPL data notes, these are the only CW files.</li>")
    out.append("<li><b>Filenames mislead in general:</b> the SHAPE set type inside a .obs file is the reliable test "
               "(<code>doppler</code> = CW, <code>delay-doppler</code> = DD).</li>")
    out.append("</ul></div>")
    out.append("<div id='tab-samp' style='display:none'>" + samples_html(SAMPLE_RECS, days) + "</div>")
    out.append(TAB_SCRIPT)
    out.append("</body></html>")
    return "\n".join(out)


# ---------------------------------------------------------------- sample images
SAMPLE_TYPES = ["IMAGING", "LPBK", "SKY"]
N_SAMPLES = 2


def pick_spread(items, n):
    if len(items) <= n:
        return list(items)
    idx = sorted({int(round(x)) for x in [i * (len(items) - 1) / (n - 1) for i in range(n)]})
    return [items[i] for i in idx]


def make_samples(days):
    """Render up to N_SAMPLES images per date per decodable type. Returns records for the page."""
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    sdir = OUT_DIR / "samples"
    sdir.mkdir(parents=True, exist_ok=True)
    for old in sdir.glob("*.png"):
        old.unlink()
    recs = []
    for d in days:
        if not d["folder_present"]:
            continue
        folder = DATA_ROOT / d["folder"]
        for cat in SAMPLE_TYPES:
            cands = [r for r in d["raw_files"] if r["category"] == cat]
            item = {"date": d["date"], "type": cat, "available": len(cands), "images": []}
            for r in pick_spread(cands, N_SAMPLES):
                arr = np.fromfile(folder / r["file"], dtype="<f4")
                if arr.size % 128:
                    continue
                grid = arr.reshape(-1, 128)
                lo, hi = np.percentile(grid, [1, 99.5])
                fname = f"{d['date']}_{cat}_{r['time_utc'].replace(':', '')}.png"
                fig, ax = plt.subplots(figsize=(4.8, 3.8))
                im = ax.imshow(grid, aspect="auto", origin="lower", cmap="viridis", vmin=lo, vmax=hi)
                ax.set_title(f"{dt.date.fromisoformat(d['date']).strftime('%b %d %Y')} {r['time_utc']} UTC\n"
                             f"{r['label']} | {grid.shape[0]} rows x 128 cols", fontsize=8)
                ax.set_xlabel("FFT bin", fontsize=8)
                ax.set_ylabel("code row", fontsize=8)
                ax.tick_params(labelsize=7)
                fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04).ax.tick_params(labelsize=7)
                fig.tight_layout()
                fig.savefig(sdir / fname, dpi=80)
                plt.close(fig)
                item["images"].append({"file": f"samples/{fname}", "time_utc": r["time_utc"],
                                       "rows": int(grid.shape[0]), "label": r["label"]})
            recs.append(item)
    return recs


def samples_html(recs, days):
    out = ["<p class='note'>Samples: up to two runs per date per type, picked evenly across the day. "
           "Colour scale is clipped at the 1st and 99.5th percentiles so faint echoes show. "
           "Rows = code length (255 or 127).</p>"]
    out.append("<h2>Images by date and type</h2>")
    by_date = {}
    for r in recs:
        by_date.setdefault(r["date"], []).append(r)
    for d in days:
        if not d["folder_present"]:
            continue
        out.append(f"<h3 style='font-size:15px;margin:14px 0 4px'>{dt.date.fromisoformat(d['date']).strftime('%a %b %d %Y')} (DOY {d['doy']})</h3>")
        for r in by_date.get(d["date"], []):
            if not r["images"]:
                out.append(f"<div class='note'>{tag(r['type'])} no decodable runs on disk.</div>")
                continue
            note = ""
            if r["available"] > len(r["images"]):
                note = f" ({r['available']} on disk, showing {len(r['images'])})"
            elif r["available"] < N_SAMPLES:
                note = f" (only {r['available']} on disk)"
            out.append(f"<div class='note'>{tag(r['type'])}{note}</div>")
            out.append("<div style='display:flex;flex-wrap:wrap;gap:10px;margin:4px 0 8px'>")
            for im in r["images"]:
                out.append(f"<figure style='margin:0;background:#fff;border:1px solid #e5e7eb;border-radius:6px;padding:6px'>"
                           f"<img src='{html.escape(im['file'])}' alt='{html.escape(r['type'])} {im['time_utc']}' "
                           f"style='width:360px;display:block'>"
                           f"<figcaption class='note'>{im['time_utc']} UTC &middot; {im['rows']} rows</figcaption></figure>")
            out.append("</div>")
    out.append("<h2>CW (continuous wave)</h2>")
    out.append("<p class='note'>CW records are GSSR DAS files (<code>PSD*.rdf</code> = OC spectra, "
               "<code>VOLT*.rdf</code> = SC voltage). Their headers decode (target, polarization, nspec), "
               "but the numeric data layout is not decoded yet, so no CW image is shown here.</p>")
    out.append("<table><tr><th class='l'>Date</th><th class='l'>File</th><th class='l'>Header</th>"
               "<th class='l'>Image</th></tr>")
    any_cw = False
    for d in days:
        for x in d["das"]:
            any_cw = True
            h = x.get("header", {})
            out.append(f"<tr><td class='l'>{d['date']}</td><td class='l'><code>{html.escape(x['name'])}</code></td>"
                       f"<td class='l'>target {html.escape(str(h.get('target')))}, pol {html.escape(str(h.get('polarization')))}, "
                       f"nspec {h.get('nspec')}, nchan {h.get('nchan')}, {html.escape(str(h.get('dasid')))}</td>"
                       f"<td class='l'><span class='warn'>not decoded yet</span></td></tr>")
    if not any_cw:
        out.append("<tr><td class='l' colspan='4'>No CW records on the included days.</td></tr>")
    out.append("</table>")
    return "\n".join(out)


# ---------------------------------------------------------------- main
def main():
    if not DATA_ROOT.is_dir():
        sys.exit(f"Data root not found: {DATA_ROOT}")
    blocks = parse_masterlog(MASTERLOG)
    days = build_days(blocks)
    top_items = scan_2009_level(DATA_ROOT)
    obs_list = [obs_summary(p, DATA_ROOT) for p in sorted(DATA_ROOT.rglob("*.obs"))
                if not (set(p.relative_to(DATA_ROOT).parts) & EXCLUDED_DAYS)
                and not re.search(r"_(26|28|29|30)[a-z]*\.", p.name)]
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    global SAMPLE_RECS
    SAMPLE_RECS = make_samples(days)

    OUT_DIR.mkdir(exist_ok=True)
    inv = {"generated": generated, "data_root": display_root(), "excluded_days": sorted(EXCLUDED_DAYS),
           "masterlog_blocks": blocks, "days": days, "two_thousand_nine_level": top_items,
           "obs_files": obs_list}
    (OUT_DIR / "co101_inventory.json").write_text(json.dumps(inv, indent=2, default=str))
    (OUT_DIR / "co101_dashboard.html").write_text(render_html(days, blocks, obs_list, top_items, generated))

    print(f"Wrote {OUT_DIR / 'co101_dashboard.html'}")
    print(f"Wrote {OUT_DIR / 'co101_inventory.json'}")
    for d in days:
        print(f"{d['date']} DOY {d['doy']} folder={d['folder'] or '-'} planned={d['planned']} "
              f"raw={d['raw']} das={[x['name'] for x in d['das']]} notes={d['notes']}")
    for it in top_items:
        print(f"2009 level: {it['name']} | {it['category']} | {it['status']} | {it['note']}")


if __name__ == "__main__":
    main()
