// Fixture-driven tests for migrate-ledger.mjs — run with `node --test scripts/*.test.mjs`.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, mkdirSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const TOOL = join(dirname(fileURLToPath(import.meta.url)), 'migrate-ledger.mjs');

function run(cwd, args, { input } = {}) {
  return spawnSync(process.execPath, [TOOL, ...args], { cwd, encoding: 'utf8', input });
}

function freshDir() {
  return mkdtempSync(join(tmpdir(), 'migrate-ledger-'));
}

function gitDir() {
  const dir = freshDir();
  spawnSync('git', ['init', '-q'], { cwd: dir });
  spawnSync('git', ['-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '--allow-empty', '-q', '-m', 'init'], { cwd: dir });
  return dir;
}

function initWithSites(dir, sites) {
  assert.equal(run(dir, ['init', '--id', 'test-migration']).status, 0);
  writeFileSync(join(dir, 'sites.json'), JSON.stringify(sites));
  assert.equal(run(dir, ['import', '--sites', 'sites.json']).status, 0);
}

test('init refuses overwrite without --force; unknown command/flag exits 2', () => {
  const dir = freshDir();
  assert.equal(run(dir, ['init', '--id', 'm']).status, 0);
  assert.equal(run(dir, ['init', '--id', 'm']).status, 2);
  assert.equal(run(dir, ['init', '--id', 'm', '--force']).status, 0);
  assert.equal(run(dir, ['frobnicate']).status, 2);
  assert.equal(run(dir, ['init', '--id', 'm', '--bogus-flag']).status, 2);
});

test('malformed ledgers fail with exit 2, not stack traces (null, non-object, dup ids)', () => {
  const dir = freshDir();
  const ledgerPath = join(dir, 'migration-ledger.json');

  writeFileSync(ledgerPath, 'null');
  let res = run(dir, ['status']);
  assert.equal(res.status, 2);
  assert.match(res.stderr, /not a JSON object/);
  assert.doesNotMatch(res.stderr, /at .*migrate-ledger\.mjs/); // no stack trace

  writeFileSync(ledgerPath, '[1,2]');
  assert.equal(run(dir, ['status']).status, 2);

  run(dir, ['init', '--id', 'm', '--force']);
  const ledger = JSON.parse(readFileSync(ledgerPath, 'utf8'));
  ledger.sites = [
    { id: 'dup', file: 'a', classification: 'mechanical', outcome: 'done', verifyRuns: {} },
    { id: 'dup', file: 'b', classification: 'mechanical', outcome: 'done', verifyRuns: {} },
  ];
  writeFileSync(ledgerPath, JSON.stringify(ledger));
  res = run(dir, ['status']);
  assert.equal(res.status, 2);
  assert.match(res.stderr, /duplicate id 'dup'/);

  ledger.sites = [];
  ledger.verify = [
    { id: 'v', type: 'command', template: 'true', paths: [], granularity: 'site' },
    { id: 'v', type: 'command', template: 'false', paths: [], granularity: 'site' },
  ];
  writeFileSync(ledgerPath, JSON.stringify(ledger));
  res = run(dir, ['status']);
  assert.equal(res.status, 2);
  assert.match(res.stderr, /duplicate id 'v'/);
});

test('gate fails on empty ledger, unclassified sites, undecided semantic, pending outcomes', () => {
  const dir = freshDir();
  assert.equal(run(dir, ['init', '--id', 'm']).status, 0);

  let gate = run(dir, ['gate']);
  assert.equal(gate.status, 1);
  assert.match(gate.stderr, /zero sites/);

  writeFileSync(join(dir, 'sites.json'), JSON.stringify([
    { id: 'a', file: 'x/A.kt', module: ':x' },
    { id: 'b', file: 'y/B.kt', module: ':y' },
  ]));
  assert.equal(run(dir, ['import', '--sites', 'sites.json']).status, 0);

  gate = run(dir, ['gate']);
  assert.equal(gate.status, 1);
  assert.match(gate.stderr, /unclassified/);

  assert.equal(run(dir, ['classify', '--site', 'a', '--as', 'semantic']).status, 2);
  assert.equal(run(dir, ['classify', '--site', 'a', '--as', 'semantic', '--reason', 'gate-invisible copy choice']).status, 0);
  assert.equal(run(dir, ['classify', '--site', 'b', '--as', 'mechanical']).status, 0);

  gate = run(dir, ['gate']);
  assert.equal(gate.status, 1);
  assert.match(gate.stderr, /semantic-escalate with NO recorded decision/);

  assert.equal(run(dir, ['decide', '--site', 'b', '--choice', 'x', '--by', 'me']).status, 2);
  assert.equal(run(dir, ['decide', '--site', 'a', '--choice', 'keep variant 2', '--by', 'owner']).status, 0);

  gate = run(dir, ['gate']);
  assert.equal(gate.status, 1);
  assert.match(gate.stderr, /outcome pending/);
});

test('re-classification clears a recorded decision and re-blocks; semantic→mechanical needs --force', () => {
  const dir = freshDir();
  initWithSites(dir, [{ id: 'a', file: 'x/A.kt' }]);
  run(dir, ['classify', '--site', 'a', '--as', 'semantic', '--reason', 'r1']);
  run(dir, ['decide', '--site', 'a', '--choice', 'keep variant 2', '--by', 'owner']);
  run(dir, ['outcome', '--site', 'a', '--as', 'done']);
  assert.equal(run(dir, ['gate']).status, 0);

  // Downgrade without --force is refused.
  let res = run(dir, ['classify', '--site', 'a', '--as', 'mechanical']);
  assert.equal(res.status, 2);
  assert.match(res.stderr, /--force/);

  // Re-escalating with a NEW reason clears the old decision → gate re-blocks.
  res = run(dir, ['classify', '--site', 'a', '--as', 'semantic', '--reason', 'r2: new dilemma']);
  assert.equal(res.status, 0);
  assert.match(res.stdout, /decision — cleared/);
  const gate = run(dir, ['gate']);
  assert.equal(gate.status, 1);
  assert.match(gate.stderr, /NO recorded decision/);

  // Forced downgrade requires a reason and also clears the (re-)decision.
  run(dir, ['decide', '--site', 'a', '--choice', 'v3', '--by', 'owner']);
  res = run(dir, ['classify', '--site', 'a', '--as', 'mechanical', '--force']);
  assert.equal(res.status, 2);
  res = run(dir, ['classify', '--site', 'a', '--as', 'mechanical', '--force', '--reason', 'escalation resolved structurally']);
  assert.equal(res.status, 0);
  assert.equal(run(dir, ['gate']).status, 0);
});

test('done sites require every declared verify to pass; verify records land in the ledger', () => {
  const dir = freshDir();
  initWithSites(dir, [{ id: 'a', file: 'x/A.kt', module: ':x' }]);
  assert.equal(run(dir, ['classify', '--site', 'a', '--as', 'mechanical']).status, 0);
  assert.equal(run(dir, ['add-verify', '--id', 'probe', '--type', 'command', '--template', 'exit 1', '--granularity', 'module']).status, 0);
  assert.equal(run(dir, ['outcome', '--site', 'a', '--as', 'done']).status, 0);

  let gate = run(dir, ['gate']);
  assert.equal(gate.status, 1);
  assert.match(gate.stderr, /verify 'probe' never ran/);

  let verify = run(dir, ['verify', '--all']);
  assert.equal(verify.status, 1);
  gate = run(dir, ['gate']);
  assert.equal(gate.status, 1);
  assert.match(gate.stderr, /verify 'probe' is fail/);

  const ledgerPath = join(dir, 'migration-ledger.json');
  const ledger = JSON.parse(readFileSync(ledgerPath, 'utf8'));
  ledger.verify[0].template = 'exit 0';
  writeFileSync(ledgerPath, JSON.stringify(ledger, null, 2));
  verify = run(dir, ['verify', '--all']);
  assert.equal(verify.status, 0);
  const after = JSON.parse(readFileSync(ledgerPath, 'utf8'));
  assert.equal(after.sites[0].verifyRuns.probe.status, 'pass');
  assert.equal(after.sites[0].verifyRuns.probe.exitCode, 0);

  gate = run(dir, ['gate']);
  assert.equal(gate.status, 0);
  assert.match(gate.stdout, /GATE PASSED/);
});

test('placeholders are shell-quoted (files with spaces work; injection is inert); empty template rejected', () => {
  const dir = freshDir();
  mkdirSync(join(dir, 'src'));
  writeFileSync(join(dir, 'src', 'My File.kt'), 'x');
  writeFileSync(join(dir, 'src', 'Plain.kt'), 'x');
  initWithSites(dir, [
    { id: 'sp', file: 'src/My File.kt' },
    { id: 'pl', file: 'src/Plain.kt' },
    { id: 'inj', file: "src/x'; touch pwned; echo '.kt" },
  ]);
  for (const s of ['sp', 'pl', 'inj']) run(dir, ['classify', '--site', s, '--as', 'mechanical']);
  assert.equal(run(dir, ['add-verify', '--id', 'exists', '--type', 'command', '--template', 'test -f {file}']).status, 0);
  const verify = run(dir, ['verify', '--all']);
  assert.equal(verify.status, 1); // only 'inj' fails (file doesn't exist) — but nothing executed
  const ledger = JSON.parse(readFileSync(join(dir, 'migration-ledger.json'), 'utf8'));
  assert.equal(ledger.sites[0].verifyRuns.exists.status, 'pass'); // space path verified correctly
  assert.equal(ledger.sites[1].verifyRuns.exists.status, 'pass');
  assert.equal(ledger.sites[2].verifyRuns.exists.status, 'fail');
  assert.equal(existsSync(join(dir, 'pwned')), false); // injection inert

  assert.equal(run(dir, ['add-verify', '--id', 'empty', '--type', 'command', '--template', '   ']).status, 2);
});

test('module-granularity command without module fails; identical site-granularity commands warn', () => {
  const dir = freshDir();
  initWithSites(dir, [
    { id: 'a', file: 'x/A.kt', module: ':x' },
    { id: 'nomod', file: 'y/B.kt' },
  ]);
  run(dir, ['classify', '--site', 'a', '--as', 'mechanical']);
  run(dir, ['classify', '--site', 'nomod', '--as', 'mechanical']);
  assert.equal(
    run(dir, ['add-verify', '--id', 'echo', '--type', 'command', '--template', 'test {module} = :x', '--granularity', 'module']).status,
    0,
  );
  const verify = run(dir, ['verify', '--all']);
  assert.equal(verify.status, 1); // nomod fails (no module)
  const ledger = JSON.parse(readFileSync(join(dir, 'migration-ledger.json'), 'utf8'));
  assert.equal(ledger.sites[0].verifyRuns.echo.status, 'pass');
  assert.equal(ledger.sites[1].verifyRuns.echo.status, 'fail');
  assert.match(ledger.sites[1].verifyRuns.echo.detail, /no module/);

  // site-granularity command with no placeholder → identical for all sites → warn.
  run(dir, ['add-verify', '--id', 'static', '--type', 'command', '--template', 'true']);
  const v2 = run(dir, ['verify', '--all', '--only', 'static']);
  assert.equal(v2.status, 0);
  assert.match(v2.stdout, /warn\[static\].*resolved identically for 2 sites/);
});

test('gate --reverify re-derives evidence from real runs (hand-faked pass gets caught)', () => {
  const dir = freshDir();
  initWithSites(dir, [{ id: 'a', file: 'x/A.kt', module: ':x' }]);
  run(dir, ['classify', '--site', 'a', '--as', 'mechanical']);
  run(dir, ['add-verify', '--id', 'probe', '--type', 'command', '--template', 'exit 1']);
  run(dir, ['outcome', '--site', 'a', '--as', 'done']);
  const ledgerPath = join(dir, 'migration-ledger.json');
  const l = JSON.parse(readFileSync(ledgerPath, 'utf8'));
  l.sites[0].verifyRuns.probe = { status: 'pass', command: 'exit 1', exitCode: 0, at: 'forged' };
  writeFileSync(ledgerPath, JSON.stringify(l, null, 2));
  assert.equal(run(dir, ['gate']).status, 0); // stored evidence trusted outside a git repo
  const gate = run(dir, ['gate', '--reverify']);
  assert.equal(gate.status, 1);
  assert.match(gate.stderr, /verify 'probe' is fail/);
});

test('grep-absent: fails while pattern present, passes when gone, fails vacuous scope, sees untracked files', () => {
  const dir = gitDir();
  mkdirSync(join(dir, 'src'));
  writeFileSync(join(dir, 'src', 'A.kt'), 'val x = legacyParam\n');
  spawnSync('git', ['add', '-A'], { cwd: dir });
  initWithSites(dir, [{ id: 'a', file: 'src/A.kt' }]);
  run(dir, ['classify', '--site', 'a', '--as', 'mechanical']);
  assert.equal(run(dir, ['add-verify', '--id', 'gone', '--type', 'grep-absent', '--pattern', 'legacyParam', '--paths', 'src']).status, 0);

  let verify = run(dir, ['verify', '--all']);
  assert.equal(verify.status, 1);
  assert.match(verify.stdout, /pattern still present/);

  writeFileSync(join(dir, 'src', 'A.kt'), 'val x = 1\n');
  spawnSync('git', ['add', '-A'], { cwd: dir });
  verify = run(dir, ['verify', '--all']);
  assert.equal(verify.status, 0);

  // UNTRACKED file with the pattern is still caught (git grep --untracked).
  writeFileSync(join(dir, 'src', 'New.kt'), 'val y = legacyParam\n');
  verify = run(dir, ['verify', '--all']);
  assert.equal(verify.status, 1);
  spawnSync('rm', [join(dir, 'src', 'New.kt')], { cwd: dir });

  // Vacuous scope (typo'd path) must FAIL, not pass.
  run(dir, ['add-verify', '--id', 'typo', '--type', 'grep-absent', '--pattern', 'legacyParam', '--paths', 'srcc']);
  verify = run(dir, ['verify', '--all', '--only', 'typo']);
  assert.equal(verify.status, 1);
  assert.match(verify.stdout, /scope matched no files/);
});

test('staleness: gate fails when the tree changed after verify evidence was recorded', () => {
  const dir = gitDir();
  mkdirSync(join(dir, 'src'));
  writeFileSync(join(dir, 'src', 'A.kt'), 'clean\n');
  spawnSync('git', ['add', '-A'], { cwd: dir });
  initWithSites(dir, [{ id: 'a', file: 'src/A.kt' }]);
  run(dir, ['classify', '--site', 'a', '--as', 'mechanical']);
  run(dir, ['add-verify', '--id', 'gone', '--type', 'grep-absent', '--pattern', 'legacyParam', '--paths', 'src']);
  assert.equal(run(dir, ['verify', '--all']).status, 0);
  run(dir, ['outcome', '--site', 'a', '--as', 'done']);
  assert.equal(run(dir, ['gate']).status, 0);

  // Reintroduce the banned pattern AFTER the recorded pass → plain gate must fail (stale).
  writeFileSync(join(dir, 'src', 'A.kt'), 'val x = legacyParam\n');
  let gate = run(dir, ['gate']);
  assert.equal(gate.status, 1);
  assert.match(gate.stderr, /STALE/);

  // The hook blocks too (it shares the staleness check).
  const hook = run(dir, ['hook'], { input: JSON.stringify({ tool_input: { command: 'git commit -m x' }, cwd: dir }) });
  assert.equal(hook.status, 2);

  // Re-verifying against the current tree surfaces the real failure.
  gate = run(dir, ['gate', '--reverify']);
  assert.equal(gate.status, 1);
  assert.match(gate.stderr, /verify 'gone' is fail/);

  // Fix the source again → reverified gate passes.
  writeFileSync(join(dir, 'src', 'A.kt'), 'clean again\n');
  assert.equal(run(dir, ['gate', '--reverify']).status, 0);
});

test('outcomes: non-done require reasons; closed-set enforced; non-numeric --line rejected', () => {
  const dir = freshDir();
  initWithSites(dir, [{ id: 'a', file: 'x/A.kt' }]);
  assert.equal(run(dir, ['outcome', '--site', 'a', '--as', 'reverted']).status, 2);
  assert.equal(run(dir, ['outcome', '--site', 'a', '--as', 'reverted', '--reason', 'emission-order regression']).status, 0);
  assert.equal(run(dir, ['outcome', '--site', 'a', '--as', 'pending']).status, 2);
  assert.equal(run(dir, ['outcome', '--site', 'a', '--as', 'bogus']).status, 2);
  const res = run(dir, ['add-site', '--file', 'y/B.kt', '--line', 'not-a-number']);
  assert.equal(res.status, 2);
  assert.match(res.stderr, /--line must be an integer/);
});

test('partition: overlap detected by check-partition and by the gate', () => {
  const dir = freshDir();
  initWithSites(dir, [
    { id: 'a', file: 'x/A.kt' },
    { id: 'b', file: 'y/B.kt' },
  ]);
  run(dir, ['classify', '--site', 'a', '--as', 'mechanical']);
  run(dir, ['classify', '--site', 'b', '--as', 'mechanical']);
  run(dir, ['outcome', '--site', 'a', '--as', 'wontfix', '--reason', 'n/a']);
  run(dir, ['outcome', '--site', 'b', '--as', 'wontfix', '--reason', 'n/a']);
  assert.equal(run(dir, ['assign', '--agent', 'a1', '--sites', 'a', '--files', 'x/A.kt,shared/Common.kt']).status, 0);
  assert.equal(run(dir, ['assign', '--agent', 'a2', '--sites', 'b', '--files', 'y/B.kt,shared/Common.kt']).status, 0);

  const check = run(dir, ['check-partition']);
  assert.equal(check.status, 1);
  assert.match(check.stderr, /share: shared\/Common\.kt/);

  const gate = run(dir, ['gate']);
  assert.equal(gate.status, 1);
  assert.match(gate.stderr, /partition: agents a1 and a2 share files/);

  assert.equal(run(dir, ['assign', '--agent', 'a2', '--sites', 'b', '--files', 'y/B.kt']).status, 0);
  assert.equal(run(dir, ['check-partition']).status, 0);
  assert.equal(run(dir, ['gate']).status, 0);
});

test('assign validates site ids; files required', () => {
  const dir = freshDir();
  initWithSites(dir, [{ id: 'a', file: 'x/A.kt' }]);
  assert.equal(run(dir, ['assign', '--agent', 'a1', '--sites', 'nope', '--files', 'x/A.kt']).status, 2);
  assert.equal(run(dir, ['assign', '--agent', 'a1', '--sites', 'a']).status, 2);
});

test('hook: allows non-commit and no-ledger; blocks while gate fails; fail-closed on malformed; honors git -C', () => {
  const dir = freshDir();

  let hook = run(dir, ['hook'], { input: JSON.stringify({ tool_input: { command: 'git commit -m x' }, cwd: dir }) });
  assert.equal(hook.status, 0);

  initWithSites(dir, [{ id: 'a', file: 'x/A.kt' }]);

  hook = run(dir, ['hook'], { input: JSON.stringify({ tool_input: { command: 'git status' }, cwd: dir }) });
  assert.equal(hook.status, 0);

  hook = run(dir, ['hook'], { input: JSON.stringify({ tool_input: { command: 'git commit -m "wip"' }, cwd: dir }) });
  assert.equal(hook.status, 2);
  assert.match(hook.stderr, /migration gate failing/);

  // Subdirectory cwd finds the ledger upward.
  mkdirSync(join(dir, 'sub'));
  hook = run(dir, ['hook'], { input: JSON.stringify({ tool_input: { command: 'git commit -m "wip"' }, cwd: join(dir, 'sub') }) });
  assert.equal(hook.status, 2);

  // `git -C <dir> commit` from an unrelated cwd finds the target repo's ledger.
  const elsewhere = freshDir();
  hook = run(elsewhere, ['hook'], { input: JSON.stringify({ tool_input: { command: `git -C ${dir} commit -m x` }, cwd: elsewhere }) });
  assert.equal(hook.status, 2);

  // Fail CLOSED on a null/malformed ledger (valid JSON, wrong shape).
  writeFileSync(join(dir, 'migration-ledger.json'), 'null');
  hook = run(dir, ['hook'], { input: JSON.stringify({ tool_input: { command: 'git commit -m x' }, cwd: dir }) });
  assert.equal(hook.status, 2);
  assert.match(hook.stderr, /malformed/);

  // Green ledger → allow.
  run(dir, ['init', '--id', 'm', '--force']);
  writeFileSync(join(dir, 'sites.json'), JSON.stringify([{ id: 'a', file: 'x/A.kt' }]));
  run(dir, ['import', '--sites', 'sites.json']);
  run(dir, ['classify', '--site', 'a', '--as', 'mechanical']);
  run(dir, ['outcome', '--site', 'a', '--as', 'done']);
  hook = run(dir, ['hook'], { input: JSON.stringify({ tool_input: { command: 'git commit -m "ok"' }, cwd: dir }) });
  assert.equal(hook.status, 0);

  // Garbage stdin → allow (never block on our own malfunction).
  hook = run(dir, ['hook'], { input: 'not json' });
  assert.equal(hook.status, 0);
});

test('status summarizes and predicts the gate', () => {
  const dir = freshDir();
  initWithSites(dir, [{ id: 'a', file: 'x/A.kt' }]);
  run(dir, ['classify', '--site', 'a', '--as', 'semantic', '--reason', 'two valid targets']);
  const status = run(dir, ['status']);
  assert.equal(status.status, 0);
  assert.match(status.stdout, /UNDECIDED semantic sites: a/);
  assert.match(status.stdout, /gate: would fail/);
});
