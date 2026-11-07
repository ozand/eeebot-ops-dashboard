# Interface audit implementation map

Status: planning only, 2026-11-07. Governing documentation issue: #432.
This is not a second backlog: GitHub Issues own assignment/status/completion.
No implementation, ADR acceptance, private access or rollout is claimed here.

## Evidence and boundary

The interface-design review covered the public GitHub Pages site: eight menu
routes, cycle detail with/without ID, and techtree redirect. Desktop 1440x1000 and
mobile 390x844 screenshots/DOM informed the findings; some interactions were
sampled, not exhaustive. Public data changed during inspection (cycle count
3660→3661), so counts in observations are snapshots, not permanent baselines.
Not a full WCAG certification, source audit, host-health check or performance test.

## Recommendation coverage

| Surface / outcome | Governing issue | Boundary / related work |
|---|---|---|
| Day total/categories/call arithmetic | [#433](https://github.com/ozand/eeebot-ops-dashboard/issues/433) | P0 regression; no diagnosed producer cause |
| Shared header/navigation/type/controls | [#434](https://github.com/ozand/eeebot-ops-dashboard/issues/434) | Proposed DESIGN.md; preserve #38 identity |
| index.html: work/result/attention hierarchy | [#435](https://github.com/ozand/eeebot-ops-dashboard/issues/435) | Preserve #395/#390, no new rest reader |
| lineage.html: orientation/mobile list | [#436](https://github.com/ozand/eeebot-ops-dashboard/issues/436) | Preserve existing #213/#218 behavior and Proposed ADR-001 |
| cycles.html: compact feed/filter coverage | [#437](https://github.com/ozand/eeebot-ops-dashboard/issues/437) | #406/#408 retain duration semantics |
| tokens.html: scale/period/selection | [#438](https://github.com/ozand/eeebot-ops-dashboard/issues/438) | After #433; no monetary cost inference |
| lessons.html: titles/corpus/archive/search | [#439](https://github.com/ozand/eeebot-ops-dashboard/issues/439) | Preserve #246/#274 privacy/corpus distinction |
| agent.html: mixed-status clarity/navigation | [#440](https://github.com/ozand/eeebot-ops-dashboard/issues/440) | #386 source authority and #385 missingness remain separate |
| hypotheses.html: lifecycle/denominators | [#441](https://github.com/ozand/eeebot-ops-dashboard/issues/441) | No scoring or selection change |
| about.html: mobile tables/system reference | [#442](https://github.com/ozand/eeebot-ops-dashboard/issues/442) | Presentation of existing boundaries |
| cycle.html: result/attempts/omissions/no-ID | [#443](https://github.com/ozand/eeebot-ops-dashboard/issues/443) | #316 retains private detail scope; #406/#408 timing |
| techtree.html redirect | #434 regression coverage | No standalone redesign task necessary |

Shared status labels/freshness/populations are covered by ADR-006 and #434, with
page-level evidence cases in #435/#440/#441/#443. No global enum/schema migration.
The work-unit stack #421/#423/#425 and independent acceptance #427/#428 remain
separate; do not substitute this visual plan for their acceptance or installation.

## Sequence and verification

1. Diagnose/fix #433 independently of the visual proposal: confidence in numbers
   takes priority over cosmetic changes.
2. Review Proposed ADR-006 and DESIGN.md, then #434 shared shell/controls.
3. #435 first-screen meaning and #440/#442 clipping; other page issues can follow
   with separate ownership and exact-revision review. #438 depends on #433.
4. Each implementation provides relevant synthetic regressions, keyboard/mobile
   evidence and captured desktop/mobile rendering, plus existing security tests
   for affected projection paths. Do not turn browser skips into passing coverage.
5. Public verification/deployment is a separately authorized step; source success
   alone never closes a production-visible criterion. Parent #432 tracks documents,
   not completion of #433–#443.

Use native controls/current stack first; no new framework, pipeline, collector,
private read or package dependency is a prerequisite. Proposed CLI validation is
one-shot documentation tooling, not a dependency of the published dashboard.
The visual target is intentionally proposed, not a description of today's CSS.

## Design artifacts

- [Proposed ADR-006](../adr/ADR-006-evidence-first-interface-and-design-source.md)
- [Proposed DESIGN.md](../../DESIGN.md), Google alpha format pinned in the ADR.
- Current-state Lat was not changed: no behavior, architecture implementation or
  tests change in this documentation-only proposal. Future implementation must
  update applicable current-state documentation in its own repository.
