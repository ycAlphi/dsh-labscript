/**
 * dsh-labscript local state: UI config + runtime pending state.
 *
 * The browser configuration page stores a small JSON file under DSH_HOME. It is
 * merged over the profile config (UI values win for the keys it manages); the
 * other profile keys (scripts, hard limits, postprocess, approval, ...) stay in
 * the profile's cordis.patch.yml. No credentials are stored here.
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export function dshHome() {
  const fromEnv = process.env.DSH_HOME
  if (typeof fromEnv === 'string' && fromEnv.trim() !== '') return fromEnv
  return path.join(os.homedir(), '.dsh')
}

export function uiConfigPath(home) {
  return path.join(home || dshHome(), 'labscript.config.json')
}

export function statePath(home) {
  return path.join(home || dshHome(), 'labscript-state.json')
}

const UI_KEYS = ['backend', 'python', 'runmanager', 'protocolsDir']
const BACKENDS = ['labscript', 'mock-labscript', 'http-sim']

export function normalizeUiConfig(input) {
  const source = input && typeof input === 'object' ? input : {}
  const backend = String(source.backend || 'mock-labscript').toLowerCase()
  if (!BACKENDS.includes(backend)) {
    throw new Error('backend must be one of ' + BACKENDS.join(', ') + ', got: ' + backend)
  }
  const runmanager = source.runmanager && typeof source.runmanager === 'object' ? source.runmanager : {}
  // host/port stay optional: when they are not stored, the bridge lets runmanager.remote
  // resolve them from labscript's labconfig ([servers]/[ports] runmanager), the same way
  // the runmanager GUI does. Storing the old hardcoded defaults here would pin them.
  const host = runmanager.host === undefined || runmanager.host === null ? '' : String(runmanager.host).trim()
  const portText = runmanager.port === undefined || runmanager.port === null ? '' : String(runmanager.port).trim()
  const port = portText === '' ? undefined : Number(portText)
  if (port !== undefined && (!Number.isInteger(port) || port < 1 || port > 65535)) throw new Error('runmanager.port must be 1-65535')
  const timeoutS = runmanager.timeoutS === undefined || runmanager.timeoutS === null || runmanager.timeoutS === ''
    ? undefined
    : Number(runmanager.timeoutS)
  if (timeoutS !== undefined && (!Number.isFinite(timeoutS) || timeoutS <= 0 || timeoutS > 600)) {
    throw new Error('runmanager.timeoutS must be 1-600')
  }
  const out = {
    backend,
    python: String(source.python || '').trim(),
    runmanager: {},
    protocolsDir: String(source.protocolsDir || '').trim(),
  }
  if (host !== '') out.runmanager.host = host
  if (port !== undefined) out.runmanager.port = port
  if (timeoutS !== undefined) out.runmanager.timeoutS = timeoutS
  return out
}

/** Read the UI config. Returns null when the file does not exist. */
export function readUiConfig(home) {
  const file = uiConfigPath(home)
  let raw
  try {
    raw = fs.readFileSync(file, 'utf8')
  } catch (error) {
    if (error && error.code === 'ENOENT') return { config: null, warning: undefined, file }
    return { config: null, warning: 'cannot read ' + file + ': ' + error.message, file }
  }
  try {
    return { config: normalizeUiConfig(JSON.parse(raw)), warning: undefined, file }
  } catch (error) {
    return { config: null, warning: 'ignoring corrupt ' + file + ': ' + error.message, file }
  }
}

export function writeUiConfig(input, home) {
  const previous = readUiConfig(home).config || {}
  const patch = input && typeof input === 'object' ? input : {}
  const runmanager = Object.assign({}, previous.runmanager || {}, patch.runmanager || {})
  // An empty host/port means "unset, ask labconfig": drop the stored key instead of
  // keeping the previous value, so a lab that moves runmanager can clear it.
  for (const key of Object.keys(runmanager)) {
    if (runmanager[key] === '' || runmanager[key] === null || runmanager[key] === undefined) delete runmanager[key]
  }
  const config = normalizeUiConfig(Object.assign({}, previous, patch, { runmanager }))
  const file = uiConfigPath(home)
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const temp = file + '.' + process.pid + '.' + Date.now() + '.tmp'
  try {
    fs.writeFileSync(temp, JSON.stringify(config, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 })
    fs.renameSync(temp, file)
  } finally {
    try { if (fs.existsSync(temp)) fs.unlinkSync(temp) } catch { /* best effort */ }
  }
  return config
}

/** Merge the UI-managed keys over the profile config (UI wins where present). */
export function mergeConfig(profile, ui) {
  const base = profile && typeof profile === 'object' ? profile : {}
  if (!ui || typeof ui !== 'object') return base
  const out = Object.assign({}, base)
  for (const key of UI_KEYS) {
    if (ui[key] === undefined) continue
    if (key === 'runmanager') out.runmanager = Object.assign({}, base.runmanager || {}, ui.runmanager || {})
    else out[key] = ui[key]
  }
  return out
}

export function readState(home) {
  const file = statePath(home)
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'))
    return {
      pending: parsed && typeof parsed.pending === 'object' ? parsed.pending : null,
      lastRun: parsed && typeof parsed.lastRun === 'object' ? parsed.lastRun : null,
    }
  } catch {
    return { pending: null, lastRun: null }
  }
}

export function writeState(state, version, home) {
  const file = statePath(home)
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const temp = file + '.' + process.pid + '.' + Date.now() + '.tmp'
  const payload = {
    version: version || null,
    updated_at: new Date().toISOString(),
    pending: state.pending || null,
    lastRun: state.lastRun || null,
  }
  try {
    fs.writeFileSync(temp, JSON.stringify(payload, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 })
    fs.renameSync(temp, file)
  } finally {
    try { if (fs.existsSync(temp)) fs.unlinkSync(temp) } catch { /* best effort */ }
  }
  return payload
}
