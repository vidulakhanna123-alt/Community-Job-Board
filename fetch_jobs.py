"""Collects community jobs, filters, scores, writes docs/jobs.json."""
import json, re, html, datetime as dt
from urllib.parse import urlparse, parse_qs
import requests, feedparser, yaml

CFG = yaml.safe_load(open("config.yaml"))
UA = {"User-Agent": "community-job-dashboard/1.0 (personal job search)"}
TITLE_RE = re.compile("|".join(CFG["titles"]), re.I)
EXCL_RE = re.compile("|".join(CFG["exclude_titles"]), re.I)
SCAM_RE = re.compile(r"registration fee|training fee|wire transfer|western union|telegram|whatsapp|@gmail\.com|crypto wallet", re.I)
US_ONLY = re.compile(r"us only|u\.s\. only|united states only|us-based|must (be )?(located|reside|based) in the (us|u\.s\.|united states)|authorized to work in the (us|u\.s\.|united states)|north america only|usa only|canada only|uk only|europe only|eu only", re.I)
OPEN = re.compile(r"worldwide|anywhere|global|apac|asia|india|international", re.I)


def get(url, **kw):
    try:
        r = requests.get(url, headers=UA, timeout=30, **kw)
        r.raise_for_status()
        return r
    except Exception as e:
        print("WARN", url, e)
        return None


def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(s or "")))).strip()


def job(title, company, location, url, source, posted="", text=""):
    return dict(title=clean(title), company=clean(company), location=clean(location),
                url=url, source=source, posted=str(posted)[:10], text=clean(text)[:6000])


def greenhouse(t):
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true")
    for j in (r.json().get("jobs", []) if r else []):
        yield job(j["title"], t, j.get("location", {}).get("name", ""), j["absolute_url"], "Greenhouse", j.get("updated_at", ""), j.get("content", ""))


def lever(t):
    r = get(f"https://api.lever.co/v0/postings/{t}?mode=json")
    for j in (r.json() if r else []):
        yield job(j["text"], t, j.get("categories", {}).get("location", ""), j["hostedUrl"], "Lever", "", j.get("descriptionPlain", ""))


def ashby(t):
    r = get(f"https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true")
    for j in (r.json().get("jobs", []) if r else []):
        comp = (j.get("compensation") or {}).get("compensationTierSummary", "")
        loc = j.get("location", "") + (" Remote" if j.get("isRemote") else "")
        yield job(j["title"], t, loc, j["jobUrl"], "Ashby", j.get("publishedAt", ""), f'{comp} {j.get("descriptionPlain", "")}')


def remoteok():
    r = get("https://remoteok.com/api")
    for j in (r.json()[1:] if r else []):
        sal = f'${j["salary_min"]}-${j["salary_max"]}' if j.get("salary_min") else ""
        yield job(j.get("position", ""), j.get("company", ""), j.get("location", "") or "Remote", j.get("url", ""), "RemoteOK", j.get("date", ""), f'{sal} {j.get("description", "")}')


def remotive():
    r = get("https://remotive.com/api/remote-jobs", params={"search": "community"})
    for j in (r.json().get("jobs", []) if r else []):
        yield job(j["title"], j["company_name"], j.get("candidate_required_location", ""), j["url"], "Remotive", j.get("publication_date", ""), f'{j.get("salary", "")} {j.get("description", "")}')


def wwr(feed):
    for e in feedparser.parse(feed).entries:
        company, _, title = e.title.partition(": ")
        yield job(title or e.title, company, getattr(e, "region", "") or "Remote", e.link, "WeWorkRemotely", e.get("published", ""), e.get("summary", ""))


def alerts(feed):
    for e in feedparser.parse(feed).entries:
        link = parse_qs(urlparse(e.link).query).get("url", [e.link])[0]
        yield job(e.title, urlparse(link).netloc, "See posting", link, "Google Alert", e.get("published", ""), e.get("summary", ""))


def salary(text):
    nums = []
    for m in re.finditer(r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}\s?[kK])\b", text):
        v = m.group(1).replace(",", "").lower().replace("k", "000").replace(" ", "")
        if int(v) >= 20000:
            nums.append(int(v))
    return (min(nums), max(nums)) if nums else (None, None)


def score(j):
    lo, hi = salary(j["text"])
    j["salary_min"], j["salary_max"] = lo, hi
    want = CFG["salary_usd"]
    j["salary_fit"] = "unknown" if lo is None else "in" if (lo <= want["max"] and hi >= want["min"]) else "above" if lo > want["max"] else "below"
    loc, blob = j["location"].lower(), (j["location"] + " " + j["text"][:2500])
    if US_ONLY.search(blob) or re.search(r"^(us|usa|united states|canada|uk|europe)$", loc.strip()):
        j["india_eligible"] = "no"
    elif OPEN.search(loc) or "remote" in loc and not loc.strip() in ("remote us", "remote usa"):
        j["india_eligible"] = "yes" if OPEN.search(loc) else "maybe"
    else:
        j["india_eligible"] = "no"
    low = j["text"].lower()
    j["skills"] = [s for s in CFG["skills"] if s in low]
    s = 30 if TITLE_RE.search(j["title"]) else 0
    s += {"yes": 30, "maybe": 15, "no": 0}[j["india_eligible"]]
    s += min(len(j["skills"]) * 5, 25)
    s += {"in": 10, "unknown": 5, "below": 0, "above": 2}[j["salary_fit"]]
    s += 5 if any(c in (j["company"] + j["url"]).lower() for c in CFG["priority_companies"]) else 0
    j["score"] = min(s, 100)
    j["text"] = j["text"][:400]  # keep the JSON small
    return j


def main():
    jobs = []
    for t in CFG.get("greenhouse", []): jobs += greenhouse(t)
    for t in CFG.get("lever", []): jobs += lever(t)
    for t in CFG.get("ashby", []): jobs += ashby(t)
    if CFG.get("remoteok"): jobs += remoteok()
    if CFG.get("remotive"): jobs += remotive()
    for f in CFG.get("wwr_feeds", []): jobs += wwr(f)
    for f in CFG.get("alert_feeds", []): jobs += alerts(f)
    seen, out = set(), []
    for j in jobs:
        if not j["url"] or j["url"] in seen: continue
        if not TITLE_RE.search(j["title"]) or EXCL_RE.search(j["title"]): continue
        if SCAM_RE.search(j["text"]): continue
        seen.add(j["url"]); out.append(score(j))
    out.sort(key=lambda j: j["score"], reverse=True)
    json.dump({"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), "jobs": out},
              open("docs/jobs.json", "w"), indent=1)
    print(f"{len(out)} jobs kept from {len(jobs)} fetched")


if __name__ == "__main__":
    main()
