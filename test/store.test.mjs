import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import {
  mergeConfig,
  normalizeUiConfig,
  readState,
  readUiConfig,
  statePath,
  uiConfigPath,
  writeState,
  writeUiConfig,
} from '../lib/store.mjs'

function tempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-labscript-store-'))
}

test('ui config normalizes defaults and rejects bad values', () => {
  const config = normalizeUiConfig({})
  assert.equal(config.backend, 'mock-labscript')
  assert.equal(config.runmanager.port, 42523)
  assert.throws(() => normalizeUiConfig({ backend: 'nonsense' }), /backend must be/)
  assert.throws(() => normalizeUiConfig({ runmanager: { port: 70000 } }), /port/)
})

test('ui config round-trips through the DSH home directory', () => {
  const home = tempHome()
  assert.equal(readUiConfig(home).config, null)
  writeUiConfig({ backend: 'labscript', python: 'C:/lab/python.exe', protocolsDir: 'C:/protocols' }, home)
  const read = readUiConfig(home)
  assert.equal(read.config.backend, 'labscript')
  assert.equal(read.config.python, 'C:/lab/python.exe')
  assert.ok(fs.existsSync(uiConfigPath(home)))
})

test('mergeConfig lets UI keys win but keeps profile keys', () => {
  const merged = mergeConfig(
    { backend: 'labscript', python: 'profile-python', scriptsFile: 'C:/scripts.json', runmanager: { host: '10.0.0.1', timeoutS: 10 } },
    { backend: 'mock-labscript', runmanager: { host: '127.0.0.1' } },
  )
  assert.equal(merged.backend, 'mock-labscript')
  assert.equal(merged.python, 'profile-python')
  assert.equal(merged.scriptsFile, 'C:/scripts.json')
  assert.equal(merged.runmanager.host, '127.0.0.1')
  assert.equal(merged.runmanager.timeoutS, 10)
})

test('UI save does not silently reset profile runmanager.timeoutS', () => {
  const home = tempHome()
  writeUiConfig({ backend: 'labscript', python: 'C:/lab/python.exe', runmanager: { host: '127.0.0.1', port: 42523 } }, home)
  const merged = mergeConfig({ runmanager: { host: '10.0.0.1', port: 1, timeoutS: 30 } }, readUiConfig(home).config)
  assert.equal(merged.runmanager.host, '127.0.0.1')
  assert.equal(merged.runmanager.port, 42523)
  assert.equal(merged.runmanager.timeoutS, 30)
})

test('partial UI saves preserve previously stored fields', () => {
  const home = tempHome()
  writeUiConfig({ backend: 'labscript', python: 'C:/lab/python.exe', protocolsDir: 'C:/protocols' }, home)
  writeUiConfig({ backend: 'mock-labscript' }, home)
  const stored = readUiConfig(home).config
  assert.equal(stored.backend, 'mock-labscript')
  assert.equal(stored.python, 'C:/lab/python.exe')
  assert.equal(stored.protocolsDir, 'C:/protocols')
})

test('runtime state round-trips and survives a missing file', () => {
  const home = tempHome()
  assert.deepEqual(readState(home), { pending: null, lastRun: null })
  writeState({ pending: { run_folder: '/tmp/run' }, lastRun: null }, '0.2.0', home)
  const loaded = readState(home)
  assert.equal(loaded.pending.run_folder, '/tmp/run')
  assert.ok(fs.existsSync(statePath(home)))
})
