/**
 * The eight labs_* tools. Each one is a thin wrapper over one python bridge
 * command plus the DSH-side concerns: approval, serialization, pending-run
 * bookkeeping and compact rendering.
 */
import { LabError } from './runtime.js'
import { listProtocols, loadProtocol } from './protocols.js'

const NEWLINE = String.fromCharCode(10)

function text(value) {
  return [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) }]
}

function lines(values) {
  return values.filter((value) => value !== undefined && value !== null && value !== '').join(NEWLINE)
}

function looseOutput(render) {
  return { schema: { type: 'object', additionalProperties: true }, render: render }
}

function number(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function progressLine(run) {
  if (!run) return 'run: none pending'
  const parts = ['run: ' + (run.mapped_run_folder || run.run_folder || '<submitted>')]
  if (run.kind) parts.push('kind=' + run.kind)
  if (run.n_h5 !== undefined) parts.push('h5=' + run.n_h5 + '/' + (run.expected_n_shots || '?'))
  else if (run.expected_n_shots) parts.push('expected=' + run.expected_n_shots)
  if (run.complete !== undefined) parts.push('complete=' + run.complete)
  if (run.elapsed_s !== undefined) parts.push('elapsed=' + run.elapsed_s + 's')
  return parts.join('  ')
}

// The bridge returns the whole panel; without this the summary alone hides it.
function globalText(value, limit = 240) {
  let rendered
  try { rendered = JSON.stringify(value) } catch { rendered = String(value) }
  if (rendered === undefined) rendered = String(value)
  return rendered.length > limit ? rendered.slice(0, limit) + '…' : rendered
}

function globalLines(args, value) {
  const globals = value.globals || {}
  const keys = Array.isArray(args.keys) ? args.keys : []
  const raw = args.raw === true && value.raw_globals ? value.raw_globals : null
  if (keys.length === 1 && keys[0] === '*') {
    const shown = raw || globals
    const names = Object.keys(shown).sort()
    return ['globals: all ' + names.length].concat(names.map((key) => '  ' + key + ' = ' + globalText(shown[key])))
  }
  if (keys.length === 0) {
    const count = Object.keys(globals).length
    return [count > 0 ? 'globals: ' + count + ' keys (pass keys=[...] or keys=["*"] to read values)' : 'globals: none']
  }
  const out = ['globals:']
  for (const key of keys) {
    const shown = raw ? raw[key] : globals[key]
    out.push('  ' + key + ' = ' + (shown === undefined ? '<missing>' : globalText(shown)))
  }
  return out
}

function scanLine(value) {
  const axes = value.scan_axes && typeof value.scan_axes === 'object' ? value.scan_axes : {}
  const names = Object.keys(axes).sort()
  const product = value.n_shots_from_arrays
  const mismatch = typeof product === 'number' && typeof value.n_shots === 'number' && value.n_shots !== product
  if (names.length === 0 && !mismatch) return ''
  const detail = names.length ? names.map((key) => key + '=' + axes[key]).join(', ') : 'none'
  return 'scan axes: ' + detail + '  n_shots_from_arrays=' + product + (mismatch ? '  (panel n_shots=' + value.n_shots + ' differs)' : '')
}

async function pendingProgress(runtime, exec) {
  const pending = runtime.state.pending
  if (!pending) return null
  const base = {
    kind: pending.kind,
    run_folder: pending.run_folder,
    expected_n_shots: pending.expected_n_shots,
    submitted_at: pending.submitted_at,
    note: pending.note,
    elapsed_s: pending.submitted_at ? Math.round((Date.now() - Date.parse(pending.submitted_at)) / 1000) : undefined,
  }
  if (!pending.run_folder) return base
  try {
    const progress = await runtime.runBridge('progress', {
      run_folder: pending.run_folder,
      expected_n_shots: pending.expected_n_shots,
    }, { signal: exec.signal })
    return Object.assign(base, progress)
  } catch (error) {
    return base
  }
}

async function requireApproval(runtime, exec, toolName, reason, approvalKey) {
  if (runtime.isMock()) return
  if (approvalKey && runtime.config.approval[approvalKey] === false) return
  const approval = typeof runtime.ctx.get === 'function' ? runtime.ctx.get('approval') : undefined
  if (!approval) {
    throw new LabError('APPROVAL_UNAVAILABLE', 'no approval service is composed; refusing to run a real-hardware action')
  }
  const outcome = await approval.request({
    agent: exec.agent,
    toolName: toolName,
    callId: exec.callId,
    reason: reason,
    displayReason: reason,
    signal: exec.signal,
  })
  if (outcome !== 'allowed-once') {
    throw new LabError('APPROVAL_DENIED', 'action not approved (' + outcome + '): ' + reason)
  }
}

async function clearStalePending(runtime, exec) {
  const pending = runtime.state.pending
  if (!pending) return
  if (pending.run_folder) {
    try {
      const progress = await runtime.runBridge('progress', {
        run_folder: pending.run_folder,
        expected_n_shots: pending.expected_n_shots,
      }, { signal: exec.signal })
      if (!progress.complete) {
        throw new LabError('RUN_PENDING', 'a run is still in progress (' + progress.n_h5 + '/' + progress.expected_n_shots + ' shots): call labs_wait first, or labs_abort')
      }
    } catch (error) {
      if (error instanceof LabError && error.code === 'RUN_PENDING') throw error
    }
  }
  runtime.clearPending()
}

function mockApprovalSkipped(runtime) {
  return runtime.isMock()
}

export function registerTools(ctx, runtime) {

  ctx.tools.register({
    name: 'labs_status',
    description: 'Read the labscript runmanager panel: loaded labscript, globals (evaluated; raw expressions with raw=true), n_shots, error_in_globals, run_shots, shot output folder, pending run progress, and the panel snapshot directory. Read-only; call this before any write. Pass keys=[...] (leaf global names; UI group names are not globals) to print the selected panel values (240 chars each; raw=true prints the raw expression), missing keys are shown as <missing>; keys=["*"] prints every global. scan axes lists array-valued globals (their lengths multiply to n_shots_from_arrays) and a differing panel n_shots is flagged. The shot output folder is the folder the NEXT run will use; it does not exist until engage. Paths in the output are already mapped for this machine through pathMap; do not map them again.',
    parameters: {
      type: 'object',
      properties: {
        keys: { type: 'array', items: { type: 'string' }, description: 'Only return these panel globals; ["*"] returns every global' },
        raw: { type: 'boolean', description: 'Also return raw (expression) values' },
      },
      additionalProperties: false,
    },
    isConcurrencySafe: () => true,
    output: looseOutput((args, value) => text(lines([
      'backend: ' + value.backend + '  plugin: v' + runtime.version,
      'labscript: ' + value.labscript,
      'shot output: ' + value.shot_output_folder,
      'n_shots: ' + value.n_shots + '  error_in_globals: ' + value.error_in_globals + '  run_shots: ' + value.run_shots,
      progressLine(value.last_run),
      'panel history: ' + (value.panel_history ? value.panel_history.dir + ' (' + value.panel_history.count + ' snapshots)' : 'n/a'),
      scanLine(value),
      ...globalLines(args, value),
    ]))),
    async execute(args, exec) {
      const keys = Array.isArray(args.keys) ? args.keys : []
      const all = keys.length === 1 && keys[0] === '*'
      const data = await runtime.runBridge('status', {
        keys: all || keys.length === 0 ? undefined : keys,
        raw: args.raw === true,
      }, { signal: exec.signal })
      if (runtime.state.pending) data.last_run = await pendingProgress(runtime, exec)
      return data
    },
  })

  ctx.tools.register({
    name: 'labs_set',
    description: 'Write runmanager panel globals. values are JSON scalars (bool/number/string); pass raw=true to write expression strings, or reps=N to write the repetition counter (np.arange(N)). A full panel snapshot is taken before the write. Does not run anything.',
    parameters: {
      type: 'object',
      properties: {
        values: { type: 'object', additionalProperties: true, description: 'Panel globals to write: key -> scalar' },
        raw: { type: 'boolean', description: 'values are raw expression strings (e.g. np.arange(0, 8, 0.3))' },
        reps: { type: 'integer', minimum: 1, description: 'Write the configured repetition global as np.arange(reps)' },
        note: { type: 'string', description: 'One sentence: what this batch is for (written to the event log)' },
      },
      required: ['values'],
      additionalProperties: false,
    },
    isConcurrencySafe: () => false,
    output: looseOutput((_args, value) => text(lines([
      'wrote ' + Object.keys(value.written || {}).length + ' keys, n_shots now ' + value.n_shots,
      value.missing && value.missing.length ? 'MISSING: ' + value.missing.join(', ') : 'readback ok',
      value.snapshot ? 'snapshot: ' + value.snapshot.name : '',
    ]))),
    async execute(args, exec) {
      return runtime.serial(() => runtime.runBridge('set', {
        values: args.values || {},
        raw: args.raw === true,
        reps: args.reps,
        note: args.note,
      }, { signal: exec.signal }))
    },
  })

  ctx.tools.register({
    name: 'labs_engage',
    description: 'Submit the current panel for a run (compiles and sends to BLACS). Returns immediately with run_folder, n_shots_at_submit and expected_n_shots; use labs_wait later to wait for completion. Refuses when error_in_globals is true or another run is still pending.',
    parameters: {
      type: 'object',
      properties: {
        note: { type: 'string', description: 'One sentence: what this run is for (written to the event log)' },
      },
      additionalProperties: false,
    },
    isConcurrencySafe: () => false,
    output: looseOutput((_args, value) => text(lines([
      'submitted run: ' + (value.mapped_run_folder || value.run_folder || '<run folder pending>'),
      'n_shots_at_submit: ' + value.n_shots_at_submit + '  expected: ' + value.expected_n_shots,
      'call labs_wait when results are needed.',
    ]))),
    async execute(args, exec) {
      return runtime.serial(async () => {
        await clearStalePending(runtime, exec)
        const before = await runtime.runBridge('status', {}, { signal: exec.signal })
        await requireApproval(runtime, exec, 'labs_engage', 'Submit a labscript run: ' + (before.labscript || '<script>') + ', n_shots=' + before.n_shots + ', backend=' + before.backend, 'engage')
        const data = await runtime.runBridge('engage', { note: args.note }, { signal: exec.signal, timeoutMs: 180000 })
        runtime.setLastRun({
          run_folder: data.run_folder,
          expected_n_shots: data.expected_n_shots,
          submitted_at: data.submitted_at,
        })
        runtime.setPending({
          kind: 'engage',
          run_folder: data.run_folder,
          expected_n_shots: data.expected_n_shots,
          submitted_at: data.submitted_at,
          note: args.note,
          snapshot: null,
        })
        return data
      })
    },
  })

  ctx.tools.register({
    name: 'labs_wait',
    description: 'Wait for the pending run to finish (blocking, timeout in seconds). If the pending run came from labs_protocol run, this call also finalizes it: postprocess, finish write-back of panel and calibration history, and panel restore. Returns expected vs actual shots and, for protocols, the postprocess/finish/restore results. A run folder that does not exist and is not the pending run of this session fails immediately instead of waiting.',
    parameters: {
      type: 'object',
      properties: {
        timeout_s: { type: 'number', minimum: 1, description: 'Override the configured run timeout (default config.engage.timeoutS)' },
        run_folder: { type: 'string', description: 'Wait for this run folder instead of the pending run' },
        note: { type: 'string', description: 'One sentence for the event log' },
      },
      additionalProperties: false,
    },
    isConcurrencySafe: () => true,
    output: looseOutput((_args, value) => text(lines([
      'run: ' + (value.mapped_run_folder || value.run_folder),
      value.aborted ? 'wait cancelled by labs_abort (run aborted)' : '',
      value.folder_exists === false ? 'folder: missing' : '',
      'shots: ' + value.n_h5 + '/' + (value.expected_n_shots || '?') + '  complete: ' + value.complete + (value.timed_out ? '  TIMED OUT' : '') + (value.diagnosis ? '  diagnosis: ' + value.diagnosis : ''),
      value.hint ? 'hint: ' + value.hint : '',
      value.finalize && value.finalize.postprocess ? 'postprocess: ' + JSON.stringify(value.finalize.postprocess).slice(0, 400) : '',
      value.finalize && value.finalize.finish ? 'finish: ' + JSON.stringify(value.finalize.finish).slice(0, 300) : '',
      value.finalize && value.finalize.restore ? 'restore: ' + (value.finalize.restore.restored ? 'yes' : 'no') : '',
    ]))),
    async execute(args, exec) {
      return runtime.serial(async () => {
        const pending = runtime.state.pending
        const runFolder = args.run_folder || (pending ? pending.run_folder : '')
        if (!runFolder) throw new LabError('NO_PENDING_RUN', 'no run folder: submit with labs_engage or labs_protocol run first, or pass run_folder')
        const expected = pending && pending.run_folder === runFolder ? pending.expected_n_shots : undefined
        if (!(pending && pending.run_folder === runFolder)) {
          let probe = null
          try {
            probe = await runtime.runBridge('progress', {
              run_folder: runFolder,
              expected_n_shots: expected,
            }, { signal: exec.signal })
          } catch (error) {
            probe = null
          }
          if (probe && probe.folder_exists === false) {
            throw new LabError('RUN_FOLDER_MISSING', 'run folder does not exist: ' + (probe.mapped_run_folder || runFolder) + '. Pass the folder of a run that exists (labs_engage / labs_protocol return one), or check pathMap.')
          }
        }
        const timeoutS = Number(args.timeout_s || runtime.config.engage.timeoutS || 900)
        let waited
        try {
          waited = await runtime.runBridge('wait', {
            run_folder: runFolder,
            expected_n_shots: expected,
            timeout_s: timeoutS,
          }, { signal: exec.signal, timeoutMs: (timeoutS + 60) * 1000 })
        } catch (error) {
          if (error instanceof LabError && error.code === 'ABORTED' && runtime.state.waitCancelledByAbort) {
            runtime.state.waitCancelledByAbort = false
            return {
              run_folder: runFolder,
              expected_n_shots: expected,
              complete: false,
              timed_out: false,
              aborted: true,
              hint: 'wait cancelled by labs_abort; the run was aborted',
            }
          }
          if (error instanceof LabError && (error.code === 'TIMEOUT' || error.code === 'ABORTED')) {
            throw new LabError(error.code, error.message + '. The run may still be executing: call labs_wait with run_folder to reconnect, or labs_abort.', error.detail)
          }
          throw error
        }
        const out = {
          wait: waited,
          run_folder: waited.run_folder,
          expected_n_shots: waited.expected_n_shots,
          n_h5: waited.n_h5,
          complete: waited.complete,
          timed_out: waited.timed_out,
          elapsed: waited.elapsed,
          diagnosis: waited.diagnosis,
          executed: waited.executed,
          hint: waited.hint,
          mapped_run_folder: waited.mapped_run_folder,
          folder_exists: waited.folder_exists,
        }
        if (waited.complete && runtime.state.lastRun && runtime.state.lastRun.run_folder === runFolder) {
          runtime.setLastRun(Object.assign({}, runtime.state.lastRun, {
            completed_at: new Date().toISOString(),
            n_h5: waited.n_h5,
          }))
        }
        if (waited.complete && pending && pending.kind === 'protocol' && pending.run_folder === runFolder) {
          const finalize = await runtime.runBridge('protocol_finalize', {
            protocol: pending.plan,
            run_folder: runFolder,
            snapshot: pending.snapshot,
            note: args.note || pending.note,
          }, { signal: exec.signal, timeoutMs: 180000 })
          out.finalize = finalize
          runtime.clearPending()
        } else if (waited.complete && pending && pending.run_folder === runFolder) {
          runtime.clearPending()
        }
        return out
      })
    },
  })

  ctx.tools.register({
    name: 'labs_abort',
    description: 'Abort the current labscript submission (runmanager abort) and drop the pending-run state. Use only when something looks wrong or the user asks; aborting is not a hardware interlock.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    isConcurrencySafe: () => false,
    output: looseOutput((_args, value) => text(lines([
      'aborted',
      value.note ? 'note: ' + value.note : '',
      value.wait_cancelled ? 'also stopped the active labs_wait' : '',
      value.result ? 'result: ' + JSON.stringify(value.result).slice(0, 300) : '',
    ]))),
    async execute(_args, exec) {
      const data = await runtime.runBridge('abort', {}, { signal: exec.signal })
      const cancelledWait = runtime.cancelWait()
      runtime.clearPending()
      return Object.assign({}, data, { wait_cancelled: cancelledWait })
    },
  })

  ctx.tools.register({
    name: 'labs_data',
    description: 'Read a run folder HDF5: globals of the first shot (execution facts) and, with scan: a per-shot table of scan values and numeric results. Read-only. run_folder defaults to the pending run, else the last run submitted through this plugin (persisted across restarts); for a historical run pass its folder explicitly. A missing folder is an error: runmanager\'s shot output is the NEXT run folder and only exists after engage.',
    parameters: {
      type: 'object',
      properties: {
        run_folder: { type: 'string', description: 'Run folder; defaults to the run submitted from this DSH session (pass a historical run folder explicitly)' },
        scan: { type: 'string', description: 'Panel global name used as the scan axis; adds a per-shot table' },
        globals: { type: 'boolean', description: 'Include the first shot globals (default true)' },
        results: { type: 'boolean', description: 'Include the results layout' },
      },
      additionalProperties: false,
    },
    isConcurrencySafe: () => true,
    output: looseOutput((_args, value) => text(lines([
      'run: ' + (value.mapped_run_folder || value.run_folder),
      'h5: ' + value.n_h5 + (value.empty ? ' (folder exists, no shots yet)' : ''),
      value.scan_table ? 'scan ' + value.scan_table.scan + ': ' + value.scan_table.n_points + ' shots' : '',
      value.globals ? 'globals: ' + Object.keys(value.globals).length + ' keys' : '',
    ]))),
    async execute(args, exec) {
      const runFolder = args.run_folder || (runtime.state.pending ? runtime.state.pending.run_folder : '') || (runtime.state.lastRun ? runtime.state.lastRun.run_folder : '')
      if (!runFolder) throw new LabError('NO_RUN_FOLDER', 'no run folder: pass run_folder or submit a run first')
      return runtime.runBridge('data', {
        run_folder: runFolder,
        scan: args.scan,
        globals: args.globals !== false,
        results: args.results === true,
      }, { signal: exec.signal })
    },
  })

  ctx.tools.register({
    name: 'labs_protocol',
    description: 'Work with YAML fixed-operation protocols. action=list lists protocol names in protocolsDir; action=show validates a plan and reports expected shots plus hard-limit check; action=run submits the protocol (set, scan, reps, engage) and returns immediately with engage-time numbers; the pending protocol is finalized by labs_wait (postprocess, finish write-back, restore). Protocol references may be a bare name, name.yaml, or an absolute path.',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['list', 'show', 'run'], description: 'list | show | run' },
        protocol: { type: 'string', description: 'Protocol name (e.g. raman_pi_qubit), file name, or absolute path' },
        note: { type: 'string', description: 'One sentence: what this protocol run is for (written to the event log)' },
      },
      required: ['action'],
      additionalProperties: false,
    },
    isConcurrencySafe: () => false,
    output: looseOutput((_args, value) => text(lines([
      value.protocols ? 'protocols: ' + value.protocols.map((item) => item.name + (item.error ? ' (error)' : '')).join(', ') : '',
      value.plan ? 'protocol: ' + (value.plan.name || value.name) : '',
      value.plan ? 'expected_n_shots: ' + value.plan.expected_n_shots + '  reps: ' + value.plan.reps : '',
      value.plan && value.plan.scan ? 'scan: ' + Object.keys(value.plan.scan).join(', ') : '',
      value.hard_limits ? 'hard limits: ' + JSON.stringify(value.hard_limits).slice(0, 240) : '',
      value.warnings && value.warnings.length ? 'warnings: ' + value.warnings.join('; ') : '',
      value.name_check && value.name_check.checked && value.name_check.missing && value.name_check.missing.length ? 'script key check: MISSING ' + value.name_check.missing.join(', ') : '',
      value.name_check && value.name_check.checked === false ? 'script key check: not checked (' + (value.name_check.reason || 'no script') + ')' : '',
      value.engage ? 'submitted: ' + (value.engage.mapped_run_folder || value.engage.run_folder || '<run folder pending>') + '  n_shots_at_submit: ' + value.n_shots_at_submit : '',
      value.engage ? 'call labs_wait to finalize this protocol.' : '',
    ]))),
    async execute(args, exec) {
      const dir = runtime.config.protocolsDir
      if (args.action === 'list') {
        return { protocols: listProtocols(dir), protocolsDir: dir || null }
      }
      if (!args.protocol) throw new LabError('BAD_ARGS', 'protocol is required for show/run')
      const loaded = loadProtocol(dir, args.protocol)
      if (args.action === 'show') {
        const planned = await runtime.runBridge('protocol_plan', { protocol: loaded.plan }, { signal: exec.signal })
        return { name: loaded.name, file: loaded.file, warnings: loaded.warnings.concat(planned.warnings || []), plan: planned.plan, hard_limits: planned.hard_limits, name_check: planned.name_check }
      }
      return runtime.serial(async () => {
        await clearStalePending(runtime, exec)
        const planned = await runtime.runBridge('protocol_plan', { protocol: loaded.plan }, { signal: exec.signal })
        if (planned.hard_limits && planned.hard_limits.checked) {
          // plan command already fails on violations; nothing to do
        }
        const nameCheck = planned.name_check
        if (nameCheck && nameCheck.checked && nameCheck.missing && nameCheck.missing.length) {
          const detail = nameCheck.missing.map((key) => {
            const suggestions = (nameCheck.suggestions || {})[key] || []
            return key + (suggestions.length ? ' (closest: ' + suggestions.join(', ') + ')' : '')
          }).join(', ')
          await requireApproval(runtime, exec, 'labs_protocol', 'Protocol ' + loaded.name + ' uses key(s) that are not in the currently loaded labscript: ' + detail + '. Continue anyway?', 'protocolNames')
        }
        await requireApproval(runtime, exec, 'labs_protocol', 'Run protocol ' + loaded.name + ': expected ' + planned.plan.expected_n_shots + ' shots, labscript ' + (loaded.plan.labscript || loaded.plan.joint_script) + ', backend ' + runtime.config.backend, 'protocolRun')
        const started = await runtime.runBridge('protocol_start', { protocol: loaded.plan, note: args.note }, { signal: exec.signal, timeoutMs: 180000 })
        if (started.engage) {
          runtime.setLastRun({
            run_folder: started.engage.run_folder,
            expected_n_shots: started.expected_n_shots,
            submitted_at: started.submitted_at,
          })
          runtime.setPending({
            kind: 'protocol',
            plan: loaded.plan,
            run_folder: started.engage.run_folder,
            expected_n_shots: started.expected_n_shots,
            snapshot: started.snapshot,
            submitted_at: started.submitted_at,
            note: args.note,
          })
        }
        return Object.assign({ name: loaded.name, file: loaded.file, warnings: loaded.warnings.concat(planned.warnings || []), name_check: nameCheck }, started)
      })
    },
  })

  ctx.tools.register({
    name: 'labs_restore',
    description: 'Restore the panel from a snapshot taken before a previous write. Defaults to the latest snapshot. Reads the snapshot and writes the raw globals back; returns the readback.',
    parameters: {
      type: 'object',
      properties: {
        snapshot: { type: 'string', description: 'Snapshot file name, absolute path, or "latest" (default)' },
        full: { type: 'boolean', description: 'Restore every global in the snapshot (default: only the keys that write touched)' },
      },
      additionalProperties: false,
    },
    isConcurrencySafe: () => false,
    output: looseOutput((_args, value) => text(lines([
      'restored from: ' + (value.restored_from && value.restored_from.file),
      'snapshot ts: ' + (value.restored_from && value.restored_from.ts),
      value.selective ? 'selective restore of ' + (value.keys || []).length + ' touched keys' : 'full panel restore',
      (value.keys || []).length ? 'keys: ' + (value.keys || []).join(', ') : '',
    ]))),
    async execute(args, exec) {
      return runtime.serial(() => runtime.runBridge('restore', { snapshot: args.snapshot || 'latest', full: args.full === true }, { signal: exec.signal }))
    },
  })

  ctx.tools.register({
    name: 'labs_doctor',
    description: 'Read-only configuration diagnostic. Checks the python interpreter, numpy/h5py, labscript/runmanager (real backend), configured paths, the approval service and version. Run this first after installing, or when a tool fails with a configuration error.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    isConcurrencySafe: () => true,
    output: looseOutput((_args, value) => text(lines((value.checks || []).map((check) => '[' + check.status + '] ' + check.name + ': ' + check.detail + (check.hint ? '  -- ' + check.hint : ''))))),
    async execute() {
      return runtime.doctor()
    },
  })

  void mockApprovalSkipped
  void number
}
