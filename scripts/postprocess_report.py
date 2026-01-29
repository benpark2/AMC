#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.request import Request, urlopen
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError


HTML_PATH = Path("docs/index.html")

IGNORE_THEATER = "AMC Bay Street 16"
PREFERRED_THEATERS = [
    "AMC Tustin 14 @ The District",
    "AMC Woodbridge 5",
    "AMC Orange 30",
]

PLANNER_HTML = """
<div id="movie-planner" class="movie-planner">
  <h2>Watch Planner</h2>
  <p class="movie-planner-sub">
    Enter <b>2–4</b> movie numbers (from the <b>#</b> column), then click <b>Suggest plan</b>.
  </p>
  <div class="movie-planner-controls">
    <input id="movie-planner-input" type="text" placeholder="e.g., 3, 4, 5" />
    <button id="movie-planner-btn" type="button">Suggest plan</button>
  </div>
  <div id="movie-planner-output" class="movie-planner-output"></div>
</div>
"""

CSS = r"""
/* ========== Base variables / system font ========== */
:root{
  --font-ui: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue",
             Arial, "Noto Sans", "Liberation Sans", sans-serif;
  --font-code: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue",
               Arial, "Noto Sans", "Liberation Sans", sans-serif;

  --page-bg: #f6f7fb;
  --card-bg: #ffffff;
  --text: #0f172a;
  --muted: rgba(15,23,42,.72);

  /* One shared border color for BOTH inner gridlines and rounded outer border */
  --grid: rgba(15,23,42,0.22);
  --grid-soft: rgba(15,23,42,0.12);

  --shadow: 0 12px 30px rgba(15,23,42,0.08);
}

/* JupyterLab font vars (nbconvert templates use these) */
:root{
  --jp-ui-font-family: var(--font-ui);
  --jp-content-font-family: var(--font-ui);
  --jp-code-font-family: var(--font-code);
}

/* Global font enforcement (kills Consolas/monospace) */
html, body,
.jp-Notebook, .jp-RenderedHTMLCommon, .jp-RenderedMarkdown, .jp-RenderedText,
.jp-OutputArea-output, .jp-Cell, .jp-OutputArea, .jp-OutputPrompt, .jp-InputPrompt,
table, th, td, input, button, select, textarea {
  font-family: var(--font-ui) !important;
}

pre, code, kbd, samp, tt,
.jp-RenderedText pre, .jp-RenderedText code,
.jp-RenderedHTMLCommon pre, .jp-RenderedHTMLCommon code {
  font-family: var(--font-code) !important;
}

/* Generated timestamp */
.report-generated{
  font-size: 13px;
  color: var(--muted);
  margin: 0 0 12px 0;
}

/* ========== Page chrome ========== */
body{
  margin: 0 !important;
  background: var(--page-bg) !important;
  color: var(--text) !important;
  line-height: 1.55 !important;
}

/* Primary content card: nbconvert lab uses <main> */
main{
  max-width: 1100px;
  margin: 24px auto !important;
  padding: 22px !important;
  background: var(--card-bg) !important;
  border-radius: 18px !important;
  box-shadow: var(--shadow) !important;
}

/* Fallback for other templates */
.jp-NotebookPanel-notebook,
#notebook-container{
  max-width: 1100px;
  margin: 24px auto !important;
  padding: 22px !important;
  background: var(--card-bg) !important;
  border-radius: 18px !important;
  box-shadow: var(--shadow) !important;
}

/* If fallback containers are inside <main>, neutralize them to avoid double cards */
main .jp-NotebookPanel-notebook,
main #notebook-container{
  max-width: none !important;
  margin: 0 !important;
  padding: 0 !important;
  background: transparent !important;
  border-radius: 0 !important;
  box-shadow: none !important;
}

/* Notebook typography ONLY inside report content (not the planner) */
main h1, main h2, main h3,
.jp-NotebookPanel-notebook h1, .jp-NotebookPanel-notebook h2, .jp-NotebookPanel-notebook h3,
#notebook-container h1, #notebook-container h2, #notebook-container h3{
  letter-spacing: -0.02em !important;
  margin-top: 0.9em !important;
  margin-bottom: 0.4em !important;
}

main p,
.jp-NotebookPanel-notebook p,
#notebook-container p{
  margin: 0 0 0.9em 0 !important;
}

main hr,
.jp-NotebookPanel-notebook hr,
#notebook-container hr{
  border: 0 !important;
  border-top: 1px solid var(--grid-soft) !important;
  margin: 18px 0 !important;
}

a{ text-decoration: none !important; }
a:hover{ text-decoration: underline !important; }

/* Code blocks — still system-ui, but polished */
pre, code{
  background: rgba(15,23,42,0.04) !important;
  border-radius: 10px !important;
}
code{ padding: 0.15em 0.35em !important; }
pre{
  padding: 12px 14px !important;
  border: 1px solid var(--grid-soft) !important;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

/* Posters in Movie column */
.movie-cell-title{
  font-weight: 800;
}
.movie-poster-wrap{
  margin-top: 8px;
}
.movie-poster{
  width: 84px;
  max-width: 100%;
  border-radius: 12px;
  border: 1px solid var(--grid) !important;
  box-shadow: 0 10px 22px rgba(15,23,42,0.12);
  display: block;
  /* Added styles */
  cursor: pointer;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}
/* Hover effect to show it's clickable */
.movie-poster:hover {
  transform: scale(1.05);
  box-shadow: 0 14px 30px rgba(15,23,42,0.25);
  border-color: rgba(37,99,235,0.4) !important;
}
@media (max-width: 900px){
  .movie-poster{ width: 72px; }
}

/* ========== Table styling (rounded border matches inner gridlines) ========== */
/* Wrapper provides rounded corners + outer border (same color as cell lines) */
.table-wrap{
  border: 1px solid var(--grid) !important;
  border-radius: 14px !important;
  margin: 14px 0 !important;
  background: #fff !important;
  overflow: hidden !important;
}

/* Mobile: make wrapper the horizontal scroll container */
@media (max-width: 900px){
  .table-wrap{
    overflow-x: auto !important;
    -webkit-overflow-scrolling: touch;
  }
  .table-wrap table{
    width: max-content !important;
    min-width: 100% !important;
  }
}

/* Table itself */
.table-wrap table,
.table-wrap table.dataframe{
  width: 100% !important;
  border-collapse: collapse !important;
  border: 0 !important; /* prevent double outer border */
}

/* Cell gridlines (same darkness as outer rounded border) */
.table-wrap th, .table-wrap td{
  border: 1px solid var(--grid) !important;
  padding: 10px 12px !important;
  vertical-align: top !important;
}

/* Make wrapper border be the ONLY outer border:
   remove perimeter cell borders so corners don't look lighter/thicker. */
.table-wrap thead tr:first-child th{ border-top: 0 !important; }
.table-wrap tbody tr:last-child td{ border-bottom: 0 !important; }
.table-wrap tr th:first-child, .table-wrap tr td:first-child{ border-left: 0 !important; }
.table-wrap tr th:last-child, .table-wrap tr td:last-child{ border-right: 0 !important; }

/* Zebra + hover */
.table-wrap tbody tr:nth-child(odd){ background: rgba(15,23,42,0.02) !important; }
.table-wrap tbody tr:hover{ background: rgba(37,99,235,0.06) !important; }

/* Sticky header */
.table-wrap thead th{
  position: sticky !important;
  top: 0 !important;
  background: #fff !important;
  z-index: 2 !important;
}

/* Column polish for schema: #, Movie, RT_C/A, IMDB, Showtimes, Runtime */
.table-wrap th:nth-child(1), .table-wrap td:nth-child(1){
  text-align: center !important;
  width: 54px !important;
  white-space: nowrap !important;
}
/* Centered columns: #1 (#), #3 (RT_C/A), and #5 (Runtime) */
.table-wrap th:nth-child(3), .table-wrap td:nth-child(3),
.table-wrap th:nth-child(5), .table-wrap td:nth-child(5){
  text-align: center !important;
  white-space: nowrap !important;
}
/* Column #4 is now Showtimes */
.table-wrap td:nth-child(4){
  font-size: 14px !important;
  line-height: 1.35 !important;
}

/* ========== Watch Planner styling ========== */
#movie-planner{
  font-family: var(--font-ui) !important;
  max-width: 980px;
  margin: 16px auto;
  padding: 14px 16px;
  border: 1px solid var(--grid-soft);
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 8px 22px rgba(15,23,42,0.06);
}

#movie-planner h2{
  margin: 0 0 6px 0;
  font-size: 20px;
  font-weight: 800;
  letter-spacing: -0.02em;
}
#movie-planner .movie-planner-sub{
  margin: 0 0 10px 0;
  color: var(--muted);
}
#movie-planner .movie-planner-controls{
  display:flex;
  gap:10px;
  align-items:center;
  flex-wrap:wrap;
  margin:10px 0 8px 0;
}
#movie-planner .movie-planner-controls input{
  flex:1;
  min-width:220px;
  padding:10px 12px;
  border-radius:10px;
  border:1px solid rgba(15,23,42,0.18);
  background: #fff;
  outline: none;
}
#movie-planner .movie-planner-controls input:focus{
  border-color: rgba(37,99,235,0.45);
  box-shadow: 0 0 0 4px rgba(37,99,235,0.12);
}
#movie-planner .movie-planner-controls button{
  padding:10px 12px;
  border-radius:10px;
  border:1px solid rgba(15,23,42,0.18);
  background: rgba(37,99,235,0.10);
  cursor:pointer;
  font-weight: 700;
}
#movie-planner .movie-planner-controls button:hover{
  background: rgba(37,99,235,0.16);
}
#movie-planner .movie-planner-output{ margin-top: 10px; }
#movie-planner .movie-planner-card{
  border:1px solid var(--grid-soft);
  border-radius:12px;
  padding:12px;
  margin:10px 0;
  background: #fff;
}
#movie-planner .movie-planner-card h3{
  margin:0 0 6px 0;
  font-size:16px;
  font-weight: 800;
}
#movie-planner .movie-planner-muted{ color: var(--muted); }

img, svg, video, canvas { max-width: 100%; height: auto; }
"""


# JS expects payload.movies and payload.showtimes.
# Each showtime item must have: movie_id, movie, theater, format, date, start, runtime_min
JS = r"""
(() => {
  const dataEl = document.getElementById('showtimes-data');
  const inputEl = document.getElementById('movie-planner-input');
  const btnEl = document.getElementById('movie-planner-btn');
  const outEl = document.getElementById('movie-planner-output');
  if (!dataEl || !inputEl || !btnEl || !outEl) return;

  const payload = JSON.parse(dataEl.textContent || "{}");
  const raw = Array.isArray(payload.showtimes) ? payload.showtimes : [];
  const IGNORE = String(payload.ignore_theater || "AMC Bay Street 16").toLowerCase();

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
  function toISO(d){ return `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}`; }

  function parseDateKey(s){
    if (!s) return null;
    const str = String(s).trim();
    let m = str.match(/(\d{4}-\d{2}-\d{2})/);
    if (m) return m[1];
    m = str.match(/(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?/);
    if (m){
      const mm = pad2(parseInt(m[1],10)), dd = pad2(parseInt(m[2],10));
      let yy = m[3] ? parseInt(m[3],10) : (new Date()).getFullYear();
      if (yy < 100) yy = 2000 + yy;
      return `${yy}-${mm}-${dd}`;
    }
    const d = new Date(str);
    if (!isNaN(d.getTime())) return toISO(d);
    return null;
  }

  function parseTimeToMinutes(s){
    if (!s) return null;
    const str = String(s).replace(/\u00a0/g," ").replace(/\u202f/g," ").trim();

    // 10:40 am / 7 pm
    const ampm = Array.from(str.matchAll(/(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b/gi));
    if (ampm.length){
      const m = ampm[ampm.length-1];
      let h = parseInt(m[1],10);
      let min = m[2] ? parseInt(m[2],10) : 0;
      const ap = m[3].toLowerCase();
      if (h === 12) h = 0;
      if (ap === "pm") h += 12;
      return h*60 + min;
    }
    // 24h
    const hm = Array.from(str.matchAll(/\b([01]?\d|2[0-3]):([0-5]\d)\b/g));
    if (hm.length){
      const m = hm[hm.length-1];
      return parseInt(m[1],10)*60 + parseInt(m[2],10);
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
    m = s.match(/\b(\d{2,3})\b/);
    return m ? parseInt(m[1],10) : null;
  }

  function minutesToLabel(total){
    let m = total % (24*60); if (m < 0) m += 24*60;
    let h = Math.floor(m/60), min = m % 60;
    const ampm = h >= 12 ? "PM" : "AM";
    h = h % 12; if (h === 0) h = 12;
    return `${h}:${String(min).padStart(2,"0")} ${ampm}`;
  }

  // Movie index (authoritative)
  const movieIndex = new Map();
  const movies = Array.isArray(payload.movies) ? payload.movies : [];
  for (const m of movies){
    const id = Number(m.movie_id);
    const name = String(m.movie || "").trim();
    if (id && name && !movieIndex.has(id)) movieIndex.set(id, name);
  }

  // Normalize usable showings (Bay Street filtered already in Python, but keep guard)
  const showings = raw
    .filter(r => !String(r.theater||"").toLowerCase().includes(IGNORE))
    .map(r => {
      const movie_id = Number(r.movie_id);
      const movie = String(r.movie||"").trim();
      const theater = String(r.theater||"").trim();
      const format = String(r.format||"").trim();
      const dateKey = parseDateKey(r.date);
      const startMin = parseTimeToMinutes(r.start);
      const runtimeMin = parseRuntimeMin(r.runtime_min);
      if (!movie_id || !movie || !theater || !dateKey || startMin === null || runtimeMin === null) return null;
      const endMin = startMin + runtimeMin + 30; // runtime + 30 min buffer
      return { movie_id, movie, theater, format, dateKey, startMin, endMin, runtimeMin,
               tRank: theaterRank(theater), fRank: formatRank(format) };
    })
    .filter(Boolean);

  const byMovie = new Map();
  for (const s of showings){
    if (!byMovie.has(s.movie_id)) byMovie.set(s.movie_id, []);
    byMovie.get(s.movie_id).push(s);
  }

  function parseSelectedIds(){
    const parts = inputEl.value.split(/[^0-9]+/).map(x=>x.trim()).filter(Boolean).map(Number).filter(n=>n>0 && isFinite(n));
    return Array.from(new Set(parts));
  }

  function bestSingle(movieId, dayKey){
    const opts = (byMovie.get(movieId) || []).filter(x => x.dateKey === dayKey);
    if (!opts.length) return null;
    let best = null;
    for (const s of opts){
      const c = (s.tRank*1000) + (s.fRank*100) + (s.startMin/1000);
      if (!best || c < best.cost) best = { dayKey, entries:[s], cost:c };
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

      const c = (same ? 0 : DIFF_THEATER_PENALTY)
              + ((first.tRank + second.tRank) * 1000)
              + ((first.fRank + second.fRank) * 100)
              + gap;

      const cand = { dayKey, entries:[first, second], cost:c };
      if (!best || cand.cost < best.cost) best = cand;
    }

    for (const a of A) for (const b of B){ consider(a,b); consider(b,a); }
    return best;
  }

  function partitions(ids){
    const res = [];
    function helper(rem, acc){
      if (!rem.length){ res.push(acc.map(g=>g.slice())); return; }
      const [first, ...rest] = rem;
      helper(rest, acc.concat([[first]]));
      for (let i=0;i<rest.length;i++){
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
      for (const d of dayKeys){ const s = bestSingle(group[0], d); if (s) cands.push(s); }
    } else {
      for (const d of dayKeys){ const p = bestPair(group[0], group[1], d); if (p) cands.push(p); }
    }
    cands.sort((x,y)=>x.cost - y.cost);
    return cands.slice(0, 10);
  }

  function pickBestPlan(ids){
    const daySet = new Set();
    for (const id of ids) for (const s of (byMovie.get(id) || [])) daySet.add(s.dateKey);
    const dayKeys = Array.from(daySet).sort();

    const parts = partitions(ids);
    let best = null;

    for (const groups of parts){
      const groupCands = groups.map(g => candidatesForGroup(g, dayKeys));
      if (groupCands.some(a => !a.length)) continue;

      function assign(i, used, picked, cost){
        if (i === groups.length){
          // prefer fewer days slightly
          const finalCost = cost + (groups.length * 50);
          if (!best || finalCost < best.cost) best = { cost: finalCost, picked: picked.slice() };
          return;
        }
        for (const cand of groupCands[i]){
          if (used.has(cand.dayKey)) continue;
          used.add(cand.dayKey);
          picked.push(cand);
          assign(i+1, used, picked, cost + cand.cost);
          picked.pop();
          used.delete(cand.dayKey);
        }
      }
      assign(0, new Set(), [], 0);
    }

    if (!best) return null;
    best.picked.sort((a,b)=>a.dayKey.localeCompare(b.dayKey));
    return best.picked;
  }

  function renderPlan(ids, plan){
    const titles = ids.map(id => `${id} — ${movieIndex.get(id) || "Unknown title"}`);
    let html = `<div class="movie-planner-card"><h3>Selected movies</h3><div class="movie-planner-muted">${titles.join("<br>")}</div></div>`;

    for (const day of plan){
      const entries = day.entries.slice().sort((a,b)=>a.startMin - b.startMin);
      const theaters = Array.from(new Set(entries.map(e=>e.theater)));
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
      outEl.innerHTML = `<div class="movie-planner-card"><h3>Enter 2–4 movie numbers</h3><div class="movie-planner-muted">Example: <code>3, 4, 5</code></div></div>`;
      return;
    }
    const missing = ids.filter(id => !movieIndex.has(id));
    if (missing.length){
      outEl.innerHTML = `<div class="movie-planner-card"><h3>Unknown movie numbers</h3><div class="movie-planner-muted">Not found in the table: <b>${missing.join(", ")}</b></div></div>`;
      return;
    }
    const noUsable = ids.filter(id => !(byMovie.get(id) || []).length);
    if (noUsable.length){
      outEl.innerHTML = `<div class="movie-planner-card"><h3>No usable showtimes for:</h3><div class="movie-planner-muted">${noUsable.map(id => `${id} — ${movieIndex.get(id)}`).join("<br>")}</div></div>`;
      return;
    }

    const plan = pickBestPlan(ids);
    if (!plan){
      outEl.innerHTML = `<div class="movie-planner-card"><h3>No feasible plan found</h3><div class="movie-planner-muted">Try different numbers or dates.</div></div>`;
      return;
    }
    renderPlan(ids, plan);
  });
})();
"""

# ----------------- Parsing helpers for your showtimes blob -----------------

DATE_LINE_RE = re.compile(r"^[•\-\*]?\s*(\d{4}-\d{2}-\d{2})\s*:\s*(.+)$")
TIME_RE = re.compile(r"(\d{1,2}:\d{2}\s*(?:am|pm))", re.I)
BRACKET_RE = re.compile(r"\[([^\]]+)\]")

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
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d{2,3})\s*(min|mins|minute|minutes)\b", t)
    if m:
        return int(m.group(1))
    nums = re.findall(r"\d+", t)
    return int(nums[0]) if nums else None

def score_show_blob(text: str) -> int:
    low = (text or "").lower()
    return low.count("amc ") * 10 + low.count("2026-") * 5 + text.count("•") * 3

def parse_showtimes_blob(blob: str) -> list[dict]:
    """
    Parses the cell that contains multiple theater blocks like:

      AMC Bay Street 16
      • 2025-12-20: 10:40 am [Laser at AMC], ...
      AMC Orange 30
      • 2025-12-20: 8:30 am [Laser at AMC], ...

    Returns list of dicts: {theater, date, start, format}
    """
    if not blob:
        return []

    blob = blob.replace("•", "\n•")
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]

    showings: list[dict] = []
    current_theater: str | None = None

    for ln in lines:
        if ln.lower().startswith("amc "):
            current_theater = ln.strip()
            continue

        m = DATE_LINE_RE.match(ln)
        if not m or not current_theater:
            continue

        date = m.group(1)
        rest = m.group(2)
        parts = [p.strip() for p in rest.split(",") if p.strip()]

        for p in parts:
            tm = TIME_RE.search(p)
            if not tm:
                continue
            start = tm.group(1).strip().lower()
            fm = BRACKET_RE.search(p)
            fmt = fm.group(1).strip() if fm else ""
            showings.append({
                "theater": current_theater,
                "date": date,
                "start": start,
                "format": fmt,
            })

    return showings



def find_runtime_in_row(texts: list[str]) -> int | None:
    # prefer something like "1h 48m"
    for t in texts:
        if re.search(r"\b\d+\s*h\b", t, re.I) and re.search(r"\b\d+\s*m\b", t, re.I):
            r = runtime_to_minutes(t)
            if r:
                return r
    # then "108 min"
    for t in texts:
        if re.search(r"\b\d{2,3}\s*(min|mins|minute|minutes)\b", t, re.I):
            r = runtime_to_minutes(t)
            if r:
                return r
    return None
    
def _norm_col(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").strip().lower()).strip("_")

def _find_col_idx(headers: list[str], keys: list[str]) -> int | None:
    keys_norm = {_norm_col(k) for k in keys}
    for i, h in enumerate(headers):
        if _norm_col(h) in keys_norm:
            return i
    return None

def _fmt_score(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return s
    v = float(m.group(0))
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    # fallback: strip trailing zeros
    out = f"{v:.1f}".rstrip("0").rstrip(".")
    return out

def _clone_cell_as_td(soup: BeautifulSoup, cell) -> "bs4.element.Tag":
    td = soup.new_tag("td")
    if not cell:
        return td
    # copy attributes (useful for showtimes formatting)
    for k, v in getattr(cell, "attrs", {}).items():
        td.attrs[k] = v
    # move children (preserves <br>, bullets, etc.)
    for child in list(cell.contents):
        td.append(child)
    return td

def get_trailer_url(title: str) -> str:
    """Generates a YouTube search link for the movie trailer."""
    query = quote(f"{title} official trailer")
    return f"https://www.youtube.com/results?search_query={query}"

def apply_final_table_schema(soup: BeautifulSoup, table, movie_by_id: dict[int, str], poster_by_id: dict[int, str]) -> None:
    thead = table.find("thead")
    tbody = table.find("tbody")
    if not thead or not tbody:
        return

    header_row = thead.find("tr")
    if not header_row:
        return

    header_cells = header_row.find_all(["th", "td"], recursive=False)
    headers = [c.get_text(" ", strip=True) for c in header_cells]

    # Locate columns
    idx_hash = next((i for i, h in enumerate(headers) if h.strip() == "#"), None)
    idx_movie = _find_col_idx(headers, ["movie_title", "movie"])
    idx_rtca = _find_col_idx(headers, ["rt_c/a", "rt_c_a", "rt_c a", "rt_c/a "])
    idx_rt_critic = _find_col_idx(headers, ["rt_critic", "rt critic"])
    idx_rt_audience = _find_col_idx(headers, ["rt_audience", "rt audience"])
    idx_imdb = _find_col_idx(headers, ["imdb_rating", "imdb"])
    idx_showtimes = _find_col_idx(headers, ["showtimes", "showtime"])
    idx_runtime = _find_col_idx(headers, ["runtime"])

    # Rewrite header
    header_row.clear()
    for title in ["#", "Movie", "RT_C/A", "Showtimes", "Runtime"]:
        th = soup.new_tag("th")
        th.string = title
        header_row.append(th)

    # Process Rows
    for tr in tbody.find_all("tr", recursive=False):
        cells = tr.find_all(["th", "td"], recursive=False)

        def get_cell(i: int | None):
            return cells[i] if i is not None and 0 <= i < len(cells) else None

        # 1. Handle Movie ID (#)
        movie_id_str = (tr.get("data-movie-id") or 
                        (get_cell(idx_hash).get_text(" ", strip=True) if get_cell(idx_hash) else "")).strip()
        num_td = soup.new_tag("td")
        num_td.string = movie_id_str

        # 2. Handle Movie Title & Poster
        movie_td = soup.new_tag("td") # INITIALIZED HERE: Always exists for this row
        
        mid_int = None
        try:
            mid_int = int(movie_id_str)
        except:
            mid_int = None

        title = movie_by_id.get(mid_int, "") if mid_int is not None else ""
        if not title:
            title = (get_cell(idx_movie).get_text(" ", strip=True) if get_cell(idx_movie) else "Unknown")

        # Create Title Label
        title_div = soup.new_tag("div", **{"class": "movie-cell-title"})
        title_div.string = title
        movie_td.append(title_div)

        # Create Poster with Trailer Link
        poster_url = poster_by_id.get(mid_int) if mid_int is not None else None
        final_poster_url = poster_url if poster_url else "https://4ddig.tenorshare.com/images/photo-recovery/images-not-found.webp"
        
        if final_poster_url:
            wrap = soup.new_tag("div", **{"class": "movie-poster-wrap"})
            
            # Link to YouTube Trailer
            query = quote(f"{title} official trailer")
            trailer_url = f"https://www.youtube.com/results?search_query={query}"
            trailer_link = soup.new_tag("a", href=trailer_url, target="_blank", rel="noopener noreferrer")
            
            img = soup.new_tag("img", src=final_poster_url)
            img.attrs["class"] = "movie-poster"
            img.attrs["loading"] = "lazy"
            img.attrs["alt"] = f"{title} poster"
            
            trailer_link.append(img)
            wrap.append(trailer_link)
            movie_td.append(wrap)

        # 3. Handle Other Columns
        if get_cell(idx_rtca):
            rtca_txt = get_cell(idx_rtca).get_text(" ", strip=True)
        else:
            c = _fmt_score(get_cell(idx_rt_critic).get_text(" ", strip=True) if get_cell(idx_rt_critic) else "")
            a = _fmt_score(get_cell(idx_rt_audience).get_text(" ", strip=True) if get_cell(idx_rt_audience) else "")
            rtca_txt = f"{c}/{a}".strip("/")

        rtca_td = soup.new_tag("td")
        rtca_td.string = rtca_txt

        imdb_td = _clone_cell_as_td(soup, get_cell(idx_imdb))
        show_td = _clone_cell_as_td(soup, get_cell(idx_showtimes))
        runtime_td = _clone_cell_as_td(soup, get_cell(idx_runtime))

        # Rebuild row
        tr.clear()
        tr.append(num_td)
        tr.append(movie_td)
        tr.append(rtca_td)
        #tr.append(imdb_td)
        tr.append(show_td)
        tr.append(runtime_td)

def _fetch_json(url: str, timeout: int = 15) -> dict | None:
    try:
        req = Request(url, headers={"User-Agent": "movie-report-bot/1.0"})
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None

def wikipedia_thumbnail_url(title: str) -> str | None:
    """
    Best-effort poster/thumbnail via Wikipedia.
    1) Try page summary directly for title and common film suffixes
    2) Fall back to MediaWiki search to pick the best page, then summary
    """

    # 1. Clean the title: Remove trailing space + anything in parentheses
    # Pattern explanation: \s* matches whitespace, \(.*?\) matches () and content inside
    clean_title = re.sub(r'\s*\(.*?\)', '', title).strip()

    
    def summary_thumb(page_title: str) -> str | None:
        t = quote(page_title.replace(" ", "_"))
        data = _fetch_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{t}")
        if not data:
            return None
        thumb = (data.get("thumbnail") or {}).get("source")
        return thumb

    candidates = [
        f"{clean_title} (2026 film)",
        f"{clean_title} (2025 film)",
        f"{clean_title} (2024 film)",
        f"{clean_title} (film)",
        clean_title,
    ]
    for c in candidates:
        thumb = summary_thumb(c)
        if thumb:
            return thumb

    # Search fallback (helps with titles that don’t match the exact page name)
    params = urlencode({
        "action": "query",
        "list": "search",
        "srsearch": f"{clean_title} film",
        "format": "json",
        "srlimit": 1,
    })
    data = _fetch_json(f"https://en.wikipedia.org/w/api.php?{params}")
    try:
        hits = data["query"]["search"]
        if hits:
            return summary_thumb(hits[0]["title"])
    except Exception:
        pass

    return None

def main() -> None:
    from datetime import datetime, timezone
    
    if not HTML_PATH.exists():
        print(f"ERROR: {HTML_PATH} not found", file=sys.stderr)
        sys.exit(1)

    soup = BeautifulSoup(HTML_PATH.read_text(encoding="utf-8"), "html.parser")

    # Set browser tab title
    desired_title = "Weekend Movies"
    if soup.title:
        soup.title.string = desired_title
    elif soup.head:
        t = soup.new_tag("title")
        t.string = desired_title
        soup.head.append(t)

    table = soup.find("table", class_=re.compile(r"\bdataframe\b")) or soup.find("table")
    if not table:
        HTML_PATH.write_text(str(soup), encoding="utf-8")
        return

    thead = table.find("thead")
    tbody = table.find("tbody")
    if not thead or not tbody:
        HTML_PATH.write_text(str(soup), encoding="utf-8")
        return

    header_row = thead.find("tr")
    header_cells = header_row.find_all(["th", "td"], recursive=False)
    headers = [c.get_text(" ", strip=True) for c in header_cells]

    already_numbered = bool(headers and headers[0].strip().lower() in ["#", "movie #", "no", "num"])

    movie_by_id: dict[int, str] = {}
    showtimes: list[dict] = []

    rows = list(tbody.find_all("tr", recursive=False))
    next_id = 1

    for tr in rows:
        cells = tr.find_all(["th", "td"], recursive=False)
        texts = [c.get_text("\n", strip=True) for c in cells]

        # Determine movie_id and title
        mid: int | None = None
        if already_numbered and texts:
            try:
                mid = int(str(texts[0]).strip())
            except Exception:
                mid = None

        if mid is None:
            mid = next_id
            next_id += 1

        tr["data-movie-id"] = str(mid)
        
        # title is usually the 2nd cell if numbered; else first non-empty
        title = ""
        if already_numbered and len(texts) >= 2:
            title = texts[1].strip()
        if not title:
            title = next((t for t in texts if t.strip()), "").strip()

        if not title:
            continue

        movie_by_id[mid] = title

        # runtime is in its own column in your row (e.g., "1h 48m")
        runtime_min = find_runtime_in_row(texts)

        # Find the showtimes blob cell: the one with the most "AMC" / bullets / dates
        best_i = None
        best_score = -1
        for i, t in enumerate(texts):
            s = score_show_blob(t)
            if s > best_score:
                best_score = s
                best_i = i
        blob = texts[best_i] if best_i is not None else ""

        parsed_showings = parse_showtimes_blob(blob)


        # Emit showtimes rows
        for sh in parsed_showings:
            # Ignore is already removed by parser; keep guard
            if IGNORE_THEATER.lower() in sh["theater"].lower():
                continue
            showtimes.append({
                "movie_id": mid,
                "movie": title,
                "theater": sh["theater"],
                "format": sh["format"],
                "date": sh["date"],
                "start": sh["start"],
                "runtime_min": runtime_min,
            })

        # Add numbering column if missing
        if not already_numbered:
            td = soup.new_tag("td")
            td.string = str(mid)
            tr.insert(0, td)

    if not already_numbered:
        th = soup.new_tag("th")
        th.string = "#"
        header_row.insert(0, th)

    # Fetch poster thumbs once per movie (best-effort, cached)
    poster_by_id: dict[int, str] = {}
    for mid, title in movie_by_id.items():
        poster = wikipedia_thumbnail_url(title)
        if poster:
            poster_by_id[mid] = poster

    apply_final_table_schema(soup, table, movie_by_id, poster_by_id)

    
    # Build movies list (authoritative for input validation)
    movies = [{"movie_id": mid, "movie": movie_by_id[mid]} for mid in sorted(movie_by_id.keys())]

    # Remove old injected blocks
    for el_id in ["movie-planner", "showtimes-data", "movie-planner-style", "movie-planner-js"]:
        old = soup.find(id=el_id)
        if old:
            old.decompose()

    """ repetitive
    # Fetch poster thumbs once per movie (best-effort, cached)
    poster_by_id: dict[int, str] = {}
    for mid, title in movie_by_id.items():
        poster = wikipedia_thumbnail_url(title)
        if poster:
            poster_by_id[mid] = poster
            """

    # Wrap table so we can give it rounded corners + a clean border
    parent = table.parent
    if not (parent and parent.name == "div" and "table-wrap" in (parent.get("class") or [])):
        wrap = soup.new_tag("div", **{"class": "table-wrap"})
        table.wrap(wrap)

    
    # Ensure viewport + styles in <head>
    if soup.head:
        if not soup.head.find("meta", attrs={"name": "viewport"}):
            meta = soup.new_tag("meta")
            meta.attrs["name"] = "viewport"
            meta.attrs["content"] = "width=device-width, initial-scale=1"
            soup.head.append(meta)

        style = soup.new_tag("style", id="movie-planner-style")
        style.string = CSS
        soup.head.append(style)

    # Pick a container (prefer <main> so it appears inside the card)
    container = soup.find("main") or soup.body or soup

    # Remove any prior injected timestamp
    old_ts = soup.find(id="report-generated")
    if old_ts:
        old_ts.decompose()

    # Build a readable timestamp (local + UTC)
    now_utc = datetime.now(timezone.utc)
    label_utc = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    label_local = None
    try:
        from zoneinfo import ZoneInfo
        la = ZoneInfo("America/Los_Angeles")
        label_local = now_utc.astimezone(la).strftime("%Y-%m-%d %I:%M %p %Z")
    except Exception:
        pass

    ts_text = f"Report generated: {label_local} / {label_utc}" if label_local else f"Report generated: {label_utc}"

    ts_div = soup.new_tag("div", id="report-generated", **{"class": "report-generated"})
    ts_div.string = ts_text

    # Put it at the very top of the container
    container.insert(0, ts_div)

    
    body = soup.body or soup
    #old code: body.insert(0, BeautifulSoup(PLANNER_HTML, "html.parser"))
    container.insert(1, BeautifulSoup(PLANNER_HTML, "html.parser"))

    data_tag = soup.new_tag("script", id="showtimes-data", type="application/json")
    data_tag.string = json.dumps({
        "ignore_theater": IGNORE_THEATER,
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
