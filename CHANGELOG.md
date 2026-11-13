# Changelog

## 0.2.7

- Fix: labs_wait no longer fails with "value is not lossless JSON". Tool results must be
  pure JSON; a completed wait could carry an undefined hint field, which the DSH host
  rejects when recording the result (the run itself was fine). labs_status pending
  progress and the aborted-wait result are cleaned the same way.
- Fix: non-ASCII arguments (e.g. a Chinese note) no longer break the bridge. Python's
  stdin on Windows uses the locale encoding (GBK); the bridge now reads stdin as UTF-8.
  Such a note used to submit the run and then fail with UnicodeEncodeError in the event
  log, which made a successful engage look failed.
- Fix: a failed event-log write can no longer turn a successful action into an error; it
  is reported on stderr and the action result is returned as usual.
- Tests: Unicode note end-to-end through the bridge process, event-log failure
  isolation, and a lossless-JSON check on tool results.

## 0.2.6

- Fix: labs_data rendered only counts. It now prints selected first-shot globals
  (keys=[...] to choose, ["*"] for all, <missing> for absent keys), the results group
  names when results: true, and a scan axis summary (unique points, min/max, median
  step, numeric column count). Pass limit to print up to 20 raw scan rows; the bridge
  only returns rows when limit is given.
- Tests: first-shot key selection, scan axis summary and columns, on-demand rows, and
  the rendered output for keys/results/scan/limit.

## 0.2.5

- labs_status can list every panel global: keys=["*"] prints all of them, sorted
  (raw=true prints the raw expressions). This makes leftover array globals visible,
  which is what makes labs_protocol refuse to run with N_SHOTS_MISMATCH.
- labs_status reports scan axes: the array-valued globals with their lengths and
  n_shots_from_arrays (their product), and flags when the panel n_shots differs from
  that product.
- labs_wait fails immediately when an explicit run folder does not exist and is not
  this session's pending run (it used to wait for the full timeout), and the result
  shows whether the folder exists.
- labs_data description: run_folder defaults to the pending run, else the last run
  submitted through this plugin (persisted across restarts).
- cmd_progress returns folder_exists.
- Tests: scan axes, all-globals rendering, wait fast-fail, progress folder_exists.

## 0.2.4

- Fix: labs_data no longer answers h5: 0 for a run folder that does not exist. A missing
  folder raises RUN_FOLDER_MISSING with the raw and mapped paths plus a hint: runmanager's
  shot output folder is the folder the NEXT run will use and only exists after engage, so
  historical runs must be passed explicitly. An existing folder with no shots reports
  empty: true instead.
- Fix: labs_status with keys=[...] drops keys that are not panel globals instead of
  returning null, so the rendered output shows <missing>. UI group names (for example
  Select_Sequence) are not globals: read their leaf keys or a run's h5.
- Descriptions: labs_status says the shot output folder is the NEXT run folder and that
  keys are leaf global names; labs_data explains the run_folder default and the error.
- Tests: missing run folder, empty run folder, missing keys dropped.

## 0.2.3

- Fix: labs_status now prints panel values. keys=[...] and raw=true were accepted but the
  rendered summary never showed any global, so the model could only see the key count
  (the tool result content is the render output; the returned object is not in context).
  With keys the selected values are printed (240 chars each, <missing> when absent);
  without keys it prints the count and a hint to pass keys.
- Tests: labs_status value rendering (values, count-only, missing keys).

## 0.2.2

- Fix: labs_status and labs_engage report pathMap-mapped paths. `labs_status.labscript`
  and `labs_status.shot_output_folder` used to be raw runmanager paths (e.g. E:\...)
  and were unusable from the agent's machine; they are now what pathMap maps them to.
  This is a field-semantics fix: read them as local paths, do not map again.
- Fix: tool output renders mapped run folders. The rendered lines of labs_engage,
  labs_wait, labs_data, labs_protocol and the pending-run progress now prefer
  `mapped_run_folder` over the raw `run_folder`.
- Fix: labs_status now lists the shot output folder, and the status progress object
  reports `mapped_run_folder`.
- Tests: pathMap exit coverage (status mapping, idempotency, progress mapping).

## 0.2.1

- Hard limits support a lower bound: limits entries can now use min as well as max
  (panel writes and scan expressions are checked on both sides).
- Boolean globals can be limited too: true/false count as 1/0, so max: 0 forbids "on"
  and min: 1 requires it.
- labs_protocol show reports keys that are missing from the currently loaded labscript;
  protocol runs ask for an explicit confirmation on real hardware before proceeding.
- labs_abort now also stops an in-flight labs_wait, so a running batch can be cancelled
  without waiting for the wait timeout.

## 0.2.0

- Safe default: backend is mock-labscript unless the config explicitly sets labscript.
- labs_abort bypasses the serial queue, so it is never blocked behind labs_wait.
- Pending/last-run state is persisted under DSH_HOME and reloaded after a restart.
- labs_doctor: read-only diagnostic for python, numpy/h5py, labscript/runmanager, paths and versions.
- Minimal Plugins-page configuration page (backend, python, runmanager, protocolsDir) + doctor results.
- Python environment requirements documented per backend.

## 0.1.0

- Eight labs_* tools: status, set, engage, wait, abort, data, protocol, restore.
- Fixed-operation YAML protocols with postprocess write-back and panel restore.
- Panel snapshots before writes plus an events.jsonl action log.
- Built-in mock-labscript backend for tests, examples and hardware-free development.
- Runmanager (labscript) backend built on the stock remote API and shot HDF5.
- Stalled-queue diagnosis, selective panel restore and accurate abort semantics.
