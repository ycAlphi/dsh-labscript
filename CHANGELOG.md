# Changelog

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
