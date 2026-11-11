# Changelog

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
