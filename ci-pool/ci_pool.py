#!/usr/bin/env python3
"""The org's Blacksmith pool meter: how many of Blacksmith's free minutes every Colorado Startups repo has used, and the
stop past which no job may go to Blacksmith.

Paul, 2026-11-04: Blacksmith's free tier takes the org's CI overflow (Colorado-Startups/code-os#200). Free tiers and
included minutes only, never pay-per-use on a card (Paul, 2026-11-02). The chooser that reads this meter is a job in
Code-OS's ci-standard.yml; this file only measures.

    ci_pool.py meter --org=ORG --ledger=FILE [--write]
        Reads every repo's job records with $GH_TOKEN (the meter App's token), updates the ledger and prints the
        reading; --write also stores it in the org variable CI_POOL. Exit 0 read, 2 usage error, 3 could not read.
    ci_pool.py --self-test
        Seeded known-good and known-bad cases, with fake APIs.

The reading is trusted for MAX_AGE. Over that time the org can burn up to the peak hourly rate on Blacksmith before a
new reading lands, so the stop sits that far under the free minutes: stop = free - MAX_AGE x peak hourly burn. The peak
is the busiest hour the meter has seen in its window, never below PEAK_FLOOR. A run whose jobs or runs the meter could
not all read makes the reading complete: false, and a chooser treats that as no reading.

Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
# Blacksmith's free tier, per organization (docs.blacksmith.sh/blacksmith-runners/overview, read 2026-11-04).
BLACKSMITH_FREE_MINUTES = 3000
# The meter runs hourly. 75 minutes trusts one reading plus 15 minutes of schedule delay; a missed pass goes stale.
MAX_AGE = dt.timedelta(minutes=75)
# The busiest hour assumed before the meter has seen one: a goal, about 1.65 x the org's busiest day on record
# (8,709 Linux minutes on GitHub-hosted runners on 2026-11-02, billing usage API, read 2026-11-04) spread over 24 hours.
PEAK_FLOOR = 600
WINDOW = dt.timedelta(days=31)
# The Blacksmith app was installed on the org on 2026-10-31 (gh api /orgs/Colorado-Startups/installations): no run
# before that day can hold a Blacksmith job, so the meter reads nothing older.
BLACKSMITH_SINCE = dt.datetime(2026, 10, 31, tzinfo=dt.timezone.utc)
LABEL_RE = re.compile(r"blacksmith-(\d+)vcpu-ubuntu-\d+(-arm)?")
# A 2-vCPU ARM minute counts as 0.625 x64 minutes (docs.blacksmith.sh/blacksmith-runners/overview).
ARM_WEIGHT = 0.625
# One pass reads at most this many job pages. A first pass, or a backlog, takes several passes; until the ledger
# catches up the reading says complete: false.
READS_PER_PASS = 3000
RUN_PAGES = 500
JOB_PAGES = 10
PAGE = 100


class Unreadable(Exception):
    """A reading the meter cannot trust: exit 3, and never a count of zero."""


def parse_time(value) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError(f"not a time: {value!r}")
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError(f"time without a zone: {value!r}")
    return t


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Api:
    """The GitHub REST reads and the one write the meter makes. The self-test passes fakes with the same methods."""

    def __init__(self, token: str):
        if not token:
            raise Unreadable("no GH_TOKEN: the meter needs the CI pool App's installation token")
        self.token = token
        self.calls = 0

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "colorado-startups-ci-pool"})
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            if method != "GET" and err.code == 404:
                raise FileNotFoundError(path) from None
            raise Unreadable(f"{method} {path}: HTTP {err.code}") from None
        except Exception as err:  # noqa: BLE001 - any failed read is unknown, never zero
            raise Unreadable(f"{method} {path}: {type(err).__name__}") from None

    def get(self, path: str, params: dict | None = None):
        return self.request("GET", path, params)


def items_of(doc, key: str, path: str) -> list:
    items = doc.get(key) if isinstance(doc, dict) and key else doc
    if not isinstance(items, list):
        raise Unreadable(f"GET {path}: no {key or 'item'} list")
    return items


def weight(label: str) -> float:
    """Free minutes one minute on this Blacksmith label uses (x64 2 vCPU = 1). An unknown label is Unreadable."""
    match = LABEL_RE.fullmatch(label)
    if not match or int(match[1]) <= 0:
        raise Unreadable(f"unknown Blacksmith runner label {label!r}")
    return int(match[1]) / 2 * (ARM_WEIGHT if match[2] else 1.0)


def job_minutes(job: dict, now: dt.datetime) -> float:
    """Free minutes one job used: 0 unless a Blacksmith runner took it. A job still running counts to now."""
    labels = [str(x) for x in job.get("labels") or [] if str(x).startswith("blacksmith-")]
    if not labels or not job.get("started_at") or not job.get("runner_name"):
        return 0.0
    end = parse_time(job["completed_at"]) if job.get("completed_at") else now
    seconds = (end - parse_time(job["started_at"])).total_seconds()
    return math.ceil(max(seconds, 1.0) / 60) * max(weight(x) for x in labels)


def repo_runs(api, org: str, repo: str, start: dt.datetime) -> tuple:
    """(runs created at or after start, newest first; whether that is all of them). No `created` filter: GitHub caps a
    filtered list at 1,000 runs. The list ends at the first older run; a list that ends short of total_count first
    is cut off, so it is not complete."""
    path = f"/repos/{org}/{repo}/actions/runs"
    runs, seen, total = [], 0, None
    for page in range(1, RUN_PAGES + 1):
        doc = api.get(path, {"per_page": PAGE, "page": page})
        items = items_of(doc, "workflow_runs", path)
        if total is None:
            total = doc.get("total_count")
        for run in items:
            seen += 1
            if parse_time(run["created_at"]) < start:
                return runs, True
            runs.append(run)
        if len(items) < PAGE:
            return runs, isinstance(total, int) and seen >= total
    return runs, False


def run_jobs(api, org: str, repo: str, run_id) -> tuple:
    """(every job of every attempt, pages read, whether that is all of them per total_count)."""
    path = f"/repos/{org}/{repo}/actions/runs/{run_id}/jobs"
    jobs, total, pages = [], None, 0
    for page in range(1, JOB_PAGES + 1):
        doc = api.get(path, {"filter": "all", "per_page": PAGE, "page": page})
        pages += 1
        items = items_of(doc, "jobs", path)
        if total is None:
            total = doc.get("total_count")
        jobs.extend(items)
        if len(items) < PAGE:
            break
    return jobs, pages, isinstance(total, int) and len(jobs) >= total


def stop_for(peak: float) -> int:
    """The stop: the free minutes less what the org can burn while one reading is trusted."""
    burn = max(peak, PEAK_FLOOR) * MAX_AGE.total_seconds() / 3600
    return max(BLACKSMITH_FREE_MINUTES - math.ceil(burn), 0)


def meter(api, org: str, ledger: dict, now: dt.datetime, reads: int = READS_PER_PASS) -> tuple:
    """(reading, ledger). ledger maps "repo/run id" to {"u": updated_at, "h": created hour, "m": minutes} for finished
    runs whose jobs were all read; any other run is read again next pass."""
    start = max(now - WINDOW, BLACKSMITH_SINCE)
    floor = start.strftime("%Y-%m-%dT%H")
    old = ledger.get("runs") if ledger.get("version") == 2 and isinstance(ledger.get("runs"), dict) else {}
    runs = {k: v for k, v in old.items() if isinstance(v, dict) and str(v.get("h", "")) >= floor}
    complete, going = True, {}
    repos = []
    for page in range(1, 51):
        items = items_of(api.get(f"/orgs/{org}/repos", {"type": "all", "per_page": PAGE, "page": page}), "", "repos")
        repos += [r["name"] for r in items if not r.get("archived")]
        if len(items) < PAGE:
            break
    for repo in sorted(repos):
        listed, whole = repo_runs(api, org, repo, start)
        complete = complete and whole
        for run in listed:
            key = f"{repo}/{run['id']}"
            done = run.get("status") == "completed"
            if done and runs.get(key, {}).get("u") == run.get("updated_at"):
                continue
            if reads <= 0:
                complete = False
                continue
            jobs, pages, all_jobs = run_jobs(api, org, repo, run["id"])
            reads -= pages
            complete = complete and all_jobs
            entry = {"u": run.get("updated_at"), "h": parse_time(run["created_at"]).strftime("%Y-%m-%dT%H"),
                     "m": sum(job_minutes(j, now) for j in jobs)}
            (runs if done and all_jobs else going)[key] = entry
            if not (done and all_jobs):
                runs.pop(key, None)
    every = {**runs, **going}
    used = sum(v["m"] for v in every.values())
    by_repo, by_hour = {}, {}
    for key, v in every.items():
        repo = key.split("/", 1)[0]
        by_repo[repo] = by_repo.get(repo, 0) + v["m"]
        by_hour[v["h"]] = by_hour.get(v["h"], 0) + v["m"]
    peak = max(by_hour.values(), default=0.0)
    stop = stop_for(peak)
    reading = {
        "version": 2,
        "at": iso(now),
        "complete": complete,
        "max_age_minutes": int(MAX_AGE.total_seconds() // 60),
        "window_start": iso(start),
        "blacksmith": {"used": round(used, 1), "free": BLACKSMITH_FREE_MINUTES, "stop": stop,
                       "left": round(max(stop - used, 0), 1), "peak_hour": round(peak, 1),
                       "by_repo": {k: round(v, 1) for k, v in sorted(by_repo.items()) if v}},
        "repos": len(repos),
    }
    return reading, {"version": 2, "runs": runs}


def write_variable(api, org: str, value: str) -> None:
    body = {"name": "CI_POOL", "value": value, "visibility": "all"}
    try:
        api.request("PATCH", f"/orgs/{org}/actions/variables/CI_POOL", body=body)
    except FileNotFoundError:
        api.request("POST", f"/orgs/{org}/actions/variables", body=body)


# ---- self-test ---------------------------------------------------------------------------------------------------


class FakeApi:
    """Two repos and one archived. alpha: a run still going, a Blacksmith run of 3 jobs, then a run older than the
    window that must end the list. beta: one GitHub-hosted run."""

    def __init__(self, fail: str = "", job_total: int | None = None):
        self.fail, self.job_total = fail, job_total
        self.calls = 0
        self.paths = []
        self.written = None

    def get(self, path: str, params: dict | None = None):
        self.calls += 1
        self.paths.append((path, (params or {}).get("page")))
        params = params or {}
        if self.fail and self.fail in path:
            raise Unreadable(f"GET {path}: HTTP 500")
        if path.endswith("/repos"):
            return [{"name": "alpha"}, {"name": "beta"}, {"name": "old", "archived": True}]
        if path.endswith("/actions/runs") and "created" in params:
            raise Unreadable(f"GET {path}: a created filter caps the list at 1,000 runs")
        if path == "/repos/org/alpha/actions/runs":
            return {"total_count": 40, "workflow_runs": [
                {"id": 2, "status": "in_progress", "updated_at": "u2", "created_at": "2026-11-03T09:00:00Z"},
                {"id": 1, "status": "completed", "updated_at": "u1", "created_at": "2026-11-03T08:00:00Z"},
                {"id": 0, "status": "completed", "updated_at": "u0", "created_at": "2026-09-01T08:00:00Z"}]}
        if path == "/repos/org/beta/actions/runs":
            return {"total_count": 1, "workflow_runs": [
                {"id": 3, "status": "completed", "updated_at": "u3", "created_at": "2026-11-02T08:00:00Z"}]}
        if path.endswith("/jobs") and params.get("filter") != "all":
            raise Unreadable(f"GET {path}: the meter must read every attempt (filter=all)")
        bs = {"labels": ["blacksmith-2vcpu-ubuntu-2404"], "runner_name": "bs"}
        jobs = {
            "/runs/1/jobs": [
                {**bs, "started_at": "2026-11-03T08:00:00Z", "completed_at": "2026-11-03T08:01:30Z"},  # 2
                {**bs, "labels": ["blacksmith-4vcpu-ubuntu-2404"], "started_at": "2026-11-03T08:00:00Z",
                 "completed_at": "2026-11-03T08:02:00Z"},  # 2 min x 2 = 4
                {**bs, "labels": ["blacksmith-2vcpu-ubuntu-2404-arm"], "started_at": "2026-11-03T08:00:00Z",
                 "completed_at": "2026-11-03T08:04:00Z"}],  # 4 x 0.625 = 2.5
            "/runs/2/jobs": [{**bs, "started_at": "2026-11-03T09:58:10Z", "completed_at": None}],  # 2, to now
            "/runs/3/jobs": [
                {"labels": ["ubuntu-latest"], "runner_name": "gh", "started_at": "2026-11-02T08:00:00Z",
                 "completed_at": "2026-11-02T08:05:00Z"},
                {"labels": ["ubuntu-latest"], "runner_name": None, "started_at": None, "completed_at": None}],
        }
        for suffix, items in jobs.items():
            if path.endswith(suffix):
                return {"total_count": self.job_total or len(items), "jobs": items}
        raise Unreadable(f"GET {path}: unexpected in the fake")

    def request(self, method, path, params=None, body=None):
        if method == "PATCH":
            raise FileNotFoundError(path)
        self.written = body


class CappedApi(FakeApi):
    """One repo with 2,500 runs in the window, served the way GitHub serves a filtered list: 10 pages, then empty."""

    def get(self, path: str, params: dict | None = None):
        self.calls += 1
        if path.endswith("/repos"):
            return [{"name": "busy"}]
        if path.endswith("/jobs"):
            return {"total_count": 0, "jobs": []}
        page = (params or {}).get("page", 1)
        base = (page - 1) * PAGE
        items = [] if page > 10 else [{"id": 9000 - base - i, "status": "completed", "updated_at": "u",
                                       "created_at": "2026-11-03T08:00:00Z"} for i in range(PAGE)]
        return {"total_count": 2500, "workflow_runs": items}


def self_test() -> int:
    wrong = []
    now = dt.datetime(2026, 11, 3, 10, 0, tzinfo=dt.timezone.utc)

    # Known good: weights, every attempt, the run still going, the list ending at the first run before the window.
    api = FakeApi()
    got, ledger = meter(api, "org", {}, now)
    if got["blacksmith"]["used"] != 10.5:  # 2 + 4 + 2.5 finished, 2 still going
        wrong.append(f"meter used {got['blacksmith']['used']}, want 10.5")
    if got["blacksmith"]["by_repo"] != {"alpha": 10.5} or got["repos"] != 2 or got["complete"] is not True:
        wrong.append(f"meter by_repo, repos or complete wrong: {got}")
    if sorted(ledger["runs"]) != ["alpha/1", "beta/3"]:
        wrong.append(f"ledger keeps {sorted(ledger['runs'])}, want the two finished runs")
    if any(p.endswith("/runs/0/jobs") for p, _ in api.paths):
        wrong.append("the meter read a run from before the window")
    if [pg for p, pg in api.paths if p.endswith("alpha/actions/runs")] != [1]:
        wrong.append("the run list did not end at the first run before the window")
    if got["blacksmith"]["stop"] != 2250 or got["max_age_minutes"] != 75:  # 3,000 - 1.25 h x 600
        wrong.append(f"stop {got['blacksmith']['stop']} or max_age {got['max_age_minutes']}, want 2250 and 75")
    again = FakeApi()
    got2, _ = meter(again, "org", ledger, now)
    if got2["blacksmith"]["used"] != 10.5 or again.calls >= api.calls:
        wrong.append(f"a second pass re-read finished runs ({again.calls} calls vs {api.calls}) or changed the count")
    busy = {"version": 2, "runs": {"alpha/9": {"u": "x", "h": "2026-11-02T15", "m": 1000}}}
    if meter(FakeApi(), "org", busy, now)[0]["blacksmith"]["stop"] != 1750:  # 3,000 - 1.25 h x 1,000
        wrong.append("a busier hour than the floor did not lower the stop")
    if stop_for(5000) != 0:
        wrong.append("a burn past the free minutes did not stop at 0")

    # Known bad: each must read complete: false or raise Unreadable, never a quiet undercount.
    capped, _ = meter(CappedApi(), "org", {}, now)
    if capped["complete"] is not False:
        wrong.append("a run list cut off at 1,000 of 2,500 still said complete")
    if meter(FakeApi(job_total=500), "org", {}, now)[0]["complete"] is not False:
        wrong.append("a job list short of its total_count still said complete")
    starved, _ = meter(FakeApi(), "org", {}, now, reads=1)
    if starved["complete"] is not False:
        wrong.append("a pass out of reads still said complete")
    stale = {"runs": {"alpha/1": {"u": "u1", "d": "2026-11-03", "m": 999}}}
    if meter(FakeApi(), "org", stale, now)[0]["blacksmith"]["used"] != 10.5:
        wrong.append("the meter trusted a version 1 ledger")
    old = {"version": 2, "runs": {"alpha/0": {"u": "x", "h": "2026-09-01T08", "m": 999}}}
    if meter(FakeApi(), "org", old, now)[0]["blacksmith"]["used"] != 10.5:
        wrong.append("the meter counted a ledger run from before the window")
    for label, fn in [
        ("an unknown Blacksmith label", lambda: weight("blacksmith-macos")),
        ("a zero-vCPU label", lambda: weight("blacksmith-0vcpu-ubuntu-2404")),
        ("a failed run list", lambda: meter(FakeApi(fail="/actions/runs"), "org", {}, now)),
        ("a failed repo list", lambda: meter(FakeApi(fail="/orgs/org/repos"), "org", {}, now)),
        ("no token", lambda: Api("")),
    ]:
        try:
            fn()
            wrong.append(f"accepted {label}")
        except Unreadable:
            pass

    fake = FakeApi()
    write_variable(fake, "org", "{}")
    if not fake.written or fake.written.get("name") != "CI_POOL":
        wrong.append("write_variable did not create CI_POOL when it was missing")

    for line in wrong:
        print(f"ci-pool self-test: FAIL: {line}")
    if wrong:
        return 1
    print("ci-pool self-test: PASS")
    return 0


def main(argv: list) -> int:
    if argv == ["--self-test"]:
        return self_test()
    parser = argparse.ArgumentParser(prog="ci_pool.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("meter")
    p.add_argument("--org", required=True)
    p.add_argument("--ledger", required=True)
    p.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", args.org):
        print(f"::error title=ci-pool usage::not an org name: {args.org!r}")
        return 2
    now = dt.datetime.now(dt.timezone.utc)
    try:
        try:
            with open(args.ledger, encoding="utf-8") as handle:
                ledger = json.load(handle)
            ledger = ledger if isinstance(ledger, dict) else {}
        except (OSError, ValueError):
            ledger = {}
        api = Api(os.environ.get("GH_TOKEN", ""))
        reading, ledger = meter(api, args.org, ledger, now)
        reading["api_calls"] = api.calls
        with open(args.ledger, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, separators=(",", ":"))
        print(json.dumps(reading, indent=1))
        if args.write:
            write_variable(api, args.org, json.dumps(reading, separators=(",", ":")))
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            b = reading["blacksmith"]
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"## CI pool\n\n| Used | Stop | Left | Peak hour | Complete |\n|---|---|---|---|---|\n"
                             f"| {b['used']} min | {b['stop']} min | {b['left']} min | {b['peak_hour']} min "
                             f"| {reading['complete']} |\n\nAPI calls: {api.calls}.\n")
        return 0
    except Unreadable as err:
        print(f"::warning title=ci-pool could not check::{err}; CI_POOL is left as it was, and goes stale")
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
