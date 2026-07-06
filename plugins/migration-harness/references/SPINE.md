# The migration spine — extracted, not designed

This harness was extracted from six real migrations executed on a production
multi-module codebase (config reconciliation, per-frame-read re-plumbing, dead-code
removal, parameter-narrowing refactors, a retired-instrumentation parameter sweep
across ~80 files, and a transport-to-domain-error boundary re-authoring across a
module graph). Everything below was observed in those runs; nothing is speculative.
When the harness and reality disagree, reality wins — update this file from the run
record (see SKILL.md § Improving this skill).

## The shared spine (every run exhibited these)

- **S1 — Stage sequence:** discover → classify → batch-escalate → transform →
  verify-each (mechanical) → completion gate → record. Judgment-heavy runs exercised
  every stage; mechanical sweeps degenerate gracefully (empty escalation round).
- **S2 — Discovery produces a mandatory inventory artifact** before any transform:
  a site list to file:line; for judgment migrations a consumed-distinction inventory
  (per consumer, which accessor/branch/behavior it depends on — this is what lets a
  replacement design be *derived* from evidence rather than invented); and premise
  verification. One run's brief instructed preserving a sidecar output for a consumer
  that discovery proved no longer existed — the plan flipped to delete-all *before*
  transform, not after.
- **S3 — Per-site classification** by the discriminator "can the completion gate tell
  right from wrong?", with default-escalate on uncertainty.
- **S4 — One batch escalation round.** All semantic sites presented together, each as
  concrete options (diffs, previews, per-consumer mapping tables). The largest run
  batched four contested design decisions into one round and had zero mid-transform
  questions.
- **S5 — Foundation inline, consumers fanned out.** The semantic unit (new type,
  chokepoint, shared helper, integration glue) is lead-authored; consumer sites are
  embarrassingly parallel under strict file partitioning with explicit don't-touch
  lists. Collision-free at up to 7 concurrent same-repo agents in real runs.
- **S6 — Per-site verify is mechanical, never self-attestation.** A transform agent
  instructed to "verify the dependency is declared, note if missing" reported clean
  anyway; a dedicated classpath probe caught the build-breaker. Machinery runs the
  probes and records real exit codes.
- **S7 — Adversarial verification on top** for judgment migrations: residual sweep,
  equivalence check, completeness critic. For reactive/streaming code, "equivalent"
  means the **emission/event order**, not just the settled values — one
  value-equivalent restructure was reverted because an extra combine hop reordered
  arrivals and leaked a new intermediate state.
- **S8 — The completion gate runs once, by the lead, after the fan-out** (build, full
  tests, invariant lint, visual zero-drift where applicable). The deterministic
  completion gate is most of the migration's value.
- **S9 — Closed outcome set per site:** done | skipped(reason) | deferred(reason) |
  reverted(reason) | wontfix(reason). Real runs produced reverts and reasoned skips;
  a plan whose only representable outcomes are done/deferred cannot record reality.
- **S10 — Record + learning routing:** execution record in the project's migration
  history; generic discoveries upstream to the owning plugin; project facts to the
  project shim; gate mechanisms upstream to whatever linter/CLI owns them when they
  generalize.

## The variance (recipe-supplied; the harness must not hardcode)

- **V1** Site-discovery mechanics: greps, compiler-report diffs, dependency-graph
  parses, evidence enumeration + reading.
- **V2** Transform judgment level: config edit → mechanical delete → uniform
  re-plumb → judgment re-authoring.
- **V3** Verify primitive: report-regeneration flip, screenshot zero-drift, pattern
  absence, compile/classpath probe, targeted tests, adversarial equivalence read.
- **V4** Escalation-site classification: what counts as "semantic" is
  recipe-specific (a visibility sweep escalates public entry points that must stay
  public; a deletion sweep escalates symbols the gate can't prove dead; a boundary
  re-authoring escalates the replacement design itself).
- **V5** Gate set: which project gates apply (platform compile canaries, e2e legs)
  is a project/config fact.
- **V6** Isolation: file partitioning served all six real runs at zero setup cost;
  per-site worktrees were designed but never needed. Partition is the default;
  worktrees are the opt-in for overlapping-file transforms or cheap per-site revert.

## Observed failure modes (all real; design against them)

- **F-a Self-attestation lies.** See S6. Verify is machinery.
- **F-b Mid-wave gate catches.** Compile failures surface mid-fan-out (e.g. a
  language rule the transform pattern violated in one spot); plan a fix loop inside
  the wave, not a restart.
- **F-c Value-equivalent, order-breaking transforms.** See S7 — emission order is
  part of behavioral equivalence; tests that assert exact sequences are the gate that
  catches this. Never weaken them to make a restructure pass; record the site as
  reverted/wontfix instead.
- **F-d Wrong premises in the brief.** Discovery re-verifies claims before transform.
- **F-e Sandbox-denied deletions reported as success.** Agents may be unable to
  delete files outside their working scope and report done anyway — the lead verifies
  deletions with `find`/`ls` and performs them if needed.
- **F-f Agent runs die occasionally** (content filters, connection drops). Keep
  dispatch prompts lean (reference, don't inline); on silence past ~10 minutes,
  check the tree for partial work and finish minimally rather than re-dispatching the
  same prompt.
- **F-g Piped exit codes mask failures.** Grep build output for the tool's own
  success/failure markers; never trust `cmd | tail`'s exit status.
- **F-h Blast radius beyond the obvious file types.** Removal migrations must sweep
  ALL file types — API specs, docs, generated/committed bundles, CI scripts, test
  harnesses, sidecar metadata — and use the VCS-tracked grep for the authoritative
  residual (filesystem grep over-counts ignored build output).
