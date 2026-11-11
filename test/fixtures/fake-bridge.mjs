// Fake python bridge used by the node tests. Reads one JSON request from
// stdin and answers with a canned envelope, so the transport and tool layer
// are tested without a python interpreter or labscript.

const chunks = []
for await (const chunk of process.stdin) chunks.push(chunk)
let request
try {
  request = JSON.parse(Buffer.concat(chunks).toString('utf8'))
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, error: { code: 'BAD_REQUEST', message: 'fake bridge could not parse request' } }))
  process.exit(0)
}

const mode = request.args && request.args.mode
if (mode === 'hang') {
  setTimeout(() => process.exit(0), 30000)
} else if (mode === 'badjson') {
  process.stdout.write('this is not json')
  process.exit(0)
} else if (mode === 'error') {
  process.stdout.write(JSON.stringify({ ok: false, error: { code: 'TEST_ERROR', message: 'canned error', detail: { k: 1 } } }))
  process.exit(0)
} else {
  process.stdout.write(JSON.stringify({ ok: true, data: answer(request.cmd, request.args || {}) }))
}

function answer(cmd, args) {
  if (cmd === 'status') {
    return { backend: 'mock-labscript', labscript: 'mock_sequence.py', shot_output_folder: '/tmp/runs', globals: { amplitude: 0.5 }, n_shots: 2, error_in_globals: false, run_shots: true, panel_history: { dir: '/tmp/history', latest: null, count: 0 } }
  }
  if (cmd === 'set') {
    return { written: args.values || {}, readback: args.values || {}, missing: [], n_shots: 2, error_in_globals: false, snapshot: { name: 'snap-1.json', file: '/tmp/history/snap-1.json', dir: '/tmp/history' } }
  }
  if (cmd === 'engage') {
    return { accepted: true, run_folder: '/tmp/runs/run_000001', mapped_run_folder: '/tmp/runs/run_000001', n_shots_at_submit: 2, expected_n_shots: 2, labscript: 'mock_sequence.py', submitted_at: new Date().toISOString() }
  }
  if (cmd === 'wait') {
    return { run_folder: args.run_folder, expected_n_shots: args.expected_n_shots || 2, n_h5: 2, complete: true, timed_out: false, elapsed: 1.2 }
  }
  if (cmd === 'progress') {
    return { run_folder: args.run_folder, n_h5: 1, expected_n_shots: args.expected_n_shots || 2, complete: false }
  }
  if (cmd === 'abort') return { aborted: true, result: { ok: true } }
  if (cmd === 'data') return { run_folder: args.run_folder, n_h5: 2, globals: { amplitude: 0.5 }, scan_table: { scan: args.scan || 'duration_test_us', n_points: 2, rows: [] } }
  if (cmd === 'restore') return { restored_from: { file: '/tmp/history/snap-1.json', ts: 't' }, written: {}, readback: {} }
  if (cmd === 'doctor') return { checks: [{ name: 'numpy', status: 'ok', detail: '2.2.5' }, { name: 'h5py', status: 'ok', detail: '3.13.0' }], backend: 'mock-labscript', python: '3.11.0' }
  if (cmd === 'protocol_plan') return { plan: { name: 'demo_scan', expected_n_shots: 2, reps: 1, scan: { duration_test_us: { expr: 'np.linspace(0, 1, 2)', n: 2 } } }, hard_limits: { checked: false, warning: 'hardLimitsFile not configured' }, name_check: { checked: true, script: 'mock_sequence.py', missing: ['old_duration_key'], suggestions: { old_duration_key: ['duration_test_us'] } }, warnings: [] }
  if (cmd === 'protocol_start') return { plan: { name: 'demo_scan', expected_n_shots: 2 }, snapshot: { name: 'snap-2.json', file: '/tmp/history/snap-2.json' }, steps: {}, engage: { accepted: true, run_folder: '/tmp/runs/run_000002', n_shots_at_submit: 2 }, n_shots_at_submit: 2, expected_n_shots: 2, submitted_at: new Date().toISOString() }
  if (cmd === 'protocol_finalize') return { run_folder: args.run_folder, postprocess: { fit: { center: 2.5 } }, finish: { panel: { duration_test_us: 2.5 } }, restore: { restored: true } }
  return { unknown: cmd }
}
