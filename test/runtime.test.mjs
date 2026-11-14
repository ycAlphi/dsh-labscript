import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from '../lib/index.js'
import { createRuntime, LabError } from '../lib/runtime.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const fixture = path.join(here, 'fixtures', 'fake-bridge.mjs')

function makePlugin(overrides) {
  const tools = new Map()
  const skills = []
  const ctx = {
    tools: { register(tool) { tools.set(tool.name, tool); return () => {} } },
    skills: { register(skill) { skills.push(skill); return () => {} } },
    logger: { info() {}, debug() {}, warn() {} },
  }
  const dshHome = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-labscript-runtime-test-'))
  const runtime = apply(ctx, Object.assign({
    python: process.execPath,
    bridgeScript: fixture,
    dshHome,
    engage: { timeoutS: 5 },
  }, overrides || {}))
  return { tools, skills, ctx, runtime, dshHome }
}

test('backend defaults to the simulator, not real hardware', () => {
  const plugin = makePlugin()
  assert.equal(plugin.runtime.config.backend, 'mock-labscript')
})

test('pending run state is persisted and reloaded after a restart', async () => {
  const plugin = makePlugin()
  await plugin.tools.get('labs_engage').execute({ note: 'persist test' }, { agent: {}, callId: 'c1' })
  // Pending state keeps the mapped folder (the raw one is in remote_run_folder) so that a
  // mapped path quoted back by the agent still refers to the same run.
  assert.equal(plugin.runtime.state.pending.run_folder, '/mnt/lab/runs/run_000001')
  assert.equal(plugin.runtime.state.pending.remote_run_folder, '/tmp/runs/run_000001')
  const reloaded = makePlugin({ dshHome: plugin.dshHome })
  assert.equal(reloaded.runtime.state.pending.run_folder, '/mnt/lab/runs/run_000001')
  await reloaded.tools.get('labs_wait').execute({}, { agent: {}, callId: 'c2' })
  assert.equal(reloaded.runtime.state.pending, null)
  const after = makePlugin({ dshHome: plugin.dshHome })
  assert.equal(after.runtime.state.pending, null)
})

test('labs_abort is not blocked behind a running labs_wait', async () => {
  const plugin = makePlugin()
  const waitTool = plugin.tools.get('labs_wait')
  const abortTool = plugin.tools.get('labs_abort')
  plugin.runtime.runBridge = async (cmd) => {
    if (cmd === 'wait') {
      await new Promise((resolve) => setTimeout(resolve, 400))
      return { run_folder: '/tmp/runs/run_000001', expected_n_shots: 2, n_h5: 2, complete: true, timed_out: false, elapsed: 0.4 }
    }
    if (cmd === 'abort') return { aborted: true, note: 'test' }
    return {}
  }
  const started = Date.now()
  const waitPromise = waitTool.execute({ run_folder: '/tmp/runs/run_000001' }, { agent: {}, callId: 'w' })
  const abortResult = await abortTool.execute({}, { agent: {}, callId: 'a' })
  const abortMs = Date.now() - started
  assert.equal(abortResult.aborted, true)
  assert.ok(abortMs < 300, 'abort took ' + abortMs + ' ms, it should bypass the serial queue')
  await waitPromise
})

test('labs_doctor reports bridge and local checks', async () => {
  const plugin = makePlugin()
  const result = await plugin.tools.get('labs_doctor').execute({}, { agent: {}, callId: 'd' })
  assert.equal(result.version, plugin.runtime.version)
  const names = result.checks.map((check) => check.name)
  assert.ok(names.includes('config'))
  assert.ok(names.includes('python-path'))
  assert.ok(names.includes('numpy'))
})

test('cancelWait kills an active wait bridge', async () => {
  const plugin = makePlugin()
  const pending = plugin.runtime.runBridge('wait', { mode: 'hang' }, {})
  await new Promise((resolve) => setTimeout(resolve, 150))
  assert.equal(plugin.runtime.cancelWait(), true)
  await assert.rejects(pending, (error) => error.code === 'ABORTED')
})

test('labs_abort stops an active labs_wait', async () => {
  const plugin = makePlugin()
  let killed = false
  plugin.runtime.state.active.wait = { kill() { killed = true } }
  plugin.runtime.runBridge = async (command) => {
    if (command === 'abort') return { aborted: true, note: 'test' }
    return {}
  }
  const exec = { agent: {}, callId: 'abort-test' }
  const result = await plugin.tools.get('labs_abort').execute({}, exec)
  assert.equal(result.wait_cancelled, true)
  assert.equal(killed, true)
})

test('labs_wait returns an aborted result when abort cancelled it', async () => {
  const plugin = makePlugin()
  plugin.runtime.runBridge = async () => { throw new LabError('ABORTED', 'wait cancelled by labs_abort') }
  plugin.runtime.state.waitCancelledByAbort = true
  const exec = { agent: {}, callId: 'wait-aborted' }
  const result = await plugin.tools.get('labs_wait').execute({ run_folder: '/tmp/runs/run_000001' }, exec)
  assert.equal(result.aborted, true)
  assert.equal(plugin.runtime.state.waitCancelledByAbort, false)
})
