import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))

function loadClientBundle() {
  const code = fs.readFileSync(path.join(here, '..', 'lib', 'client.js'), 'utf8')
  const loaded = []
  const fakeWindow = { __ModuleLoader__: { load(spec) { loaded.push(spec) } } }
  const run = new Function('window', 'document', code)
  run(fakeWindow, { getElementById() { return null }, createElement() { return {} }, head: { appendChild() {} } })
  return loaded
}

function fakeReact() {
  return {
    createElement: (type, props, ...children) => ({ type, props, children }),
    useState: (initial) => [initial, () => {}],
    useEffect: () => {},
  }
}

test('client bundle wraps itself for the DSH module loader', () => {
  const loaded = loadClientBundle()
  assert.equal(loaded.length, 1)
  assert.equal(loaded[0].id, 'dsh-labscript')
  assert.equal(typeof loaded[0].factory, 'function')
})

test('client bundle registers the Plugins-page configuration page', () => {
  const loaded = loadClientBundle()
  const exports = loaded[0].factory((request) => {
    if (request === 'react') return fakeReact()
    throw new Error('unexpected client module request: ' + request)
  })
  assert.equal(typeof exports.apply, 'function')
  assert.deepEqual(exports.inject, ['slots'])
  assert.equal(typeof exports.ConfigPage, 'function')

  let injectedSlot = null
  const registrations = []
  const ctx = {
    effect(factory) { const disposer = factory(); return typeof disposer === 'function' ? disposer : () => {} },
    slots: {
      inject(name, register) { injectedSlot = name; const disposer = register(); return typeof disposer === 'function' ? disposer : () => {} },
      register(options, component) { registrations.push({ options, component }); return () => {} },
    },
  }
  exports.apply(ctx)
  assert.equal(injectedSlot, 'plugins.bundle.config')
  assert.equal(registrations.length, 1)
  assert.equal(registrations[0].options.key, 'dsh-labscript')
  assert.equal(registrations[0].component, exports.ConfigPage)
})
