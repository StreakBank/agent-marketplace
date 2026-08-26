export const meta = {
  name: 'lens-triple-audit',
  description: 'Per-lane finder → two default-REFUTE skeptics → promoter on money/legal refutes',
  whenToUse: 'Any audit/measurement fan-out where each finding must survive adversarial verification before it is recorded. Pass lanes + brief path + output dir via args.',
  phases: [
    { title: 'Find', detail: 'one finder per lane, coverage manifest mandatory', model: 'opus' },
    { title: 'Refute', detail: 'two independent default-REFUTE skeptics per finder', model: 'opus' },
    { title: 'Promote', detail: 'one promoter per money/legal REFUTE, trying to overturn it', model: 'opus' },
  ],
}
// args = { lanes: [{id, repo, units, paths, loc, criteria, baseSha, note}], briefsPath, outDir, contextNote }
const { lanes, briefsPath, outDir, contextNote } = args
const sub = (lane) => `lane=${lane.id} · repo=${lane.repo} · units=${lane.units} · paths=${lane.paths} · loc≈${lane.loc} · criteria=${lane.criteria} · base_sha=${lane.baseSha}${lane.note ? ' · lane note: ' + lane.note : ''}`

const results = await pipeline(
  lanes,
  // Stage 1 — finder
  (lane) => agent(
    `Follow the FINDER brief skeleton in ${briefsPath} §A EXACTLY, with these substitutions: ${sub(lane)}. ${contextNote}
Write your full report (findings in REGISTER-SCHEMA shape, the coverage manifest, BAR-OBJECTION section if any) to ${outDir}/30-finder-${lane.id}.md. Your FINAL TEXT is data, not a message: return ONLY (1) 'FILES_EXAMINED: <n>', (2) the finding count by severity, (3) one line per finding: '<finding-id> | <severity> | <criterion#> | <money-path? yes/no> | <one-line title>' (≤40 lines total).`,
    { label: `find:${lane.id}`, phase: 'Find', model: 'opus' }
  ),
  // Stage 2 — two independent REFUTE skeptics (barrier within the lane only)
  (finderSummary, lane) => parallel([1, 2].map((k) => () => agent(
    `Follow the SKEPTIC brief skeleton in ${briefsPath} §B EXACTLY. You are skeptic #${k} of 2 for ${sub(lane)}. Default posture REFUTE: re-read every cited line yourself. Read the finder's report at ${outDir}/30-finder-${lane.id}.md (do not trust its summary). ${contextNote}
Write your full verdict list to ${outDir}/31-skeptic-${lane.id}-${k}.md. Your FINAL TEXT is data: one line per finding '<finding-id> | CONFIRMED|ADJUSTED|REFUTED | <your corrected severity> | <one-line reason>', then the finder's coverage-manifest verdict ('MANIFEST: ok' or 'MANIFEST: deficient — <why>'), then EXACTLY one last line 'MONEY_LEGAL_REFUTES: <comma-separated finding ids that are money-path or legal-obligation AND you REFUTED>' or 'MONEY_LEGAL_REFUTES: none'.`,
    { label: `refute:${lane.id}#${k}`, phase: 'Refute', model: 'opus' }
  ))).then((skeptics) => ({ finderSummary, skeptics: skeptics.filter(Boolean) })),
  // Stage 3 — promoters for money/legal refutes (union across both skeptics)
  async (r, lane) => {
    const ids = new Set()
    for (const s of r.skeptics) {
      const m = /MONEY_LEGAL_REFUTES:\s*(.+)$/m.exec(String(s))
      if (m && !/^\s*none\s*$/i.test(m[1])) m[1].split(',').map((x) => x.trim()).filter(Boolean).forEach((x) => ids.add(x))
    }
    log(`${lane.id}: ${ids.size} money/legal refute(s) → promoters`)
    const promoters = await parallel([...ids].map((fid) => () => agent(
      `You are the PROMOTER for finding ${fid} of ${sub(lane)}. The promoter rule: a money-path or legal-obligation finding that a skeptic REFUTED gets one agent whose brief is to OVERTURN the refutation — a refutation on those surfaces is itself a decision, so it gets an adversary too. Read the finder report ${outDir}/30-finder-${lane.id}.md and both skeptic files ${outDir}/31-skeptic-${lane.id}-1.md and ${outDir}/31-skeptic-${lane.id}-2.md; re-read every cited line; build the strongest evidence-based case that the finding is REAL. ${contextNote}
Write ${outDir}/32-promoter-${lane.id}-${fid}.md. FINAL TEXT (data): '${fid} | OVERTURNED|UPHELD-REFUTATION | <severity> | <one-line reason>'.`,
      { label: `promote:${lane.id}:${fid}`, phase: 'Promote', model: 'opus' }
    )))
    return { lane: lane.id, finderSummary: r.finderSummary, skeptics: r.skeptics, promoters: promoters.filter(Boolean) }
  }
)
return results.filter(Boolean)
