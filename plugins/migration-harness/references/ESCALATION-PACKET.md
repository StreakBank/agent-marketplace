# Escalation packet — how the batch decision round is presented

The escalation stage exists because some sites are **gate-invisible**: they compile,
pass tests, and satisfy the invariant lint under two or more semantically different
choices. Transforming those autonomously is a silent guess. The packet is how all of
them are put in front of the owner at once — one round, one-tap decisions.

Three rules, validated in real runs:

1. **Concrete diff, not abstract question.** "Keep the explicit edge (diff A) vs
   revert and defer (diff B)" beats "how should we handle the edge?". Stage the work
   both ways when cheap; show real code.
2. **Default-escalate on uncertainty.** Over-escalating costs one batched question;
   guessing wrong ships a silent semantic error that passes every gate.
3. **Batch.** Classify all sites first; present the semantic set together. No
   death-by-a-thousand-questions mid-transform. (Multiple *rounds* are a smell that
   discovery was incomplete.)

## Packet shape

For each decision (one decision may cover several sites):

- **Title + the sites it covers** (ledger ids, file:line).
- **Why the gate can't tell** — one sentence (this is the site's
  `classificationReason`).
- **Options** (2–4), each with:
  - a concrete preview: the actual diff, the type/case-set sketch, or before/after
    code — not prose;
  - when the decision affects multiple consumers, a **per-consumer mapping table**
    (consumer → what it consumes today → what it gets under this option), so the
    owner can see the blast radius cell-for-cell;
  - the cost/risk one-liner.
- **A recommendation with its reason** — the packet author has read the evidence;
  say what you'd pick. Put the recommended option first.

## Recording

Every answer lands in the ledger immediately:

```sh
node "$ML" decide --site s3 --choice "Option A — keep the explicit edge" --by <owner> --notes "<any caveat the owner stated>"
```

The gate blocks until every semantic site has a decision, so an unanswered packet is
mechanically un-committable, not just procedurally discouraged. If the owner's answer
changes the plan for *mechanical* sites too (a discovery premise was wrong), update
the inventory before dispatching transforms — don't patch mid-wave.
