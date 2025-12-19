#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

HTML_PATH = Path("docs/index.html")

IGNORE_THEATER_SUBSTR = "AMC Bay Street 16"
PREFERRED_THEATERS = [
    "AMC Tustin 14 @ The District",
    "AMC Woodbridge 5",
    "AMC Orange 30",
]

PLANNER_HTML = """
<div id="movie-planner" class="movie-planner">
  <h2>Watch Planner</h2>
  <p class="movie-planner-sub">
    Enter <b>2–4</b> movie numbers (from the <b>#</b> column in the table), then click <b>Suggest plan</b>.
  </p>
  <div class="movie-planner-controls">
    <input id="movie-planner-input" type="text" inputmode="numeric" placeholder="e.g., 2, 5, 9" />
    <button id="movie-planner-btn" type="button">Suggest plan</button>
  </div>
  <div id="movie-planner-output" class="movie-planner-output"></div>
</div>
"""

CSS = r"""
/* --- Mobile + planner styles (injected by CI) --- */
.movie-planner{
  max-width: 980px; margin: 16px auto; padding: 14px 16px;
  border: 1px solid rgba(0,0,0,.12); border-radius: 12px;
}
.movie-planner h2{ margin: 0 0 6px 0; font-size: 20px; }
.movie-planner-sub{ margin: 0 0 10px 0; opacity: .85; }
.movie-planner-controls{
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  margin: 10px 0 8px 0;
}
.movie-planner-controls input{
  flex: 1; min-width: 220px; padding: 10px 12px; border-radius: 10px;
  border: 1px solid rgba(0,0,0,.2);
}
.movie-planner-controls button{
  padding: 10px 12px; border-radius: 10px; border: 1px solid rgba(0,0,0,.2);
  cursor: pointer;
}
.movie-planner-output{ margin-top: 10px; }
.movie-planner-card{
  border: 1px solid rgba(0,0,0,.12); border-radius: 12px; padding: 12px; margin: 10px 0;
}
.movie-planner-card h3{ margin: 0 0 6px 0; font-size: 16px; }
.movie-planner-muted{ opacity: .8; }

img, svg, video, canvas { max-width: 100%; height: auto; }

/* Make tables scroll on small screens instead of clipping */
table.dataframe{ max-width: 100%; }
@media (max-width: 900px){
  table, table.dataframe{
    display: block;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    max-width: 100%;
  }
  pre{
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
}
"""

# JS stays the same as your "improved parsing" version, but expects payload.movies + payload.showtimes
# (This is the same JS you already updated to show "Can't plan..." reasons.)
JS = r"""
(() => {
  const dataEl = document.getElementById('showtimes-data');
  const inputEl = document.getElementById('movie-planner-input');
  const btnEl = document.getElementById('movie-planner-btn');
  const outEl = document.getElementById('movie-planner-output');

  if (!dataEl || !inputEl || !btnEl || !outEl) return;

  const payload = JSON.parse(dataEl.textContent || "{}");
  const raw = Array.isArray(payload.showtimes) ? payload.showtimes : [];
  const IGNORE = (payload.ignore_theater || "AMC Bay Street 16").toLowerCase();

  function theaterRank(theater) {
    const t = (theater || "").toLowerCase();
    if (t.includes("amc tustin 14") && (t.includes("district") || t.includes("the district"))) return 0;
    if (t.includes("amc woodbridge 5")) return 1;
    if (t.includes("amc orange 30")) return 2;
    return 3;
  }

  function isDolby(fmt){ return /dolby/i.test(fmt || ""); }
  function isImaxLaser(fmt){ return /imax/i.test(fmt || "") && /laser/i.test(fmt || ""); }
  function formatRank(fmt){
    if (isDolby(fmt)) return 0;
    if (isImaxLaser(fmt)) return 1;
    return 2;
  }

  function pad2(n){ return String(n).padStart(2,"0"); }
  function toISODateFromDateObj(d){
    return `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}`;
  }

  function parseDateKeyFromString(s){
    if (!s) return null;
    const str = String(s).trim();
    if (!str) return null;

    let m = str.match(/(\d{4}-\d{2}-\d{2})/);
    if (m) return m[1];

    m = str.match(/(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?/);
    if (m){
      const mm = pad2(parseInt(m[1],10));
      const dd = pad2(parseInt(m[2],10));
      let yy = m[3] ? parseInt(m[3],10) : (new Date()).getFullYear();
      if (yy < 100) yy = 2000 + yy;
      return `${yy}-${mm}-${dd}`;
    }

    const d = new Date(str);
    if (!isNaN(d.getTime())) return toISODateFromDateObj(d);

    return str;
  }

  function parseTimeToMinutes(s){
    if (!s) return null;
    let str = String(s).replace(/\u00a0/g, " ").replace(/\u202f/g, " ");

    // Support a/p shorthand: "7p", "7:10p"
    const apShort = Array.from(str.matchAll(/(\d{1,2})(?::(\d{2}))?\s*([ap])\b/gi));
    if (apShort.length){
      const m = apShort[apShort.length - 1];
      let h = parseInt(m[1],10);
      let min = m[2] ? parseInt(m[2],10) : 0;
      const ap = m[3].toLowerCase();
      if (h === 12) h = 0;
      if (ap === "p") h += 12;
      if (h < 0 || h > 23 || min < 0 || min > 59) return null;
      return h*60 + min;
    }

    const ampm = Array.from(str.matchAll(/(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b/gi));
    if (ampm.length){
      const m = ampm[ampm.length - 1];
      let h = parseInt(m[1],10);
      let min = m[2] ? parseInt(m[2],10) : 0;
      const ap = m[3].toLowerCase();
      if (h === 12) h = 0;
      if (ap === "pm") h += 12;
      if (h < 0 || h > 23 || min < 0 || min > 59) return null;
      return h*60 + min;
    }

    const hm = Array.from(str.matchAll(/\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?\b/g));
    if (hm.length){
      const m = hm[hm.length - 1];
      const h = parseInt(m[1],10);
      const min = parseInt(m[2],10);
      return h*60 + min;
    }

    return null;
  }

  function parseRuntimeMin(v){
    if (v === null || v === undefined) return null;
    if (typeof v === "number" && isFinite(v) && v > 0) return Math.round(v);

    const s = String(v).trim().toLowerCase();
    if (!s) return null;

    let m = s.match(/(\d+)\s*:\s*(\d{2})/);
    if (m) return parseInt(m[1],10)*60 + parseInt(m[2],10);

    const h = s.match(/(\d+)\s*(h|hr|hrs|hour|hours)\b/);
    const mm = s.match(/(\d+)\s*(m|min|mins|minute|minutes)\b/);
    if (h) return parseInt(h[1],10)*60 + (mm ? parseInt(mm[1],10) : 0);

    m = s.match(/(\d+)\s*(min|mins|minute|minutes)\b/);
    if (m) return parseInt(m[1],10);

    m = s.match(/\b(\d{2,3})\b/);
    if (m) return parseInt(m[1],10);

    return null;
  }

  function minutesToLabel(totalMinutes){
    let m = totalMinutes % (24*60);
    if (m < 0) m += 24*60;
    let h = Math.floor(m/60);
    let min = m % 60;
    const ampm = h >= 12 ? "PM" : "AM";
    h = h % 12;
    if (h === 0) h = 12;
    return `${h}:${String(min).padStart(2,"0")} ${ampm}`;
  }

  const movieIndex = new Map();
  const movies = Array.isArray(payload.movies) ? payload.movies : [];
  for (const m of movies){
    const id = Number(m.movie_id);
    const name = String(m.movie || "").trim();
    if (id && name && !movieIndex.has(id)) movieIndex.set(id, name);
  }
  if (movieIndex.size === 0){
    for (const r of raw){
      const id = Number(r.movie_id);
      const name = String(r.movie || "").trim();
      if (id && name && !movieIndex.has(id)) movieIndex.set(id, name);
    }
  }

  const rawByMovie = new Map();
  for (const r of raw){
    const id = Number(r.movie_id);
    if (!id) continue;
    if (!rawByMovie.has(id)) rawByMovie.set(id, []);
    rawByMovie.get(id).push(r);
  }

  const showings = raw
    .filter(r => !String(r.theater || "").toLowerCase().includes(IGNORE))
    .map(r => {
      const movie_id = Number(r.movie_id);
      const movie = String(r.movie || "").trim();
      const theater = String(r.theater || "").trim();
      const format = String(r.format || "").trim();

      const dateKey = parseDateKeyFromString(r.date) || parseDateKeyFromString(r.start);
      const startMin = parseTimeToMinutes(r.start);
      const runtimeMin = parseRuntimeMin(r.runtime_min);

      if (!movie_id || !movie || !theater || !dateKey || startMin === null || runtimeMin === null) return null;

      const endMin = startMin + runtimeMin + 30;
      return {
        movie_id, movie, theater, format,
        dateKey, startMin, endMin, runtimeMin,
        tRank: theaterRank(theater),
        fRank: (formatRank(format)),
      };
    })
    .filter(Boolean);

  const byMovie = new Map();
  for (const s of showings){
    if (!byMovie.has(s.movie_id)) byMovie.set(s.movie_id, []);
    byMovie.get(s.movie_id).push(s);
  }

  function parseSelectedIds(){
    const parts = inputEl.value
      .split(/[^0-9]+/)
      .map(x => x.trim())
      .filter(Boolean)
      .map(x => Number(x))
      .filter(n => Number.isFinite(n) && n > 0);
    return Array.from(new Set(parts));
  }

  function explainWhyNoUsableShowings(id){
    const rows = (rawByMovie.get(id) || []).filter(r => !String(r.theater||"").toLowerCase().includes(IGNORE));
    const probs = new Set();
    if (!rows.length) probs.add("no rows (or all ignored theater)");
    for (const r of rows){
      const theater = String(r.theater || "").trim();
      const dateKey = parseDateKeyFromString(r.date) || parseDateKeyFromString(r.start);
      const startMin = parseTimeToMinutes(r.start);
      const runtimeMin = parseRuntimeMin(r.runtime_min);
      if (!theater) probs.add("missing theater");
      if (!dateKey) probs.add("missing/unparseable date");
      if (startMin === null) probs.add("missing/unparseable start time");
      if (runtimeMin === null) probs.add("missing/unparseable runtime");
    }
    return Array.from(probs);
  }

  function bestSingle(movieId, dayKey){
    const options = (byMovie.get(movieId) || []).filter(x => x.dateKey === dayKey);
    if (!options.length) return null;
    let best = null;
    for (const s of options){
      const c = (s.tRank * 1000) + (s.fRank * 100) + (s.startMin / 1000);
      if (!best || c < best.cost) best = { dayKey, entries: [s], cost: c };
    }
    return best;
  }

  function bestPair(aId, bId, dayKey){
    const A = (byMovie.get(aId) || []).filter(x => x.dateKey === dayKey);
    const B = (byMovie.get(bId) || []).filter(x => x.dateKey === dayKey);
    if (!A.length || !B.length) return null;

    const DIFF_THEATER_PENALTY = 5000;
    const TRANSIT_MIN = 35;

    let best = null;

    function consider(first, second){
      const same = first.theater === second.theater;
      const transit = same ? 0 : TRANSIT_MIN;
      const earliestSecond = first.endMin + transit;
      const gap = second.startMin - earliestSecond;
      if (gap < 0) return;

      const c =
        (same ? 0 : DIFF_THEATER_PENALTY) +
        ((first.tRank + second.tRank) * 1000) +
        ((first.fRank + second.fRank) * 100) +
        gap;

      const cand = { dayKey, entries: [first, second], cost: c };
      if (!best || cand.cost < best.cost) best = cand;
    }

    for (const a of A){
      for (const b of B){
        consider(a, b);
        consider(b, a);
      }
    }
    return best;
  }

  function partitions(ids){
    const res = [];
    function helper(remaining, acc){
      if (!remaining.length){ res.push(acc.map(g => g.slice())); return; }
      const [first, ...rest] = remaining;
      helper(rest, acc.concat([[first]]));
      for (let i = 0; i < rest.length; i++){
        const second = rest[i];
        const next = rest.slice(0,i).concat(rest.slice(i+1));
        helper(next, acc.concat([[first, second]]));
      }
    }
    helper(ids.slice().sort((a,b)=>a-b), []);
    return res;
  }

  function candidatesForGroup(group, dayKeys){
    const cands = [];
    if (group.length === 1){
      const id = group[0];
      for (const d of dayKeys){
        const s = bestSingle(id, d);
        if (s) cands.push(s);
      }
    } else {
      const [a,b] = group;
      for (const d of dayKeys){
        const p = bestPair(a, b, d);
        if (p) cands.push(p);
      }
    }
    cands.sort((x,y)=>x.cost - y.cost);
    return cands.slice(0, 10);
  }

  function pickBestPlan(selectedIds){
    const daySet = new Set();
    for (const id of selectedIds){
      for (const s of (byMovie.get(id) || [])) daySet.add(s.dateKey);
    }
    const dayKeys = Array.from(daySet).sort();

    const parts = partitions(selectedIds);
    let best = null;

    for (const groups of parts){
      const groupCands = groups.map(g => candidatesForGroup(g, dayKeys));
      if (groupCands.some(arr => !arr.length)) continue;

      function assign(i, usedDays, picked, totalCost){
        if (i === groups.length){
          const costWithDayCount = totalCost + (groups.length * 50);
          if (!best || costWithDayCount < best.cost){
            best = { cost: costWithDayCount, picked: picked.slice() };
          }
          return;
        }
        for (const cand of groupCands[i]){
          if (usedDays.has(cand.dayKey)) continue;
          usedDays.add(cand.dayKey);
          picked.push(cand);
          assign(i+1, usedDays, picked, totalCost + cand.cost);
          picked.pop();
          usedDays.delete(cand.dayKey);
        }
      }

      assign(0, new Set(), [], 0);
    }

    if (!best) return null;
    best.picked.sort((a,b)=>a.dayKey.localeCompare(b.dayKey));
    return best.picked;
  }

  function renderPlan(selectedIds, plan){
    const titles = selectedIds.map(id => `${id} — ${movieIndex.get(id) || "Unknown title"}`);

    let html = `<div class="movie-planner-card">
      <h3>Selected movies</h3>
      <div class="movie-planner-muted">${titles.join("<br>")}</div>
    </div>`;

    for (const day of plan){
      const entries = day.entries.slice().sort((a,b)=>a.startMin - b.startMin);
      const theaters = Array.from(new Set(entries.map(e => e.theater)));
      const theaterLine = theaters.length === 1 ? theaters[0] : theaters.join(" → ");

      html += `<div class="movie-planner-card">
        <h3>${day.dayKey} — ${theaterLine}</h3>
        <div class="movie-planner-muted">End time = start + runtime + 30 min. ${theaters.length>1 ? "Includes 35 min transit buffer." : ""}</div>
        <ol>`;

      for (const e of entries){
        html += `<li>
          <b>${e.movie}</b> — ${e.format || "Standard"}<br>
          <span class="movie-planner-muted">${e.theater}</span><br>
          <b>${minutesToLabel(e.startMin)}</b> → <b>${minutesToLabel(e.endMin)}</b>
          <span class="movie-planner-muted">(runtime ${e.runtimeMin}m + 30m buffer)</span>
        </li>`;
      }

      html += `</ol></div>`;
    }

    outEl.innerHTML = html;
  }

  btnEl.addEventListener('click', () => {
    const ids = parseSelectedIds();

    if (ids.length < 2 || ids.length > 4){
      outEl.innerHTML = `<div class="movie-planner-card">
        <h3>Enter 2–4 movie numbers</h3>
        <div class="movie-planner-muted">Example: <code>2, 5, 9</code></div>
      </div>`;
      return;
    }

    const missing = ids.filter(id => !movieIndex.has(id));
    if (missing.length){
      outEl.innerHTML = `<div class="movie-planner-card">
        <h3>Unknown movie numbers</h3>
        <div class="movie-planner-muted">Not found in the table: <b>${missing.join(", ")}</b></div>
      </div>`;
      return;
    }

    const noUsable = ids.filter(id => !(byMovie.get(id) || []).length);
    if (noUsable.length){
      const lines = noUsable.map(id => {
        const why = explainWhyNoUsableShowings(id);
        return `<li><b>${id} — ${movieIndex.get(id)}</b><br><span class="movie-planner-muted">${why.join(", ")}</span></li>`;
      }).join("");
      outEl.innerHTML = `<div class="movie-planner-card">
        <h3>Can’t plan some selections (missing/unclear data)</h3>
        <div class="movie-planner-muted">These movies are in the table, but I couldn’t parse usable showtimes:</div>
        <ul>${lines}</ul>
      </div>`;
      return;
    }

    const plan = pickBestPlan(ids);
    if (!plan){
      outEl.innerHTML = `<div class="movie-planner-card">
        <h3>No feasible plan found</h3>
        <div class="movie-planner-muted">
          I couldn’t find showtimes that fit the constraints. Try different movie numbers or verify date/start/runtime are present.
        </div>
      </div>`;
      return;
    }

    renderPlan(ids, plan);
  });
})();
"""

# --- Robust row inference helpers (NEW) ---

TIME_AMPM_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
TIME_AP_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\b", re.I)   # "7p"
TIME24_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?\b")

DATE_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
DATE_SLASH_RE = re.compile(r"\b(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b")

def norm_header(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def pick_col(headers_norm: list[str], candidates: list[str]) -> int | None:
    for cand in candidates:
        c = norm_header(cand)
        for i, h in enumerate(headers_norm):
            if c and c in h:
                return i
    return None

def runtime_to_minutes(text: str | None) -> int | None:
    if not text:
        return None
    t = text.strip().lower()

    mh = re.search(r"(\d+)\s*h", t)
    mm = re.search(r"(\d+)\s*m", t)
    if mh:
        return int(mh.group(1)) * 60 + (int(mm.group(1)) if mm else 0)

    m = re.search(r"(\d+)\s*:\s*(\d{2})", t)
    if m:
        return int(m.group(1))*60 + int(m.group(2))

    nums = re.findall(r"\d+", t)
    if not nums:
        return None
    return int(nums[0])

def extract_times(text: str) -> list[str]:
    if not text:
        return []
    s = text.replace("\u00a0", " ").replace("\u202f", " ")

    out: list[str] = []

    # 7p / 7:10p
    for h, m, ap in TIME_AP_RE.findall(s):
        hh = int(h)
        mm = int(m) if m else 0
        apu = "AM" if ap.lower() == "a" else "PM"
        out.append(f"{hh}:{mm:02d} {apu}")

    # 7:05 PM
    for h, m, ap in TIME_AMPM_RE.findall(s):
        hh = int(h)
        mm = int(m) if m else 0
        out.append(f"{hh}:{mm:02d} {ap.upper()}")

    # 24h
    for h, m, _sec in TIME24_RE.findall(s):
        out.append(f"{int(h):02d}:{int(m):02d}")

    # de-dupe preserving order
    seen = set()
    uniq = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq

def infer_theater(all_text: str, cells_text: list[str]) -> str:
    # Strongest: exact preferred theaters
    low = all_text.lower()
    for t in PREFERRED_THEATERS:
        if t.lower() in low:
            return t

    # Any line/cell containing "AMC"
    for x in cells_text:
        if "amc" in x.lower():
            return x.strip()

    # Regex fallback: extract a chunk starting with AMC...
    m = re.search(r"\bAMC\b[^\n|]{3,80}", all_text, re.I)
    if m:
        return m.group(0).strip()
    return ""

def infer_format(all_text: str) -> str:
    if re.search(r"dolby", all_text, re.I):
        return "Dolby Cinema"
    if re.search(r"imax", all_text, re.I) and re.search(r"laser", all_text, re.I):
        return "IMAX with Laser"
    if re.search(r"\bimax\b", all_text, re.I):
        return "IMAX"
    return ""

def infer_date(all_text: str) -> str:
    m = DATE_ISO_RE.search(all_text)
    if m:
        return m.group(1)
    m = DATE_SLASH_RE.search(all_text)
    if m:
        return m.group(1)
    return ""

def infer_runtime_raw(all_text: str) -> str:
    # first try "123 min"
    m = re.search(r"\b(\d{2,3})\s*(min|mins|minute|minutes)\b", all_text, re.I)
    if m:
        return f"{m.group(1)} min"
    # then "2h 10m"
    m = re.search(r"\b(\d+)\s*h(?:\s*(\d+)\s*m)?\b", all_text, re.I)
    if m:
        h = m.group(1)
        mm = m.group(2)
        return f"{h}h {mm}m" if mm else f"{h}h"
    # then "2:10"
    m = re.search(r"\b(\d+)\s*:\s*(\d{2})\b", all_text)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return ""

def main() -> None:
    if not HTML_PATH.exists():
        print(f"ERROR: {HTML_PATH} not found", file=sys.stderr)
        sys.exit(1)

    soup = BeautifulSoup(HTML_PATH.read_text(encoding="utf-8"), "html.parser")

    table = soup.find("table", class_=re.compile(r"\bdataframe\b")) or soup.find("table")
    if not table:
        HTML_PATH.write_text(str(soup), encoding="utf-8")
        return

    # Handle both thead/tbody and "flat" tables
    thead = table.find("thead")
    tbody = table.find("tbody")

    if thead and tbody:
        header_row = thead.find("tr")
        body_rows = list(tbody.find_all("tr", recursive=False))
    else:
        rows = list(table.find_all("tr", recursive=False))
        if not rows:
            HTML_PATH.write_text(str(soup), encoding="utf-8")
            return
        header_row = rows[0]
        body_rows = rows[1:]

    header_cells = header_row.find_all(["th", "td"], recursive=False)
    headers = [c.get_text(" ", strip=True) for c in header_cells]
    headers_norm = [norm_header(h) for h in headers]

    movie_i = pick_col(headers_norm, ["movie", "title", "film", "name"])
    theater_i = pick_col(headers_norm, ["theater", "theatre", "cinema", "location", "venue"])
    format_i = pick_col(headers_norm, ["format", "presentation", "experience", "screen"])
    date_i = pick_col(headers_norm, ["date", "day"])
    start_i = pick_col(headers_norm, ["start time", "showtime", "showtimes", "start", "time"])
    runtime_i = pick_col(headers_norm, ["runtime", "duration", "length", "run time", "minutes"])

    already_numbered = bool(headers and headers[0].strip().lower() in ["#", "movie #", "no", "num"])

    movie_id_by_title: dict[str, int] = {}
    next_id = 1
    showtimes: list[dict] = []

    for tr in list(body_rows):
        cells = tr.find_all(["th", "td"], recursive=False)
        texts = [c.get_text("\n", strip=True) for c in cells]
        all_text = " | ".join([t for t in texts if t]).strip()

        # Rule #1: ignore Bay Street 16 even if it appears in any column
        if IGNORE_THEATER_SUBSTR.lower() in all_text.lower():
            tr.decompose()
            continue

        def get(idx: int | None) -> str:
            if idx is None:
                return ""
            return texts[idx] if 0 <= idx < len(texts) else ""

        title = (get(movie_i) or "").strip()
        theater = (get(theater_i) or "").strip()
        fmt = (get(format_i) or "").strip()
        date = (get(date_i) or "").strip()
        start_raw = (get(start_i) or "").strip()
        runtime_raw = (get(runtime_i) or "").strip()

        # If movie column detection failed, fall back to first "non-empty" text
        if not title:
            title = next((t for t in texts if t.strip()), "").strip()

        if not title:
            continue

        # Robust inference if columns weren't found / are blank
        if not theater:
            theater = infer_theater(all_text, texts)
        if not fmt:
            fmt = infer_format(all_text)
        if not date:
            date = infer_date(all_text)
        if not runtime_raw:
            runtime_raw = infer_runtime_raw(all_text)

        # Extract times from the "start" cell or whole row if needed
        times = extract_times(start_raw) if start_raw else []
        if not times:
            times = extract_times(all_text)

        if title not in movie_id_by_title:
            movie_id_by_title[title] = next_id
            next_id += 1
        mid = movie_id_by_title[title]

        # Add numbering column
        if not already_numbered:
            td = soup.new_tag("td")
            td.string = str(mid)
            tr.insert(0, td)

        runtime_min = runtime_to_minutes(runtime_raw)

        # Emit a row per time (if we found any)
        for start in times:
            showtimes.append({
                "movie_id": mid,
                "movie": title,
                "theater": theater,
                "format": fmt,
                "date": date,
                "start": start,
                "runtime_min": runtime_min,
            })

    if not already_numbered:
        th = soup.new_tag("th")
        th.string = "#"
        header_row.insert(0, th)

    # Build authoritative movie list
    movies = [{"movie_id": mid, "movie": title}
              for title, mid in sorted(movie_id_by_title.items(), key=lambda x: x[1])]

    # Remove old injected blocks
    for el_id in ["movie-planner", "showtimes-data", "movie-planner-style", "movie-planner-js"]:
        old = soup.find(id=el_id)
        if old:
            old.decompose()

    # Ensure viewport + styles in <head>
    head = soup.head
    if head:
        if not head.find("meta", attrs={"name": "viewport"}):
            meta = soup.new_tag("meta")
            meta.attrs["name"] = "viewport"
            meta.attrs["content"] = "width=device-width, initial-scale=1"
            head.append(meta)

        style = soup.new_tag("style", id="movie-planner-style")
        style.string = CSS
        head.append(style)

    body = soup.body or soup

    # Insert planner at top
    planner = BeautifulSoup(PLANNER_HTML, "html.parser")
    body.insert(0, planner)

    # Data blob for JS
    data_tag = soup.new_tag("script", id="showtimes-data", type="application/json")
    data_tag.string = json.dumps({
        "ignore_theater": IGNORE_THEATER_SUBSTR,
        "preferred_theaters": PREFERRED_THEATERS,
        "movies": movies,
        "showtimes": showtimes,
    }, ensure_ascii=False)

    js_tag = soup.new_tag("script", id="movie-planner-js")
    js_tag.string = JS

    body.append(data_tag)
    body.append(js_tag)

    HTML_PATH.write_text(str(soup), encoding="utf-8")

if __name__ == "__main__":
    main()
