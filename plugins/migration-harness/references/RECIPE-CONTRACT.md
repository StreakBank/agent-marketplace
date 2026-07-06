# Recipe contract — the slots a migration recipe must fill

A **recipe** is a reusable, migration-class-specific instantiation of the harness: it
supplies the variance (SPINE.md §V1–V6) so a run only has to supply the repo and the
sites. Recipes live with whatever stack-specific plugin/catalog owns their domain —
this harness is stack-agnostic and ships none.

A recipe document must fill every slot below. A recipe missing the classification or
verify slots is a how-to article, not a recipe — the harness can't run it safely.

## 1. Name + target invariant

One sentence stating the post-migration invariant, phrased so a gate could check it
("no production render path threads parameter X", "presentation imports no transport
type", "every config entry resolves to a real class").

## 2. Site discovery

The exact mechanics that enumerate candidate sites (grep patterns, report diffs,
dependency parses), plus what a false positive looks like and how to confirm a real
site. State what the **inventory artifact** must contain for this recipe (minimum:
file/line/module; judgment recipes: the consumed-distinction inventory).

## 3. Classification table

The recipe's escalation sites, named concretely:

| Mostly | The escalation site(s) for this recipe |
|---|---|
| e.g. mechanical | e.g. a candidate that satisfies the lint but is a public entry point a human must confirm |

Everything not listed classifies mechanical by the discriminator ("can the completion
gate tell right from wrong?"), with default-escalate on uncertainty.

## 4. Transform

The transform shape and its judgment level (config edit / mechanical delete / uniform
re-plumb / re-authoring). For re-authoring recipes: what the foundation unit is
(lead-authored) vs what fans out, and the reference implementation to imitate if one
exists.

## 5. Per-site verify declarations

The typed entries a run registers with the ledger tool, with placeholders — the
consuming project's config/shim supplies concrete command templates:

```sh
node "$ML" add-verify --id compile --type command --template "<compile probe for {module}>" --granularity module
node "$ML" add-verify --id residue --type grep-absent --pattern "<retired symbol regex>" --paths <src-roots>
```

State which failures the per-site verify can catch and which only the completion gate
catches — that boundary is what the classification table is derived from.

## 6. Completion gate

The full-run gate the lead executes once after the fan-out (build, full test suite,
invariant lint, visual zero-drift, platform canaries), and any recipe-specific proof
obligation ("regenerate the report and show the cited entries flipped", "the
screenshot diff is zero pixels").

## 7. Known failure modes

Anything this recipe class has actually produced in real runs (reverts, false
positives, blast-radius surprises), so the next run designs against them instead of
rediscovering them. Cross-reference SPINE.md §F-a…F-h; add recipe-specific ones.
