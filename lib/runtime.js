/**
 * dsh-labscript runtime: effective configuration, python bridge transport and
 * runtime state.
 *
 * Every tool call spawns the bundled python bridge once, writes one JSON
 * request on stdin and reads one JSON envelope from stdout. The bridge is a
 * short-lived process: timeouts and cancellation kill it.
 *
 * Configuration comes from two places and is merged on every read: the profile
 * config (advanced settings) and the small JSON file written by the browser
 * configuration page (backend, python, runmanager, protocolsDir). Pending-run
 * state is persisted under DSH_HOME so a restart does not lose track of a run.
 */
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  mergeConfig,
  readState,
  readUiConfig,
  statePath,
  uiConfigPath,
  writeState,
} from './store.mjs'

const PACKAGE = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
const VERSION = PACKAGE.version
const DEFAULT_ENGAGE_TIMEOUT_S = 900
const DEFAULT_APPROVAL = { engage: true, protocolRun: true, protocolNames: true }
const DEFAULT_BACKEND = 'mock-labscript'
const BACKENDS = ['labscript', 'mock-labscript', 'http-sim']

export class LabError extends Error {
  constructor(code, message, detail) {
    super(message)
    this.name = 'LabError'
    this.code = code
    this.detail = detail || null
  }
}

function normalizeConfig(raw) {
  const input = raw && typeof raw === 'object' ? raw : {}
  const backend = String(input.backend || DEFAULT_BACKEND).toLowerCase()
  if (!BACKENDS.includes(backend)) {
    throw new LabError('BAD_CONFIG', 'backend must be "labscript", "mock-labscript" or "http-sim", got: ' + backend)
  }
  const approval = Object.assign({}, DEFAULT_APPROVAL, input.approval || {})
  return {
    python: input.python || '',
    bridgeScript: input.bridgeScript || '',
    backend,
    runmanager: input.runmanager || {},
    protocolsDir: input.protocolsDir || '',
    scriptsFile: input.scriptsFile || '',
    hardLimitsFile: input.hardLimitsFile || '',
    calibrationLog: input.calibrationLog || '',
    pathMap: input.pathMap || [],
    reps: input.reps || {},
    postprocess: input.postprocess || {},
    engage: input.engage || {},
    panelHistoryDir: input.panelHistoryDir || '',
    approval: {
      engage: approval.engage !== false,
      protocolRun: approval.protocolRun !== false,
      protocolNames: approval.protocolNames !== false,
    },
    mock: input.mock || {},
    simulator: input.simulator || {},
    dshHome: input.dshHome || '',
    requestTimeoutMs: Number(input.requestTimeoutMs || 60000),
  }
}

function bridgePayload(config) {
  return {
    backend: config.backend,
    runmanager: config.runmanager,
    scriptsFile: config.scriptsFile,
    hardLimitsFile: config.hardLimitsFile,
    calibrationLog: config.calibrationLog,
    pathMap: config.pathMap,
    reps: config.reps,
    postprocess: config.postprocess,
    engage: Object.assign({ timeoutS: DEFAULT_ENGAGE_TIMEOUT_S }, config.engage || {}),
    panelHistoryDir: config.panelHistoryDir,
    mock: config.mock,
    simulator: config.simulator,
  }
}

export function createRuntime(ctx, rawConfig) {
  const profileConfig = rawConfig && typeof rawConfig === 'object' ? rawConfig : {}
  const home = typeof profileConfig.dshHome === 'string' && profileConfig.dshHome !== '' ? profileConfig.dshHome : undefined
  let uiCache = { key: 'unread', config: null, warning: undefined, file: uiConfigPath(home) }

  function uiState() {
    const file = uiConfigPath(home)
    let stat
    try {
      stat = fs.statSync(file)
    } catch {
      uiCache = { key: 'missing', config: null, warning: undefined, file }
      return uiCache
    }
    const key = String(stat.mtimeMs) + ':' + stat.size
    if (key !== uiCache.key) {
      const read = readUiConfig(home)
      uiCache = { key, config: read.config, warning: read.warning, file }
    }
    return uiCache
  }

  function currentConfig() {
    return normalizeConfig(mergeConfig(profileConfig, uiState().config))
  }

  // Fail fast on a broken profile config instead of failing on the first tool call.
  currentConfig()

  const loadedState = readState(home)
  const state = {
    pending: loadedState.pending,
    lastRun: loadedState.lastRun,
    queue: Promise.resolve(),
    active: {},
    waitCancelledByAbort: false,
  }

  function saveState() {
    try {
      writeState(state, VERSION, home)
    } catch {
      /* state persistence is best effort */
    }
  }

  function setPending(pending) {
    state.pending = pending || null
    saveState()
  }

  function setLastRun(lastRun) {
    state.lastRun = lastRun || null
    saveState()
  }

  function clearPending() {
    state.pending = null
    saveState()
  }

  function runBridge(command, args, options) {
    const opts = options || {}
    let config
    try {
      config = currentConfig()
    } catch (error) {
      return Promise.reject(error)
    }
    if (!config.python) {
      return Promise.reject(new LabError('BAD_CONFIG', 'config.python is required (path to a python interpreter with numpy and h5py; use the labscript-suite environment for real hardware)'))
    }
    return new Promise((resolve, reject) => {
      let settled = false
      let child
      const finish = (error, value) => {
        if (settled) return
        settled = true
        if (error) reject(error)
        else resolve(value)
      }
      try {
        child = spawn(config.python, [bridgeScriptOf(config)], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
        state.active[command] = child
      } catch (error) {
        finish(new LabError('SPAWN_FAILED', 'cannot spawn python bridge: ' + error.message))
        return
      }
      const stdout = []
      const stderr = []
      const timeoutMs = Number(opts.timeoutMs || config.requestTimeoutMs)
      const timer = setTimeout(() => {
        try { child.kill() } catch { /* ignore */ }
        finish(new LabError('TIMEOUT', 'bridge call timed out after ' + timeoutMs + ' ms: ' + command, {
          hint: command === 'wait' ? 'the run may still be executing; use labs_wait with run_folder or labs_abort' : undefined,
        }))
      }, timeoutMs)
      const onAbort = () => {
        try { child.kill() } catch { /* ignore */ }
        finish(new LabError('ABORTED', 'bridge call cancelled: ' + command, {
          hint: command === 'wait' ? 'the run may still be executing; use labs_wait with run_folder or labs_abort' : undefined,
        }))
      }
      if (opts.signal) {
        if (opts.signal.aborted) onAbort()
        else opts.signal.addEventListener('abort', onAbort, { once: true })
      }
      child.stdout.on('data', (chunk) => stdout.push(chunk))
      child.stderr.on('data', (chunk) => stderr.push(chunk))
      child.on('error', (error) => {
        if (state.active[command] === child) delete state.active[command]
        clearTimeout(timer)
        if (opts.signal) opts.signal.removeEventListener('abort', onAbort)
        finish(new LabError('SPAWN_FAILED', 'python bridge failed: ' + error.message))
      })
      child.on('close', (code) => {
        if (state.active[command] === child) delete state.active[command]
        clearTimeout(timer)
        if (opts.signal) opts.signal.removeEventListener('abort', onAbort)
        const cancelledByAbort = command === 'wait' && state.waitCancelledByAbort
        const outText = Buffer.concat(stdout).toString('utf8').trim()
        const errText = Buffer.concat(stderr).toString('utf8').trim()
        if (!outText) {
          if (cancelledByAbort) {
            finish(new LabError('ABORTED', 'wait cancelled by labs_abort'))
            return
          }
          finish(new LabError('BRIDGE_EMPTY_OUTPUT', 'bridge produced no JSON (exit ' + code + '): ' + tail(errText)))
          return
        }
        let envelope
        try {
          envelope = JSON.parse(outText)
        } catch {
          finish(new LabError('BRIDGE_BAD_JSON', 'bridge output is not JSON: ' + tail(outText)))
          return
        }
        if (!envelope || envelope.ok !== true) {
          const info = envelope && envelope.error ? envelope.error : {}
          finish(new LabError(info.code || 'BRIDGE_ERROR', info.message || 'bridge error', info.detail || null))
          return
        }
        finish(null, envelope.data)
      })
      try {
        child.stdin.write(JSON.stringify({ cmd: command, args: args || {}, config: bridgePayload(config) }))
        child.stdin.end()
      } catch (error) {
        try { child.kill() } catch { /* ignore */ }
        finish(new LabError('SPAWN_FAILED', 'cannot write to python bridge: ' + error.message))
      }
    })
  }

  function bridgeScriptOf(config) {
    return config.bridgeScript || fileURLToPath(new URL('../python/bridge.py', import.meta.url))
  }

  function cancelWait() {
    const child = state.active.wait
    if (!child) return false
    state.waitCancelledByAbort = true
    try {
      child.kill()
    } catch {
      /* ignore */
    }
    return true
  }

  function serial(task) {
    const run = state.queue.then(task, task)
    state.queue = run.then(() => undefined, () => undefined)
    return run
  }

  async function doctor() {
    const checks = []
    const add = (name, status, detail, hint) => checks.push({ name, status, detail: detail || '', hint: hint || '' })
    let config
    try {
      config = currentConfig()
      add('config', 'ok', 'profile config loaded')
    } catch (error) {
      add('config', 'fail', error.message, 'fix the profile cordis.patch.yml config')
      return { version: VERSION, backend: 'unknown', checks, configFile: uiState().file, stateFile: statePath(home) }
    }
    add('version', 'ok', 'dsh-labscript ' + VERSION)
    add('backend', config.backend === 'labscript' ? 'warn' : 'ok', config.backend, config.backend === 'labscript' ? 'REAL HARDWARE mode: engage writes to the live panel' : 'simulator mode')
    const pythonSet = config.python !== ''
    const pythonIsFile = pythonSet && fs.existsSync(config.python)
    const pythonHasSeparator = pythonSet && (config.python.includes('/') || config.python.includes('\\'))
    const pythonStatus = !pythonSet ? 'fail' : pythonIsFile ? 'ok' : pythonHasSeparator ? 'fail' : 'warn'
    add('python-path', pythonStatus, config.python || '(not configured)',
      !pythonSet ? 'set the labscript-suite python interpreter (or any python with numpy+h5py for mock)'
        : pythonStatus === 'fail' ? 'path does not exist'
        : pythonStatus === 'warn' ? 'not an absolute file path; will be resolved through PATH'
        : '')
    let approval = null
    try { approval = typeof ctx.get === 'function' ? ctx.get('approval') : undefined } catch { approval = undefined }
    add('approval', approval ? 'ok' : 'warn', approval ? 'approval service available' : 'no approval service', approval ? '' : 'real-hardware engage/protocol run will be refused')
    try {
      fs.mkdirSync(path.dirname(statePath(home)), { recursive: true })
      fs.accessSync(path.dirname(statePath(home)), fs.constants.W_OK)
      add('state-dir', 'ok', path.dirname(statePath(home)))
    } catch (error) {
      add('state-dir', 'fail', path.dirname(statePath(home)), 'cannot write runtime state: ' + error.message)
    }
    if (config.protocolsDir) {
      add('protocols-dir', fs.existsSync(config.protocolsDir) ? 'ok' : 'warn', config.protocolsDir, fs.existsSync(config.protocolsDir) ? '' : 'directory does not exist (lists will be empty)')
    }
    if (config.python) {
      try {
        const data = await runBridge('doctor', {}, { timeoutMs: 60000 })
        for (const check of data.checks || []) checks.push(check)
      } catch (error) {
        add('python-bridge', 'fail', error.message, error.detail && error.detail.hint ? error.detail.hint : 'run the bridge manually to see the traceback')
      }
    }
    return {
      version: VERSION,
      backend: config.backend,
      checks,
      configFile: uiState().file,
      stateFile: statePath(home),
      python: config.python,
    }
  }

  return {
    ctx,
    version: VERSION,
    uiHome: home,
    statePath: () => statePath(home),
    uiConfigPath: () => uiConfigPath(home),
    get config() {
      return currentConfig()
    },
    state,
    setPending,
    setLastRun,
    clearPending,
    saveState,
    cancelWait,
    runBridge,
    serial,
    doctor,
    isMock() {
      return currentConfig().backend === 'mock-labscript'
    },
  }
}

function tail(text) {
  if (!text) return ''
  const lines = text.split(String.fromCharCode(10))
  return lines.slice(-8).join('; ')
}
