import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { listProtocols, loadProtocol, normalizeProtocol } from '../lib/protocols.js'

function makeDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-labscript-protocols-'))
}

test('listProtocols and name resolution work', () => {
  const dir = makeDir()
  fs.writeFileSync(path.join(dir, 'demo_scan.yaml'), [
    'name: demo_scan',
    'labscript: /tmp/sequence.py',
    'scan:',
    '  duration_test_us: np.linspace(0, 8, 3)',
    'reps: 2',
  ].join(String.fromCharCode(10)))
  fs.writeFileSync(path.join(dir, 'legacy.yaml'), [
    'joint_script: legacy_sequence',
    'analysis: demo.peaks',
    'finish:',
    '  restore: true',
    '  panel: { duration_test_us: fit.center }',
    'unknown_key: 1',
  ].join(String.fromCharCode(10)))
  const list = listProtocols(dir)
  assert.equal(list.length, 2)
  for (const ref of ['demo_scan', 'demo_scan.yaml', path.join(dir, 'demo_scan.yaml')]) {
    const loaded = loadProtocol(dir, ref)
    assert.equal(loaded.name, 'demo_scan')
    assert.equal(loaded.plan.reps, 2)
    assert.equal(loaded.plan.engage, true)
    assert.equal(loaded.plan.finish.restore, true)
  }
  const legacy = loadProtocol(dir, 'legacy')
  assert.equal(legacy.plan.postprocess, 'demo.peaks')
  assert.deepEqual(legacy.plan.finish.panel, { duration_test_us: 'fit.center' })
  assert.ok(legacy.warnings.some((item) => item.includes('analysis')))
  assert.ok(legacy.warnings.some((item) => item.includes('unknown_key')))
})

test('loadProtocol reports missing files', () => {
  const dir = makeDir()
  assert.throws(() => loadProtocol(dir, 'nope'), /protocol not found/)
})

test('normalizeProtocol rejects bad plans', () => {
  assert.throws(() => normalizeProtocol([], 'inline'), /mapping/)
  assert.throws(() => normalizeProtocol({ set: {} }, 'inline'), /labscript/)
  assert.throws(() => normalizeProtocol({ labscript: 'x', reps: 0 }, 'inline'), /reps/)
})
