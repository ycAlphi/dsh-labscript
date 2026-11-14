# Changelog

## 0.3.2

Implements the upstream request written from the field (labscript-suite workflow terms and
`runmanager.remote` limits). No output field is renamed or removed.

- `n_shots` is documented and rendered as the authoritative shot count. The scan-axes line is
  labelled a heuristic: runmanager expands list/array globals but passes tuples through, and an
  expansion setting of `none` turns an array into a non-axis; neither is visible through
  `runmanager.remote`. A mismatch reads "n_shots is authoritative" instead of printing two numbers
  with no verdict. The bridge's axis guess ignores tuples and dicts, and the mock backend follows the
  same rule (it used to multiply tuples in, pinning the wrong semantics into the tests); the mock
  also keeps Python types, without which the tuple/list distinction was untestable.
- Skill and tool descriptions no longer tell the agent to "read the runmanager output": compilation
  errors appear only in runmanager's output tab (the compiler subprocess writes its traceback to
  stderr and runmanager redirects it to an in-memory Qt widget), so the instruction is now to say
  that no shot file appeared, ask the operator to paste the tab, and not guess the cause.
  `labs_set` also warns that a global name must not shadow a Python builtin or a
  `from labscript import *` name: runmanager accepts it and labscript fails at compile time.
- `compiled_not_executed` explains the other official causes: BLACS may keep a shot out of its
  queue (the shot's connection table must be a subset of the lab connection table) or put it back
  at the head after a device programming timeout; all of that is visible only in the BLACS GUI.
  More shot files than expected points at a changed panel or BLACS Repeat all / Repeat last, which
  re-runs completed shots without creating new files.
- `labs_status` reports `shuffle` (from `get_shuffle`; "unknown" when that runmanager version has
  no such method). With shuffle on, shot order is not axis order: read each shot's parameters from
  its own h5 globals.
- runmanager host/port/timeout default to labscript's labconfig when unset, resolved by
  `runmanager.remote.Client` itself (`[servers]/[ports]/[timeouts]`, port 42523, timeout 60 s);
  configured values still win. The Plugins page leaves host/port blank for that reason, and clearing
  them hands the endpoint back to labconfig. `labs_doctor` prints the effective endpoint and its
  source. The DSH-side bridge budget (`requestTimeoutMs`) defaults to 180 s so it stays above
  runmanager's client timeout instead of racing it.
- mock backend: `mock.errorInGlobals` makes `error_in_globals` true so the engagement refusal can
  be tested, and `mock.shuffle` sets the reported shuffle flag.
- Docs: a terminology table maps the plugin's short names to the suite's (shot output folder,
  sequence, shot file, parameter space/axes, acquired data) without renaming fields.
- Compilation logs stay out of scope: nothing about a compilation failure is written anywhere
  readable (see README), so no `labs_compile_log` was added; a guessed diagnosis would be worse
  than none.
- Tool descriptions stay close to their 0.2.8 size (+147 characters): the explanations live in the
  runtime skill (loaded on demand) and in the wait/engage diagnostics (shown only when they matter),
  so they do not sit in context on every turn.
- Tests: 33 python + 47 node.

## 0.2.8

Fixes from a real labscript-suite session (2026-11-14, 145 tool calls, no run submitted by the
plugin). Each defect was reproduced against the stock runmanager source before the fix.

- Fix: labs_set failed with "TypeError: 'NoneType' object is not iterable" after the write had
  already landed. runmanager's set_globals RPC is a side-effect call whose handler returns None,
  and the bridge fed that reply to dict(). It now reports the requested values and lets the
  readback decide success. When a write really fails, the error carries `landed` (what the panel
  holds now) and the hint says to read the keys back instead of claiming the panel may be half
  written and pointing at labs_restore.
- Fix: labs_wait blocked for the whole timeout when compilation aborted. It returns after
  engage.noShotS seconds (default 25) with diagnosis no_shots (no shot file appeared at all) or
  compile_stalled (files stopped appearing), so the runmanager compile error can be read instead
  of waiting 5-15 minutes. The hint says to call labs_wait again if a slow compile is expected.
- Fix: a pending run that never produced a shot blocked labs_engage forever. A pending submission
  with zero shots and older than engage.staleS (default 180 s) is dropped and reported as
  dropped_stale_pending; the RUN_PENDING error now states the run's age and both ways out.
- Fix: with pathMap configured, labs_wait(run_folder=<the folder labs_engage printed>) failed with
  RUN_FOLDER_MISSING: the pending run kept the unmapped path and the wait compared strings. Pending
  state now stores the mapped folder (raw kept as remote_run_folder) and both forms match.
- Fix: shot h5 files that only hold compiled globals were counted as progress, so h5=1/64 looked
  like "a shot ran". Progress reports n_h5 (files) and n_executed (shots with acquisition data,
  located by binary search over the file order), completion requires acquisition data in the last
  shot, and labs_status renders executed=.
- labs_protocol show prints the closest key names for plan keys missing from the loaded labscript
  (previously only the run path did), so stale protocols can be renamed without guessing.
- labs_set description: one call can mix scalars, an expression scan axis and reps=N.
- Tests: 28 python + 46 node, adding a client whose set_globals returns None, a stalled
  compilation, a stale zero-shot pending run, mapped/raw pending folders and the show suggestions.

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
