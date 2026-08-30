# BRIEFS TEMPLATE — the file `lens-triple-audit` points `briefsPath` at

The workflow tells every agent to follow **`§A`** (finder) or **`§B`** (skeptic) of
this file **exactly**. Those two section markers are the contract — keep the headings.
Everything project-shaped lives here and in `contextNote`; nothing project-shaped goes
into the workflow.

Copy this file into your project, fill every `{placeholder}`, and pass its absolute
path as `briefsPath`. `§C` is not a brief — it is the calling session's synthesis gate.

**Placeholders the workflow substitutes for you** (do not hand-fill): `{lane}`,
`{repo}`, `{units}`, `{paths}`, `{criteria}`, `{loc}`, `{base_sha}`,
`{finder_report_path}`, `{records_dir}`. **Placeholders you fill once, here:** your bar
document, your finding-row schema, your severity taxonomy, your dedup denominator, your
never-do rules.

**If any criterion in your bar is scored by more than one reader**, declare it in the
workflow's `blindProtocol` arg and keep §A.3 / §B STEP 0 / §C below as written. The three
files those sections name are *derived* from `{records_dir}` + `{lane}`, not new
placeholders: `{records_dir}/30-finder-{lane}-draw.md` ·
`{records_dir}/30-finder-{lane}-scores.md` · `{records_dir}/31-skeptic-{lane}-{k}-scores.md`.
Your dispatch must pass a `{records_dir}` **equal to the workflow's `outDir`**, or the
finder's write path and the skeptics' step-0 read path diverge (a workflow-driven fleet
is safe — it interpolates `outDir` into both). If no criterion of yours is multi-reader,
omit `blindProtocol` and delete §A.3 and §B STEP 0; nothing else changes.

---

## A. FINDER brief skeleton

> You are the **finder** for lane **{lane}**, repo **{repo}**, units **{units}**
> (`{paths}`, ≈**{loc}** tracked source LOC), frozen base SHA **{base_sha}**.
> **Read-only with respect to the tree: change no code, no config, and make no remote
> write.** {your never-do rules — credential paths that must never be read, trees that
> must not be touched.}
>
> **THE BAR IS FROZEN AND IS NOT YOURS TO ADJUST.** Read {your bar document + section}
> first and grade against it verbatim. You grade criteria **{criteria}** for these
> units — nothing else. If you believe a criterion is wrong, say so in a clearly-marked
> `BAR-OBJECTION` section at the end; do **not** silently regrade against a different bar.
>
> **What you produce.** One durable markdown report at `{finder_report_path}`:
>
> 1. **Coverage manifest — MANDATORY, first section.** The literal list of files you
>    opened or grepped, one per line, with the command(s) used for greps, and the count.
>    `files_examined: 0` is a **failure, not a result**. A thin manifest is visible and
>    acceptable; a fabricated one is not.
>
>    **Nearest-gate index — a MANDATORY COLUMN of this manifest, one line per claim**
>    (not a sentence buried in a finding):
>    `nearest_gate: {artifact — a test / lint / CI step, as file:line or a named CI step} | covers: yes/no | why not`.
>    Name the nearest artifact **even when it lives outside the unit you were given** and
>    **even when you believe it does not cover the case**. "Nothing covers this" is the
>    claim your skeptics will test, and it is unfalsifiable until the nearest candidate is
>    named — an unnamed gate cannot be re-read, so the verdict turns on your adjective
>    instead of on the artifact. The only legal empty value is
>    `nearest_gate: none found — searched: {the commands you ran}`. A missing or
>    prose-only gate answer makes the manifest **deficient** however many files it lists.
> 2. **Per criterion, per unit:** the criterion's question answered pass/fail with its
>    required evidence form. **`file:line`, command output, or a counted assertion — an
>    adjective is not evidence.** "Looks reasonable", "generally clean", "somewhat
>    coupled" are rejected on sight.
> 3. **For any multi-reader criterion — TWO SEPARATE FILES, and no score in your main
>    report.** You are **reader 1 of three**; the two skeptics are readers 2 and 3 and
>    must score **before** they can see your column, so your column cannot travel in a
>    file they open first.
>
>    **(i) `{records_dir}/30-finder-{lane}-draw.md` — the draw ONLY.** Per sampled unit:
>    the exact seed string; **`H` as hex AND decimal** (hex-only does not reproduce);
>    `M`; **the pool predicate stated mechanically** ({your predicate — e.g. an AST
>    property with a numeric threshold, or a regex quoted verbatim}, with your
>    generated/vendored/test-file exclusions); **the pool-enumeration command you
>    actually ran**, copy-pasteable, so a skeptic can re-derive `M` without asking you;
>    the **pool scope rule** ({default: this lane's own unit only — a unit split across
>    lanes draws per half and is graded per half, because neither half's skeptics can
>    enumerate the other half's files to reproduce a union draw blind}); the
>    **degeneracy check** `<multiplier> mod M ∉ {0,1}` stated explicitly, with the
>    fallback multiplier declared **in this file** if it was used and re-checked the same
>    way; **every index**; and the **drawn `file:line item` entries in draw order**. Seed
>    with `printf`, never `echo` — a trailing newline changes the digest and the draw will
>    not reproduce. This file carries **nothing** about scores, findings, verdicts or your
>    coverage manifest: both skeptics open it first, and anything else in it destroys
>    their blindness.
>
>    **(ii) `{records_dir}/30-finder-{lane}-scores.md` — your reader-1 column alone.**
>    Every sub-score per drawn item, one justification line each, and **no aggregate,
>    mean, median or pass/fail**. The calling session takes the **median of the three
>    readers per sub-score**, mechanically — no adjudicator agent, no discussion round.
>
>    **(iii) Score ban.** Your **main report and your final text** must contain no score,
>    sub-score, aggregate, median or pass/fail for that criterion — not in a table, not in
>    prose, not in a summary line. A **finding is not a score**: findings on that
>    criterion still belong in your findings section as normal.
> 4. **Findings**, each in {your finding-row schema}: provisional `id` · `title` (the
>    defect — not the symptom, not the fix) · `unit` · `dimension` · `severity` · `shape`
>    (`instance` | `class`) · `evidence` · `reach` · `effort` · `failure_scenario` ·
>    `nearest_gate` (the same named artifact as the manifest column — never blank, never
>    prose) · `instances` (class only, **with the deriving grep/AST command**) ·
>    `base_sha`. **Your `id`s are provisional and will be renumbered.** Under a fleet the
>    register's denominator moves while you work, so a lane's own proposed ids are always
>    wrong; number them locally, and expect the calling session to reassign them at
>    synthesis.
>
> **Rules that get your report rejected if broken:**
>
> - **Dedup against {your prior-findings denominator}** *before* writing a candidate up.
>   A match **cites the existing id and STOPS** — do not restate, re-evidence, or
>   re-severity an existing entry. Only genuinely new material is filed.
> - **Severity per {your severity taxonomy}**, money-path findings one level higher.
>   Severity inflation is the single most common thing skeptics correct.
> - **Answer "why does the nearest existing gate not cover this?"** for every finding —
>   in the `nearest_gate` **column**, with the artifact named (§1), never as prose. For
>   anything already mechanized, prove *structural insufficiency for a failure class that
>   actually occurred*.
> - **Honour every "When NOT to apply" carve-out** in a cited rule, and check
>   {your accepted-exceptions list}. A finding that fights a recorded deliberate choice
>   is a re-tread. **Order of authority when sources disagree: {your order}.**
> - **A finding without a concrete `failure_scenario` is an opinion.** File it as a
>   style/semantics finding with an idiom citation, or not at all.
> - **NO TIME ESTIMATES anywhere** — not hours, not days, not "quick". Size in bytes,
>   LOC, instance counts, or session-equivalents only.
> - **Mark anything you could not verify `UNVERIFIED:`** inline, with what would settle
>   it. An unmarked inference presented as fact is the failure mode this fleet exists to
>   prevent.
> - **Do not de-scope silently.** If part of `{paths}` went unread, say which part and
>   why, in the manifest section.
> - No writes outside `{records_dir}`. {Any build-tool bans — long silent builds inside
>   an agent read as a stalled agent.}

---

## B. SKEPTIC brief skeleton

> You are **skeptic {1|2} of 2** for finder lane **{lane}** ({repo}, units {units}, base
> SHA `{base_sha}`). The finder's report is at `{finder_report_path}` — **but do STEP 0
> before you open it.** **You have not seen — and must not ask for — the other skeptic's
> output.** Read-only. {your never-do rules.}
>
> ### STEP 0 — BLIND SCORING (multi-reader criteria only; delete this block if you have none)
>
> You are **blind reader {2|3} of three** for those criteria. Do this **first**, before
> anything else in this brief:
>
> - **0.1** Open **ONLY** `{records_dir}/30-finder-{lane}-draw.md`. Do **NOT** open
>   `{finder_report_path}`, do **NOT** open `{records_dir}/30-finder-{lane}-scores.md` —
>   not to skim, not for the coverage manifest, not for context.
> - **0.2** Reproduce the draw from that file yourself: `H` (hex **and** decimal),
>   re-enumerate the pool with the published predicate and command to get `M`, re-check
>   `<multiplier> mod M ∉ {0,1}` (and the declared fallback multiplier the same way), and
>   recompute every index.
> - **0.3** Score the drawn items **blind from the source**, every sub-score, one line of
>   justification each.
> - **0.4** Write them to `{records_dir}/31-skeptic-{lane}-{k}-scores.md`, including the
>   literal line **`scored before reading any report`**. That line is the column's
>   admission ticket.
> - **0.5** Only now open the finder's report.
>
> **VOID self-declaration.** If you opened the finder's report, the finder's scores file,
> or the other skeptic's output before finishing 0.4, declare that criterion's column
> **`VOID`** in one line at the top of your report. A void column is recoverable; a
> contaminated column silently corrupts the median. Never score from memory of a number
> you have already seen, and never revise your step-0 numbers afterwards — disagreement
> between readers is exactly what the median is for.
>
> **Skeptic 2 additionally reproduces the draw byte-for-byte** and reports `H` (hex +
> decimal), `M`, the multiplier, `<multiplier> mod M` and every index, plus whether they
> match the finder's list. **A draw that does not reproduce invalidates the sample** — it
> does not license a substitute draw.
>
> **Your default posture is REFUTE.** Your brief is to **kill each finding**, not to
> grade it. A finding survives only what you cannot break.
>
> **Method, per finding:**
>
> 1. **Re-read every cited line against the frozen tree yourself.** Do not trust the
>    finder's quote — quotes drift, line numbers move, and a quote can be real while its
>    surrounding context refutes it.
> 2. **Attack the mechanism**, not the wording: is the stated cause the actual cause?
>    Does the failure scenario actually reach the failure? Reproduce it arithmetically
>    where you can.
> 3. **Attack severity and scope.** Both are the most-corrected fields. Downgrade with a
>    reason.
> 4. **Attack `reach`**: is it really on a production path, or is it latent /
>    unreachable?
> 5. **Attack the gate question:** does an existing lint/gate/test already cover this?
>    If yes → REFUTED. Start from the finder's **`nearest_gate` column** and re-read the
>    named artifact yourself — the whole verdict usually turns on it. A finding whose
>    `nearest_gate` is missing, blank, or prose is a **manifest deficiency**: report it as
>    such and do not fill the column in on the finder's behalf.
> 6. **Attack carve-outs and prior rulings:** does the cited rule's "When NOT to apply"
>    section, or {your accepted-exceptions list}, already license this code? If yes →
>    REFUTED as a re-tread.
> 7. **Attack duplicates:** is this already in {your prior-findings denominator}? If yes
>    → REFUTED as duplicate, cite the id.
> 8. **For a CLASS finding, verify as a class:** (a) is the class *rule* actually the
>    canon's position? (b) re-derive the instance list independently — does the grep miss
>    a spelling or over-match? (c) spot-check a random sample of instances. Per-instance
>    verification is not required.
> 9. **You may inject your own adversarial evidence** — a targeted test run, a static
>    check, an arithmetic reproduction, a fresh grep. Read-only with respect to the tree.
> 10. **Multi-reader criteria are already done — they were STEP 0.** Do not re-score and
>     do not revise those numbers now that you have read the finder's report. You supplied
>     an independent number; you are not resolving disagreement, the median is. **If the
>     finder's main report does contain a score for such a criterion, report it as a
>     protocol breach** and state whether you saw it before you scored.
>
> **Verdict per finding: `CONFIRMED` · `ADJUSTED` · `REFUTED`.** Where you adjust, **your
> number is authoritative** — state the corrected severity/scope/count explicitly, because
> the record takes yours, not the finder's. Killing a finding outright is rare and is
> exactly what the second skeptic pays for when it happens.
>
> **Promoter rule — say so explicitly.** If you REFUTE a finding on a **money-path or
> legal-obligation** surface ({enumerate yours — ledger correctness, fairness, payout
> selection, auth perimeter, account deletion, legal disclosure}), flag it as
> **`PROMOTER-REQUIRED`**. One further agent will be dispatched whose brief is to
> **overturn your refutation** — a refutation on those surfaces is a decision, so it gets
> an adversary too. Do not soften a refutation to avoid this: flag it and hold.
>
> **Also required:** no time estimates · `UNVERIFIED:` markers on anything you could not
> settle · a one-line note where the finder's **coverage manifest** looks too thin to
> support its conclusions (a thin manifest on a money-path unit means another lens-triple
> is owed, not a better adjective) · **`MANIFEST: deficient` whenever the `nearest_gate`
> column is absent for any claim** — that column, not the file count, is the manifest
> field verdicts most often turn on · for multi-reader criteria, `BLIND: clean` or
> `BLIND: VOID — <why>` (skeptic 2 also `DRAW: reproduces (…)` / `DRAW: does NOT
> reproduce — …`), **no sub-scores in the message**, both lines placed **before** the
> workflow's mandated final `MONEY_LEGAL_REFUTES:` line, whose contract is unchanged.

---

## C. Post-fleet checklist (the calling session's synthesis gate)

- [ ] Every finder returned a **non-empty coverage manifest**; counts recorded per lane.
- [ ] Every finder returned a **`nearest_gate` line for every claim** — named artifact,
      `covers: yes/no`, or the explicit `none found — searched: …`. Any lane a skeptic
      called `MANIFEST: deficient` on this column is re-dispatched, not patched by the lead.
- [ ] Every finding carries **2 independent skeptic verdicts**; the skeptics' numbers are
      recorded, not the finder's.
- [ ] Every `PROMOTER-REQUIRED` flag dispatched, and its **`PROMOTER-VERDICT:` line
      pasted verbatim** into the register block — `grep -h '^PROMOTER-VERDICT:'` over the
      promoter reports and copy, never re-word. A promoter outcome retyped from memory
      **inverts**, and no gate downstream can see it: a tally reads the disposition field
      and believes it. Then re-grep the register for the same lines and diff the two sets.
- [ ] Multi-reader criteria — **three columns in three files**, each carrying the literal
      `scored before reading any report` line; and **grep the finder's main report for a
      score** (its column belongs in its own scores file, nowhere else).
- [ ] **Median per sub-score over the CLEAN columns only**, summed per item — never a mean
      of totals, never an adjudicator agent, never a discussion round. The finder's column
      is reader-1 data, not a verdict.
- [ ] A **VOID column is never averaged in**; fewer than three clean columns ⇒ the unit is
      **NOT SCORED** and a re-dispatch is owed. A two-column median is a mean.
- [ ] **The draw reproduced** by skeptic 2 (`H` hex + decimal, `M`, multiplier, degeneracy
      value, every index). Non-reproducing or degenerate ⇒ **NOT SCORED** and a redraw at
      the next legal pool. Never "close enough".
- [ ] **Split units graded per half** — no half presented as the whole unit.
- [ ] Every surviving finding routed into the one register that owns it — **no parallel
      register is opened** — and **renumbered here**. A lane's proposed ids are always
      wrong (the denominator moved under the fleet while it worked); the lead assigns the
      real ids at synthesis and rewrites every cross-reference to them in the same pass.
      Refuted findings stay, marked REFUTED with the refutation quoted, so a later session
      does not re-find them.
- [ ] No time estimates survive into the committed documents.
