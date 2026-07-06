#!/usr/bin/env node
// migrate-ledger — the deterministic ledger + gate machinery for staged codebase
// migrations (discover → classify → batch-escalate → transform → verify-each → gate).
//
// Zero runtime dependencies; Node >= 18.3 (util.parseArgs). The ledger is a JSON file
// living in the consuming repo on the migration branch. The model/lead does the
// judgment work (discovery reading, transforms, escalation packets); this tool owns
// everything deterministic: site accounting, classification/decision records, agent
// file-set partitioning, mechanical per-site verification, and the block gate that
// refuses to pass while a semantic-escalate site lacks a recorded human decision.
//
// Verify evidence is freshness-checked: each verify run records a working-tree
// fingerprint (HEAD + porcelain status, ledger writes excluded); the gate fails a
// done site whose evidence predates later tree changes (re-run `verify`). Outside a
// git repo the fingerprint is unavailable and the staleness check is skipped.
//
// Exit codes: 0 = ok/pass · 1 = gate/check failed · 2 = usage or ledger error.

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { resolve, dirname, join } from 'node:path';
import { parseArgs } from 'node:util';
import { createHash } from 'node:crypto';
import process from 'node:process';

const SCHEMA_VERSION = 1;
const OUTCOMES = ['pending', 'done', 'skipped', 'deferred', 'reverted', 'wontfix'];
const REASON_REQUIRED = ['skipped', 'deferred', 'reverted', 'wontfix'];
const ISOLATIONS = ['partition', 'worktree', 'lead-serial'];
const VERIFY_TYPES = ['command', 'grep-absent'];
const DEFAULT_LEDGER = 'migration-ledger.json';
const LEDGER_BASENAMES = [DEFAULT_LEDGER, 'ledger.json'];

function fail(msg, code = 2) {
  process.stderr.write(`migrate-ledger: ${msg}\n`);
  process.exit(code);
}

function nowIso() {
  return new Date().toISOString();
}

function loadLedger(path) {
  if (!existsSync(path)) fail(`no ledger at ${path} (run 'init' first, or pass --ledger)`);
  let raw;
  try {
    raw = JSON.parse(readFileSync(path, 'utf8'));
  } catch (e) {
    fail(`ledger at ${path} is not valid JSON: ${e.message}`);
  }
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    fail(`ledger at ${path} is not a JSON object`);
  }
  if (raw.schemaVersion !== SCHEMA_VERSION) {
    fail(`ledger schemaVersion ${raw.schemaVersion} unsupported (expected ${SCHEMA_VERSION})`);
  }
  if (!Array.isArray(raw.sites)) fail('ledger has no sites[] array');
  if (!Array.isArray(raw.verify)) fail('ledger has no verify[] array');
  if (!Array.isArray(raw.agents)) fail('ledger has no agents[] array');
  if (!ISOLATIONS.includes(raw.isolation)) {
    fail(`ledger isolation '${raw.isolation}' invalid (expected ${ISOLATIONS.join('|')})`);
  }
  for (const [field, arr] of [['sites', raw.sites], ['verify', raw.verify], ['agents', raw.agents]]) {
    const ids = new Set();
    for (const item of arr) {
      if (!item || typeof item.id !== 'string') fail(`ledger ${field}[] entry without a string id`);
      if (ids.has(item.id)) fail(`ledger ${field}[] has duplicate id '${item.id}' — refusing to run against an inconsistent ledger`);
      ids.add(item.id);
    }
  }
  return raw;
}

function saveLedger(path, ledger) {
  writeFileSync(path, JSON.stringify(ledger, null, 2) + '\n');
}

function siteById(ledger, id) {
  const site = ledger.sites.find((s) => s.id === id);
  if (!site) fail(`no site '${id}' in ledger`);
  return site;
}

// Working-tree fingerprint: HEAD sha + porcelain status (ledger files excluded, so
// ledger bookkeeping between verify and gate doesn't read as a source change).
// Returns null outside a git work tree — staleness checking is then unavailable.
function treeFingerprint(cwd) {
  const head = spawnSync('git', ['rev-parse', 'HEAD'], { cwd, encoding: 'utf8' });
  if (head.error || (head.status !== 0 && !/unknown revision|ambiguous argument/.test(head.stderr ?? ''))) {
    // Not a git repo (or git missing) — includes rev-parse failing outside a work tree.
    if (head.error || /not a git repository/i.test(head.stderr ?? '')) return null;
  }
  const status = spawnSync('git', ['status', '--porcelain'], { cwd, encoding: 'utf8' });
  if (status.error || status.status !== 0) return null;
  const filtered = (status.stdout ?? '')
    .split('\n')
    .filter((line) => line && !LEDGER_BASENAMES.some((b) => line.includes(b)))
    .join('\n');
  const headSha = head.status === 0 ? head.stdout.trim() : 'no-head';
  return createHash('sha256').update(`${headSha}\n${filtered}`).digest('hex');
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

function cmdInit(args) {
  const { values } = parseArgs({
    args,
    options: {
      ledger: { type: 'string' },
      id: { type: 'string' },
      invariant: { type: 'string' },
      isolation: { type: 'string' },
      force: { type: 'boolean' },
    },
  });
  if (!values.id) fail("init requires --id <migration-id>");
  const isolation = values.isolation ?? 'partition';
  if (!ISOLATIONS.includes(isolation)) fail(`--isolation must be one of ${ISOLATIONS.join('|')}`);
  const path = resolve(values.ledger ?? DEFAULT_LEDGER);
  if (existsSync(path) && !values.force) fail(`${path} exists (use --force to overwrite)`);
  mkdirSync(dirname(path), { recursive: true });
  saveLedger(path, {
    schemaVersion: SCHEMA_VERSION,
    migration: {
      id: values.id,
      invariant: values.invariant ?? null,
      createdAt: nowIso(),
    },
    isolation,
    verify: [],
    agents: [],
    sites: [],
  });
  process.stdout.write(`ok: ledger initialized at ${path} (isolation=${isolation})\n`);
}

function parseLine(value) {
  if (value === undefined || value === null) return null;
  const n = Number(value);
  if (Number.isNaN(n) || !Number.isInteger(n)) fail(`--line must be an integer (got '${value}')`);
  return n;
}

function normalizeSite(raw, index, existingIds) {
  if (!raw || typeof raw !== 'object') fail(`site #${index} is not an object`);
  if (!raw.file || typeof raw.file !== 'string') fail(`site #${index} needs a string 'file'`);
  const id = raw.id ?? `s${index + 1}`;
  if (existingIds.has(id)) fail(`duplicate site id '${id}'`);
  existingIds.add(id);
  return {
    id,
    title: raw.title ?? raw.file,
    file: raw.file,
    line: raw.line ?? null,
    module: raw.module ?? null,
    classification: 'unclassified',
    classificationReason: null,
    decision: null,
    outcome: 'pending',
    outcomeReason: null,
    verifyRuns: {},
  };
}

function cmdImport(args) {
  const { values } = parseArgs({
    args,
    options: { ledger: { type: 'string' }, sites: { type: 'string' } },
  });
  if (!values.sites) fail('import requires --sites <json-file> (array of {id?,title?,file,line?,module?})');
  const path = resolve(values.ledger ?? DEFAULT_LEDGER);
  const ledger = loadLedger(path);
  let incoming;
  try {
    incoming = JSON.parse(readFileSync(resolve(values.sites), 'utf8'));
  } catch (e) {
    fail(`could not read sites file: ${e.message}`);
  }
  if (!Array.isArray(incoming)) fail('sites file must be a JSON array');
  const ids = new Set(ledger.sites.map((s) => s.id));
  for (const raw of incoming) {
    ledger.sites.push(normalizeSite(raw, ledger.sites.length, ids));
  }
  saveLedger(path, ledger);
  process.stdout.write(`ok: imported ${incoming.length} site(s); ledger now has ${ledger.sites.length}\n`);
}

function cmdAddSite(args) {
  const { values } = parseArgs({
    args,
    options: {
      ledger: { type: 'string' },
      id: { type: 'string' },
      title: { type: 'string' },
      file: { type: 'string' },
      line: { type: 'string' },
      module: { type: 'string' },
    },
  });
  if (!values.file) fail('add-site requires --file <path>');
  const path = resolve(values.ledger ?? DEFAULT_LEDGER);
  const ledger = loadLedger(path);
  const ids = new Set(ledger.sites.map((s) => s.id));
  ledger.sites.push(
    normalizeSite(
      {
        id: values.id,
        title: values.title,
        file: values.file,
        line: parseLine(values.line),
        module: values.module,
      },
      ledger.sites.length,
      ids,
    ),
  );
  saveLedger(path, ledger);
  process.stdout.write(`ok: added site ${ledger.sites[ledger.sites.length - 1].id}\n`);
}

function cmdClassify(args) {
  const { values } = parseArgs({
    args,
    options: {
      ledger: { type: 'string' },
      site: { type: 'string' },
      as: { type: 'string' },
      reason: { type: 'string' },
      force: { type: 'boolean' },
    },
  });
  if (!values.site || !values.as) fail('classify requires --site <id> --as mechanical|semantic [--reason]');
  if (!['mechanical', 'semantic'].includes(values.as)) fail("--as must be 'mechanical' or 'semantic'");
  if (values.as === 'semantic' && !values.reason) {
    fail('classifying a site as semantic requires --reason (why is it gate-invisible?)');
  }
  const path = resolve(values.ledger ?? DEFAULT_LEDGER);
  const ledger = loadLedger(path);
  const site = siteById(ledger, values.site);
  if (site.classification === 'semantic' && values.as === 'mechanical' && !values.force) {
    fail(
      `site ${site.id} is semantic-escalate — downgrading to mechanical removes the human-decision requirement. ` +
        `Re-run with --force --reason "<why the escalation no longer applies>" if that is really intended.`,
    );
  }
  if (site.classification === 'semantic' && values.as === 'mechanical' && !values.reason) {
    fail('downgrading a semantic site requires --reason');
  }
  if (site.decision) {
    // The stored decision answered the PRIOR escalation; a re-classification (new
    // reason, or a downgrade) makes it stale — clear it so the gate re-blocks.
    site.decision = null;
    process.stdout.write(`warn: ${site.id} had a recorded decision — cleared (it answered the previous classification; re-run 'decide' if still semantic)\n`);
  }
  site.classification = values.as;
  site.classificationReason = values.reason ?? null;
  saveLedger(path, ledger);
  process.stdout.write(`ok: ${site.id} classified ${values.as}\n`);
}

function cmdDecide(args) {
  const { values } = parseArgs({
    args,
    options: {
      ledger: { type: 'string' },
      site: { type: 'string' },
      choice: { type: 'string' },
      by: { type: 'string' },
      notes: { type: 'string' },
    },
  });
  if (!values.site || !values.choice || !values.by) {
    fail('decide requires --site <id> --choice <text> --by <who>');
  }
  const path = resolve(values.ledger ?? DEFAULT_LEDGER);
  const ledger = loadLedger(path);
  const site = siteById(ledger, values.site);
  if (site.classification !== 'semantic') {
    fail(`site ${site.id} is '${site.classification}' — decisions are recorded only on semantic-escalate sites`);
  }
  site.decision = {
    choice: values.choice,
    by: values.by,
    at: nowIso(),
    notes: values.notes ?? null,
    reasonDecided: site.classificationReason,
  };
  saveLedger(path, ledger);
  process.stdout.write(`ok: decision recorded on ${site.id} (by ${values.by})\n`);
}

function cmdAssign(args) {
  const { values } = parseArgs({
    args,
    options: {
      ledger: { type: 'string' },
      agent: { type: 'string' },
      sites: { type: 'string' },
      files: { type: 'string' },
      'files-from': { type: 'string' },
    },
  });
  if (!values.agent || !values.sites) fail('assign requires --agent <id> --sites s1,s2 [--files f1,f2 | --files-from <file>]');
  let files = [];
  if (values.files) files = values.files.split(',').map((f) => f.trim()).filter(Boolean);
  if (values['files-from']) {
    files = files.concat(
      readFileSync(resolve(values['files-from']), 'utf8')
        .split('\n')
        .map((f) => f.trim())
        .filter(Boolean),
    );
  }
  if (files.length === 0) fail('assign requires a non-empty file set (--files or --files-from)');
  const path = resolve(values.ledger ?? DEFAULT_LEDGER);
  const ledger = loadLedger(path);
  const siteIds = values.sites.split(',').map((s) => s.trim()).filter(Boolean);
  for (const id of siteIds) siteById(ledger, id); // validate
  const existing = ledger.agents.find((a) => a.id === values.agent);
  if (existing) {
    existing.sites = siteIds;
    existing.files = files;
  } else {
    ledger.agents.push({ id: values.agent, sites: siteIds, files });
  }
  saveLedger(path, ledger);
  process.stdout.write(`ok: agent ${values.agent} assigned ${siteIds.length} site(s), ${files.length} file(s)\n`);
}

function partitionOverlaps(ledger) {
  const overlaps = [];
  for (let i = 0; i < ledger.agents.length; i++) {
    for (let j = i + 1; j < ledger.agents.length; j++) {
      const a = ledger.agents[i];
      const b = ledger.agents[j];
      const setB = new Set(b.files);
      const shared = a.files.filter((f) => setB.has(f));
      if (shared.length > 0) overlaps.push({ a: a.id, b: b.id, shared });
    }
  }
  return overlaps;
}

function cmdCheckPartition(args) {
  const { values } = parseArgs({ args, options: { ledger: { type: 'string' } } });
  const path = resolve(values.ledger ?? DEFAULT_LEDGER);
  const ledger = loadLedger(path);
  if (ledger.agents.length === 0) fail('no agents declared (assign first)', 2);
  const overlaps = partitionOverlaps(ledger);
  if (overlaps.length > 0) {
    for (const o of overlaps) {
      process.stderr.write(`FAIL[partition]: agents ${o.a} and ${o.b} share: ${o.shared.join(', ')}\n`);
    }
    process.stderr.write('PARTITION CHECK FAILED\n');
    process.exit(1);
  }
  process.stdout.write(`ok: ${ledger.agents.length} agent file sets are pairwise disjoint\n`);
}

function cmdAddVerify(args) {
  const { values } = parseArgs({
    args,
    options: {
      ledger: { type: 'string' },
      id: { type: 'string' },
      type: { type: 'string' },
      template: { type: 'string' },
      pattern: { type: 'string' },
      paths: { type: 'string' },
      granularity: { type: 'string' },
    },
  });
  if (!values.id || !values.type) fail('add-verify requires --id <id> --type command|grep-absent');
  if (!VERIFY_TYPES.includes(values.type)) fail(`--type must be one of ${VERIFY_TYPES.join('|')}`);
  if (values.type === 'command' && !values.template?.trim()) {
    fail("type 'command' requires a non-empty --template (placeholders {module} {file} {line} are shell-quoted automatically — do NOT quote them in the template)");
  }
  if (values.type === 'grep-absent' && !values.pattern) fail("type 'grep-absent' requires --pattern [--paths p1,p2]");
  const granularity = values.granularity ?? 'site';
  if (!['site', 'module', 'global'].includes(granularity)) fail("--granularity must be site|module|global");
  const path = resolve(values.ledger ?? DEFAULT_LEDGER);
  const ledger = loadLedger(path);
  if (ledger.verify.some((v) => v.id === values.id)) fail(`verify entry '${values.id}' already exists`);
  ledger.verify.push({
    id: values.id,
    type: values.type,
    template: values.template ?? null,
    pattern: values.pattern ?? null,
    paths: values.paths ? values.paths.split(',').map((p) => p.trim()).filter(Boolean) : [],
    granularity,
  });
  saveLedger(path, ledger);
  process.stdout.write(`ok: verify entry '${values.id}' added\n`);
}

// Substituted values are single-quote shell-escaped: templates must NOT quote the
// placeholders themselves (`test -f {file}`, never `test -f "{file}"`). This keeps
// paths with spaces correct and inventory values inert as shell input.
function escapeShell(value) {
  return `'${String(value).replaceAll("'", `'\\''`)}'`;
}

function resolveTemplate(template, site) {
  return template
    .replaceAll('{module}', escapeShell(site.module ?? ''))
    .replaceAll('{file}', escapeShell(site.file))
    .replaceAll('{line}', escapeShell(site.line ?? ''));
}

function spawnRecord(status, command, exitCode, detail) {
  return { status, command, exitCode, detail, at: nowIso() };
}

function runVerifyEntry(entry, site, cache, cwd) {
  try {
    if (entry.type === 'command') {
      if (entry.granularity === 'module' && !site.module) {
        return spawnRecord('fail', entry.template, null, 'site has no module but verify granularity is module');
      }
      const command = resolveTemplate(entry.template, site);
      if (!command.replaceAll("''", '').trim()) {
        return spawnRecord('fail', command, null, 'template resolved to an empty command');
      }
      const cacheKey = `command:${command}`;
      if (cache.has(cacheKey)) return { ...cache.get(cacheKey), at: nowIso() };
      const res = spawnSync(command, { shell: true, cwd, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
      let record;
      if (res.error) {
        record = spawnRecord('fail', command, res.status, `spawn failed: ${res.error.message}`);
      } else {
        const combined = `${res.stdout ?? ''}${res.stderr ?? ''}`;
        record = spawnRecord(res.status === 0 ? 'pass' : 'fail', command, res.status, res.status === 0 ? null : combined.slice(-2000));
      }
      cache.set(cacheKey, record);
      return record;
    }
    if (entry.type === 'grep-absent') {
      const paths = entry.paths.length > 0 ? entry.paths : ['.'];
      const label = `git grep -nE --untracked '${entry.pattern}' -- ${paths.join(' ')}`;
      const cacheKey = `grep:${entry.pattern}:${paths.join(',')}`;
      if (cache.has(cacheKey)) return { ...cache.get(cacheKey), at: nowIso() };
      // Guard against a vacuous pass: `git grep` exits 1 both for "pattern absent"
      // and for "pathspec matched no files" — a typo'd or migration-removed scope
      // would silently green. Require the scope to resolve to at least one
      // tracked-or-untracked (non-ignored) file first.
      const scope = spawnSync('git', ['ls-files', '-c', '-o', '--exclude-standard', '--', ...paths], {
        cwd, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
      });
      let record;
      if (scope.error || scope.status !== 0) {
        record = spawnRecord('fail', label, scope.status ?? null, `scope check failed: ${scope.error?.message ?? (scope.stderr ?? '').slice(0, 500)}`);
      } else if (!(scope.stdout ?? '').trim()) {
        record = spawnRecord('fail', label, null, `scope matched no files (paths: ${paths.join(', ')}) — absence would be vacuous; fix --paths`);
      } else {
        const res = spawnSync('git', ['grep', '-nE', '--untracked', entry.pattern, '--', ...paths], {
          cwd, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
        });
        if (res.error) {
          record = spawnRecord('fail', label, res.status, `git grep spawn failed: ${res.error.message}`);
        } else if (res.status === 1) {
          record = spawnRecord('pass', label, 1, null);
        } else if (res.status === 0) {
          record = spawnRecord('fail', label, 0, `pattern still present:\n${(res.stdout ?? '').slice(0, 2000)}`);
        } else {
          record = spawnRecord('fail', label, res.status, `git grep errored: ${res.error?.message ?? (res.stderr ?? '').slice(0, 500)}`);
        }
      }
      cache.set(cacheKey, record);
      return record;
    }
    return spawnRecord('fail', null, null, `unknown verify type '${entry.type}'`);
  } catch (e) {
    return spawnRecord('fail', entry.template ?? entry.pattern ?? null, null, `verify runner error: ${e.message}`);
  }
}

function runVerifies(ledger, targets, entries, cwd) {
  const cache = new Map();
  const tree = treeFingerprint(cwd);
  const resolvedBySiteEntry = new Map(); // entryId -> Map(command -> [siteIds])
  let failures = 0;
  for (const site of targets) {
    for (const entry of entries) {
      const record = runVerifyEntry(entry, site, cache, cwd);
      record.tree = tree;
      site.verifyRuns[entry.id] = record;
      if (entry.type === 'command' && entry.granularity === 'site' && record.command) {
        const m = resolvedBySiteEntry.get(entry.id) ?? new Map();
        m.set(record.command, [...(m.get(record.command) ?? []), site.id]);
        resolvedBySiteEntry.set(entry.id, m);
      }
      const tag = record.status === 'pass' ? 'ok' : 'FAIL';
      process.stdout.write(`${tag}[${site.id}:${entry.id}]: ${record.command ?? entry.type} (exit ${record.exitCode})\n`);
      if (record.status !== 'pass') {
        failures++;
        if (record.detail) process.stdout.write(`  ${record.detail.split('\n').slice(0, 6).join('\n  ')}\n`);
      }
    }
  }
  for (const [entryId, m] of resolvedBySiteEntry) {
    for (const [command, siteIds] of m) {
      if (siteIds.length > 1) {
        process.stdout.write(
          `warn[${entryId}]: site-granularity command resolved identically for ${siteIds.length} sites (${siteIds.join(', ')}) — a missing {file}/{module} placeholder makes per-site evidence collapse into one run: ${command}\n`,
        );
      }
    }
  }
  return failures;
}

function cmdVerify(args) {
  const { values } = parseArgs({
    args,
    options: {
      ledger: { type: 'string' },
      site: { type: 'string' },
      all: { type: 'boolean' },
      only: { type: 'string' },
      cwd: { type: 'string' },
    },
  });
  if (!values.site && !values.all) fail('verify requires --site <id> or --all');
  const path = resolve(values.ledger ?? DEFAULT_LEDGER);
  const ledger = loadLedger(path);
  if (ledger.verify.length === 0) fail('no verify entries declared (add-verify first)');
  const targets = values.all ? ledger.sites : [siteById(ledger, values.site)];
  const entries = values.only ? ledger.verify.filter((v) => v.id === values.only) : ledger.verify;
  if (values.only && entries.length === 0) fail(`no verify entry '${values.only}'`);
  const cwd = values.cwd ? resolve(values.cwd) : dirname(path);
  const failures = runVerifies(ledger, targets, entries, cwd);
  saveLedger(path, ledger);
  if (failures > 0) {
    process.stderr.write(`VERIFY FAILED (${failures} failing run(s))\n`);
    process.exit(1);
  }
  process.stdout.write('VERIFY PASSED\n');
}

function cmdOutcome(args) {
  const { values } = parseArgs({
    args,
    options: {
      ledger: { type: 'string' },
      site: { type: 'string' },
      as: { type: 'string' },
      reason: { type: 'string' },
    },
  });
  if (!values.site || !values.as) fail(`outcome requires --site <id> --as ${OUTCOMES.filter((o) => o !== 'pending').join('|')}`);
  if (!OUTCOMES.includes(values.as) || values.as === 'pending') fail(`--as must be one of ${OUTCOMES.filter((o) => o !== 'pending').join('|')}`);
  if (REASON_REQUIRED.includes(values.as) && !values.reason) {
    fail(`outcome '${values.as}' requires --reason`);
  }
  const path = resolve(values.ledger ?? DEFAULT_LEDGER);
  const ledger = loadLedger(path);
  const site = siteById(ledger, values.site);
  site.outcome = values.as;
  site.outcomeReason = values.reason ?? null;
  saveLedger(path, ledger);
  process.stdout.write(`ok: ${site.id} outcome=${values.as}\n`);
}

function gateFailures(ledger, cwd) {
  const failures = [];
  const currentTree = cwd ? treeFingerprint(cwd) : null;
  for (const site of ledger.sites) {
    if (site.classification !== 'mechanical' && site.classification !== 'semantic') {
      failures.push(`${site.id}: unclassified (classify every site before transform)`);
    }
    if (site.classification === 'semantic' && !site.decision) {
      failures.push(`${site.id}: semantic-escalate with NO recorded decision`);
    }
    if (site.outcome === 'pending') {
      failures.push(`${site.id}: outcome pending`);
    }
    if (site.outcome === 'done') {
      for (const entry of ledger.verify) {
        const run = site.verifyRuns[entry.id];
        if (!run) failures.push(`${site.id}: verify '${entry.id}' never ran`);
        else if (run.status !== 'pass') failures.push(`${site.id}: verify '${entry.id}' is ${run.status}`);
        else if (currentTree && run.tree && run.tree !== currentTree) {
          failures.push(`${site.id}: verify '${entry.id}' evidence is STALE (tree changed since the run) — re-run verify or gate --reverify`);
        }
      }
    }
    if (REASON_REQUIRED.includes(site.outcome) && !site.outcomeReason) {
      failures.push(`${site.id}: outcome '${site.outcome}' without a reason`);
    }
  }
  if (ledger.isolation === 'partition' && ledger.agents.length > 0) {
    for (const o of partitionOverlaps(ledger)) {
      failures.push(`partition: agents ${o.a} and ${o.b} share files (${o.shared.length})`);
    }
  }
  if (ledger.sites.length === 0) {
    failures.push('ledger has zero sites — refusing to pass an empty migration (import the discovery inventory first)');
  }
  return failures;
}

function cmdGate(args) {
  const { values } = parseArgs({
    args,
    options: { ledger: { type: 'string' }, reverify: { type: 'boolean' }, cwd: { type: 'string' } },
  });
  const path = resolve(values.ledger ?? DEFAULT_LEDGER);
  const ledger = loadLedger(path);
  const cwd = values.cwd ? resolve(values.cwd) : dirname(path);
  if (values.reverify && ledger.verify.length > 0) {
    const doneSites = ledger.sites.filter((s) => s.outcome === 'done');
    runVerifies(ledger, doneSites, ledger.verify, cwd);
    saveLedger(path, ledger);
  }
  const failures = gateFailures(ledger, cwd);
  if (failures.length > 0) {
    for (const f of failures) process.stderr.write(`FAIL[gate]: ${f}\n`);
    process.stderr.write(`GATE FAILED (${failures.length} finding(s))\n`);
    process.exit(1);
  }
  const done = ledger.sites.filter((s) => s.outcome === 'done').length;
  process.stdout.write(
    `GATE PASSED — ${ledger.sites.length} site(s): ${done} done, ${ledger.sites.length - done} closed with reasons\n`,
  );
}

function cmdStatus(args) {
  const { values } = parseArgs({ args, options: { ledger: { type: 'string' } } });
  const path = resolve(values.ledger ?? DEFAULT_LEDGER);
  const ledger = loadLedger(path);
  const byClass = {};
  const byOutcome = {};
  for (const s of ledger.sites) {
    byClass[s.classification] = (byClass[s.classification] ?? 0) + 1;
    byOutcome[s.outcome] = (byOutcome[s.outcome] ?? 0) + 1;
  }
  process.stdout.write(`migration: ${ledger.migration.id} (isolation=${ledger.isolation})\n`);
  if (ledger.migration.invariant) process.stdout.write(`invariant: ${ledger.migration.invariant}\n`);
  process.stdout.write(`sites: ${ledger.sites.length}\n`);
  process.stdout.write(`  classification: ${JSON.stringify(byClass)}\n`);
  process.stdout.write(`  outcomes:       ${JSON.stringify(byOutcome)}\n`);
  const undecided = ledger.sites.filter((s) => s.classification === 'semantic' && !s.decision);
  if (undecided.length > 0) {
    process.stdout.write(`  UNDECIDED semantic sites: ${undecided.map((s) => s.id).join(', ')}\n`);
  }
  process.stdout.write(`verify entries: ${ledger.verify.map((v) => v.id).join(', ') || '(none)'}\n`);
  process.stdout.write(`agents: ${ledger.agents.map((a) => `${a.id}(${a.files.length}f)`).join(', ') || '(none)'}\n`);
  const failures = gateFailures(ledger, dirname(path));
  process.stdout.write(failures.length === 0 ? 'gate: WOULD PASS\n' : `gate: would fail (${failures.length} finding(s))\n`);
}

// Hook mode: wire as a PreToolUse hook on Bash in the consuming project (optional —
// see SHIM-TEMPLATE). Reads the hook payload JSON on stdin; if the command is a git
// commit and a ledger exists ($MIGRATE_LEDGER, or upward of the command's cwd /
// `git -C` target using the DEFAULT names), runs the gate and blocks the commit
// (exit 2) while it fails. No ledger found → allow (exit 0). Best-effort against
// accidents: a renamed --ledger file is invisible to the hook — keep the default
// name, or export MIGRATE_LEDGER.
function findLedgerUpward(startDir) {
  let dir = resolve(startDir);
  for (;;) {
    for (const candidate of [join(dir, DEFAULT_LEDGER), join(dir, '.migration', 'ledger.json')]) {
      if (existsSync(candidate)) return candidate;
    }
    const parent = dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

function cmdHook() {
  let payload;
  try {
    payload = JSON.parse(readFileSync(0, 'utf8'));
  } catch {
    process.exit(0); // not a hook payload — never block on our own malfunction
  }
  const command = payload?.tool_input?.command ?? '';
  if (!/\bgit\b[^\n;|&]*\bcommit\b/.test(command)) process.exit(0);
  const cwd = payload?.cwd ?? process.cwd();
  // Honor `git -C <dir>` so a commit targeted at another directory still finds
  // that repo's ledger (first -C occurrence; quoted or bare).
  const cMatch = command.match(/\bgit\b\s+(?:[^\n;|&]*?\s)?-C\s+(?:"([^"]+)"|'([^']+)'|(\S+))/);
  const target = cMatch ? resolve(cwd, cMatch[1] ?? cMatch[2] ?? cMatch[3]) : cwd;
  const ledgerPath = process.env.MIGRATE_LEDGER ?? findLedgerUpward(target) ?? (target !== cwd ? findLedgerUpward(cwd) : null);
  if (!ledgerPath || !existsSync(ledgerPath)) process.exit(0);
  let ledger;
  try {
    ledger = JSON.parse(readFileSync(ledgerPath, 'utf8'));
  } catch {
    process.stderr.write(`migrate-ledger hook: ${ledgerPath} is unreadable — blocking commit until fixed\n`);
    process.exit(2);
  }
  if (
    ledger === null || typeof ledger !== 'object' || Array.isArray(ledger) ||
    ledger.schemaVersion !== SCHEMA_VERSION || !Array.isArray(ledger.sites) ||
    !Array.isArray(ledger.verify) || !Array.isArray(ledger.agents)
  ) {
    process.stderr.write(`migrate-ledger hook: ${ledgerPath} malformed — blocking commit until fixed\n`);
    process.exit(2);
  }
  const failures = gateFailures(ledger, dirname(ledgerPath));
  if (failures.length > 0) {
    process.stderr.write(`migrate-ledger hook: migration gate failing (${ledgerPath}):\n`);
    for (const f of failures.slice(0, 10)) process.stderr.write(`  - ${f}\n`);
    process.stderr.write('Resolve (decide/verify/outcome) or close sites with reasons before committing.\n');
    process.exit(2);
  }
  process.exit(0);
}

// ---------------------------------------------------------------------------

const USAGE = `migrate-ledger <command> [options]

commands:
  init            --id <migration-id> [--invariant <text>] [--isolation partition|worktree|lead-serial] [--force]
  import          --sites <json-file>            bulk-add discovery inventory (array of {id?,title?,file,line?,module?})
  add-site        --file <path> [--id --title --line --module]
  classify        --site <id> --as mechanical|semantic [--reason <why>] [--force]
                  (semantic requires --reason; semantic→mechanical downgrade requires --force --reason;
                   any re-classification clears a previously recorded decision)
  decide          --site <id> --choice <text> --by <who> [--notes]        (semantic sites only)
  assign          --agent <id> --sites s1,s2 (--files f1,f2 | --files-from <file>)
  check-partition                                 pairwise agent file-set disjointness
  add-verify      --id <id> --type command|grep-absent (--template <cmd> | --pattern <re> [--paths p1,p2]) [--granularity site|module|global]
                  ({module}/{file}/{line} in templates are shell-quoted automatically — don't quote them yourself;
                   grep-absent fails when its --paths scope matches no files, so absence can't pass vacuously)
  verify          (--site <id> | --all) [--only <verify-id>] [--cwd <dir>]
  outcome         --site <id> --as done|skipped|deferred|reverted|wontfix [--reason]  (non-done requires --reason)
  gate            [--reverify] [--cwd <dir>]      exit 1 while any semantic site lacks a decision, any outcome is
                                                  pending, any done site lacks passing verifies, or (in a git repo)
                                                  the tree changed since the verify evidence was recorded
  status
  hook                                            PreToolUse stdin-JSON mode: block git commit while gate fails

Every command accepts --ledger <path> (default ./${DEFAULT_LEDGER}).
Exit codes: 0 ok/pass · 1 gate/check failed · 2 usage or ledger error.`;

const [command, ...rest] = process.argv.slice(2);
try {
  switch (command) {
    case 'init': cmdInit(rest); break;
    case 'import': cmdImport(rest); break;
    case 'add-site': cmdAddSite(rest); break;
    case 'classify': cmdClassify(rest); break;
    case 'decide': cmdDecide(rest); break;
    case 'assign': cmdAssign(rest); break;
    case 'check-partition': cmdCheckPartition(rest); break;
    case 'add-verify': cmdAddVerify(rest); break;
    case 'verify': cmdVerify(rest); break;
    case 'outcome': cmdOutcome(rest); break;
    case 'gate': cmdGate(rest); break;
    case 'status': cmdStatus(rest); break;
    case 'hook': cmdHook(); break;
    case undefined:
    case '--help':
    case 'help': process.stdout.write(USAGE + '\n'); process.exit(command ? 0 : 2); break;
    default: fail(`unknown command '${command}'\n\n${USAGE}`);
  }
} catch (e) {
  if (e?.code === 'ERR_PARSE_ARGS_UNKNOWN_OPTION' || e?.code === 'ERR_PARSE_ARGS_INVALID_OPTION_VALUE') {
    fail(e.message);
  }
  throw e;
}
