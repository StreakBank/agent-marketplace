# audit-fleet

A dynamic workflow that puts **adversarial verification between a finding and the
record**. An audit fan-out where every agent reports its own findings is
self-attestation: the finder is the only reader, severity inflation goes uncorrected,
and a confidently-wrong mechanism story becomes a fix ticket. This plugin runs the
shape that fixes that.

Ships one workflow: **`lens-triple-audit`**.

## The shape

Per lane, three stages — the "lens triple" is the finder plus its two skeptics:

```
  FIND      1 finder per lane
            grades the lane against a frozen bar, cites file:line or command output,
            publishes a MANDATORY coverage manifest (files_examined: 0 is a failure,
            not a result)
              |
  REFUTE    2 skeptics per finder, dispatched in parallel, mutually blind
            default posture REFUTE — the brief is to KILL each finding, not grade it;
            each re-reads every cited line itself and returns
            CONFIRMED | ADJUSTED | REFUTED plus a verdict on the finder's manifest
              |
  PROMOTE   1 promoter per money/legal REFUTE (union across both skeptics)
            brief: OVERTURN the refutation
```

Lanes run through the pipeline independently; the two-skeptic barrier is **within a
lane only**, so lane N+1's finder does not wait on lane N's skeptics.

**The promoter rule.** When a skeptic refutes a finding on a **money-path or
legal-obligation** surface, one further agent is dispatched whose only brief is to
overturn that refutation. The reasoning: on those surfaces a refutation is itself a
decision, so it gets an adversary too. Everywhere else, two skeptics is the bar. Cost
is one agent per money/legal refute — a handful per campaign, not per finding.

Skeptics signal this to the workflow with a final line
`MONEY_LEGAL_REFUTES: <ids>` (or `none`); the workflow takes the **union** across both
skeptics and fans out one promoter per id.

## What it does NOT do

- It does not allocate finding ids, write your register, or compute a verdict. Every
  stage writes a durable markdown file under `outDir`; the workflow returns the
  agents' short data-shaped summaries and the calling session does the reconciliation.
- It does not carry a severity taxonomy, a dedup denominator, or a bar. Those are
  yours, and they reach the agents through `briefsPath` and `contextNote`.
- It does not review a diff. It grades **standing state** — which is exactly what a
  change-scoped review tool cannot do.

## Quickstart

1. Write a briefs file with two skeletons — `§A FINDER` and `§B SKEPTIC` — carrying
   your bar, your finding-row schema, your severity taxonomy and your never-do rules.
   Copy [`BRIEFS-TEMPLATE.md`](BRIEFS-TEMPLATE.md) and fill it in. The workflow tells
   each agent to follow `§A` / `§B` of that file **exactly**; everything
   project-shaped lives there and in `contextNote`, never in the workflow.
2. Freeze a base SHA per repo and split the surface into lanes.
3. Invoke the workflow with the args below. Give it a durable `outDir` — not a session
   temp directory, which does not survive a reboot.

```
/audit-fleet:lens-triple-audit
```

## The `args` contract

```jsonc
{
  "lanes": [
    {
      "id":       "F1",                        // short, filename-safe: used in every output path
      "repo":     "<repo> (branch <branch>)",  // free text, echoed into prompts
      "units":    "<what this lane covers>",   // your ledger aliases / ids
      "paths":    "<the paths the finder must read>",
      "loc":      7592,                        // tracked source LOC — sizes the manifest expectation
      "criteria": "1,2,3,7,8,9,11",            // which of YOUR bar's criteria this lane grades
      "baseSha":  "9c4d3cf",                   // frozen base; every citation is against this
      "note":     "optional per-lane note"     // thresholds, the governing rule, a known trap
    }
  ],
  "briefsPath":  "/abs/path/to/briefs.md",     // must contain §A FINDER and §B SKEPTIC skeletons
  "outDir":      "/abs/path/to/durable/dir",   // every report is written here
  "contextNote": "one paragraph: working dir, read-only posture, where the bar / schema / severity / dedup list live, and the never-do rules"
}
```

Every field is interpolated into agent prompts verbatim. Both paths must be
**absolute** — subagent working directories reset between calls.

Output files, per lane:

| Path | Written by |
|---|---|
| `<outDir>/30-finder-<laneId>.md` | the finder |
| `<outDir>/31-skeptic-<laneId>-1.md`, `…-2.md` | each skeptic |
| `<outDir>/32-promoter-<laneId>-<findingId>.md` | each promoter |

## Lessons the shape is built on

**Hand blind-scored work to the skeptics separately — never a finder's report before
scoring.** If any criterion is scored by multiple independent readers (a sampled
readability score, a rubric graded 0–2), the skeptic brief's "read the finder's report"
step **contradicts** blind scoring and voids every such column. On one production run
this was caught only after the fleet had finished: the multi-reader criterion had to
be marked NOT SCORED fleet-wide. If a criterion needs blind readers, the brief must
hand skeptics the **drawn item list separately**, require the score to be recorded
**before** the finder's report is opened, and define the sampling predicate
mechanically. A finder's column is reader-1 data, never a score.

**Seeded draws need a rejection rule.** A modular-arithmetic draw
(`seed mod M`) produces a degenerate contiguous block for some `M` — reject any `M`
where the multiplier's residue is `0` or `1`, and publish `H`, `M` and the selected
indices so the draw reproduces. Seed with `printf`, never `echo`: a trailing newline
changes the digest.

**Two skeptics pay for themselves.** Measured across two campaigns: severity
*inflation* is the single most common thing skeptics fix, and a finding refuted by
**both** skeptics is rare but real (one campaign produced a fix-priority reversal from
one). A single skeptic gives you no signal on contested findings.

**Verification-mix expectations are audit-shape-specific.** A diff-scoped campaign
measured 66% double-confirmed / 29% adjusted / 6% contested / 0% both-refuted. The same
protocol on a **standing-tree** audit measured **32.6% / 49.4% / 15.7% / 2.2%** over 178
verdict slots. Budget roughly half your findings needing a severity/scope rewrite, and
a non-zero kill rate. Do not carry a diff-derived calibration into a standing-tree run.

**Findings a skeptic refutes stay in the record**, marked REFUTED with the refutation
quoted. That is the "do not re-find" mechanism that makes prior coverage load-bearing
across sessions.

## Cost

**≈0.9M subagent tokens per lane triple on Opus** (1 finder + 2 skeptics), measured on
a production run: 31 agents (8 finders, 18 skeptics, 3 promoters, 1 extra lens) over
8,112,759 subagent tokens, grading ≈73,000 of ≈213,000 tracked source LOC. An earlier
estimate of ~0.55M per triple, derived from diff-scoped waves, **under-predicted by
~40%** — size a standing-tree fleet from the higher number.

The workflow pins `model: 'opus'` on every stage. Skeptics that read cheaply do not
refute; the cost is the point of the tool.

## Known gaps (0.1.0)

- **No argument validation.** The workflow destructures `args` directly. A missing or
  malformed `args` fails with a raw `TypeError` rather than a diagnostic, and there is
  no traversal/flag guard on `briefsPath` / `outDir` before they land in prompts. The
  script is shipped byte-for-byte as validated in production; hardening is queued.
- **`args` is assumed pre-parsed.** Some runtimes hand a workflow the caller's raw JSON
  string instead of an object. Validated only against a runtime that parses it.

## Improving this workflow

Generic discoveries — a stage-ordering trap, a better skeptic posture, a runtime
gotcha — belong **here**, so every project using the plugin compounds them. Clone the
marketplace (`gh repo clone StreakBank/agent-marketplace`), edit this plugin, bump
`plugin.json` semver plus a CHANGELOG line, run `scripts/check-coupling.sh audit-fleet`,
and push. See CONTRIBUTING §8.

Project-specific facts — your bar, your severity taxonomy, which surfaces count as
money-path, your dedup denominator — are **not** generic. They stay on your side, in
your briefs file, your `contextNote`, and a thin rule in your own estate.
