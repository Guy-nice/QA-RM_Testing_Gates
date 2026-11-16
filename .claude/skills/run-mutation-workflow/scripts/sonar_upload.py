# -*- coding: utf-8 -*-
"""
Step 7b: upload the mutation report to SonarQube by running the Sonar scan.

Deterministic and token-free (no LLM). The scan is the normal `sonar-scanner`
run; the `sonar-mutation-report-plugin` server plugin reads the report written
by sonar_report.py (property `sonar.mutation.reportPath`) and publishes the
measures and survivor issues.

Credential rules: the token is read ONLY from the SONAR_TOKEN environment
variable and handed to the scanner through the subprocess environment. It is
never put in argv, a file, a log line, or the gate.json. If it is missing the
upload fails with a clear instruction and the caller (the agent) asks the
user to export it in their shell.

`upload()` never raises. It returns
  {"ok": bool, "stage": str, "message": str, ...}
and a failure maps to exit code 6 in run.py. The Sonar quality gate result is
printed and returned but is NOT folded into the agent's own verdict.

Standalone:  python sonar_upload.py [--project-root DIR] [--no-pr]
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:  # package-relative import -- resolves at any install location
    from . import sonar_report
except ImportError:  # direct invocation: py .../sonar_upload.py
    sys.path.insert(0, str(Path(__file__).parent))
    import sonar_report

PLUGIN_KEY = "mutationreport"
PUBLISHED_MARKER = "Mutation Report: published"
EXIT_UPLOAD_FAILED = 6

# Coverage inputs a project's properties may reference. A local scan without
# them would replace CI's analysis with one that has no coverage.
_COVERAGE_KEYS = (
    "sonar.javascript.lcov.reportPaths", "sonar.typescript.lcov.reportPaths",
    "sonar.coverage.jacoco.xmlReportPaths", "sonar.python.coverage.reportPaths",
    "sonar.cs.opencover.reportsPaths", "sonar.cs.vscoveragexml.reportsPaths",
    "sonar.go.coverage.reportPaths", "sonar.coverageReportPaths",
)


def _fail(stage: str, message: str, **extra) -> dict:
    return {"ok": False, "stage": stage, "message": message, **extra}


def read_properties(path: Path) -> dict:
    props = {}
    if not path.exists():
        return props
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    return props


def _gh_pull_request(project_root: Path):
    """(number, head_branch, base_branch) of the current branch's PR, or None."""
    if not shutil.which("gh"):
        return None
    try:
        out = subprocess.run(
            ["gh", "pr", "view", "--json", "number,headRefName,baseRefName"],
            cwd=str(project_root), capture_output=True, text=True, timeout=30, check=False)
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
        return str(data["number"]), data["headRefName"], data["baseRefName"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def detect_target(project_root: Path, use_pr: bool = True, project_key: str = "", host: str = "",
                  pr_override=None) -> dict:
    """Host, project key and PR, in the documented precedence order: the `project_key` / `host`
    arguments (--sonar-project-key / --sonar-host), then sonar-project.properties, then the
    SONAR_PROJECT_KEY / SONAR_HOST_URL environment variables.

    sonar-project.properties is optional: a repo that configures Sonar elsewhere (the Gradle
    `sonar { }` block, a CI template) can name the project and server by argument or environment.
    Returns {"error": ...} when the target cannot be determined."""
    base = sonar_report.find_properties_dir(Path(project_root))
    props = read_properties(base / "sonar-project.properties") if base else {}
    host = host or props.get("sonar.host.url") or os.environ.get("SONAR_HOST_URL", "")
    project = project_key or props.get("sonar.projectKey") or os.environ.get("SONAR_PROJECT_KEY", "")
    how = ("Name them with --sonar-project-key and --sonar-host (or the SONAR_PROJECT_KEY and "
           "SONAR_HOST_URL environment variables), add a sonar-project.properties with "
           "sonar.projectKey and sonar.host.url, or run with --skip-sonar.")
    if not project:
        return {"error": "Sonar project key unknown: no sonar-project.properties with sonar.projectKey "
                         "in the project root or any parent up to the repo root. " + how}
    if not host:
        return {"error": "Sonar host unknown: " + how}
    pr, pr_reason = None, ""
    if use_pr and pr_override:
        # Explicit PR (--sonar-pr and friends): no gh needed.
        pr = (str(pr_override[0]), pr_override[1], pr_override[2])
    elif use_pr:
        pr = _gh_pull_request(Path(project_root))
        if pr is None:
            pr_reason = ("the GitHub CLI (gh) is not on the PATH" if not shutil.which("gh")
                         else "gh pr view found no pull request for the current branch")
    return {"host": host.rstrip("/"), "project": project, "pr": pr, "pr_reason": pr_reason,
            "props": props, "base": base or Path(project_root), "has_file": bool(base)}


class CircuitOpenError(OSError):
    """Raised instead of calling Sonar while the circuit breaker is open."""


class CircuitBreaker:
    """Stops calling an external dependency after `threshold` consecutive failures.

    Open: calls fail fast with CircuitOpenError until `reset_s` has passed, then one
    trial call is let through (half-open); success closes it, failure re-opens it.
    Only dependency failures count (network errors, HTTP 5xx, unreadable payloads):
    an HTTP 4xx means Sonar answered, so it never trips the breaker."""

    def __init__(self, threshold: int = 3, reset_s: float = 30.0, clock=time.monotonic):
        self.threshold, self.reset_s, self._clock = threshold, reset_s, clock
        self.reset()

    def reset(self) -> None:
        self._failures = 0
        self._opened_at = None

    def call(self, fn, *args, **kwargs):
        if self._opened_at is not None and self._clock() - self._opened_at < self.reset_s:
            raise CircuitOpenError(
                f"circuit breaker open: {self._failures} consecutive Sonar failures, "
                f"not calling it again for {self.reset_s:.0f}s")
        try:
            result = fn(*args, **kwargs)
        except urllib.error.HTTPError as exc:
            self._record(failed=exc.code >= 500)
            raise
        except (OSError, ValueError):
            self._record(failed=True)
            raise
        self._record(failed=False)
        return result

    def _record(self, failed: bool) -> None:
        if not failed:
            self.reset()
            return
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = self._clock()


# One breaker for every Sonar call in this process: the plugin check, the analysis polling
# and the quality gate all talk to the same server.
SONAR_BREAKER = CircuitBreaker()


def _fetch_json(url: str, token: str, timeout: int):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_json(url: str, token: str, timeout: int = 30):
    return SONAR_BREAKER.call(_fetch_json, url, token, timeout)


def check_plugin_installed(host: str, token: str):
    """None when the mutation plugin is installed, else a failure dict.

    A scan against a server without the plugin 'succeeds' and publishes
    nothing, so this is checked up front."""
    try:
        data = _http_json(f"{host}/api/plugins/installed", token)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _fail("token-rejected",
                         f"Sonar rejected the token (HTTP {exc.code}). Use a valid user token "
                         "with permission to analyze this project.")
        return _fail("server", f"Sonar returned HTTP {exc.code} for the plugin check")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _fail("server-unreachable", f"cannot reach {host}: {exc}")
    keys = {p.get("key") for p in data.get("plugins", [])}
    if PLUGIN_KEY not in keys:
        return _fail("plugin-missing",
                     f"the sonar-mutation-report-plugin is not installed on {host}. "
                     "Ask the Sonar admin to install it; a scan would publish nothing.")
    return None


def coverage_warnings(project_root: Path, props: dict) -> list:
    """Coverage inputs named in the properties file that do not exist on disk."""
    warnings = []
    for key in _COVERAGE_KEYS:
        value = props.get(key)
        if not value:
            continue
        for pattern in (v.strip() for v in value.split(",") if v.strip()):
            if not glob.glob(str(Path(project_root) / pattern), recursive=True):
                warnings.append(f"{key}={pattern} not found")
    return warnings


def _wait_for_task(project_root: Path, props: dict, host: str, token: str, timeout_s: int):
    work = Path(project_root) / props.get("sonar.working.directory", ".scannerwork")
    task_file = work / "report-task.txt"
    if not task_file.exists():
        return None, _fail("no-task", f"scanner did not write {task_file}; cannot wait for analysis")
    info = read_properties(task_file)
    ce_url = info.get("ceTaskUrl")
    if not ce_url:
        return None, _fail("no-task", "report-task.txt has no ceTaskUrl")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            task = _http_json(ce_url, token)["task"]
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            # A single blip (5xx, timeout, bad JSON) is retried; the breaker ends the wait once
            # Sonar keeps failing. A 4xx or a payload without a task will not heal: stop now.
            transient = (isinstance(exc, (OSError, ValueError)) and not isinstance(exc, CircuitOpenError)
                         and not (isinstance(exc, urllib.error.HTTPError) and exc.code < 500))
            if not transient:
                return None, _fail("analysis", f"cannot read the analysis task: {exc}")
            time.sleep(2)
            continue
        status = task.get("status")
        if status == "SUCCESS":
            return {"analysisId": task.get("analysisId"),
                    "dashboard": info.get("dashboardUrl", "")}, None
        if status in ("FAILED", "CANCELED"):
            return None, _fail("analysis", f"Sonar analysis {status}: {task.get('errorMessage', '')}".strip())
        time.sleep(2)
    return None, _fail("analysis-timeout", f"Sonar analysis not finished after {timeout_s}s")


def _quality_gate(host: str, token: str, analysis_id: str) -> dict:
    try:
        status = _http_json(f"{host}/api/qualitygates/project_status?analysisId="
                            f"{urllib.parse.quote(str(analysis_id))}", token)["projectStatus"]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return {"status": "UNKNOWN", "failed": []}
    failed = [f"{c.get('metricKey')} {c.get('actualValue', '?')} (limit {c.get('errorThreshold', '?')})"
              for c in status.get("conditions", []) if c.get("status") == "ERROR"]
    return {"status": status.get("status", "UNKNOWN"), "failed": failed}


def upload(project_root: Path, use_pr: bool = True, wait_timeout_s: int = 600,
           env=None, project_key: str = "", host: str = "", pr_override=None) -> dict:
    """Run the Sonar scan for the already-written report. Never raises."""
    project_root = Path(project_root)
    env = dict(os.environ if env is None else env)
    SONAR_BREAKER.reset()
    try:
        report = project_root / sonar_report.REPORT_RELPATH
        if not report.exists():
            return _fail("no-report", f"{report} not found; nothing to upload")

        target = detect_target(project_root, use_pr, project_key, host, pr_override)
        if "error" in target:
            return _fail("no-sonar-config", target["error"])
        host, project, pr, props = target["host"], target["project"], target["pr"], target["props"]
        base = target["base"]  # where sonar-project.properties lives; the scanner runs from here

        if pr:
            where = f"PR #{pr[0]}"
        else:
            where = f"branch analysis (no PR found: {target['pr_reason'] or 'PR detection is off'})"
            if use_pr:
                print("  WARNING: uploading as a branch analysis, so the result will not appear on "
                      "the pull request dashboards. To target a PR, pass --sonar-pr <number> "
                      "(with --sonar-pr-branch and --sonar-pr-base if needed), or put gh on the PATH.")
        print(f"  Sonar target : {host}  project={project}  {where}")

        token = env.get("SONAR_TOKEN", "")
        if not token:
            return _fail("token-missing",
                         "SONAR_TOKEN is not set. Create a Sonar user token and export it in your "
                         "shell (never paste it into the chat), then run:\n"
                         f"    python {Path(__file__).name} --project-root {project_root}")

        missing = check_plugin_installed(host, token)
        if missing:
            return missing

        scanner = shutil.which("sonar-scanner", path=env.get("PATH"))
        if not scanner:
            return _fail("scanner-missing",
                         "sonar-scanner is not installed (brew install sonar-scanner, or see the "
                         "SonarScanner CLI docs)")

        cmd = [scanner, f"-Dsonar.host.url={host}", f"-Dsonar.projectKey={project}"]
        if target["has_file"]:
            wired = sonar_report.wire_properties(project_root, base)
            if wired in ("added", "updated"):
                print(f"  wired {sonar_report.PROPERTY} into sonar-project.properties "
                      "(commit it, so CI keeps the measures)")
            for w in coverage_warnings(base, props):
                print(f"  WARNING: {w} -- this scan would publish an analysis without that coverage")
        else:
            # No properties file to wire: hand the report to the scanner directly.
            cmd.append(f"-D{sonar_report.PROPERTY}={sonar_report.report_path_from(base, project_root)}")
            print("  WARNING: no sonar-project.properties. This scan uses the scanner defaults, so "
                  "the sources, exclusions and coverage your build configures for Sonar are not "
                  "applied. For CI to keep the mutation measures, add "
                  f"{sonar_report.PROPERTY}={sonar_report.REPORT_RELPATH} to that configuration "
                  "and commit the report.")
        if pr:
            cmd += [f"-Dsonar.pullrequest.key={pr[0]}", f"-Dsonar.pullrequest.branch={pr[1]}",
                    f"-Dsonar.pullrequest.base={pr[2]}"]
        print(f"  running: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd, cwd=str(base), env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace")
        published, tail = False, []
        for line in proc.stdout:
            published = published or (PUBLISHED_MARKER in line)
            tail = (tail + [line.rstrip()])[-15:]
            if re.search(r"\s(WARN|ERROR)\s", line) or PUBLISHED_MARKER in line:
                print("    " + line.rstrip().replace(token, "***"))
        if proc.wait() != 0:
            return _fail("scan-failed", "sonar-scanner failed:\n    " +
                         "\n    ".join(l.replace(token, "***") for l in tail))
        if not published:
            return _fail("report-not-read",
                         "the scan finished but the plugin did not publish the report "
                         f"(no '{PUBLISHED_MARKER}' line). Check sonar.mutation.reportPath.")

        task, err = _wait_for_task(base, props, host, token, wait_timeout_s)
        if err:
            return err
        gate = _quality_gate(host, token, task["analysisId"])
        print(f"  Sonar quality gate: {gate['status']}"
              + (f"  failed: {'; '.join(gate['failed'])}" if gate["failed"] else ""))
        if task["dashboard"]:
            print(f"  dashboard: {task['dashboard']}")
        return {"ok": True, "stage": "done", "message": "uploaded", "host": host,
                "project": project, "pullRequest": pr[0] if pr else None,
                "qualityGate": gate["status"], "qualityGateFailed": gate["failed"],
                "dashboard": task["dashboard"]}
    except Exception as exc:  # pragma: no cover - defensive: upload must never crash the run
        return _fail("unexpected", f"unexpected upload error: {exc}")


def report_failure(result: dict) -> None:
    print(f"\n  SONAR UPLOAD FAILED [{result['stage']}]")
    for line in str(result["message"]).splitlines():
        print(f"  {line}")
    print("  The mutation gate result is unaffected; the run is marked incomplete (exit 6).")


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload the mutation report to SonarQube")
    ap.add_argument("--project-root", default=".")
    ap.add_argument("--sonar-project-key", default="", help="Sonar project key (else sonar-project.properties, "
                    "else SONAR_PROJECT_KEY)")
    ap.add_argument("--sonar-host", default="", help="Sonar server URL (else sonar-project.properties, "
                    "else SONAR_HOST_URL)")
    ap.add_argument("--no-pr", action="store_true",
                    help="Always run a branch analysis (servers without pull request support)")
    args = ap.parse_args()
    result = upload(Path(args.project_root).resolve(), use_pr=not args.no_pr,
                    project_key=args.sonar_project_key, host=args.sonar_host)
    if not result["ok"]:
        report_failure(result)
        return EXIT_UPLOAD_FAILED
    return 0


if __name__ == "__main__":
    sys.exit(main())
