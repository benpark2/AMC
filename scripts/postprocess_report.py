#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.request import Request, urlopen
from urllib.parse import quote
from urllib.error import HTTPError, URLError

from scripts.movie_titles import wikipedia_title_candidates


HTML_PATH = Path("docs/index.html")

NOISY_OUTPUT_PATTERNS = [
    re.compile(r"Preparing IMDb dataset lookup for", re.I),
    re.compile(r"^\[INFO\]\s+AMC returned a JS/queue page", re.I),
    re.compile(r"^\[WARN\]\s+Browser fallback failed", re.I),
    re.compile(r"Playwright Sync API inside the asyncio loop", re.I),
    re.compile(r"Please use the Async API instead", re.I),
]

def _all_lines_are_noisy(lines: list[str]) -> bool:
    if not lines:
        return False
    for line in lines:
        if not any(rx.search(line) for rx in NOISY_OUTPUT_PATTERNS):
            return False
    return True

def remove_noisy_output(soup: BeautifulSoup) -> None:
    """Remove captured scraper INFO/WARN lines from public HTML only."""
    diagnostic_rx = re.compile(r"^\s*\[(?:INFO|WARN)\](?:\s|$)", re.I)

    for tag in list(soup.find_all("pre")):
        raw = tag.get_text("\n", strip=False)
        lines = raw.splitlines(keepends=True)
        if not lines:
            continue

        kept = [line for line in lines if not diagnostic_rx.search(line)]
        if len(kept) == len(lines):
            continue

        cleaned = "".join(kept)
        if cleaned.strip():
            tag.clear()
            tag.append(cleaned)
            continue

        container = tag
        for parent in tag.parents:
            classes = parent.get("class", []) if getattr(parent, "attrs", None) else []
            if any(cls in {
                "jp-OutputArea-child", "jp-OutputArea-output", "jp-RenderedText",
                "output_area", "output_subarea",
            } for cls in classes):
                container = parent
                break
        container.decompose()

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

/* Visible only when the live AMC request was blocked/too sparse and the
   notebook reused a recent successful report for the exact same weekend. */
.amc-cache-notice{
  font-size: 13px;
  color: var(--text);
  margin: 26px 0 4px 0;
  padding: 10px 12px;
  border: 1px solid var(--grid);
  border-radius: 10px;
  background: rgba(15,23,42,0.04);
}
.amc-cache-notice summary{cursor:pointer;font-weight:600;}
.amc-cache-notice p{margin:8px 0 0;line-height:1.5;}

/* Lightweight per-theatre/date provenance, with text rendered via CSS so the
   lossless previous-report showtime parser still sees the original date lines. */
.showtime-fetch-date::after{
  content: attr(data-label);
  margin-left: 6px;
  font-size: 10px;
  line-height: 1.2;
  color: var(--muted);
  white-space: nowrap;
}
.showtime-fetch-date[data-source="old"]::after{font-weight: 650;}
.showtime-fetch-date[data-source="scenef"]::after{font-weight: 600;}

/* A showtime's source can vary by theatre/date even within one movie. The
   compact per-movie label reports that mixture without implying that the
   report-generated timestamp is a fresh AMC retrieval time. */
.showtimes-freshness{
  display: block;
  margin-top: 7px;
  font-size: 11px;
  line-height: 1.4;
  color: var(--muted);
  font-weight: 500;
}
.showtimes-freshness[data-source="cached"],
.showtimes-freshness[data-source="mixed"]{
  font-weight: 650;
  color: var(--text);
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

/* Column polish for schema: #, Movie, RT_C/A, IMDb, Showtimes, Runtime */
.table-wrap th:nth-child(1), .table-wrap td:nth-child(1){
  text-align: center !important;
  width: 54px !important;
  white-space: nowrap !important;
}
/* Centered columns: #1 (#), #3 (RT_C/A), #4 (IMDb), and #6 (Runtime) */
.table-wrap th:nth-child(3), .table-wrap td:nth-child(3),
.table-wrap th:nth-child(4), .table-wrap td:nth-child(4),
.table-wrap th:nth-child(6), .table-wrap td:nth-child(6){
  text-align: center !important;
  white-space: nowrap !important;
}
/* Column #5 is Showtimes */
.table-wrap td:nth-child(5){
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
    """Score likely showtime-table blobs without assuming a calendar year."""
    raw = text or ""
    low = raw.lower()
    iso_date_count = len(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", raw))
    return low.count("amc ") * 10 + iso_date_count * 5 + raw.count("•") * 3

def parse_showtimes_blob(blob: str) -> list[dict]:
    """
    Parses the cell that contains multiple theater blocks like:

      AMC Bay Street 16
      • 2025-12-20: 10:40 am [Laser at AMC], ...
      AMC Orange 30
      • 2025-12-20: 8:30 am [Laser at AMC], ...

    Returns list of dicts: {theater, date, start, format, a_list_excluded}

    The exclusion flag is read from the visible ⛔ marker already emitted by
    the notebook. Serializing it into showtimes-data makes the previous-report
    fallback lossless on future runs while preserving compatibility with older
    reports that did not include the field.
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
                "a_list_excluded": "⛔" in p,
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
    if not s or s.lower() in {"none", "nan", "null", "n/a"}:
        return ""
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return "" if s.lower() in {"none", "nan", "null", "n/a"} else s
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


def _linked_score_td(soup: BeautifulSoup, text: str, url: str | None) -> "bs4.element.Tag":
    """Render a score while preserving a resolved provider identity URL.

    New/repertory titles often have a real provider page before a numeric score
    is published.  The old report discarded that URL whenever ``label`` was
    blank, which also deprived the poster/audience enrichment pass of a strong
    identity bridge.  Keep it as inert metadata without changing the visible
    table; a clickable link is still shown only when a score exists.
    """
    td = soup.new_tag("td")
    label = (text or "").strip()
    href = (url or "").strip() if isinstance(url, str) else ""
    if href.startswith("http"):
        td["data-source-url"] = href
    if href.startswith("http") and label:
        a = soup.new_tag("a", href=href, target="_blank", rel="noopener noreferrer")
        a.string = label
        td.append(a)
    else:
        td.string = label
    return td

def get_trailer_url(title: str) -> str:
    """Generates a YouTube search link for the movie trailer."""
    query = quote(f"{title} official trailer")
    return f"https://www.youtube.com/results?search_query={query}"

def read_cached_showtime_combos(marker_path: Path) -> set[tuple[str, str]]:
    """Read exact theatre/date source records; do not infer cache from movie title.

    The marker is written ONLY when the notebook recovered an entire combination
    from an earlier published report. A previous report may itself contain older
    cache, so its generation timestamp is not a trustworthy AMC retrieval time.
    """
    if not marker_path.is_file():
        return set()
    combos = set()
    for line in marker_path.read_text(encoding="utf-8", errors="replace").splitlines():
        theater, sep, date_text = line.rpartition(" | ")
        if sep and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text.strip()):
            combos.add((" ".join(theater.split()).casefold(), date_text.strip()))
    return combos


def _combo_key(theater: str, date_text: str) -> tuple[str, str]:
    """Canonicalize theater spelling for provenance across report generations."""
    theater = str(theater or "").replace("@", " at ").casefold()
    return (re.sub(r"[^a-z0-9]+", " ", theater).strip(), str(date_text or "").strip())


def _safe_fetch_date(value: object, today: str) -> str | None:
    """Validate inherited fetch dates instead of inventing timestamps."""
    raw = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) or raw > today:
        return None
    try:
        from datetime import date
        date.fromisoformat(raw)
    except ValueError:
        return None
    return raw


def read_previous_fetch_dates(report_path: Path, today: str) -> dict[tuple[str, str], str]:
    """Only exact theater/date provenance from a previously generated report.

    Older HTML files have no provenance, and deliberately become 'Old'. Never
    use the previous report's generation timestamp as a fetch timestamp.
    """
    if not report_path.is_file():
        return {}
    try:
        old_soup = BeautifulSoup(report_path.read_text(encoding="utf-8", errors="replace"), "html.parser")
        node = old_soup.find("script", id="showtimes-data")
        payload = json.loads(node.string or node.get_text() or "{}") if node else {}
        records = payload.get("amc_fetch_dates") or []
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(records, list):
        return {}
    out = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        key = _combo_key(rec.get("theater"), rec.get("date"))
        date_text = _safe_fetch_date(rec.get("fetched_on"), today)
        if key[0] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", key[1]) and date_text:
            out[key] = date_text
    return out


def read_workflow_fetch_dates(directory: Path, today: str) -> dict[tuple[str, str], str]:
    """Read dates written only after the browser yielded new, usable rows."""
    records = {}
    if not directory.is_dir():
        return records
    for path in directory.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        key = _combo_key(rec.get("theater"), rec.get("date"))
        value = _safe_fetch_date(rec.get("fetched_on"), today)
        if key[0] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", key[1]) and value:
            records[key] = value
    return records


def resolve_showtime_fetch_dates(
    showtimes: list[dict], cached_combos: set[tuple[str, str]],
    previous: dict[tuple[str, str], str], workflow: dict[tuple[str, str], str],
) -> dict[tuple[str, str], str | None]:
    """Live fetch wins; reused previous rows keep their *original* known date.

    All missing dates remain None ('Old'), including the first v47 run's cache
    recovered from a v46 report. Avoid accidentally backdating old schedules.
    """
    cached = {_combo_key(t, d) for t, d in cached_combos}
    result = {}
    for sh in showtimes:
        key = _combo_key(sh.get("theater"), sh.get("date"))
        if not key[0] or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key[1]):
            continue
        if key in cached:
            result[key] = previous.get(key)
        else:
            result[key] = workflow.get(key)  # never claim a fetch date absent live evidence
    return result


def read_scenef_provenance(directory: Path, today: str) -> dict[tuple[str, str], dict]:
    """Per-combination proof of SceneF acceptance from this Papermill run."""
    result = {}
    if not directory.is_dir():
        return result
    for path in directory.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(rec, dict) or rec.get("provider") != "scenef":
            continue
        key = _combo_key(rec.get("theater"), rec.get("date"))
        verified = rec.get("verified_at")
        try:
            from datetime import datetime
            date_value = datetime.fromisoformat(str(verified).replace("Z", "+00:00")).date().isoformat()
        except (ValueError, TypeError, AttributeError):
            continue
        if not key[0] or not _safe_fetch_date(date_value, today):
            continue
        url = str(rec.get("scenef_url") or "")
        if not url.startswith("https://scenef.com/"):
            url = "https://scenef.com/week?region=orangecounty"
        result[key] = {
            "provider": "scenef",
            "verified_on": date_value,
            "fetched_on": None,  # Do not claim a third-party fetch was AMC.
            "scenef_fetched_on": _safe_fetch_date(rec.get("fetched_on"), today),
            "scenef_url": url,
        }
    return result


def read_previous_scenef_provenance(report_path: Path, today: str) -> dict[tuple[str, str], dict]:
    """Only preserve SceneF origin when an exact previous-report slot is reused."""
    if not report_path.is_file():
        return {}
    try:
        doc = BeautifulSoup(report_path.read_text(encoding="utf-8"), "html.parser")
        tag = doc.find("script", id="showtimes-data")
        payload = json.loads(tag.string or tag.get_text() or "{}") if tag else {}
    except (OSError, ValueError, TypeError):
        return {}
    result = {}
    for rec in payload.get("amc_fetch_dates") or []:
        if not isinstance(rec, dict) or rec.get("provider") != "scenef":
            continue
        key = _combo_key(rec.get("theater"), rec.get("date"))
        verified = _safe_fetch_date(rec.get("verified_on"), today)
        if not key[0] or not verified:
            continue
        url = str(rec.get("scenef_url") or "")
        result[key] = {
            "provider": "scenef", "verified_on": verified,
            "fetched_on": None,
            "scenef_fetched_on": _safe_fetch_date(rec.get("scenef_fetched_on"), today),
            "scenef_url": url if url.startswith("https://scenef.com/") else "https://scenef.com/week?region=orangecounty",
        }
    return result


def resolve_scenef_provenance(
    showtimes: list[dict], cached_combos: set[tuple[str, str]],
    previous: dict[tuple[str, str], dict], current: dict[tuple[str, str], dict],
    live_dates: dict[tuple[str, str], str],
) -> dict[tuple[str, str], dict]:
    """AMC live takes precedence, then accepted SceneF, then old SceneF."""
    cached = {_combo_key(t, d) for t, d in cached_combos}
    valid = {_combo_key(x.get("theater"), x.get("date")) for x in showtimes}
    result = {}
    for key in valid:
        if key in live_dates:
            continue
        if key in current:
            result[key] = current[key]
        elif key in cached and key in previous:
            result[key] = previous[key]
    return result


def label_showtime_fetch_dates(
    soup: BeautifulSoup, show_td, fetch_dates: dict[tuple[str, str], str | None],
    scenef_provenance: dict[tuple[str, str], dict] | None = None,
) -> None:
    """Attach a label after *each* theatre/date showtime line without changing its text.

    The CSS-generated wording and aria-label keep original showtime table text
    intact for the next run's cached-row/A-List parser.
    """
    from bs4 import NavigableString
    theater = None
    for node in list(show_td.contents):
        if not isinstance(node, NavigableString):
            continue
        raw = str(node).strip()
        if raw.casefold().startswith("amc "):
            theater = raw
            continue
        match = DATE_LINE_RE.match(raw)
        if not match or not theater:
            continue
        key = _combo_key(theater, match.group(1))
        fetched_on = fetch_dates.get(key)
        marker = soup.new_tag("span", **{"class": "showtime-fetch-date"})
        sf = (scenef_provenance or {}).get(key)
        if sf:
            verified = sf["verified_on"]
            marker["data-source"] = "scenef"
            fetched = sf.get("scenef_fetched_on")
            marker["data-label"] = (
                f" · SceneF fetched {fetched}, verified {verified} (third-party)"
                if fetched else f" · SceneF verified {verified} (third-party)"
            )
            marker["aria-label"] = f"Third-party showtimes from SceneF, verified {verified}; AMC A-List status unverified"
        else:
            marker["data-source"] = "known" if fetched_on else "old"
            marker["data-label"] = f" · Fetched {fetched_on}" if fetched_on else " · Old (fetch date unknown)"
            marker["aria-label"] = f"AMC fetched on {fetched_on}" if fetched_on else "AMC fetch date unknown; old schedule"
        node.insert_after(marker)


def movie_showtime_freshness(showtimes: list[dict], cached_combos: set[tuple[str, str]]) -> dict[int, tuple[str, str, str]]:
    """Compute per-film provenance at theatre/date granularity, not per row.

    One combination may contain many different showtimes. The prior-report
    marker proves only *source*, not the actual time those rows were fetched.
    Runner-local reuse from an earlier Papermill attempt belongs to this same
    workflow and is not a previous-publication fallback.
    """
    by_movie: dict[int, set[tuple[str, str]]] = {}
    for item in showtimes:
        try:
            movie_id = int(item["movie_id"])
        except (ValueError, TypeError, KeyError):
            continue
        theater = " ".join(str(item.get("theater") or "").split()).casefold()
        date_text = str(item.get("date") or "").strip()
        if theater and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
            by_movie.setdefault(movie_id, set()).add((theater, date_text))

    status = {}
    for movie_id, combos in by_movie.items():
        cached = combos & cached_combos
        if not cached:
            status[movie_id] = ("fresh", "Fresh this run", "Showtime schedules were retrieved during this workflow")
        elif len(cached) == len(combos):
            status[movie_id] = (
                "cached", "Cached from previous report",
                "All displayed theatre/date schedules were carried forward; original AMC fetch time is unknown",
            )
        else:
            status[movie_id] = (
                "mixed", f"Partly cached ({len(cached)}/{len(combos)} theatre/dates)",
                "Some schedules were retrieved this run; others were carried forward from a prior report",
            )
    return status


def mark_scenef_showtime_freshness(
    showtimes: list[dict], freshness_by_movie: dict[int, tuple[str, str, str]],
    scenef_provenance: dict[tuple[str, str], dict],
) -> dict[int, tuple[str, str, str]]:
    """Never describe third-party schedules as fresh AMC showtimes."""
    by_movie: dict[int, set[tuple[str, str]]] = {}
    for row in showtimes:
        try:
            movie_id = int(row["movie_id"])
        except (TypeError, ValueError, KeyError):
            continue
        by_movie.setdefault(movie_id, set()).add(_combo_key(row.get("theater"), row.get("date")))
    updated = dict(freshness_by_movie)
    for movie_id, combos in by_movie.items():
        from_sf = combos & scenef_provenance.keys()
        if not from_sf:
            continue
        if len(from_sf) == len(combos):
            updated[movie_id] = (
                "scenef", "SceneF third-party showtimes",
                "All displayed schedules came from SceneF, not directly from AMC; A-List eligibility unverified",
            )
        else:
            updated[movie_id] = (
                "mixed", f"Includes SceneF ({len(from_sf)}/{len(combos)} theater/dates)",
                "Some schedules came from SceneF rather than AMC; third-party A-List eligibility unverified",
            )
    return updated


def apply_final_table_schema(
    soup: BeautifulSoup, table, movie_by_id: dict[int, str], poster_by_id: dict[int, str],
    showtime_freshness: dict[int, tuple[str, str, str]] | None = None,
    fetch_dates: dict[tuple[str, str], str | None] | None = None,
    scenef_provenance: dict[tuple[str, str], dict] | None = None,
) -> None:
    thead = table.find("thead")
    tbody = table.find("tbody")
    if not thead or not tbody:
        return

    header_row = thead.find("tr")
    if not header_row:
        return

    remove_noisy_output(soup)

    header_cells = header_row.find_all(["th", "td"], recursive=False)
    headers = [c.get_text(" ", strip=True) for c in header_cells]

    # Locate columns
    idx_hash = next((i for i, h in enumerate(headers) if h.strip() == "#"), None)
    idx_movie = _find_col_idx(headers, ["movie_title", "movie"])
    idx_rtca = _find_col_idx(headers, ["rt_c/a", "rt_c_a", "rt_c a", "rt_c/a "])
    idx_rt_critic = _find_col_idx(headers, ["rt_critic", "rt critic"])
    idx_rt_audience = _find_col_idx(headers, ["rt_audience", "rt audience"])
    idx_imdb = _find_col_idx(headers, ["imdb_rating", "imdb"])
    idx_rt_url = _find_col_idx(headers, ["rt_url"])
    idx_imdb_url = _find_col_idx(headers, ["imdb_url"])
    idx_showtimes = _find_col_idx(headers, ["showtimes", "showtime"])
    idx_runtime = _find_col_idx(headers, ["runtime"])

    # Rewrite header
    header_row.clear()
    for title in ["#", "Movie", "RT_C/A", "IMDb", "Showtimes", "Runtime"]:
        th = soup.new_tag("th")
        th.string = title
        header_row.append(th)

    # Process Rows
    for tr in tbody.find_all("tr", recursive=False):
        cells = tr.find_all(["th", "td"], recursive=False)

        def get_cell(i: int | None):
            return cells[i] if i is not None and 0 <= i < len(cells) else None

        def get_text(i: int | None) -> str:
            cell = get_cell(i)
            return cell.get_text(" ", strip=True) if cell else ""

        # 1. Handle Movie ID (#)
        movie_id_str = (tr.get("data-movie-id") or get_text(idx_hash)).strip()
        num_td = soup.new_tag("td")
        num_td.string = movie_id_str

        # 2. Handle Movie Title & Poster
        movie_td = soup.new_tag("td")

        mid_int = None
        try:
            mid_int = int(movie_id_str)
        except Exception:
            mid_int = None

        title = movie_by_id.get(mid_int, "") if mid_int is not None else ""
        if not title:
            title = get_text(idx_movie) or "Unknown"

        title_div = soup.new_tag("div", **{"class": "movie-cell-title"})
        title_div.string = title
        movie_td.append(title_div)

        poster_url = poster_by_id.get(mid_int) if mid_int is not None else None
        final_poster_url = poster_url if poster_url else "https://4ddig.tenorshare.com/images/photo-recovery/images-not-found.webp"

        if final_poster_url:
            wrap = soup.new_tag("div", **{"class": "movie-poster-wrap"})
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

        # 3. Handle linked score columns
        if get_cell(idx_rtca):
            rtca_txt = get_text(idx_rtca)
        else:
            c = _fmt_score(get_text(idx_rt_critic))
            a = _fmt_score(get_text(idx_rt_audience))
            rtca_txt = f"{c}/{a}".strip("/")
        rtca_td = _linked_score_td(soup, rtca_txt, get_text(idx_rt_url) or None)

        imdb_td = _linked_score_td(soup, _fmt_score(get_text(idx_imdb)), get_text(idx_imdb_url) or None)
        show_td = _clone_cell_as_td(soup, get_cell(idx_showtimes))
        label_showtime_fetch_dates(soup, show_td, fetch_dates or {}, scenef_provenance)
        # Show a compact source indicator inside the existing Showtimes column.
        # A separate per-movie timestamp would be misleading for mixed-source
        # dates/theatres and for reports whose underlying cache was inherited.
        freshness = (showtime_freshness or {}).get(mid_int)
        if freshness:
            kind, label, explanation = freshness
            note = soup.new_tag("span", **{"class": "showtimes-freshness"})
            note["data-source"] = kind
            note["title"] = explanation
            note.string = label
            show_td.append(note)
        runtime_td = _clone_cell_as_td(soup, get_cell(idx_runtime))

        # Rebuild row
        tr.clear()
        tr.append(num_td)
        tr.append(movie_td)
        tr.append(rtca_td)
        tr.append(imdb_td)
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
    """Return an exact-candidate Wikipedia thumbnail, if one exists.

    This first-pass lookup intentionally avoids MediaWiki's broad search result.
    A broad one-result search can attach artwork from a song, album, or different
    same-title film (the failure mode seen with concert/event listings).  The
    later ``finalize_posters.py`` pass already has a stronger Wikimedia-only
    search/validation pipeline, so leaving a placeholder here is safer than
    committing a plausible-but-wrong image.
    """

    def summary_thumb(page_title: str) -> str | None:
        t = quote(page_title.replace(" ", "_"))
        data = _fetch_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{t}")
        if not data:
            return None
        thumb = (data.get("thumbnail") or {}).get("source")
        return thumb if isinstance(thumb, str) and thumb.startswith(("http://", "https://")) else None

    # Reuse the same generic title normalization as IMDb/RT/final poster lookup.
    # This preserves the visible AMC title while using an original-film title
    # and anniversary-derived year for metadata when appropriate.
    for candidate in wikipedia_title_candidates(title):
        thumb = summary_thumb(candidate)
        if thumb:
            return thumb

    return None

def main() -> None:
    from datetime import datetime, timezone
    
    if not HTML_PATH.exists():
        print(f"ERROR: {HTML_PATH} not found", file=sys.stderr)
        sys.exit(1)

    soup = BeautifulSoup(HTML_PATH.read_text(encoding="utf-8"), "html.parser")
    remove_noisy_output(soup)

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
                "a_list_excluded": bool(sh.get("a_list_excluded")),
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

    # The notebook's exact fallback marker is the only reliable per-combination
    # provenance in the current report. Do not re-date inherited cached rows.
    fallback_marker = Path("build/amc_previous_report_fallback.txt")
    cached_combos = read_cached_showtime_combos(fallback_marker)
    freshness_by_movie = movie_showtime_freshness(showtimes, cached_combos)
    # Read the previous report BEFORE replacing docs/index.html. This metadata
    # follows exact theatre/date pairs into the next run's cached fallback.
    # Fresh dates come from notebook markers recorded at successful AMC fetch.
    from datetime import timezone as _Tz
    from zoneinfo import ZoneInfo as _Zone
    fetch_today = datetime.now(_Tz.utc).astimezone(_Zone("America/Los_Angeles")).date().isoformat()
    # The workflow saves the committed report here BEFORE nbconvert overwrites
    # docs/index.html, so preserve that source rather than reading this run's
    # raw, unprocessed notebook HTML.
    previous_dates = read_previous_fetch_dates(Path("build/previous_report.html"), fetch_today)
    live_dates = read_workflow_fetch_dates(Path("build/amc_live_fetch_dates"), fetch_today)
    fetch_dates = resolve_showtime_fetch_dates(showtimes, cached_combos, previous_dates, live_dates)
    previous_scenef = read_previous_scenef_provenance(Path("build/previous_report.html"), fetch_today)
    current_scenef = read_scenef_provenance(Path("build/scenef_combo_sources"), fetch_today)
    scenef_provenance = resolve_scenef_provenance(
        showtimes, cached_combos, previous_scenef, current_scenef, live_dates
    )
    # Third-party verification dates are NOT AMC's own fetch dates. Keep the
    # two apart so the report cannot silently relabel SceneF as live AMC.
    for key in scenef_provenance:
        fetch_dates[key] = None
    freshness_by_movie = mark_scenef_showtime_freshness(showtimes, freshness_by_movie, scenef_provenance)
    apply_final_table_schema(
        soup, table, movie_by_id, poster_by_id, freshness_by_movie, fetch_dates,
        scenef_provenance,
    )

    
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

    # If the live AMC page was blocked or only partially hydrated, the notebook
    # may have reused a recent successful report for the exact same weekend.
    # Surface that fact in the public report rather than silently presenting
    # carried-forward showtimes as if they were freshly retrieved.
    old_fallback_notice = soup.find(id="amc-cache-notice")
    if old_fallback_notice:
        old_fallback_notice.decompose()

    insert_pos = 1
    if fallback_marker.exists():
        combos = [
            line.strip()
            for line in fallback_marker.read_text(encoding="utf-8", errors="ignore").splitlines()
            if line.strip()
        ]
        if combos:
            # Put operational notes after the movie table and planner. A compact,
            # collapsed footer avoids interrupting normal reading and planning.
            notice = soup.new_tag("details", id="amc-cache-notice", **{"class": "amc-cache-notice"})
            summary = soup.new_tag("summary")
            summary.string = f"AMC schedule notes: {len(combos)} theater/date schedule(s) reused"
            notice.append(summary)
            explanation = soup.new_tag("p")
            explanation.string = (
                "AMC blocked or incompletely rendered some live schedules. "
                "These combinations were carried forward from a validated recent report "
                "for the same weekend (maximum age: 36 hours): "
                + "; ".join(combos)
            )
            notice.append(explanation)
            container.append(notice)

    
    if scenef_provenance:
        # Source attribution is mandatory for republication; keep it visible
        # but out of the reader's way beneath the planner and movie table.
        sf_note = soup.new_tag("details", id="scenef-schedule-notice", **{"class": "amc-cache-notice"})
        heading = soup.new_tag("summary")
        heading.string = f"Third-party showtime source: SceneF ({len(scenef_provenance)} theater/date schedule(s))"
        sf_note.append(heading)
        for (_theater, _date), _record in sorted(scenef_provenance.items()):
            item = soup.new_tag("p")
            item.append(f"{_theater} | {_date} — verified {_record['verified_on']} · ")
            link = soup.new_tag("a", href=_record["scenef_url"])
            link.string = "Showtimes via SceneF.com"
            item.append(link)
            item.append(" · AMC A-List eligibility is not verified for third-party showtimes.")
            sf_note.append(item)
        container.append(sf_note)
        credit = soup.new_tag("p", **{"class": "report-generated"})
        credit.append("Third-party listings: ")
        sf_link = soup.new_tag("a", href="https://scenef.com/week?region=orangecounty")
        sf_link.string = "Showtimes via SceneF.com"
        credit.append(sf_link)
        container.append(credit)

    body = soup.body or soup
    #old code: body.insert(0, BeautifulSoup(PLANNER_HTML, "html.parser"))
    container.insert(insert_pos, BeautifulSoup(PLANNER_HTML, "html.parser"))

    data_tag = soup.new_tag("script", id="showtimes-data", type="application/json")
    data_tag.string = json.dumps({
        "ignore_theater": IGNORE_THEATER,
        "preferred_theaters": PREFERRED_THEATERS,
        "movies": movies,
        "showtimes": showtimes,
        # Persist exact source dates inside the committed HTML for future runs.
        # null signifies unknown/old rather than a guessed report timestamp.
        "amc_fetch_dates": [
            dict({"theater": key[0], "date": key[1], "fetched_on": fetch_dates[key]},
                 **scenef_provenance.get(key, {}))
            for key in sorted(fetch_dates)
        ],
    }, ensure_ascii=False)

    js_tag = soup.new_tag("script", id="movie-planner-js")
    js_tag.string = JS

    body.append(data_tag)
    body.append(js_tag)

    HTML_PATH.write_text(str(soup), encoding="utf-8")

if __name__ == "__main__":
    main()
