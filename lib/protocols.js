/**
 * Protocol files: discovery, YAML parsing, name resolution.
 *
 * A protocol is a YAML mapping. The schema is deliberately small: name,
 * labscript (or joint_script), set, scan, reps, engage, postprocess,
 * finish.restore/panel/history. Unknown keys are reported as warnings; the
 * legacy key "analysis" is accepted as an alias for "postprocess".
 */
import fs from 'node:fs'
import path from 'node:path'
import YAML from 'yaml'

const KNOWN_KEYS = new Set(['name', 'labscript', 'joint_script', 'set', 'scan', 'reps', 'engage', 'postprocess', 'finish', 'analysis'])
const MAX_PROTOCOL_BYTES = 256 * 1024

function protocolFiles(dir) {
  if (!dir) return []
  let entries = []
  try {
    entries = fs.readdirSync(dir)
  } catch (error) {
    return []
  }
  return entries
    .filter((name) => name.endsWith('.yaml') || name.endsWith('.yml'))
    .sort()
    .map((name) => path.join(dir, name))
}

export function listProtocols(dir) {
  return protocolFiles(dir).map((file) => {
    try {
      const loaded = loadProtocolFile(file)
      return { name: loaded.name, file: path.basename(file), warnings: loaded.warnings }
    } catch (error) {
      return { name: path.basename(file), file: path.basename(file), error: error.message }
    }
  })
}

export function resolveProtocolPath(dir, ref) {
  const value = String(ref || '').trim()
  if (!value) throw new Error('protocol name is required')
  if (path.isAbsolute(value)) {
    if (fs.existsSync(value)) return value
    throw new Error('protocol file not found: ' + value)
  }
  const candidates = [path.join(dir || '', value)]
  if (!value.endsWith('.yaml') && !value.endsWith('.yml')) {
    candidates.push(path.join(dir || '', value + '.yaml'))
    candidates.push(path.join(dir || '', value + '.yml'))
  }
  for (const candidate of candidates) {
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate
  }
  throw new Error('protocol not found in protocolsDir: ' + value)
}

export function loadProtocol(dir, ref) {
  return loadProtocolFile(resolveProtocolPath(dir, ref))
}

export function loadProtocolFile(file) {
  const stat = fs.statSync(file)
  if (stat.size > MAX_PROTOCOL_BYTES) throw new Error('protocol file is too large: ' + file)
  const text = fs.readFileSync(file, 'utf8')
  let data
  try {
    data = YAML.parse(text)
  } catch (error) {
    throw new Error('cannot parse protocol YAML ' + path.basename(file) + ': ' + error.message)
  }
  return normalizeProtocol(data, file)
}

export function normalizeProtocol(data, file) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('protocol must be a YAML mapping: ' + (file || 'inline'))
  }
  const warnings = []
  for (const key of Object.keys(data)) {
    if (!KNOWN_KEYS.has(key)) warnings.push('unknown protocol key ignored: ' + key)
  }
  const plan = {
    name: data.name ? String(data.name) : path.basename(file || 'protocol').replace(/\.ya?ml$/, ''),
    labscript: data.labscript ? String(data.labscript) : undefined,
    joint_script: data.joint_script ? String(data.joint_script) : undefined,
    set: data.set && typeof data.set === 'object' ? Object.assign({}, data.set) : {},
    scan: data.scan && typeof data.scan === 'object' ? Object.assign({}, data.scan) : {},
    reps: data.reps === undefined ? 1 : data.reps,
    engage: data.engage === undefined ? true : Boolean(data.engage),
    postprocess: null,
    finish: { restore: true, panel: {}, history: {} },
  }
  if (data.analysis !== undefined && data.postprocess === undefined) {
    plan.postprocess = data.analysis ? String(data.analysis) : null
    warnings.push('legacy key "analysis" used as "postprocess"')
  } else if (data.postprocess !== undefined) {
    plan.postprocess = data.postprocess ? String(data.postprocess) : null
  }
  if (data.finish && typeof data.finish === 'object') {
    plan.finish.restore = data.finish.restore === undefined ? true : Boolean(data.finish.restore)
    if (data.finish.panel && typeof data.finish.panel === 'object') plan.finish.panel = Object.assign({}, data.finish.panel)
    if (data.finish.history && typeof data.finish.history === 'object') plan.finish.history = Object.assign({}, data.finish.history)
    for (const key of Object.keys(data.finish)) {
      if (!['restore', 'panel', 'history'].includes(key)) warnings.push('unknown finish key ignored: ' + key)
    }
  }
  if (!plan.labscript && !plan.joint_script) throw new Error('protocol needs "labscript" or "joint_script": ' + (file || 'inline'))
  if (!Number.isInteger(plan.reps) || plan.reps < 1) throw new Error('protocol reps must be a positive integer: ' + (file || 'inline'))
  return { name: plan.name, file: file || null, plan, warnings }
}
