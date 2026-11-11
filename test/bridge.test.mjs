import assert from 'node:assert/strict'
import test from 'node:test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import fs from 'node:fs'
import os from 'node:os'
import { createRuntime, LabError } from '../lib/runtime.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const fixture = path.join(here, 'fixtures', 'fake-bridge.mjs')

function makeRuntime(extra) {
  const dshHome = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-labscript-runtime-'))
  return createRuntime({}, Object.assign({
    python: process.execPath,
    bridgeScript: fixture,
    backend: 'mock-labscript',
    dshHome,
    requestTimeoutMs: 5000,
  }, extra || {}))
}

test('bridge returns data for a normal call', async () => {
  const runtime = makeRuntime()
  const data = await runtime.runBridge('status', {})
  assert.equal(data.backend, 'mock-labscript')
  assert.equal(data.n_shots, 2)
})

test('bridge maps error envelopes to LabError', async () => {
  const runtime = makeRuntime()
  await assert.rejects(runtime.runBridge('status', { mode: 'error' }), (error) => {
    assert.ok(error instanceof LabError)
    assert.equal(error.code, 'TEST_ERROR')
    assert.deepEqual(error.detail, { k: 1 })
    return true
  })
})

test('bridge rejects non JSON output', async () => {
  const runtime = makeRuntime()
  await assert.rejects(runtime.runBridge('status', { mode: 'badjson' }), /BRIDGE_BAD_JSON|not JSON/)
})

test('bridge times out and kills the child', async () => {
  const runtime = makeRuntime({ requestTimeoutMs: 150 })
  await assert.rejects(runtime.runBridge('status', { mode: 'hang' }), (error) => {
    assert.equal(error.code, 'TIMEOUT')
    return true
  })
})

test('bridge honours the abort signal', async () => {
  const runtime = makeRuntime()
  const controller = new AbortController()
  const pending = runtime.runBridge('status', { mode: 'hang' }, { signal: controller.signal })
  setTimeout(() => controller.abort(), 100)
  await assert.rejects(pending, (error) => {
    assert.equal(error.code, 'ABORTED')
    return true
  })
})

test('bridge rejects without config.python', async () => {
  const runtime = createRuntime({}, { backend: 'mock-labscript' })
  await assert.rejects(runtime.runBridge('status', {}), /config.python is required/)
})

test('bridge validates backend names', async () => {
  assert.throws(() => createRuntime({}, { backend: 'real', python: 'x' }), /backend must be/)
})
