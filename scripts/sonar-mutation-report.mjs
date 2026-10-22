// Converts Stryker's JSON report into the mutation-report contract that the
// sonar-mutation-report-plugin reads (schemaVersion 1). Stryker's raw file has
// no `summary` or `mutants`, so the plugin shows "No files with mutation data".
//
// Usage: node scripts/sonar-mutation-report.mjs <stryker.json> <out.json>
// Changed lines come from `git diff` against origin/$GITHUB_BASE_REF when that
// is set (pull requests); otherwise every mutant counts as new.

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { execFileSync } from "node:child_process";

const [, , inPath, outPath] = process.argv;
if (!inPath || !outPath) {
  console.error("usage: node scripts/sonar-mutation-report.mjs <stryker.json> <out.json>");
  process.exit(2);
}

const stryker = JSON.parse(readFileSync(inPath, "utf8"));

// Stryker status -> plugin count bucket. CompileError and Ignored never ran a
// test, so they are left out (the plugin does the same with NON_VIABLE).
const BUCKET = {
  Killed: "killed",
  Timeout: "timedOut",
  Survived: "survived",
  NoCoverage: "noCoverage",
  RuntimeError: "unknown",
};

const base = process.env.GITHUB_BASE_REF ? `origin/${process.env.GITHUB_BASE_REF}` : null;

// Map of file -> Set of changed line numbers (new side), or null when unknown.
function changedLines(file) {
  if (!base) return null;
  let diff;
  try {
    diff = execFileSync("git", ["diff", "-U0", "--no-color", `${base}...HEAD`, "--", file], {
      encoding: "utf8",
    });
  } catch {
    return null;
  }
  const lines = new Set();
  for (const hunk of diff.matchAll(/^@@ -\S+ \+(\d+)(?:,(\d+))? @@/gm)) {
    const start = Number(hunk[1]);
    const count = hunk[2] === undefined ? 1 : Number(hunk[2]);
    for (let n = start; n < start + count; n++) lines.add(n);
  }
  return lines;
}

// Language from the file extension, so the Sonar Languages tab is right for
// every Stryker-supported file type, not just TypeScript.
function languageOf(file) {
  if (/\.(ts|tsx|mts|cts)$/.test(file)) return "typescript";
  if (/\.(js|jsx|mjs|cjs)$/.test(file)) return "javascript";
  return "other";
}

function sumInto(target, source) {
  for (const key of Object.keys(target)) target[key] += source[key] ?? 0;
}

function emptyCounts() {
  return { total: 0, killed: 0, timedOut: 0, memoryError: 0, survived: 0, noCoverage: 0, unknown: 0, suppressed: 0, testsExecuted: 0, testKills: 0 };
}

function addMutant(counts, status) {
  const bucket = BUCKET[status];
  if (!bucket) return false;
  counts.total++;
  counts[bucket]++;
  return true;
}

const overall = emptyCounts();
const newOverall = emptyCounts();
const files = {};
const languages = {};
const mutants = [];

for (const [file, data] of Object.entries(stryker.files ?? {})) {
  const changed = changedLines(file);
  const fileOverall = emptyCounts();
  const fileNew = emptyCounts();
  const lang = languageOf(file);
  const lineCount = (data.source ?? "").split("\n").length;
  let changedCount = 0;

  for (const m of data.mutants ?? []) {
    const line = m.location?.start?.line ?? 0;
    const isNew = changed === null || changed.has(line);
    if (!addMutant(fileOverall, m.status)) continue;
    addMutant(overall, m.status);
    if (isNew) {
      addMutant(fileNew, m.status);
      addMutant(newOverall, m.status);
      if ((m.status === "Survived" || m.status === "NoCoverage") && line > 0) {
        mutants.push({
          file,
          line,
          operator: m.mutatorName ?? "unknown",
          status: m.status === "Survived" ? "SURVIVED" : "NO_COVERAGE",
        });
      }
    }
  }

  if (changed) changedCount = changed.size;
  else changedCount = lineCount;

  files[file] = {
    language: lang,
    lines: lineCount,
    changedLines: changedCount,
    overall: fileOverall,
    new: fileNew,
  };
}

// The plugin counts "tests executed" only when the engine reports it; Stryker
// does not expose it in this file, so those fields stay absent.
for (const f of Object.values(files)) {
  delete f.overall.testsExecuted;
  delete f.overall.testKills;
  delete f.new.testsExecuted;
  delete f.new.testKills;
}
delete overall.testsExecuted;
delete overall.testKills;
delete newOverall.testsExecuted;
delete newOverall.testKills;

// One entry per language, summed from its files.
for (const f of Object.values(files)) {
  const entry = (languages[f.language] ??= { overall: emptyCounts(), new: emptyCounts() });
  sumInto(entry.overall, f.overall);
  sumInto(entry.new, f.new);
}

// The gate verdict uses the same score as the plugin's gate: killed / total on
// changed lines, timeouts not counted as killed. Threshold is Stryker's `break`.
const threshold = stryker.thresholds?.break ?? 80;
const newScore = newOverall.total ? (newOverall.killed / newOverall.total) * 100 : null;
const verdict = newScore === null ? undefined : newScore >= threshold ? "PASS" : "FAIL";

const report = {
  schemaVersion: 1,
  run: {
    runId: `ci-${process.env.GITHUB_RUN_ID ?? Date.now()}`,
    threshold,
    round: 1,
    ...(verdict ? { verdict } : {}),
    tools: ["stryker"],
    commit: (() => {
      try {
        return execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim();
      } catch {
        return undefined;
      }
    })(),
  },
  summary: { overall, new: newOverall },
  languages,
  files,
  mutants,
};

mkdirSync(dirname(outPath), { recursive: true });
writeFileSync(outPath, JSON.stringify(report, null, 2) + "\n");
console.log(`wrote ${outPath}: ${overall.total} mutants, ${newOverall.total} on changed lines, ${mutants.length} actionable`);
