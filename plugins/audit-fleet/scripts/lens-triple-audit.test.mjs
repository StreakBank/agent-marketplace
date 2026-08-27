// Unit tests for workflows/lens-triple-audit.js.
//
// The workflow is NOT a standalone module: it is evaluated by the Claude Code
// dynamic-workflow runtime inside a function wrapper, with `args`, `agent`,
// `pipeline`, `parallel` and `log` injected as globals and top-level `return`
// legal. `node --check` at module scope therefore (correctly) rejects it. These
// tests reproduce that wrapper — which is what makes them a real syntax gate as
// well as a behaviour gate — and drive the pipeline with stub agents, so the
// argument validation, the path guard and the blind-reader prompt ordering are
// all exercised without spending a single subagent token.
//
// Run: node --test plugins/audit-fleet/scripts/lens-triple-audit.test.mjs

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const SRC = readFileSync(new URL('../workflows/lens-triple-audit.js', import.meta.url), 'utf8')
  .replace(/^export const meta/m, 'const meta')

// The runtime's wrapper, reproduced. If the workflow has a syntax error, or uses
// a global the runtime does not inject, construction throws here.
// (`new Function` over interpolated source is a code-injection shape in general;
// here the only interpolated string is this plugin's own workflow file, read from
// disk beside this test — the same bytes the runtime itself evaluates. There is no
// external input. Reproducing the wrapper is the whole point: a plain import
// cannot load a file with top-level `return`.)
const build = () => new Function(
  `return async (args, agent, pipeline, parallel, log) => {\n${SRC}\n}`
)()

const stubs = () => {
  const prompts = []
  const agent = async (prompt, opts) => {
    prompts.push({ prompt, ...opts })
    return opts.phase === 'Refute'
      ? 'X1 | CONFIRMED | HIGH | reason\nMANIFEST: ok\nMONEY_LEGAL_REFUTES: none'
      : 'FILES_EXAMINED: 3'
  }
  const parallel = (fns) => Promise.all(fns.map((f) => f()))
  const pipeline = async (items, s1, s2, s3) => {
    const out = []
    for (const item of items) out.push(await s3(await s2(await s1(item), item), item))
    return out
  }
  return { prompts, agent, parallel, pipeline, log: () => {} }
}

const LANE = { id: 'F1', repo: 'r (branch b)', units: 'u', paths: 'p/', loc: 100, criteria: '1,2,7', baseSha: 'abc1234' }
const OK = {
  lanes: [LANE],
  briefsPath: '/abs/briefs.md',
  outDir: '/abs/out',
  contextNote: 'read-only; bar lives at ...',
}
const run = async (args) => {
  const s = stubs()
  const res = await build()(args, s.agent, s.pipeline, s.parallel, s.log)
  return { res, prompts: s.prompts }
}
const rejects = (args, needle) => assert.rejects(() => run(args), (e) => {
  assert.match(e.message, /^lens-triple-audit: /)
  assert.match(e.message, needle)
  return true
})

test('happy path: 1 finder + 2 skeptics per lane, no promoters when none flagged', async () => {
  const { res, prompts } = await run(OK)
  assert.equal(res.length, 1)
  assert.equal(prompts.filter((p) => p.phase === 'Find').length, 1)
  assert.equal(prompts.filter((p) => p.phase === 'Refute').length, 2)
  assert.equal(prompts.filter((p) => p.phase === 'Promote').length, 0)
  assert.ok(prompts.every((p) => p.model === 'opus'))
})

test('promoter fans out once per money/legal refute, union across skeptics', async () => {
  const s = stubs()
  let n = 0
  const agent = async (prompt, opts) => {
    s.prompts.push({ prompt, ...opts })
    if (opts.phase !== 'Refute') return 'ok'
    n += 1
    return `MANIFEST: ok\nMONEY_LEGAL_REFUTES: ${n === 1 ? 'A1, A2' : 'A2'}`
  }
  await build()(OK, agent, s.pipeline, s.parallel, s.log)
  const promos = s.prompts.filter((p) => p.phase === 'Promote')
  assert.equal(promos.length, 2)
  assert.deepEqual(promos.map((p) => p.label).sort(), ['promote:F1:A1', 'promote:F1:A2'])
})

test('args must be a parsed object, and the error says so', async () => {
  await rejects(JSON.stringify(OK), /RAW JSON STRING/)
  await rejects(undefined, /args must be an object/)
})

test('every missing or mistyped top-level field names itself', async () => {
  await rejects({ ...OK, lanes: [] }, /args\.lanes must be a non-empty array/)
  await rejects({ ...OK, lanes: 'F1' }, /args\.lanes must be a non-empty array/)
  await rejects({ ...OK, briefsPath: undefined }, /args\.briefsPath is required/)
  await rejects({ ...OK, outDir: '' }, /args\.outDir is required/)
  await rejects({ ...OK, contextNote: '   ' }, /args\.contextNote is required/)
})

test('every missing or mistyped lane field names itself, with its index', async () => {
  const lane = (patch) => ({ ...OK, lanes: [LANE, { ...LANE, id: 'F2', ...patch }] })
  await rejects(lane({ repo: undefined }), /args\.lanes\[1\]\.repo is required/)
  await rejects(lane({ units: 42 }), /args\.lanes\[1\]\.units is required/)
  await rejects(lane({ paths: '' }), /args\.lanes\[1\]\.paths is required/)
  await rejects(lane({ criteria: null }), /args\.lanes\[1\]\.criteria is required/)
  await rejects(lane({ baseSha: undefined }), /args\.lanes\[1\]\.baseSha is required/)
  await rejects(lane({ loc: '7592' }), /args\.lanes\[1\]\.loc must be a positive finite number/)
  await rejects(lane({ loc: 0 }), /args\.lanes\[1\]\.loc must be a positive finite number/)
  await rejects(lane({ note: 12 }), /args\.lanes\[1\]\.note is optional but must be a string/)
  await rejects({ ...OK, lanes: [LANE, LANE] }, /must be unique/)
  await rejects({ ...OK, lanes: [null] }, /args\.lanes\[0\] must be an object/)
})

test('lane id is filename-safe — it is interpolated into every output path', async () => {
  for (const id of ['../escape', 'a/b', '..', 'x'.repeat(65), '-lead']) {
    await rejects({ ...OK, lanes: [{ ...LANE, id }] }, /must be filename-safe/)
  }
  const { prompts } = await run({ ...OK, lanes: [{ ...LANE, id: 'F1a.2-x' }] })
  assert.match(prompts[0].prompt, /\/abs\/out\/30-finder-F1a\.2-x\.md/)
})

test('path guard: absolute, no ".." segment, no control chars, no shell metacharacters', async () => {
  await rejects({ ...OK, outDir: 'out' }, /args\.outDir must be an ABSOLUTE path/)
  await rejects({ ...OK, briefsPath: 'rel/briefs.md' }, /args\.briefsPath must be an ABSOLUTE path/)
  await rejects({ ...OK, outDir: '/abs/../../etc' }, /args\.outDir must not contain a '\.\.' path segment/)
  await rejects({ ...OK, briefsPath: '/abs/../b.md' }, /args\.briefsPath must not contain a '\.\.' path segment/)
  await rejects({ ...OK, outDir: '/abs/out\nrm -rf /' }, /must not contain control characters/)
  await rejects({ ...OK, outDir: '/abs/$(whoami)' }, /must not contain shell metacharacters/)
  await rejects({ ...OK, briefsPath: '/abs/b.md; rm -rf /' }, /must not contain shell metacharacters/)
  // A legal absolute path survives all of it — POSIX, and a Windows drive path whose
  // separators are backslashes (a metacharacter everywhere else).
  await run({ ...OK, outDir: '/abs/deep_dir-1/out', briefsPath: '/abs/deep_dir-1/briefs.md' })
  await run({ ...OK, outDir: 'C:\\abs\\out', briefsPath: 'C:\\abs\\briefs.md' })
  await rejects({ ...OK, outDir: 'C:\\abs\\..\\out' }, /must not contain a '\.\.' path segment/)
  await rejects({ ...OK, outDir: 'C:\\abs\\$(id)' }, /must not contain shell metacharacters/)
})

test('allowedRoot confines both paths and is itself guarded', async () => {
  const root = '/abs/records'
  await run({ ...OK, allowedRoot: root, outDir: '/abs/records/run-1', briefsPath: '/abs/records/briefs.md' })
  await run({ ...OK, allowedRoot: root, outDir: root, briefsPath: '/abs/records/b.md' })
  await rejects({ ...OK, allowedRoot: root, briefsPath: '/abs/records/b.md', outDir: '/abs/records-evil' }, /args\.outDir must resolve inside args\.allowedRoot/)
  await rejects({ ...OK, allowedRoot: root, briefsPath: '/elsewhere/b.md' }, /args\.briefsPath must resolve inside args\.allowedRoot/)
  await rejects({ ...OK, allowedRoot: 'records' }, /args\.allowedRoot must be an ABSOLUTE path/)
})

// --- blind multi-reader protocol -------------------------------------------

const BLIND = { criteria: '7', dimensions: 'naming / abstraction / clarity', poolPredicate: 'bodies of >= 3 statements' }

test('no blindProtocol → prompts carry no blind stage at all (0.1.0 behaviour)', async () => {
  const { prompts } = await run(OK)
  for (const p of prompts) {
    assert.doesNotMatch(p.prompt, /BLIND|STEP 0|-draw\.md|-scores\.md/)
  }
  const skeptic = prompts.find((p) => p.phase === 'Refute').prompt
  assert.match(skeptic, /Default posture REFUTE: re-read every cited line yourself\. Read the finder's report at/)
})

test('blindProtocol gates on lane.criteria, deterministically — not by agent judgement', async () => {
  const off = await run({ ...OK, lanes: [{ ...LANE, criteria: '1,2,3' }], blindProtocol: BLIND })
  assert.doesNotMatch(off.prompts[0].prompt, /BLIND-READER PROTOCOL/)
  const on = await run({ ...OK, blindProtocol: BLIND })
  assert.match(on.prompts[0].prompt, /BLIND-READER PROTOCOL/)
  // '7' must not match lane criteria '17,27'
  const near = await run({ ...OK, lanes: [{ ...LANE, criteria: '17,27' }], blindProtocol: BLIND })
  assert.doesNotMatch(near.prompts[0].prompt, /BLIND-READER PROTOCOL/)
})

test('finder writes draw + scores separately and is banned from scoring in its report', async () => {
  const { prompts } = await run({ ...OK, blindProtocol: BLIND })
  const f = prompts[0].prompt
  assert.match(f, /\/abs\/out\/30-finder-F1-draw\.md/)
  assert.match(f, /\/abs\/out\/30-finder-F1-scores\.md/)
  assert.match(f, /2654435761 mod M not in \{0,1\}/)
  assert.match(f, /2654435789/)
  assert.match(f, /printf, NEVER echo/)
  assert.match(f, /bodies of >= 3 statements/)
  assert.match(f, /no score, sub-score, aggregate or median/)
})

test('skeptic scores BEFORE opening any report — ordering is in the prompt, and reader index is right', async () => {
  const { prompts } = await run({ ...OK, blindProtocol: BLIND })
  const [s1, s2] = prompts.filter((p) => p.phase === 'Refute').map((p) => p.prompt)
  for (const s of [s1, s2]) {
    assert.ok(s.indexOf('STEP 0 FIRST') < s.indexOf("read the finder's report"), 'STEP 0 must precede the report read')
    assert.match(s, /open ONLY \/abs\/out\/30-finder-F1-draw\.md/)
    assert.match(s, /do NOT open \/abs\/out\/30-finder-F1\.md/)
    assert.match(s, /'scored before reading any report'/)
    assert.match(s, /VOID/)
  }
  assert.match(s1, /BLIND READER 2 OF THREE/)
  assert.match(s2, /BLIND READER 3 OF THREE/)
  assert.match(s2, /reproduce the draw byte-for-byte/)
  assert.doesNotMatch(s1, /reproduce the draw byte-for-byte/)
  assert.match(s1, /\/abs\/out\/31-skeptic-F1-1-scores\.md/)
  assert.match(s2, /\/abs\/out\/31-skeptic-F1-2-scores\.md/)
  // The MONEY_LEGAL_REFUTES contract line still exists; the blind delivery line
  // is additive and must not displace it.
  assert.match(s2, /MONEY_LEGAL_REFUTES/)
  assert.match(s2, /DRAW: reproduces/)
  assert.match(s1, /BLIND: clean/)
})

test('blindProtocol shape is validated field by field', async () => {
  const bad = (patch, needle) => rejects({ ...OK, blindProtocol: { ...BLIND, ...patch } }, needle)
  await rejects({ ...OK, blindProtocol: 'crit7' }, /args\.blindProtocol must be an object/)
  await bad({ criteria: '' }, /args\.blindProtocol\.criteria is required/)
  await bad({ criteria: [] }, /args\.blindProtocol\.criteria is required/)
  await bad({ dimensions: undefined }, /args\.blindProtocol\.dimensions is required/)
  await bad({ poolPredicate: '' }, /args\.blindProtocol\.poolPredicate is required/)
  await bad({ scale: 4 }, /args\.blindProtocol\.scale is optional/)
  await bad({ drawCount: 8.5 }, /args\.blindProtocol\.drawCount is optional/)
  await bad({ multiplier: -1 }, /args\.blindProtocol\.multiplier is optional/)
  await bad({ fallbackMultiplier: 'x' }, /args\.blindProtocol\.fallbackMultiplier is optional/)
})

test('blindProtocol accepts arrays and overrides, and pluralises', async () => {
  const { prompts } = await run({
    ...OK,
    lanes: [{ ...LANE, criteria: '7,12' }],
    blindProtocol: { criteria: [7, 12], dimensions: ['a', 'b'], scale: '0-3', drawCount: 12, poolPredicate: 'P', poolScope: 'this half only', multiplier: 11, fallbackMultiplier: 13 },
  })
  const f = prompts[0].prompt
  assert.match(f, /criteria 7, 12 of your bar are multi-reader/)
  assert.match(f, /a \/ b; each 0-3/)
  assert.match(f, /all 12 indices/)
  assert.match(f, /11 mod M not in \{0,1\}/)
  assert.match(f, /fallback multiplier 13/)
  assert.match(f, /Pool scope: this half only/)
})
