# QA-RM Testing Gates

Playground repo for testing quality gates, mutation testing, and Backstage catalog registration. Freely experiment here — nothing here is production.

## Testing

- **Unit tests:** `src/calculator.test.ts`, run via Node's built-in test runner (`node --test`). Run with `npm test`.
- **Coverage:** generated via `c8` as `coverage/lcov.info`. Run with `npm run coverage`.
- **Mutation testing:** [Stryker](https://stryker-mutator.io/) mutates `src/calculator.ts` and re-runs the unit tests against each mutant to verify they actually catch regressions, not just execute the code. Config: `stryker.conf.json`. Report: `mutation-reports/mutation.html`. Run with `npx stryker run`.
  - Latest run: **100% mutation score** (19/19 mutants killed, 0 survived).
- **CI:** runs on every push/PR via `.github/workflows/test-and-mutation.yml` — installs deps, runs unit tests with coverage, then runs Stryker mutation testing.
