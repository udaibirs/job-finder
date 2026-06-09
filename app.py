"""Job Finder - single-file Flask app (deploy build).

Keyword search across live job sources, default location India, results on a web
page. Everything is in this one file so it uploads to GitHub with no folders.

Live sources (no setup): RemoteOK (JSON API), WeWorkRemotely (RSS).
Blocked sources (need a provider key): Indeed India, Naukri, LinkedIn Jobs.
"""
import re
import requests
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)
DEFAULT_LOCATION = "India"
# A realistic browser User-Agent. RemoteOK's Cloudflare blocks bot-style agents
# (it returns an HTML challenge instead of JSON), so we present as a browser.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# --------------------------------------------------------------------------- #
# Core types
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    source: str
    posted: str = ""
    salary: str = ""
    tags: str = ""

    def as_dict(self):
        return asdict(self)


def keyword_match(job, keyword):
    """True if every word of the keyword appears in the title or tags."""
    if not keyword:
        return True
    haystack = f"{job.title} {job.tags}".lower()
    return all(w in haystack for w in re.split(r"\s+", keyword.lower().strip()) if w)


class Source:
    name = "base"
    configured = True

    def fetch(self, keyword, location):
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Live sources
# --------------------------------------------------------------------------- #
class RemoteOK(Source):
    name = "RemoteOK"
    API = "https://remoteok.com/api"
    RSS = "https://remoteok.com/remote-jobs.rss"

    def fetch(self, keyword, location):
        """Try the JSON API; if it's blocked or not JSON, fall back to the RSS feed."""
        try:
            return self._fetch_json(keyword)
        except Exception:
            return self._fetch_rss(keyword)

    def _fetch_json(self, keyword):
        r = requests.get(self.API, headers=HEADERS, timeout=25)
        r.raise_for_status()
        data = r.json()  # raises if Cloudflare served an HTML challenge instead
        jobs = []
        for row in data:
            if not isinstance(row, dict) or not row.get("position"):
                continue
            lo, hi = row.get("salary_min"), row.get("salary_max")
            salary = f"${lo:,} - ${hi:,}" if lo and hi else ""
            job = Job(
                title=row.get("position", ""),
                company=row.get("company", ""),
                location=row.get("location") or "Remote",
                url=row.get("url", ""),
                source=self.name,
                posted=(row.get("date", "") or "")[:10],
                salary=salary,
                tags=",".join(row.get("tags", []) or []),
            )
            if keyword_match(job, keyword):
                jobs.append(job)
        return jobs

    def _fetch_rss(self, keyword):
        r = requests.get(self.RSS, headers=HEADERS, timeout=25)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        jobs = []
        for item in root.iter("item"):
            job = Job(
                title=_text(item, "title"),
                company=_text(item, "company"),
                location=_text(item, "location") or "Remote",
                url=_text(item, "link"),
                source=self.name,
                posted=_text(item, "pubDate")[:16],
                tags=_text(item, "tags"),
            )
            if job.title and keyword_match(job, keyword):
                jobs.append(job)
        return jobs


class WeWorkRemotely(Source):
    name = "WeWorkRemotely"
    FEEDS = [
        "https://weworkremotely.com/categories/remote-marketing-jobs.rss",
        "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/all-other-remote-jobs.rss",
        "https://weworkremotely.com/remote-jobs.rss",
    ]

    def fetch(self, keyword, location):
        seen, jobs = set(), []
        for feed in self.FEEDS:
            try:
                r = requests.get(feed, headers=HEADERS, timeout=25)
                r.raise_for_status()
                root = ET.fromstring(r.content)
            except Exception:
                continue
            for item in root.iter("item"):
                link = _text(item, "link")
                if not link or link in seen:
                    continue
                seen.add(link)
                company, title = _split_title(_text(item, "title"))
                job = Job(
                    title=title,
                    company=company,
                    location=_text(item, "region") or "Remote",
                    url=link,
                    source=self.name,
                    posted=_text(item, "pubDate")[:16],
                    tags=_text(item, "category"),
                )
                if keyword_match(job, keyword):
                    jobs.append(job)
        return jobs


def _text(item, tag):
    el = item.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def _split_title(raw):
    if ":" in raw:
        company, _, title = raw.partition(":")
        return company.strip(), title.strip()
    return "", raw.strip()


# --------------------------------------------------------------------------- #
# Blocked sources - need a provider key (SerpApi / Apify / Bright Data etc).
# They return nothing until configured, so the app keeps working.
# --------------------------------------------------------------------------- #
class _APIStub(Source):
    configured = False

    def fetch(self, keyword, location):
        return []


class IndeedIndia(_APIStub):
    name = "Indeed India"


class Naukri(_APIStub):
    name = "Naukri"


class LinkedInJobs(_APIStub):
    name = "LinkedIn Jobs"


ALL_SOURCES = [RemoteOK(), WeWorkRemotely(), IndeedIndia(), Naukri(), LinkedInJobs()]
REGISTRY = {s.name: s for s in ALL_SOURCES}


def get_sources(names):
    if not names:
        return ALL_SOURCES
    return [REGISTRY[n] for n in names if n in REGISTRY]


def run_search(keyword, location, source_names):
    sources = get_sources(source_names)
    jobs, status = [], []
    with ThreadPoolExecutor(max_workers=len(sources) or 1) as pool:
        futures = {pool.submit(s.fetch, keyword, location): s for s in sources}
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                found = fut.result()
                jobs.extend(found)
                status.append({"source": src.name, "count": len(found),
                               "ok": True, "configured": src.configured})
            except Exception as e:
                status.append({"source": src.name, "count": 0, "ok": False,
                               "configured": src.configured, "error": str(e)[:200]})
    return jobs, sorted(status, key=lambda s: s["source"])


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template_string(
        PAGE,
        sources=[{"name": s.name, "configured": s.configured} for s in ALL_SOURCES],
        default_location=DEFAULT_LOCATION,
    )


@app.route("/api/search")
def api_search():
    keyword = (request.args.get("keyword") or "").strip()
    location = (request.args.get("location") or DEFAULT_LOCATION).strip()
    selected = request.args.getlist("source") or None
    jobs, status = run_search(keyword, location, selected)
    return jsonify({"keyword": keyword, "location": location,
                    "count": len(jobs), "jobs": [j.as_dict() for j in jobs],
                    "status": status})


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Finder</title>
<style>
  :root { --bg:#0f1220; --card:#1a1f35; --muted:#8b93b0; --acc:#5b8cff; --line:#2a3150; --text:#e8ebf5; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--text); }
  header { padding:28px 20px 8px; max-width:980px; margin:0 auto; }
  h1 { margin:0 0 4px; font-size:24px; }
  .sub { color:var(--muted); font-size:14px; }
  .wrap { max-width:980px; margin:0 auto; padding:16px 20px 60px; }
  form { display:flex; flex-wrap:wrap; gap:10px; align-items:flex-end; background:var(--card); padding:16px; border-radius:14px; border:1px solid var(--line); }
  .field { display:flex; flex-direction:column; gap:6px; }
  .field label { font-size:12px; color:var(--muted); }
  input[type=text] { background:#11152a; border:1px solid var(--line); color:var(--text); padding:10px 12px; border-radius:9px; font-size:14px; min-width:220px; }
  button { background:var(--acc); color:#fff; border:0; padding:11px 20px; border-radius:9px; font-size:14px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.6; cursor:default; }
  .sources { display:flex; flex-wrap:wrap; gap:14px; margin:14px 2px 0; }
  .sources label { font-size:13px; color:var(--text); display:flex; align-items:center; gap:6px; }
  .sources .off { color:var(--muted); }
  .pill { font-size:10px; padding:1px 6px; border-radius:20px; background:#33304a; color:#cdb6ff; }
  .status { display:flex; flex-wrap:wrap; gap:8px; margin:18px 0 6px; }
  .chip { font-size:12px; padding:4px 10px; border-radius:20px; border:1px solid var(--line); color:var(--muted); }
  .chip.ok { color:#7ee0a8; border-color:#27543a; }
  .chip.warn { color:#f0c674; border-color:#5a4a23; }
  .count { margin:6px 2px 14px; color:var(--muted); font-size:14px; }
  .job { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin-bottom:10px; }
  .job a { color:var(--text); text-decoration:none; font-weight:600; font-size:16px; }
  .job a:hover { color:var(--acc); }
  .meta { color:var(--muted); font-size:13px; margin-top:4px; display:flex; flex-wrap:wrap; gap:10px; }
  .src { font-size:11px; color:#a9b3d9; border:1px solid var(--line); padding:1px 7px; border-radius:20px; }
  .empty { color:var(--muted); padding:30px 4px; }
</style>
</head>
<body>
<header>
  <h1>Job Finder</h1>
  <div class="sub">Keyword search across live job boards. Default location: {{ default_location }}.</div>
</header>
<div class="wrap">
  <form id="f">
    <div class="field">
      <label for="keyword">Keyword</label>
      <input type="text" id="keyword" name="keyword" placeholder="e.g. digital marketing" autofocus>
    </div>
    <div class="field">
      <label for="location">Location</label>
      <input type="text" id="location" name="location" value="{{ default_location }}">
    </div>
    <button type="submit" id="go">Search</button>
    <div class="sources">
      {% for s in sources %}
      <label class="{{ '' if s.configured else 'off' }}">
        <input type="checkbox" name="source" value="{{ s.name }}" {{ 'checked' if s.configured else '' }}>
        {{ s.name }}{% if not s.configured %} <span class="pill">setup needed</span>{% endif %}
      </label>
      {% endfor %}
    </div>
  </form>
  <div id="status" class="status"></div>
  <div id="count" class="count"></div>
  <div id="results"></div>
</div>
<script>
const f = document.getElementById('f');
const results = document.getElementById('results');
const statusEl = document.getElementById('status');
const countEl = document.getElementById('count');
const go = document.getElementById('go');
f.addEventListener('submit', async (e) => {
  e.preventDefault();
  go.disabled = true; go.textContent = 'Searching...';
  results.innerHTML = ''; statusEl.innerHTML = ''; countEl.textContent = '';
  const params = new URLSearchParams();
  params.set('keyword', document.getElementById('keyword').value);
  params.set('location', document.getElementById('location').value);
  document.querySelectorAll('input[name=source]:checked').forEach(c => params.append('source', c.value));
  try {
    const res = await fetch('/api/search?' + params.toString());
    const data = await res.json();
    renderStatus(data.status);
    countEl.textContent = data.count + ' job' + (data.count === 1 ? '' : 's') + ' found'
      + (data.keyword ? ' for "' + data.keyword + '"' : '');
    renderJobs(data.jobs);
  } catch (err) {
    countEl.textContent = 'Something went wrong: ' + err;
  } finally {
    go.disabled = false; go.textContent = 'Search';
  }
});
function renderStatus(status) {
  statusEl.innerHTML = status.map(s => {
    if (!s.configured) return '<span class="chip warn">' + s.source + ': setup needed</span>';
    if (!s.ok) return '<span class="chip warn">' + s.source + ': error</span>';
    return '<span class="chip ok">' + s.source + ': ' + s.count + '</span>';
  }).join('');
}
function renderJobs(jobs) {
  if (!jobs.length) {
    results.innerHTML = '<div class="empty">No matching jobs. Try a broader keyword, or enable more sources.</div>';
    return;
  }
  results.innerHTML = jobs.map(j => '' +
    '<div class="job">' +
      '<a href="' + j.url + '" target="_blank" rel="noopener">' + (esc(j.title) || 'Untitled role') + '</a>' +
      '<div class="meta">' +
        (j.company ? '<span>' + esc(j.company) + '</span>' : '') +
        '<span>' + esc(j.location) + '</span>' +
        (j.salary ? '<span>' + esc(j.salary) + '</span>' : '') +
        (j.posted ? '<span>' + esc(j.posted) + '</span>' : '') +
        '<span class="src">' + esc(j.source) + '</span>' +
      '</div>' +
    '</div>').join('');
}
function esc(s) {
  return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
