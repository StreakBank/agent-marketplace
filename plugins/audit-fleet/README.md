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
            not a result) whose nearest_gate column names, per claim, the nearest
            test/lint/CI artifact — even one outside the unit, even one it believes
            does not cover the case
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

### How the lead consumes a promoter (since 0.1.2)

Every promoter report **ends with one greppable line**, alone, in this shape:

```
PROMOTER-VERDICT: <finding-id> | OVERTURNED|UPHELD-REFUTATION | P<0-3> | <reach>
```

(The `P<n>` field is the finding's priority in **your** taxonomy as it stands after the
promoter; `<reach>` is where it lands in production. The workflow carries no taxonomy —
keep the four fields and the `PROMOTER-VERDICT:` prefix and write your own scale.)

**The lead pastes that line verbatim into the register block. It does not re-describe the
outcome.** `grep -h '^PROMOTER-VERDICT:' <outDir>/32-promoter-*.md`, paste, then re-grep
the register and diff the two sets.

This exists because of a measured failure: in one five-promoter sitting, **two outcomes
were transcribed into the register inverted** — an `UPHELD-REFUTATION` recorded as
overturned and the reverse — and **no gate could catch it**. Nothing downstream re-reads
the promoter's argument; a tally script reads the register's disposition field and
believes it, so an inverted disposition survives every subsequent check and silently
re-prioritises a money-path finding. A verbatim quote of a machine-shaped line is the only
step in the chain that is mechanically checkable, which is why it is a requirement on the
promoter's output rather than advice to the lead.

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
      "criteria": "1,2,3,7,8,9,11",            // which of YOUR bar's criteria this lane grades.
                                               //   ENUMERATE them: blindProtocol matches whole
                                               //   tokens, so a range ("1-9") silently misses.
      "baseSha":  "9c4d3cf",                   // frozen base; every citation is against this
      "note":     "optional per-lane note"     // thresholds, the governing rule, a known trap
    }
  ],
  "briefsPath":  "/abs/path/to/briefs.md",     // must contain §A FINDER and §B SKEPTIC skeletons
  "outDir":      "/abs/path/to/durable/dir",   // every report is written here
  "contextNote": "one paragraph: working dir, read-only posture, where the bar / schema / severity / dedup list live, and the never-do rules",

  // ---- optional ----
  "allowedRoot": "/abs/path/to/records",       // if set, briefsPath + outDir must live inside it
  "blindProtocol": {                           // omit unless a criterion has MULTIPLE readers
    "criteria":      "7",                      // criterion id(s) in YOUR bar that are multi-reader ("7", "7,12", or [7,12])
    "dimensions":    "naming / abstraction-level / comprehensibility",  // the sub-scores each reader records
    "poolPredicate": "<mechanical statement of what enters the pool>",  // quotable verbatim by a skeptic re-deriving M
    "scale":         "0-2",                    // default "0-2"
    "drawCount":     8,                        // default 8
    "poolScope":     "<override the per-lane pool rule>",
    "multiplier":    2654435761,               // draw multiplier; default Knuth 32-bit
    "fallbackMultiplier": 2654435789           // used when the degeneracy check rejects the first
  }
}
```

Every field is interpolated into agent prompts verbatim. **Arguments are validated
before the first agent is dispatched** and a bad one throws naming itself
(`lens-triple-audit: args.lanes[2].loc must be a positive finite number — got "7592"`)
rather than failing three agents deep. `briefsPath` / `outDir` must be **absolute**
(subagent working directories reset between calls) and are rejected if they carry a `..`
segment, a control character, or a shell metacharacter — they reach prompts that agents
turn into shell commands. `lanes[].id` must be filename-safe and unique: it is
interpolated into every output path.

Output files, per lane:

| Path | Written by |
|---|---|
| `<outDir>/30-finder-<laneId>.md` | the finder |
| `<outDir>/31-skeptic-<laneId>-1.md`, `…-2.md` | each skeptic |
| `<outDir>/32-promoter-<laneId>-<findingId>.md` | each promoter |
| `<outDir>/30-finder-<laneId>-draw.md` | the finder — `blindProtocol` only: the draw, **no scores** |
| `<outDir>/30-finder-<laneId>-scores.md` | the finder — `blindProtocol` only: its reader-1 column |
| `<outDir>/31-skeptic-<laneId>-<k>-scores.md` | each skeptic — `blindProtocol` only: its blind column |

### The blind-reader protocol (`blindProtocol`)

Declaring it makes the workflow **enforce** the ordering the next section's first lesson
is about, instead of merely documenting it. When a lane's `criteria` include a declared
blind criterion — matched mechanically against `lanes[].criteria`, never left to the
agent to judge — the workflow:

- tells the **finder** it is reader 1 of three, to write the draw and its own column to
  two **separate** files, and bans every score, sub-score, aggregate and median from its
  main report and final text (a *finding* on that criterion is not a score, and still
  belongs in the findings section);
- gives each **skeptic** a STEP 0 that runs *before* the finder's report is opened: open
  only the draw file, reproduce `H`/`M`/the indices, score blind from source, write the
  column with the literal line `scored before reading any report` — then, and only then,
  read the report, with no revision of the step-0 numbers afterwards. Skeptic 2 also
  reproduces the draw byte-for-byte; a draw that does not reproduce invalidates the
  sample rather than licensing a substitute;
- adds `BLIND: clean` / `BLIND: VOID — <why>` (and skeptic 2's `DRAW:` line) to the
  skeptic's returned data, so contamination is visible in the workflow's own output.

Adjudication stays with the calling session: **median of the three readers per sub-score,
clean columns only.** A void column is never averaged in, and fewer than three clean
columns means the unit is NOT SCORED, not scored on two. Pool scope defaults to the
lane's own unit — a unit split across lanes draws per half and is graded per half,
because neither half's skeptics can enumerate the other half's files to reproduce a union
draw blind.

`blindProtocol.criteria` is matched as **whole tokens** against `lanes[].criteria` (both
split on non-alphanumerics), so `"7"` fires on a lane grading `"1,2,3,7"` and not on one
grading `"17,27"`. The corollary is a caller obligation: a lane must **enumerate** its
criteria. A lane that writes them as a range or a shorthand — `"1-9"`, `"all"` — will
**silently skip the blind stage**, because no token in it equals `7`. Silently: there is
no warning, and that criterion is then scored non-blind.

Omit `blindProtocol` and the blind stage is absent and the stage graph is 0.1.0's. Three
prompt changes are unconditional and apply with it omitted: 0.1.1 points the finder at
"your register's row schema" where 0.1.0 named an artifact only the authoring project had
(see `PROVENANCE.md`); 0.1.2 adds the finder's mandatory `nearest_gate` manifest column
and the promoter's `PROMOTER-VERDICT:` line. `args` that 0.1.0 accepted malformed throw
since 0.1.1.

## Lessons the shape is built on

**Hand blind-scored work to the skeptics separately — never a finder's report before
scoring.** If any criterion is scored by multiple independent readers (a sampled
readability score, a rubric graded 0–2), the skeptic brief's "read the finder's report"
step **contradicts** blind scoring and voids every such column. On one production run
this was caught only after the fleet had finished: the multi-reader criterion had to
be marked NOT SCORED fleet-wide. If a criterion needs blind readers, the brief must
hand skeptics the **drawn item list separately**, require the score to be recorded
**before** the finder's report is opened, and define the sampling predicate
mechanically. A finder's column is reader-1 data, never a score. **Since 0.1.1 the
workflow enforces this** — declare `blindProtocol` and the ordering is in the prompts,
not left to a brief the agent may read out of order.

The re-run that first used the enforced protocol produced **three clean blind columns**
and a draw reproduced byte-for-byte on a criterion that had been unscorable before it —
which is the whole return: the lesson only stops costing you once the tool carries it.

**"No gate covers this" must name the gate it means.** A finder that answers the
nearest-gate question in prose — "no existing test covers this path" — hands the skeptic
an adjective instead of an artifact, and the skeptic then has to guess which gate the
finder had in mind before it can agree or refute. On one production fleet **11 of 12 lanes
were called `MANIFEST: deficient` on exactly this**, and essentially every skeptic verdict
turned on it. So since 0.1.2 the finder prompt makes it a **manifest column, not prose**:
one `nearest_gate:` line per claim, naming a test / lint / CI artifact as `file:line` or a
named CI step, **even when the artifact is outside the audited unit** and **even when the
finder believes it does not cover the case** — those are precisely the two excuses that
produced the blank column. The only legal empty value names the searches that came back
empty. The corollary for consumers: a finder cannot answer this well on a unit whose gates
live elsewhere in the repo, so give lanes read access beyond their own `paths`.

**Seeded draws need a rejection rule.** A modular-arithmetic draw
(`seed mod M`) produces a degenerate contiguous block for some `M` — reject any `M`
where the multiplier's residue is `0` or `1`, and publish `H`, `M` and the selected
indices so the draw reproduces. Publish `H` in **hex and decimal**: a hex-only `H` is
where one production draw stopped reproducing. Seed with `printf`, never `echo`: a
trailing newline changes the digest.

**A union pool across split lanes is unexecutable under blind scoring.** If a unit is
split across two lanes, neither half's skeptics can enumerate the other half's files
from their own coverage manifest, so no skeptic can reproduce a union draw blind —
handing one lane the other's file list is exactly the contamination the protocol exists
to stop. Draw per half and grade per half.

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

### Manifest sanity

**A lane's own proposed finding ids are always wrong.** Lanes run concurrently and each
one numbers its findings against the denominator it saw when it started — which the rest
of the fleet is moving underneath it the whole time. Two lanes will hand you the same id
for different defects, and every lane's ids will collide with whatever landed in the
register while the fleet ran. Treat a finder's ids as **lane-local labels**, never as
register ids: **the lead renumbers at synthesis**, in one pass, rewriting every
cross-reference (including the skeptics' and promoters' verdict lines) to the assigned
ids. Do not ask lanes to coordinate ids with each other — that is a barrier between lanes,
and the whole point of the shape is that lanes do not wait on each other.

The same caution applies to a finder's own counts: `files_examined` and a class finding's
`instances` are the finder's arithmetic, and both are re-derived by the skeptics. Take the
skeptics' numbers into the register, not the finder's.

## Cost

**≈0.9M subagent tokens per lane triple on Opus** (1 finder + 2 skeptics), measured on
a production run: 31 agents (8 finders, 18 skeptics, 3 promoters, 1 extra lens) over
8,112,759 subagent tokens, grading ≈73,000 of ≈213,000 tracked source LOC. An earlier
estimate of ~0.55M per triple, derived from diff-scoped waves, **under-predicted by
~40%** — size a standing-tree fleet from the higher number.

The workflow pins `model: 'opus'` on every stage. Skeptics that read cheaply do not
refute; the cost is the point of the tool.

## Known gaps (0.1.2)

Both 0.1.0 gaps are closed: `args` is validated field by field before any dispatch, and
`briefsPath` / `outDir` are guarded for absoluteness, `..` segments, control characters,
shell metacharacters and (optionally) an `allowedRoot` — see the `args` contract above.
`scripts/lens-triple-audit.test.mjs` covers both, and runs in CI.

Remaining:

- **`args` is still assumed pre-parsed.** A runtime that hands the workflow the caller's
  raw JSON string now gets a diagnostic that says exactly that instead of a raw
  `TypeError`, but the workflow does not parse it for you.
- **The path guard is lexical, not filesystem-resolved.** The workflow runtime is not
  guaranteed to expose Node's `path`/`fs`, so the guard is pure string logic: it rejects
  `..` segments and confines to `allowedRoot` by prefix, but does **not** resolve
  symlinks. A symlinked `outDir` still writes wherever the link points.
- **The `PROMOTER-VERDICT:` line is enforced on the promoter, not on the register.** The
  workflow can require the line, and it comes back in the workflow's own output — but it
  cannot see what the lead then writes down. The grep-and-diff step in the briefs
  template's §C is the only check on the transcription itself.
- **UNVERIFIED: the blind protocol has not been run through the plugin install path.**
  It was validated as a project-local workflow with identical script text — see
  `PROVENANCE.md`.

## Improving this workflow

Generic discoveries — a stage-ordering trap, a better skeptic posture, a runtime
gotcha — belong **here**, so every project using the plugin compounds them. Clone the
marketplace (`gh repo clone StreakBank/agent-marketplace`), edit this plugin, bump
`plugin.json` semver plus a CHANGELOG line, run `scripts/check-coupling.sh audit-fleet`
and `node --test plugins/audit-fleet/scripts/lens-triple-audit.test.mjs` — which
reproduces the runtime's function wrapper and is therefore the real syntax gate:
`node --check` on the `.js` path passes without exercising anything, and the same bytes
checked as ESM fail on the workflow's (legal, runtime-provided) top-level `return`. Then
push. See CONTRIBUTING §8.

Project-specific facts — your bar, your severity taxonomy, which surfaces count as
money-path, your dedup denominator — are **not** generic. They stay on your side, in
your briefs file, your `contextNote`, and a thin rule in your own estate.
