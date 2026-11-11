// End-to-end demo against the built-in mock-labscript backend.
// Run: LABSCRIPT_PYTHON=/path/to/python3 node scripts/demo.mjs
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import { apply } from '../lib/index.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const repo = path.dirname(here)
const dataDir = path.join(repo, 'local_data', 'demo')
fs.mkdirSync(dataDir, { recursive: true })
const python = process.env.LABSCRIPT_PYTHON || 'python3'

const tools = new Map()
const ctx = {
  tools: { register(tool) { tools.set(tool.name, tool); return () => {} } },
  skills: { register() {} },
  logger: { info() {}, debug() {}, warn() {} },
}
apply(ctx, {
  python,
  backend: 'mock-labscript',
  protocolsDir: path.join(repo, 'examples', 'protocols'),
  postprocess: { entry: 'postprocess_demo:analyze', pythonPaths: [path.join(repo, 'examples')] },
  panelHistoryDir: path.join(dataDir, 'panel_history'),
  calibrationLog: path.join(dataDir, 'calibration.jsonl'),
  mock: { stateDir: path.join(dataDir, 'mock') },
  engage: { timeoutS: 20, pollS: 0.05, settleS: 0.1 },
})

const exec = { agent: {}, callId: 'demo', signal: undefined }

async function call(name, args) {
  const tool = tools.get(name)
  if (!tool) throw new Error('missing tool ' + name)
  const value = await tool.execute(args || {}, exec)
  const rendered = tool.output && tool.output.render ? tool.output.render(args, value) : null
  console.log('[' + name + '] ' + (rendered ? rendered[0].text : JSON.stringify(value)).split(String.fromCharCode(10)).slice(0, 8).join(String.fromCharCode(10)))
  return value
}

const status = await call('labs_status', {})
if (status.backend !== 'mock-labscript') throw new Error('demo expects the mock backend')
await call('labs_set', { values: { amplitude: 0.6 }, note: 'demo: write a panel value' })
const engaged = await call('labs_engage', { note: 'demo: exploratory run' })
await call('labs_status', {})
const waited = await call('labs_wait', { timeout_s: 20 })
await call('labs_data', { scan: 'duration_test_us' })
const shown = await call('labs_protocol', { action: 'show', protocol: 'demo_scan' })
await call('labs_protocol', { action: 'run', protocol: 'demo_scan', note: 'demo: fixed protocol run' })
const finalized = await call('labs_wait', { timeout_s: 20 })
await call('labs_data', { scan: 'duration_test_us' })
console.log(String.fromCharCode(10) + 'demo ok:')
console.log('  engage run: ' + engaged.run_folder + ' shots=' + engaged.n_shots_at_submit)
console.log('  wait: complete=' + waited.complete + ' n_h5=' + waited.n_h5)
console.log('  protocol: expected=' + shown.plan.expected_n_shots + ' finalized=' + Boolean(finalized.finalize && finalized.finalize.postprocess))
console.log('  calibration log: ' + path.join(dataDir, 'calibration.jsonl'))
