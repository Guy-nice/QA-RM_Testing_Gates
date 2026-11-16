---
name: run-mutation-workflow
description: Mutation testing on changed files - score, gate, optional test generation. Use for "run mutation testing", "run the workflow", "run the mutation agent", "mutation score on my changes".
argument-hint: "[--threshold N] [--file <path>] [--languages ts,python] [--base <ref>] [--module <m>] [--full-suite] [--exclude-tests <glob>] [--remediate] [--jira-id <ID>] [--skip-sonar] [--sonar-project-key <key>] [--sonar-host <url>]"
---

Orchestrate the gate runner and the test writer as separate agent dispatches; each starts fresh. The user's arguments follow this text as `ARGUMENTS: ...`.

`<py>` is `py` on Windows, `python3` elsewhere. Scripts live in `${CLAUDE_SKILL_DIR}/scripts`, which resolves to the skill's own directory wherever it is installed.

## 1. Jira ID (fresh runs only)

Before the first dispatch, have a Jira ID, a user decline, or ask once: "Jira story/task ID for this run? (skip is fine)". A bare re-run after a failure is a fresh run and gets the ask too. A skip proceeds without `--jira-id`; a given ID goes through as `--jira-id <ID>`. The script validates it before mutating (an invalid ID is an `error:` line) and labels the issue on a final PASS.

## 2. Flags

| User says | Flag |
|---|---|
| threshold N | `--threshold N` (default 80) |
| specific file(s), any language | `--file <path>` each |
| only some languages | `--languages typescript,python` |
| Java build module (Maven module, or Gradle project such as `:core`) | `--module <m>` |
| PR number, PR/branch link, base branch | `--base <ref>` |
| whole suite, or a survivor that should already be covered | `--full-suite` |
| leave test classes out of PIT (slow, need a database) | `--exclude-tests <glob>` (Java/Kotlin; tests that need Docker are already left out when Docker is not running) |
| Sonar project key or server, repo has no `sonar-project.properties` | `--sonar-project-key <key>` `--sonar-host <url>` |
| report only, no fixes | omit `--remediate` |
| repo without Sonar | `--skip-sonar` (never on your own) |
| Sonar server without pull request analysis | `--sonar-no-pr` |
| Pull request number when the GitHub CLI (`gh`) is not available | `--sonar-pr <number>` with `--sonar-pr-branch <branch>` and `--sonar-pr-base <branch>` (default base `main`) |

Always pass `--remediate` unless the user asked for report-only: the 15-second pause in step 3 is the user's chance to cancel the LLM round. Scope defaults to uncommitted + untracked changes, else the last commit; pass `--base` only when the user named a PR, link or branch.

## 3. Rounds

1. **Round 1** - dispatch `unified-mutation-testing` with the flags. Its output ends in `NEXT: STOP` (print it to the user, done) or `NEXT: HANDOFF`.
2. **Pause** - on `HANDOFF`, print the full report to the user (verdict, score, every mutant needing tests), then run `<py> "${CLAUDE_SKILL_DIR}/scripts/countdown.py" 15`. It tells the user round 2 starts in 15s and can be interrupted; an interrupt ends the run.
3. **Round 2, LLM** - dispatch `mutation-test-writer` with the `MUTATION_HANDOFF:` line verbatim. If its report lists `newFileNeeded`, ask the user before anything else; on a yes, run `<py> "${CLAUDE_SKILL_DIR}/scripts/run.py" --regen-prompt --allow-new-files --run-id <runId>` and dispatch the writer again.
4. **Re-run the scripts** - dispatch `unified-mutation-testing` with `--verify --run-id <runId> --round 2 --threshold <N> --tests-added <testsAdded>` (the total from the writer's report, with the same `--file` and `--languages` as round 1; add `--jira-id` if given). This re-runs the engines and the analysis exactly as in round 1. Print its report: PASS ends the run.
5. **Still below threshold** - the verdict is `MANUAL_REVIEW`: a bug to report, with a bug-report draft in the run's artifacts. Print it and stop. The writer runs once per run; a second dispatch never happens, whatever the survivors look like.

New test files stay uncommitted. Never edit test or source files yourself while a run is under way, and never commit during one: hand edits between rounds bypass the writer and the round limit, and the report's scores and "tests added" would describe changes the pipeline did not make.

A report-only run the user later asks to remediate is continued, not restarted: run `<py> "${CLAUDE_SKILL_DIR}/scripts/run.py" --regen-prompt --remediate --run-id <runId>` (about a second), then `<py> "${CLAUDE_SKILL_DIR}/scripts/final_report.py" --run-id <runId>`, and go on from step 2. The pipeline refuses it when the run was already verified, passed, or failed with no survivors or uncovered mutants (engine errors that tests cannot fix; re-run instead).

## Sonar upload (final state only)

Every run ends by uploading its final state to Sonar: round 1 if it passed or remediation is off, otherwise round 2 (a round-1 failure that goes on to remediation uploads nothing). The report's `sonar:` line says `uploaded` with the Sonar quality gate, or `UPLOAD FAILED [stage]`. A failure never changes the mutation verdict; it makes the run incomplete (exit code 6). Print the report's `sonar:` lines, then act on the stage:

| Stage | Action |
|---|---|
| `token-missing`, `token-rejected` | Ask the user to create a Sonar **user token** and run `export SONAR_TOKEN=...` in their own shell, then run `<py> "${CLAUDE_SKILL_DIR}/scripts/sonar_upload.py" --project-root .` (the report is on disk; do not re-run the engines). Never ask for the token in chat. If it is pasted anyway, warn once that it is now in the transcript and should be revoked, pass it only as an environment variable for that one command, and never write it to a file, log or commit |
| `plugin-missing` | The Sonar admin must install `sonar-mutation-report-plugin`; nothing else to try |
| `no-sonar-config` | `sonar-project.properties` (with `sonar.projectKey`) or the host is missing: tell the user what to add, or that `--skip-sonar` opts out |
| `scanner-missing` | The user installs `sonar-scanner` |
| anything else | Print the message and stop |

Never print or log a token; do not retry in a loop. The Sonar report file (mutation-report.json in the project's .sonar directory) must be committed to the PR, or CI's next scan drops the mutation measures.

## Reading the result

The gate is mutation score only. Read the per-language lines next to the merged score: 95% over 200 TypeScript mutants and 20% over 10 Python ones average to a pass. A survivor that looks already covered: re-run once with `--full-suite` (test-suite narrowing in the README can miss a spec that reaches the file only through a re-export). Relay the report's verdict, per-language lines, artifact paths and timing as printed; add the writer's own token total beside it.
