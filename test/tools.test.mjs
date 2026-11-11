import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../lib/index.js'
import { LabError } from '../lib/runtime.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const fixture = path.join(here, 'fixtures', 'fake-bridge.mjs')

function makePlugin(configOverrides, approvalOutcome) {
  const tools = new Map()
  const skills = []
  const ctx = {
    tools: { register(tool) { tools.set(tool.name, tool); return () => {} } },
    skills: { register(skill) { skills.push(skill); return () => {} } },
    logger: { info() {}, debug() {}, warn() {} },
    get(name) {
      if (name !== 'approval') return undefined
      return {
        async request() { return approvalOutcome || 'allowed-once' },
      }
    },
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-labscript-tools-'))
  fs.writeFileSync(path.join(dir, 'demo_scan.yaml'), [
    'name: demo_scan',
    'labscript: /tmp/sequence.py',
    'scan:',
    '  duration_test_us: np.linspace(0, 1, 2)',
    'postprocess: demo.peaks',
  ].join(String.fromCharCode(10)))
  const config = Object.assign({
    python: process.execPath,
    bridgeScript: fixture,
    dshHome: dir,
    backend: 'mock-labscript',
    protocolsDir: dir,
    engage: { timeoutS: 5 },
  }, configOverrides || {})
  apply(ctx, config)
  return { tools, skills, ctx, dir }
}

const exec = { agent: {}, callId: 'call-1', signal: undefined }

test('registers nine tools and one skill', () => {
  const plugin = makePlugin()
  assert.deepEqual([...plugin.tools.keys()].sort(), ['labs_abort', 'labs_data', 'labs_doctor', 'labs_engage', 'labs_protocol', 'labs_restore', 'labs_set', 'labs_status', 'labs_wait'])
  assert.equal(plugin.skills.length, 1)
  assert.equal(plugin.skills[0].name, 'labscript')
  assert.equal(plugin.tools.get('labs_set').isConcurrencySafe(), false)
  assert.equal(plugin.tools.get('labs_status').isConcurrencySafe(), true)
})

test('labs_status and labs_set execute through the bridge', async () => {
  const plugin = makePlugin()
  const status = await plugin.tools.get('labs_status').execute({}, exec)
  assert.equal(status.n_shots, 2)
  const written = await plugin.tools.get('labs_set').execute({ values: { amplitude: 0.7 }, note: 'test' }, exec)
  assert.equal(written.n_shots, 2)
  assert.equal(written.snapshot.name, 'snap-1.json')
})

test('labs_engage submits and sets pending, labs_wait clears it', async () => {
  const plugin = makePlugin()
  const engaged = await plugin.tools.get('labs_engage').execute({ note: 'test run' }, exec)
  assert.equal(engaged.n_shots_at_submit, 2)
  const status = await plugin.tools.get('labs_status').execute({}, exec)
  assert.ok(status.last_run)
  assert.equal(status.last_run.run_folder, '/tmp/runs/run_000001')
  const waited = await plugin.tools.get('labs_wait').execute({}, exec)
  assert.equal(waited.complete, true)
  assert.equal(waited.n_h5, 2)
  const after = await plugin.tools.get('labs_status').execute({}, exec)
  assert.equal(after.last_run, undefined)
})

test('labs_protocol list/show/run and wait finalizes', async () => {
  const plugin = makePlugin()
  const list = await plugin.tools.get('labs_protocol').execute({ action: 'list' }, exec)
  assert.equal(list.protocols[0].name, 'demo_scan')
  const show = await plugin.tools.get('labs_protocol').execute({ action: 'show', protocol: 'demo_scan' }, exec)
  assert.equal(show.plan.expected_n_shots, 2)
  assert.ok(show.name_check && show.name_check.missing.includes('old_duration_key'))
  const run = await plugin.tools.get('labs_protocol').execute({ action: 'run', protocol: 'demo_scan', note: 'cal' }, exec)
  assert.equal(run.n_shots_at_submit, 2)
  const waited = await plugin.tools.get('labs_wait').execute({}, exec)
  assert.equal(waited.complete, true)
  assert.deepEqual(waited.finalize.postprocess, { fit: { center: 2.5 } })
  assert.equal(waited.finalize.restore.restored, true)
})

test('labs_abort clears pending state', async () => {
  const plugin = makePlugin()
  await plugin.tools.get('labs_engage').execute({}, exec)
  await plugin.tools.get('labs_abort').execute({}, exec)
  const status = await plugin.tools.get('labs_status').execute({}, exec)
  assert.equal(status.last_run, undefined)
})

test('real backend asks approval and fails closed when rejected', async () => {
  const plugin = makePlugin({ backend: 'labscript' }, 'rejected')
  await assert.rejects(plugin.tools.get('labs_engage').execute({}, exec), (error) => {
    assert.ok(error instanceof LabError)
    assert.equal(error.code, 'APPROVAL_DENIED')
    return true
  })
})

test('real backend proceeds when approval is allowed once', async () => {
  const plugin = makePlugin({ backend: 'labscript' }, 'allowed-once')
  const engaged = await plugin.tools.get('labs_engage').execute({}, exec)
  assert.equal(engaged.accepted, true)
})

test('real backend refuses when no approval service is composed', async () => {
  const tools = new Map()
  const ctx = {
    tools: { register(tool) { tools.set(tool.name, tool); return () => {} } },
    skills: { register() {} },
    logger: { info() {}, debug() {}, warn() {} },
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-labscript-approval-'))
  apply(ctx, { python: process.execPath, bridgeScript: fixture, backend: 'labscript', dshHome: dir })
  await assert.rejects(tools.get('labs_engage').execute({}, exec), /no approval service/)
})

test('protocol key warning asks for approval before running on real hardware', async () => {
  const plugin = makePlugin({ backend: 'labscript' }, 'rejected')
  await assert.rejects(plugin.tools.get('labs_protocol').execute({ action: 'run', protocol: 'demo_scan' }, exec), (error) => {
    assert.ok(error instanceof LabError)
    assert.equal(error.code, 'APPROVAL_DENIED')
    return true
  })
})
