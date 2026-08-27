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
// args = {
//   lanes: [{ id, repo, units, paths, loc, criteria, baseSha, note? }],
//   briefsPath, outDir, contextNote,
//   allowedRoot?,    // optional: briefsPath + outDir must live inside this absolute root
//   blindProtocol?,  // optional: { criteria, dimensions, scale?, drawCount?, poolPredicate,
//                    //             poolScope?, multiplier?, fallbackMultiplier? }
// }                  // blindProtocol absent → no blind stage; behaviour is 0.1.0's exactly.

// ---------------------------------------------------------------------------
// Argument validation. Every failure throws NAMING THE FIELD: a fan-out this
// expensive must not die on a raw TypeError three agents deep, and a bad path
// must not reach a prompt at all.
// ---------------------------------------------------------------------------
const BAD = (m) => { throw new Error(`lens-triple-audit: ${m}`) }
const show = (v) => v === undefined ? 'undefined'
  : v === null ? 'null'
  : Array.isArray(v) ? `an array (length ${v.length})`
  : typeof v === 'object' ? 'an object'
  : JSON.stringify(String(v)).slice(0, 60)
const filled = (v) => typeof v === 'string' && v.trim() !== ''

if (typeof args !== 'object' || args === null || Array.isArray(args)) {
  BAD(`args must be an object { lanes, briefsPath, outDir, contextNote } — got ${show(args)}. If your runtime hands workflows the caller's RAW JSON STRING rather than a parsed object, parse it before invoking.`)
}

// Path guard. briefsPath / outDir are interpolated into every stage prompt and the
// agents turn them into shell commands and file writes, so: absolute, no '..'
// segment, no control characters, no shell metacharacters, and — when allowedRoot
// is supplied — inside that root.
const norm = (p) => p.replace(/\\/g, '/').replace(/\/+$/, '')
const checkPath = (field, v, root) => {
  if (!filled(v)) BAD(`args.${field} is required and must be a non-empty string — got ${show(v)}`)
  const winAbs = /^[A-Za-z]:[\\/]/.test(v)
  if (!/^\//.test(v) && !winAbs) BAD(`args.${field} must be an ABSOLUTE path (subagent working directories reset between calls) — got ${show(v)}`)
  if (v.split(/[\\/]+/).some((seg) => seg === '..')) BAD(`args.${field} must not contain a '..' path segment — it is interpolated verbatim into agent prompts and file writes — got ${show(v)}`)
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(v)) BAD(`args.${field} must not contain control characters or newlines — a newline breaks the prompt line it is interpolated into — got ${show(v)}`)
  // Backslash is a separator on a Windows drive path and a metacharacter everywhere else.
  if ((winAbs ? /[`$;&|<>()*?"']/ : /[`$;&|<>()*?"'\\]/).test(v)) BAD(`args.${field} must not contain shell metacharacters (\` $ ; & | < > ( ) * ? " '${winAbs ? '' : ' \\'}) — agents paste it into shell commands — got ${show(v)}`)
  if (root && !(norm(v) === norm(root) || norm(v).startsWith(norm(root) + '/'))) {
    BAD(`args.${field} must resolve inside args.allowedRoot (${root}) — got ${show(v)}`)
  }
}
if (args.allowedRoot !== undefined) checkPath('allowedRoot', args.allowedRoot, null)
const { lanes, briefsPath, outDir, contextNote, allowedRoot, blindProtocol } = args
checkPath('briefsPath', briefsPath, allowedRoot)
checkPath('outDir', outDir, allowedRoot)
if (!filled(contextNote)) BAD(`args.contextNote is required and must be a non-empty string — one paragraph naming the working dir, the read-only posture, where the bar / schema / severity / dedup list live, and the never-do rules — got ${show(contextNote)}`)

if (!Array.isArray(lanes) || lanes.length === 0) BAD(`args.lanes must be a non-empty array of lane objects { id, repo, units, paths, loc, criteria, baseSha, note? } — got ${show(lanes)}`)
lanes.forEach((lane, i) => {
  const at = `args.lanes[${i}]`
  if (typeof lane !== 'object' || lane === null || Array.isArray(lane)) BAD(`${at} must be an object { id, repo, units, paths, loc, criteria, baseSha, note? } — got ${show(lane)}`)
  if (!filled(lane.id)) BAD(`${at}.id is required and must be a non-empty string — got ${show(lane.id)}`)
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(lane.id)) BAD(`${at}.id must be filename-safe — [A-Za-z0-9._-], ≤64 chars, no path separators and no '..' — because it is interpolated into every output path — got ${show(lane.id)}`)
  for (const k of ['repo', 'units', 'paths', 'criteria', 'baseSha']) {
    if (!filled(lane[k])) BAD(`${at}.${k} is required and must be a non-empty string — got ${show(lane[k])}`)
  }
  if (typeof lane.loc !== 'number' || !Number.isFinite(lane.loc) || lane.loc <= 0) BAD(`${at}.loc must be a positive finite number (tracked source LOC — it sizes the coverage-manifest expectation) — got ${show(lane.loc)}`)
  if (lane.note !== undefined && typeof lane.note !== 'string') BAD(`${at}.note is optional but must be a string when present — got ${show(lane.note)}`)
})
const seen = new Set()
for (const lane of lanes) {
  if (seen.has(lane.id)) BAD(`args.lanes[].id must be unique — duplicate '${lane.id}' would make two lanes overwrite each other's reports`)
  seen.add(lane.id)
}

// Blind multi-reader protocol (optional). If any criterion in your bar is scored by
// more than one reader, the finder is reader 1 and the two skeptics are readers 2
// and 3 — and a reader who has seen a prior score is not independent, so its column
// is voided rather than averaged in. Declaring it here makes the workflow enforce
// the ordering; omitting it leaves the pipeline exactly as it was without it.
const bp = (() => {
  if (blindProtocol === undefined) return null
  if (typeof blindProtocol !== 'object' || blindProtocol === null || Array.isArray(blindProtocol)) BAD(`args.blindProtocol must be an object { criteria, dimensions, scale?, drawCount?, poolPredicate, poolScope?, multiplier?, fallbackMultiplier? } — got ${show(blindProtocol)}`)
  const rawCriteria = Array.isArray(blindProtocol.criteria) ? blindProtocol.criteria : String(blindProtocol.criteria ?? '').split(',')
  const criteria = rawCriteria.map((c) => String(c).trim()).filter(Boolean)
  if (!criteria.length) BAD(`args.blindProtocol.criteria is required — the criterion id(s) in YOUR bar that are multi-reader, as a string ("7" or "7,12") or an array — got ${show(blindProtocol.criteria)}`)
  const dimensions = Array.isArray(blindProtocol.dimensions) ? blindProtocol.dimensions.map(String).join(' / ') : blindProtocol.dimensions
  if (!filled(dimensions)) BAD(`args.blindProtocol.dimensions is required and must be a non-empty string or array — the sub-scores each reader records, e.g. "naming / abstraction-level / comprehensibility" — got ${show(blindProtocol.dimensions)}`)
  if (!filled(blindProtocol.poolPredicate)) BAD(`args.blindProtocol.poolPredicate is required and must be a non-empty string — the MECHANICAL statement of what enters the sampling pool, quotable verbatim by a skeptic re-deriving M — got ${show(blindProtocol.poolPredicate)}`)
  for (const k of ['scale', 'poolScope']) {
    if (blindProtocol[k] !== undefined && !filled(blindProtocol[k])) BAD(`args.blindProtocol.${k} is optional but must be a non-empty string when present — got ${show(blindProtocol[k])}`)
  }
  for (const k of ['drawCount', 'multiplier', 'fallbackMultiplier']) {
    if (blindProtocol[k] !== undefined && (typeof blindProtocol[k] !== 'number' || !Number.isInteger(blindProtocol[k]) || blindProtocol[k] <= 0)) BAD(`args.blindProtocol.${k} is optional but must be a positive integer when present — got ${show(blindProtocol[k])}`)
  }
  return {
    criteria,
    label: `criteri${criteria.length > 1 ? 'a' : 'on'} ${criteria.join(', ')}`,
    dimensions,
    scale: blindProtocol.scale || '0-2',
    drawCount: blindProtocol.drawCount || 8,
    poolPredicate: blindProtocol.poolPredicate,
    poolScope: blindProtocol.poolScope || "THIS lane's own unit only — no union across a split unit; a unit split across lanes draws per half and is graded per half, because neither half's skeptics can enumerate the other half's files to reproduce a union draw blind",
    multiplier: blindProtocol.multiplier || 2654435761,
    fallbackMultiplier: blindProtocol.fallbackMultiplier || 2654435789,
  }
})()

const sub = (lane) => `lane=${lane.id} · repo=${lane.repo} · units=${lane.units} · paths=${lane.paths} · loc≈${lane.loc} · criteria=${lane.criteria} · base_sha=${lane.baseSha}${lane.note ? ' · lane note: ' + lane.note : ''}`
// Deterministic gate: does THIS lane grade a blind criterion? Decided here from
// lane.criteria, never left to the agent to judge from its own brief.
const blindFor = (lane) => {
  if (!bp) return []
  const graded = String(lane.criteria).split(/[^A-Za-z0-9]+/).filter(Boolean)
  return bp.criteria.filter((c) => graded.includes(c))
}
const drawFile = (lane) => `${outDir}/30-finder-${lane.id}-draw.md`
const finderScores = (lane) => `${outDir}/30-finder-${lane.id}-scores.md`
const skepticScores = (lane, k) => `${outDir}/31-skeptic-${lane.id}-${k}-scores.md`

const finderBlind = (lane) => {
  const c = blindFor(lane)
  if (!c.length) return ''
  return ` BLIND-READER PROTOCOL — ${bp.label} of your bar ${c.length > 1 ? 'are' : 'is'} multi-reader and you are READER 1 OF THREE, so no score of yours may be visible to readers 2 and 3 before they score. Per brief §A, ALSO write TWO separate files. (1) ${drawFile(lane)} — the seeded draw ONLY: the seed string, H as hex AND decimal (hex-only does not reproduce), M, the pool predicate stated mechanically (${bp.poolPredicate}), the pool-enumeration command you actually ran, copy-pasteable, so a skeptic can re-derive M without asking you, the degeneracy check '${bp.multiplier} mod M not in {0,1}' with the fallback multiplier ${bp.fallbackMultiplier} declared explicitly if used and re-checked the same way, all ${bp.drawCount} indices, and the ${bp.drawCount} drawn 'file:line item' entries in draw order. Seed with printf, NEVER echo — a trailing newline changes the digest and the draw will not reproduce. That file carries NOTHING about scores, findings, verdicts or your coverage manifest: both skeptics open it first and anything else in it destroys their blindness. Pool scope: ${bp.poolScope}. (2) ${finderScores(lane)} — your reader-1 sub-scores ALONE (${bp.dimensions}; each ${bp.scale}; one justification line each), with no aggregate, mean, median or pass/fail. Your main report AND your final text must contain no score, sub-score, aggregate or median for ${bp.label} — a FINDING is not a score, so ${bp.label} findings still belong in the findings section as normal.`
}

const skepticHead = (lane, k) => {
  const c = blindFor(lane)
  if (!c.length) {
    return `Follow the SKEPTIC brief skeleton in ${briefsPath} §B EXACTLY. You are skeptic #${k} of 2 for ${sub(lane)}. Default posture REFUTE: re-read every cited line yourself. Read the finder's report at ${outDir}/30-finder-${lane.id}.md (do not trust its summary).`
  }
  return `Follow the SKEPTIC brief skeleton in ${briefsPath} §B EXACTLY, IN ITS ORDER. You are skeptic #${k} of 2 for ${sub(lane)}. STEP 0 FIRST — ${bp.label} of this lane's bar ${c.length > 1 ? 'are' : 'is'} multi-reader and you are BLIND READER ${k + 1} OF THREE. BEFORE you open the finder's report: open ONLY ${drawFile(lane)} (do NOT open ${outDir}/30-finder-${lane.id}.md and do NOT open ${finderScores(lane)}, not even to skim the coverage manifest); reproduce H as hex and decimal, M and the ${bp.drawCount} indices from it, re-running the published pool-enumeration command yourself; score the ${bp.drawCount} drawn items BLIND from the source (${bp.dimensions}, each ${bp.scale}, one justification line each); write them to ${skepticScores(lane, k)} including the literal line 'scored before reading any report'.${k === 2 ? ` As skeptic 2 you also reproduce the draw byte-for-byte and report H (hex + decimal), M, the multiplier, the degeneracy value ${bp.multiplier} mod M, and whether the ${bp.drawCount} indices match the finder's list exactly; a draw that does not reproduce INVALIDATES THE SAMPLE — it does not license a substitute draw.` : ''} If you opened any report or scores file before writing that file, declare your ${bp.label} column VOID in one line at the top of your report: a void column is recoverable, a contaminated column silently corrupts the median. Never score from memory of a number you have already seen, and never revise your step-0 numbers afterwards — disagreement between readers is what the median is for. THEN, and only then, read the finder's report at ${outDir}/30-finder-${lane.id}.md (do not trust its summary) and do the refutation work; if its main report contains a score for ${bp.label}, report that as a protocol breach and state whether you saw it before scoring. Default posture REFUTE: re-read every cited line yourself.`
}

const skepticDelivery = (lane, k) => {
  const c = blindFor(lane)
  if (!c.length) return ''
  return ` Before that last line, add 'BLIND: clean' or 'BLIND: VOID — <why>'${k === 2 ? ` and 'DRAW: reproduces (H=<hex>/<dec>, M=<m>, ${bp.multiplier} mod M=<r>, indices match)' or 'DRAW: does NOT reproduce — <what differs>'` : ''}; no sub-scores in your final text.`
}

const results = await pipeline(
  lanes,
  // Stage 1 — finder
  (lane) => agent(
    `Follow the FINDER brief skeleton in ${briefsPath} §A EXACTLY, with these substitutions: ${sub(lane)}. ${contextNote}
Write your full report (findings in your register's row schema, the coverage manifest, BAR-OBJECTION section if any) to ${outDir}/30-finder-${lane.id}.md.${finderBlind(lane)} Your FINAL TEXT is data, not a message: return ONLY (1) 'FILES_EXAMINED: <n>', (2) the finding count by severity, (3) one line per finding: '<finding-id> | <severity> | <criterion#> | <money-path? yes/no> | <one-line title>' (≤40 lines total).`,
    { label: `find:${lane.id}`, phase: 'Find', model: 'opus' }
  ),
  // Stage 2 — two independent REFUTE skeptics (barrier within the lane only)
  (finderSummary, lane) => parallel([1, 2].map((k) => () => agent(
    `${skepticHead(lane, k)} ${contextNote}
Write your full verdict list to ${outDir}/31-skeptic-${lane.id}-${k}.md. Your FINAL TEXT is data: one line per finding '<finding-id> | CONFIRMED|ADJUSTED|REFUTED | <your corrected severity> | <one-line reason>', then the finder's coverage-manifest verdict ('MANIFEST: ok' or 'MANIFEST: deficient — <why>'), then EXACTLY one last line 'MONEY_LEGAL_REFUTES: <comma-separated finding ids that are money-path or legal-obligation AND you REFUTED>' or 'MONEY_LEGAL_REFUTES: none'.${skepticDelivery(lane, k)}`,
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
