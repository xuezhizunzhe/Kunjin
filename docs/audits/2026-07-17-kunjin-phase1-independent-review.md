# KunJin Phase 1 Independent Review

Reviewed on 2026-07-18. This audit evaluates the held-fund brief vertical slice,
not the complete KunJin product and not future investment performance.

## Outcome

Phase 1 passes its technical and safety completion gate. It can return a bounded,
source-linked, Chinese held-fund brief with an amount-free personal relationship
overlay. It correctly preserves useful partial facts while abstaining when an
owner action lacks current complete evidence.

The public sample is deliberately named `useful_partial`. It is not described as
healthy, decision-sufficient, or actionable. The final public run returned
`sync_status=partial`, `decision_evidence_status=insufficient`, and action
abstention. The final owner run returned an anonymous summary with
`primary_state=abstain`, `decision_evidence_state=insufficient`, and
`terminal_status=partial`.

Phase 1 does not provide 90 percent of the help required by a beginner across
buying, holding, and selling funds. It is a safety-oriented foundation, not a
complete fund-decision product.

## Final Evidence

- Full local suite: `2537 passed in 194.99s`.
- Exact failure matrix: `30 passed in 21.67s`.
- Focused brief suite after the final projection fixes: `156 passed` before the
  last three identity-metadata regressions, which separately passed `3/3`.
- Phase 1 acceptance smoke matrix: `7 passed`.
- Ruff, compileall with temporary pycache, Phase 0 and Phase 1 Bash syntax, and
  `git diff --check`: passed.
- Public live acceptance:
  `/private/tmp/kunjin-phase1-live-20260718-15`.
- Anonymous owner acceptance:
  `/private/tmp/kunjin-phase1-owner-20260718-07`.

The live directories are local acceptance evidence, not permanent source-code
attestations. Their summaries intentionally do not retain private fund codes,
amounts, shares, costs, profits, or portfolio weights.

## P1 Findings Closed

The review cycle found and fixed these high-priority defects:

1. Minimum D2 appeared both obtained and missing and could look like complete D2.
2. Tier 2 or partial action-critical facts could support `watch` or transaction
   review instead of an explicit gap and abstention.
3. A liquidation or termination event could bypass redemption terms, route
   gates, or a redemption restriction.
4. Useful partial output was mislabeled healthy, and beginner Chinese omitted
   concrete fact values, full manager teams, or precise coverage limits.
5. A failed primary source could demand manual supplementation before registered
   alternatives were checked; cross-field alternatives lost their field IDs.
6. Manual codes were not required to bind to a same-brief gap and controlled
   registry supplementation path.
7. Beginner identity text and identity evidence metadata could select or cite a
   non-target sibling share.
8. A source attempt marked usable could be shown as usable inside an incomplete
   fact gap.
9. Ordinary facts could use reserved names such as `d2` to imitate a completed
   stage gate.
10. The initial safety fix changed the immutable policy V1 checksum. The owner
    database correctly rejected that drift. V1 was restored; the stricter
    behavior now lives in engine validation without rewriting local history.

Two final fresh reviewers found no remaining P0 or P1 implementation defect after
the last fixes. The final product recheck required regeneration of the public and
owner evidence, which produced the live-15 and owner-07 directories above.

## Retained P2 And Later-Phase Bindings

- `BriefEvidenceStatus.validate()` does not itself enforce mutual exclusion among
  obtained, missing, stale, conflicted, unsupported, and cooldown fields. Current
  engine output is mutually consistent, but a future schema-hardening task should
  encode this invariant directly.
- The real live matrix proves strict abstention but does not contain a positive
  Tier 1 hard-event case with every redemption and route gate satisfied. The
  mature exit-review path is covered synthetically, not by a real external event.
- Fully exhausted manual supplementation is covered by controlled unit and smoke
  fixtures. The final public live sample correctly remained partial because an
  official alternative was still unchecked.
- Some low-level D1 and disclosure gaps use a generic controlled-sync next step,
  and duplicate facts can appear under more than one evidence scope. A later
  beginner-output pass should group them without losing stable codes.
- The live summary does not embed a Git tree or source-file hash. A future durable
  attestation format should bind acceptance output to a committed revision.
- The fee explanation proves whether fee evidence exists but is not yet a full
  share-specific fee comparison or redemption-fee calculation.
- Production redemption-term coverage remains limited, so risk-reducing actions
  commonly and correctly abstain.

These P2 items do not authorize relaxing current action gates. They bind to
schema hardening, richer evidence adapters, D2/D3 work, or Phase E rather than
interactive retries during a user request.

## Independent Score

- Phase 1 engineering and financial guardrails: **87/100**.
- Coverage of the owner's complete target workflow: **approximately 45/100**.

The second score is the relevant one for the original beginner-help question.
The range of independent reviewer estimates was 38-60 percent because scoring
weights differ, but every reviewer reached the same conclusion: the complete
workflow is well below 90 percent.

## Missing Product Capabilities

The following remain outside Phase 1 and prevent a 90-percent claim:

- broad dated news and market intelligence with cross-source verification;
- complete D2 portfolio analysis for theme, manager, top-holding overlap,
  concentration, style, industry, country, currency, credit, and duration;
- D3 candidate screening, peer comparison, share-class choice, fee break-even,
  and purchase-before-check workflow;
- Phase E holding thesis, invalidation conditions, risk-event monitoring,
  rebalancing, and mature sell-timing review;
- mature buy/add/switch-buy conclusions, post-trade simulation, and exact amount
  authorization.

No automatic trade, guaranteed return, certain market forecast, mature timing,
or exact transaction amount is implemented or implied by this phase.
