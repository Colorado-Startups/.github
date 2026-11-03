#!/usr/bin/env python3
"""The org's CI pool: which runner a job gets, how many free Blacksmith minutes the whole org has used, and whether a
change touched a job's paths.

Paul, 2026-11-03: "No one repo gets assigned the minutes." Blacksmith's free tier (3,000 x64 2-vCPU minutes a month per
organization, docs.blacksmith.sh/blacksmith-runners/overview) and GitHub's included Actions minutes are one pool every
Colorado Startups repo draws on. Free tiers and included minutes only, never pay-per-use on a card (Paul, 2026-11-02).

    ci_pool.py choose --size=standard|short [--arch=x64|arm]
        The runner for one job, as JSON for fromJSON in runs-on, to $GITHUB_OUTPUT as runner= and reason=. Reads the
        meter's reading from $CI_POOL (the org variable) and the off switch from $CI_RUNNER. Always exits 0.
    ci_pool.py changes --paths=LINES [--base=SHA]
        run=true when the change since BASE touched a path that matches one glob, to $GITHUB_OUTPUT. Always exits 0.
    ci_pool.py meter --org=ORG --ledger=FILE [--write]
        Reads every repo's job records with $GH_TOKEN (the meter App's token), updates the ledger, and prints the
        reading; --write also stores it in the org variable CI_POOL. Exit 0 read, 3 could not read.
    ci_pool.py --self-test
        Seeded known-good and known-bad cases for all three.

The chooser:

- A short job (seconds of shell, git or an API call, under GitHub's 15-minute single-CPU limit) gets ubuntu-slim. GitHub
  bills Linux 1-core at $0.002 a minute and Linux 2-core at $0.006 (docs.github.com/en/billing/reference/
  actions-runner-pricing, read 2026-11-03), and every job bills at least one minute. A free Blacksmith minute saves
  three times as much on a standard job as on a short one, so short jobs never draw on the Blacksmith pool.
- A standard job gets Blacksmith while the meter's reading is fresh, complete and under STOP_MINUTES, and the
  GitHub-hosted standard runner otherwise. Anything it cannot read means GitHub-hosted, never Blacksmith past free:
  no reading, a malformed one, one older than MAX_AGE, one from the future, one the meter marked incomplete.
- CI_RUNNER set to a standard GitHub-hosted label is the off switch: it wins for standard jobs. A Blacksmith label or
  any other label there is ignored, since only the meter may send a job to Blacksmith.

The meter runs in Colorado-Startups/.github on a schedule (.github/workflows/ci-pool-meter.yml), a public repo, so its
own minutes on standard runners cost nothing. A repo's GITHUB_TOKEN cannot read another repo, so the meter uses a
GitHub App's installation token (ci-pool/README.md lists what Paul creates). It counts each Blacksmith job of every
attempt, rounded up to a whole minute and weighted as Blacksmith counts its free minutes, over a rolling 31 days, which
covers any day the free tier resets on. It writes the reading to the org variable CI_POOL, which every repo's
workflows read as ${{ vars.CI_POOL }} with no token and no API call. Blacksmith publishes no usage API; its CLI's
`blacksmith usage` needs an org token and a downloaded binary, so it is the cross-check, not the source.

Standard library only. Exit codes follow the house contract: 0 done, 2 usage error, 3 could not check.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
# Blacksmith's free tier, per organization (docs.blacksmith.sh/blacksmith-runners/overview, read 2026-11-03).
BLACKSMITH_FREE_MINUTES = 3000
# The pool stops at 90% of the free minutes. The 300 left cover what the meter cannot see in time: up to an hour
# between readings, runs still going, and a re-run of an old run past the first page.
STOP_MINUTES = 2700
# The meter runs hourly; a reading older than this means the meter stopped, so the chooser stops trusting it.
MAX_AGE = dt.timedelta(hours=3)
FUTURE_SLACK = dt.timedelta(minutes=10)
WINDOW = dt.timedelta(days=31)
# The Blacksmith app was installed on the org on 2026-10-31 (gh api /orgs/Colorado-Startups/installations): no run
# before that day can hold a Blacksmith job, so the meter reads nothing older.
BLACKSMITH_SINCE = dt.datetime(2026, 10, 31, tzinfo=dt.timezone.utc)
# GitHub's included Actions amount for the org, in dollars of gross use; past it the $0 budget stops CI org-wide
# (gh api /organizations/Colorado-Startups/settings/billing/budgets, read 2026-11-03).
GITHUB_INCLUDED_USD = 300.0

GITHUB_STANDARD = "ubuntu-latest"
GITHUB_SHORT = "ubuntu-slim"
STANDARD_LABELS = {"ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04"}
BLACKSMITH = {"x64": "blacksmith-2vcpu-ubuntu-2404", "arm": "blacksmith-2vcpu-ubuntu-2404-arm"}
LABEL_RE = re.compile(r"blacksmith-(\d+)vcpu-ubuntu-\d+(-arm)?")
# A 2-vCPU ARM minute counts as 0.625 x64 minutes (docs.blacksmith.sh/blacksmith-runners/overview).
ARM_WEIGHT = 0.625
# One pass reads at most this many job lists. A first pass, or a backlog, takes several passes; until the ledger
# catches up the reading says complete: false and the chooser keeps every job on GitHub-hosted.
READS_PER_PASS = 3000
PAGE = 100
NEVER = "0000000000000000000000000000000000000000"


class UsageError(Exception):
    """A bad argument: exit 2."""


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


def output(values: dict) -> None:
    for key, value in values.items():
        print(f"{key}={value}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


# ---- choose ------------------------------------------------------------------------------------------------------


def pool_used(raw: str, now: dt.datetime) -> float:
    """The org's Blacksmith minutes from the meter's reading, or Unreadable for anything the chooser must not trust."""
    if not raw or not raw.strip():
        raise Unreadable("no meter reading: the org variable CI_POOL is empty")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise Unreadable("the CI_POOL reading is not JSON") from None
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise Unreadable("the CI_POOL reading is not version 1")
    try:
        at = parse_time(doc.get("at"))
    except ValueError:
        raise Unreadable("the CI_POOL reading has no valid time") from None
    if at > now + FUTURE_SLACK:
        raise Unreadable(f"the CI_POOL reading is from the future ({iso(at)})")
    if now - at > MAX_AGE:
        raise Unreadable(f"the CI_POOL reading is stale ({iso(at)}); the meter has stopped")
    if doc.get("complete") is not True:
        raise Unreadable("the meter has not read every run yet (complete is not true)")
    used = (doc.get("blacksmith") or {}).get("used") if isinstance(doc.get("blacksmith"), dict) else None
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
        raise Unreadable("the CI_POOL reading has no valid blacksmith.used")
    return float(used)


def choose(size: str, arch: str, pool: str, override: str, now: dt.datetime) -> tuple:
    """(runner label, reason). Never raises: anything unexpected is GitHub-hosted."""
    if size == "short":
        return GITHUB_SHORT, "short job: ubuntu-slim, GitHub's cheapest runner; it never draws on the Blacksmith pool"
    override = (override or "").strip()
    if override in STANDARD_LABELS:
        return override, f"CI_RUNNER is {override}, the off switch"
    note = f"; CI_RUNNER {override!r} ignored: only the meter sends a job to Blacksmith" if override else ""
    try:
        used = pool_used(pool, now)
    except Unreadable as err:
        return GITHUB_STANDARD, f"GitHub-hosted: {err}{note}"
    except Exception as err:  # noqa: BLE001 - the chooser never fails a run; unknown is GitHub-hosted
        return GITHUB_STANDARD, f"GitHub-hosted: {type(err).__name__}{note}"
    if used >= STOP_MINUTES:
        return GITHUB_STANDARD, f"GitHub-hosted: the org has used {used:.0f} Blacksmith minutes, at or past the stop of {STOP_MINUTES}{note}"
    label = BLACKSMITH.get(arch, BLACKSMITH["x64"])
    return label, f"Blacksmith: the org has used {used:.0f} of {STOP_MINUTES} pool minutes ({BLACKSMITH_FREE_MINUTES} free){note}"


# ---- changes -----------------------------------------------------------------------------------------------------


def glob_re(pattern: str) -> re.Pattern:
    """A path glob: ** crosses folders, * and ? stay inside one, a trailing / means the whole folder."""
    if pattern.endswith("/"):
        pattern += "**"
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out)


def touched(files: list, patterns: list) -> bool:
    regexes = [glob_re(p) for p in patterns]
    return any(r.fullmatch(f) for r in regexes for f in files)


def changed_files(base: str) -> list:
    """Files changed from BASE to HEAD, or None when git cannot say (a first push, a shallow clone, a gone commit)."""
    if not base or base == NEVER or not re.fullmatch(r"[0-9a-f]{40}", base):
        return None
    try:
        out = subprocess.run(["git", "diff", "--name-only", "-z", f"{base}...HEAD"], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [f for f in out.stdout.decode("utf-8", "replace").split("\0") if f]


def changes(paths: str, base: str) -> tuple:
    """(run, reason). Unknown means run: a skipped job that should have run is worse than a minute spent."""
    patterns = [p.strip() for p in (paths or "").splitlines() if p.strip() and not p.strip().startswith("#")]
    if not patterns:
        return True, "no paths: every change runs"
    files = changed_files(base)
    if files is None:
        return True, "could not list the change: run"
    if touched(files, patterns):
        return True, f"{len(files)} files changed; at least one matches the paths"
    return False, f"{len(files)} files changed; none matches the paths"


# ---- meter -------------------------------------------------------------------------------------------------------


class Api:
    """The GitHub REST reads and the one write the meter makes. The self-test passes a fake with the same methods."""

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


def label_class(labels: list) -> str:
    """blacksmith, slim, standard or other (larger, macOS, Windows, self-hosted), for the misrouted count."""
    labels = [str(x) for x in labels or []]
    if any(x.startswith("blacksmith-") for x in labels):
        return "blacksmith"
    if labels == [GITHUB_SHORT]:
        return "slim"
    if len(labels) == 1 and labels[0] in STANDARD_LABELS:
        return "standard"
    return "other"


def job_brief(job: dict) -> list:
    """[name, runner class, seconds] of a job that ran, or None: what the misrouted count needs, kept small."""
    if not job.get("started_at") or not job.get("completed_at") or not job.get("runner_name"):
        return None
    seconds = (parse_time(job["completed_at"]) - parse_time(job["started_at"])).total_seconds()
    return [str(job.get("name")), label_class(job.get("labels")), round(seconds)]


def misrouted(cls: str, seconds: float, pool_left: bool) -> str:
    """Why a finished job used paid or over-budget capacity while free capacity was left, or "" when it did not.
    pool_left is the pool's state at this reading, not when the job ran."""
    if cls == "standard" and seconds <= 60:
        return "short job on a 2-core runner, not ubuntu-slim"
    if cls == "standard" and pool_left:
        return "GitHub-hosted minutes while Blacksmith's free minutes were left"
    if cls == "blacksmith" and not pool_left:
        return "Blacksmith past the pool's stop"
    if cls == "other":
        return "a larger, macOS, Windows or self-hosted runner"
    return ""


def paged(api, path: str, key: str, params: dict | None = None, limit: int = 50):
    for page in range(1, limit + 1):
        doc = api.get(path, {**(params or {}), "per_page": PAGE, "page": page})
        items = doc.get(key) if isinstance(doc, dict) else doc
        if not isinstance(items, list):
            raise Unreadable(f"GET {path}: no {key} list")
        yield items
        if len(items) < PAGE:
            return


def meter(api, org: str, ledger: dict, now: dt.datetime, reads: int = READS_PER_PASS) -> tuple:
    """(reading, ledger). ledger maps "repo/run id" to {"u": updated_at, "d": day, "m": minutes, "j": [misrouted]}
    for finished runs; a run still going is read again next pass."""
    start = max(now - WINDOW, BLACKSMITH_SINCE)
    runs = {k: v for k, v in (ledger.get("runs") or {}).items() if v.get("d", "") >= start.date().isoformat()}
    complete = True
    going = {}
    repos = [r["name"] for page in paged(api, f"/orgs/{org}/repos", "", {"type": "all"}) for r in page if not r.get("archived")]
    latest = {}
    for repo in sorted(repos):
        for page in paged(api, f"/repos/{org}/{repo}/actions/runs", "workflow_runs", {"created": f">={start.date().isoformat()}"}):
            new = False
            for run in page:
                key = f"{repo}/{run['id']}"
                done = run.get("status") == "completed"
                latest.setdefault((repo, run.get("workflow_id")), key)
                if done and runs.get(key, {}).get("u") == run.get("updated_at"):
                    continue
                new = True
                if reads <= 0:
                    complete = False
                    continue
                jobs = []
                for jpage in paged(api, f"/repos/{org}/{repo}/actions/runs/{run['id']}/jobs", "jobs", {"filter": "all"}, limit=10):
                    reads -= 1
                    jobs.extend(jpage)
                minutes = sum(job_minutes(j, now) for j in jobs)
                entry = {"u": run.get("updated_at"), "d": parse_time(run["created_at"]).date().isoformat(), "m": minutes,
                         "c": [b for b in map(job_brief, jobs) if b]}
                if done:
                    runs[key] = entry
                else:
                    going[key] = entry
            if not new:
                break
    used = sum(v["m"] for v in runs.values()) + sum(v["m"] for v in going.values())
    pool_left = used < STOP_MINUTES
    by_repo, wrong = {}, {}
    for key, v in {**runs, **going}.items():
        repo = key.split("/", 1)[0]
        by_repo[repo] = by_repo.get(repo, 0) + v["m"]
    # The latest run of each workflow among a repo's newest runs keeps its job list for the misrouted count; every
    # other run keeps only its minutes, so the ledger stays small.
    latest_keys = set(latest.values())
    for key, v in runs.items():
        if key not in latest_keys:
            v.pop("c", None)
            continue
        for name, cls, seconds in v.get("c", []):
            why = misrouted(cls, seconds, pool_left)
            if why:
                wrong.setdefault(key.split("/", 1)[0], []).append({"run": key, "job": name, "why": why})
    reading = {
        "version": 1,
        "at": iso(now),
        "complete": complete,
        "window_start": iso(start),
        "blacksmith": {"used": round(used, 1), "free": BLACKSMITH_FREE_MINUTES, "stop": STOP_MINUTES,
                       "left": round(max(BLACKSMITH_FREE_MINUTES - used, 0), 1),
                       "by_repo": {k: round(v, 1) for k, v in sorted(by_repo.items()) if v}},
        "misrouted": {"jobs": sum(len(v) for v in wrong.values()), "by_repo": {k: v[:20] for k, v in sorted(wrong.items())}},
        "repos": len(repos),
    }
    return reading, {"version": 1, "runs": runs}


def github_usage(api, org: str, now: dt.datetime) -> dict:
    """This month's gross Actions use against the included amount, and days left at the latest full day's use.
    Unreadable means the field is null; it never blocks the Blacksmith reading."""
    try:
        doc = api.get(f"/organizations/{org}/settings/billing/usage", {"year": now.year, "month": now.month})
        items = [i for i in doc.get("usageItems", []) if str(i.get("product", "")).lower() == "actions"]
        gross = sum(float(i.get("grossAmount", 0)) for i in items)
        today = now.date().isoformat()
        days = {}
        for i in items:
            day = str(i.get("date", ""))[:10]
            if day and day < today:
                days[day] = days.get(day, 0.0) + float(i.get("grossAmount", 0))
        last = max(days) if days else None
        per_day = days.get(last) if last else None
        left = max(GITHUB_INCLUDED_USD - gross, 0.0)
        return {"gross_usd": round(gross, 2), "included_usd": GITHUB_INCLUDED_USD, "left_usd": round(left, 2),
                "latest_day": last, "latest_day_usd": round(per_day, 2) if per_day is not None else None,
                "days_left": round(left / per_day, 1) if per_day else None}
    except (Unreadable, ValueError, TypeError, AttributeError) as err:
        return {"could_not_check": str(err) or type(err).__name__}


def write_variable(api, org: str, value: str) -> None:
    body = {"name": "CI_POOL", "value": value, "visibility": "all"}
    try:
        api.request("PATCH", f"/orgs/{org}/actions/variables/CI_POOL", body=body)
    except FileNotFoundError:
        api.request("POST", f"/orgs/{org}/actions/variables", body=body)


# ---- self-test ---------------------------------------------------------------------------------------------------


class FakeApi:
    """Two repos: one Blacksmith run of 3 jobs, one GitHub-hosted run, one run still going, one repo archived."""

    def __init__(self, fail: str = ""):
        self.fail = fail
        self.calls = 0
        self.written = None

    def get(self, path: str, params: dict | None = None):
        self.calls += 1
        if self.fail and self.fail in path:
            raise Unreadable(f"GET {path}: HTTP 500")
        if path.endswith("/repos"):
            return [{"name": "alpha"}, {"name": "beta"}, {"name": "old", "archived": True}]
        if path == "/repos/org/alpha/actions/runs":
            return {"workflow_runs": [
                {"id": 2, "workflow_id": 1, "status": "in_progress", "updated_at": "u2", "created_at": "2026-11-03T09:00:00Z"},
                {"id": 1, "workflow_id": 1, "status": "completed", "updated_at": "u1", "created_at": "2026-11-03T08:00:00Z"}]}
        if path == "/repos/org/beta/actions/runs":
            return {"workflow_runs": [
                {"id": 3, "workflow_id": 7, "status": "completed", "updated_at": "u3", "created_at": "2026-11-02T08:00:00Z"}]}
        if path.endswith("/jobs") and (params or {}).get("filter") != "all":
            raise Unreadable(f"GET {path}: the meter must read every attempt (filter=all)")
        bs = {"labels": ["blacksmith-2vcpu-ubuntu-2404"], "runner_name": "bs"}
        if path.endswith("/runs/1/jobs"):
            return {"jobs": [
                {**bs, "name": "a", "started_at": "2026-11-03T08:00:00Z", "completed_at": "2026-11-03T08:01:30Z"},  # 2
                {**bs, "name": "b", "labels": ["blacksmith-4vcpu-ubuntu-2404"], "started_at": "2026-11-03T08:00:00Z",
                 "completed_at": "2026-11-03T08:02:00Z"},  # 2 min x 2 = 4
                {**bs, "name": "c", "labels": ["blacksmith-2vcpu-ubuntu-2404-arm"], "started_at": "2026-11-03T08:00:00Z",
                 "completed_at": "2026-11-03T08:04:00Z"}]}  # 4 x 0.625 = 2.5
        if path.endswith("/runs/2/jobs"):
            return {"jobs": [{**bs, "name": "d", "started_at": "2026-11-03T09:58:10Z", "completed_at": None}]}  # 2
        if path.endswith("/runs/3/jobs"):
            return {"jobs": [
                {"name": "lint", "labels": ["ubuntu-latest"], "runner_name": "gh", "started_at": "2026-11-02T08:00:00Z",
                 "completed_at": "2026-11-02T08:00:20Z"},
                {"name": "test", "labels": ["ubuntu-latest"], "runner_name": "gh", "started_at": "2026-11-02T08:00:00Z",
                 "completed_at": "2026-11-02T08:05:00Z"},
                {"name": "gate", "labels": ["ubuntu-slim"], "runner_name": "gh", "started_at": "2026-11-02T08:00:00Z",
                 "completed_at": "2026-11-02T08:00:05Z"},
                {"name": "skipped", "labels": ["ubuntu-latest"], "runner_name": None, "started_at": None, "completed_at": None}]}
        if "billing/usage" in path:
            return {"usageItems": [
                {"date": "2026-11-01T00:00:00Z", "product": "actions", "grossAmount": 20.0},
                {"date": "2026-11-02T00:00:00Z", "product": "actions", "grossAmount": 30.0},
                {"date": "2026-11-02T00:00:00Z", "product": "Packages", "grossAmount": 5.0},
                {"date": "2026-11-03T00:00:00Z", "product": "actions", "grossAmount": 9.0}]}
        raise Unreadable(f"GET {path}: unexpected in the fake")

    def request(self, method, path, params=None, body=None):
        if method == "PATCH":
            raise FileNotFoundError(path)
        self.written = body


def self_test() -> int:
    wrong = []
    now = dt.datetime(2026, 11, 3, 10, 0, tzinfo=dt.timezone.utc)

    def reading(**change):
        doc = {"version": 1, "at": "2026-11-03T09:17:00Z", "complete": True, "blacksmith": {"used": 1200}}
        for key, value in change.items():
            if key == "used":
                doc["blacksmith"]["used"] = value
            elif value is None:
                doc.pop(key)
            else:
                doc[key] = value
        return json.dumps(doc)

    def picks(label, want, size="standard", arch="x64", pool=None, override=""):
        got, reason = choose(size, arch, reading() if pool is None else pool, override, now)
        if got != want:
            wrong.append(f"choose, {label}: got {got} ({reason}), want {want}")

    # Known good: the pool has free minutes, so a standard job goes to Blacksmith; ARM gets the ARM label.
    picks("fresh reading under the stop", BLACKSMITH["x64"])
    picks("fresh reading, ARM job", BLACKSMITH["arm"], arch="arm")
    picks("a short job", GITHUB_SHORT, size="short")
    picks("a short job never draws on Blacksmith", GITHUB_SHORT, size="short", override="blacksmith-2vcpu-ubuntu-2404")
    picks("the off switch", "ubuntu-24.04", override="ubuntu-24.04")
    picks("one minute under the stop", BLACKSMITH["x64"], pool=reading(used=STOP_MINUTES - 1))
    # Known bad: each of these must mean GitHub-hosted, never Blacksmith past free.
    for label, pool in [
        ("no reading", ""),
        ("whitespace", "  "),
        ("not JSON", "{used: 5"),
        ("a JSON list", "[1]"),
        ("version 2", reading(version=2)),
        ("no time", reading(at=None)),
        ("a time without a zone", reading(at="2026-11-03T09:17:00")),
        ("a stale reading", reading(at="2026-11-03T06:00:00Z")),
        ("a reading from the future", reading(at="2026-11-03T11:00:00Z")),
        ("an incomplete reading", reading(complete=False)),
        ("complete as a string", reading(complete="true")),
        ("at the stop", reading(used=STOP_MINUTES)),
        ("past the free minutes", reading(used=3500)),
        ("negative minutes", reading(used=-1)),
        ("minutes as a string", reading(used="12")),
        ("minutes as a boolean", reading(used=False)),
        ("no blacksmith block", reading(blacksmith=None)),
        ("blacksmith as a list", reading(blacksmith=[1])),
    ]:
        picks(label, GITHUB_STANDARD, pool=pool)
    picks("a Blacksmith override past the stop", GITHUB_STANDARD, pool=reading(used=2900), override="blacksmith-2vcpu-ubuntu-2404")
    picks("a larger-runner override", BLACKSMITH["x64"], override="ubuntu-latest-16-cores")
    picks("a self-hosted override, no reading", GITHUB_STANDARD, pool="", override="self-hosted")

    # changes: globs and the unknown-means-run rule.
    for pattern, path, want in [
        ("site/**", "site/a/b.ts", True), ("site/", "site/x", True), ("site/*", "site/a/b.ts", False),
        ("**/*.py", "tools/x.py", True), ("**/*.py", "x.py", True), ("*.md", "docs/a.md", False),
        ("package.json", "package.json", True), ("package.json", "site/package.json", False),
        (".github/workflows/**", ".github/workflows/ci.yml", True), ("a?c", "abc", True), ("a?c", "a/c", False),
    ]:
        if touched([path], [pattern]) is not want:
            wrong.append(f"touched({path!r}, {pattern!r}) is not {want}")
    for label, base in [("no base", ""), ("a first push", NEVER), ("not a SHA", "main"), ("a flag", "--output=x")]:
        if changes("site/**", base)[0] is not True:
            wrong.append(f"changes with {label} did not run")
    if changes("# only a comment\n", "")[0] is not True:
        wrong.append("changes with no paths did not run")

    # meter: weights, every attempt, the run still going, the ledger and the misrouted count.
    api = FakeApi()
    got, ledger = meter(api, "org", {}, now)
    if got["blacksmith"]["used"] != 10.5:  # 2 + 4 + 2.5 finished, 2 still going
        wrong.append(f"meter used {got['blacksmith']['used']}, want 10.5")
    if got["blacksmith"]["by_repo"] != {"alpha": 10.5} or got["repos"] != 2 or got["complete"] is not True:
        wrong.append(f"meter by_repo, repos or complete wrong: {got}")
    if sorted(ledger["runs"]) != ["alpha/1", "beta/3"]:
        wrong.append(f"ledger keeps {sorted(ledger['runs'])}, want the two finished runs")
    whys = sorted(x["why"] for x in got["misrouted"]["by_repo"].get("beta", []))
    if whys != ["GitHub-hosted minutes while Blacksmith's free minutes were left", "short job on a 2-core runner, not ubuntu-slim"]:
        wrong.append(f"misrouted for beta: {whys}")
    again = FakeApi()
    got2, _ = meter(again, "org", ledger, now)
    if got2["blacksmith"]["used"] != 10.5 or again.calls >= api.calls:
        wrong.append(f"a second pass re-read finished runs ({again.calls} calls vs {api.calls}) or changed the count")
    starved, _ = meter(FakeApi(), "org", {}, now, reads=1)
    if starved["complete"] is not False:
        wrong.append("a pass out of reads still said complete")
    if choose("standard", "x64", json.dumps(starved), "", now)[0] != GITHUB_STANDARD:
        wrong.append("an incomplete meter reading still chose Blacksmith")
    old = {"runs": {"alpha/0": {"u": "x", "d": "2026-09-01", "m": 999}}}
    if meter(FakeApi(), "org", old, now)[0]["blacksmith"]["used"] != 10.5:
        wrong.append("the meter counted a run from before the window")
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
    if choose("standard", "x64", json.dumps(got), "", now)[0] != BLACKSMITH["x64"]:
        wrong.append("the meter's own reading did not round-trip through the chooser")

    usage = github_usage(FakeApi(), "org", now)
    if usage != {"gross_usd": 59.0, "included_usd": 300.0, "left_usd": 241.0, "latest_day": "2026-11-02",
                 "latest_day_usd": 30.0, "days_left": 8.0}:
        wrong.append(f"github_usage: {usage}")
    if "could_not_check" not in github_usage(FakeApi(fail="billing"), "org", now):
        wrong.append("a failed billing read did not say could_not_check")
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


# ---- main --------------------------------------------------------------------------------------------------------


def main(argv: list) -> int:
    if argv == ["--self-test"]:
        return self_test()
    parser = argparse.ArgumentParser(prog="ci_pool.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("choose")
    p.add_argument("--size", choices=["standard", "short"], default="standard")
    p.add_argument("--arch", default="x64")
    p = sub.add_parser("changes")
    p.add_argument("--paths", default="")
    p.add_argument("--base", default="")
    p = sub.add_parser("meter")
    p.add_argument("--org", required=True)
    p.add_argument("--ledger", required=True)
    p.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    if args.mode == "choose":
        label, reason = choose(args.size, args.arch, os.environ.get("CI_POOL", ""), os.environ.get("CI_RUNNER", ""), now)
        output({"runner": json.dumps(label), "label": label, "reason": reason})
        print(f"::notice title=ci-pool runner::{label}: {reason}")
        return 0
    if args.mode == "changes":
        run, reason = changes(args.paths, args.base)
        output({"run": "true" if run else "false", "reason": reason})
        return 0
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", args.org):
        print(f"::error title=ci-pool usage::not an org name: {args.org!r}")
        return 2
    try:
        try:
            with open(args.ledger, encoding="utf-8") as handle:
                ledger = json.load(handle)
            if not isinstance(ledger, dict) or not isinstance(ledger.get("runs"), dict):
                ledger = {}
        except (OSError, ValueError):
            ledger = {}
        api = Api(os.environ.get("GH_TOKEN", ""))
        reading, ledger = meter(api, args.org, ledger, now)
        reading["github"] = github_usage(api, args.org, now)
        reading["api_calls"] = api.calls
        with open(args.ledger, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, separators=(",", ":"))
        text = json.dumps(reading, separators=(",", ":"))
        print(json.dumps(reading, indent=1))
        if args.write:
            write_variable(api, args.org, text)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            b = reading["blacksmith"]
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"## CI pool\n\n| Pool | Used | Left | Stop |\n|---|---|---|---|\n"
                             f"| Blacksmith, rolling 31 days | {b['used']} min | {b['left']} min | {b['stop']} min |\n"
                             f"\nComplete: {reading['complete']}. Misrouted jobs in the latest runs: {reading['misrouted']['jobs']}. "
                             f"API calls: {api.calls}.\n")
        return 0
    except Unreadable as err:
        print(f"::warning title=ci-pool could not check::{err}; CI_POOL is left as it was, and goes stale")
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
