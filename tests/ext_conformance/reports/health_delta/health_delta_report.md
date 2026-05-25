# Extension Health & Regression Delta Report

> Generated: 2026-05-25T08:45:59Z
> Baseline: 2026-02-07T23:31:53Z

## Aggregate Comparison

| Metric | Baseline | Current | Delta |
|--------|----------|---------|-------|
| Tested | 223 | 222 | -1 |
| Passed | 209 | 219 | +10 |
| Failed | 14 | 3 | -11 |
| Pass rate | 93.7% | 98.6% | +4.9pp |

## Delta Summary

| Category | Count |
|----------|-------|
| Regressions | 0 |
| Fixes | 12 |
| New extensions | 209 |
| Removed | 0 |
| Unchanged failures | 1 |
| Excluded fixtures | 1 |
| **Net change** | **+12** |

## Excluded Test Fixtures

| Extension | Tier | Reason |
|-----------|------|--------|
| base_fixtures | 3 | test-only fixture cluster with intentional negative entries; covered by shape tests, excluded from release-facing extension pass-rate claims |

## Fixes (was failing, now passing)

| Extension | Tier | Previous Reason |
|-----------|------|-----------------|
| community/nicobailon-interview-tool | 4 | Extension crashes during initialization (missing data, broken API, FS dependency). |
| community/qualisero-background-notify | 3 | Extension uses relative imports to unbundled sibling/parent modules. |
| community/qualisero-pi-agent-scip | 3 | Extension uses relative imports to unbundled sibling/parent modules. |
| community/qualisero-safe-git | 3 | Extension uses relative imports to unbundled sibling/parent modules. |
| npm/aliou-pi-guardrails | 3 | Extension crashes during initialization (missing data, broken API, FS dependency). |
| npm/aliou-pi-processes | 3 | Extension uses relative imports to unbundled sibling/parent modules. |
| npm/aliou-pi-toolchain | 3 | Extension crashes during initialization (missing data, broken API, FS dependency). |
| npm/marckrenn-pi-sub-core | 3 | Extension crashes during initialization (missing data, broken API, FS dependency). |
| npm/pi-search-agent | 3 | Extension requires an npm package not available as a virtual module stub. |
| npm/pi-web-access | 3 | Extension requires an npm package not available as a virtual module stub. |
| npm/qualisero-pi-agent-scip | 3 | Extension requires an npm package not available as a virtual module stub. |
| third-party/qualisero-pi-agent-scip | 3 | Extension requires an npm package not available as a virtual module stub. |

