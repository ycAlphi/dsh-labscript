# dsh-labscript

Control a [labscript-suite](https://labscriptsuite.org/) experiment from DeepSeek Harness (DSH).

The plugin exposes the stock labscript control surface only: the runmanager remote API
(panel globals, n_shots, engage, abort, run_shots, error_in_globals) plus the shot HDF5
files. It does not require any custom BLACS/lyse patch. Everything lab-specific
(analysis, protocols, hard limits) is supplied by configuration.

## Status

v0.2.0. The mock-labscript backend, the protocol/postprocess flow and the
snapshot/diagnosis behaviour are covered by node and python tests. A client
configuration page (Plugins page) and a read-only `labs_doctor` are included. The real
labscript backend is implemented against the stock runmanager remote API but has
not yet been validated on laboratory hardware. Start real-machine work with a
read-only status call, then a single-key write/restore, then a few shots.

## Requirements

- DSH >= 0.2 and Node.js ^22.19 or >= 24.
- Python >= 3.9. Mock mode needs only numpy >= 1.23 and h5py >= 3.8; real
  hardware needs the labscript-suite Python environment (labscript, runmanager,
  labscript_utils, zprocess importable). Tested with Python 3.11.0,
  numpy 2.2.5, h5py 3.13.0. The plugin never stores credentials; zprocess
  authentication is read by labscript from labconfig.

## Install

Install the npm package `dsh-labscript` (add it to the DSH profile
dependencies and run `pnpm install`), or clone this repository and use a
`link:` dependency for local development. docs/INSTALL.zh.md walks through both. The backend defaults to
`mock-labscript`; real hardware is only used when the config sets
`backend: labscript` explicitly. Connection settings can be edited in
Plugins -> dsh-labscript after install (advanced keys stay in the profile
config). Run `labs_doctor` first if anything looks misconfigured.

## Tools

| Tool | What it does |
|---|---|
| labs_doctor | Read-only configuration diagnostic: python, numpy/h5py, labscript/runmanager, paths, approval, versions. |
| labs_status | Read the panel: labscript, globals, n_shots, error_in_globals, run_shots, pending run progress, snapshot directory |
| labs_set | Write panel globals (or raw expressions, or the repetition counter). Snapshots the panel first. Does not run. |
| labs_engage | Submit the current panel for a run; returns immediately with n_shots_at_submit and expected_n_shots |
| labs_wait | Wait (blocking, with timeout) for the pending run; finalizes a pending protocol: postprocess, finish write-back, restore |
| labs_abort | Abort the current submission |
| labs_data | Read the run folder HDF5: first-shot globals and, with scan, a per-shot table |
| labs_protocol | list / show / run YAML fixed-operation protocols |
| labs_restore | Restore the panel from the latest snapshot taken before a write |

A runtime skill named labscript carries the workflow rules (status first, fixed
protocols for stable flows, submit then end the turn, one run at a time, never
guess panel state).

## Fixed-operation protocols

    name: demo_scan
    labscript: /path/to/your/sequence.py   # or: joint_script: registry_key
    set:
      do_acquire: true
      amplitude: 0.5
    scan:
      duration_test_us: np.linspace(0.0, 8.0, 5)   # expression, number or list
    reps: 2
    postprocess: demo.peaks                        # entry configured once
    finish:
      restore: true
      panel:
        duration_test_us: fit.center               # written back to the panel
      history:
        demo.peaks.center_us: fit.center           # appended to the calibration jsonl

    labs_protocol show demo_scan     # validates the plan and hard limits
    labs_protocol run demo_scan      # set + scan + reps + engage, returns immediately
    labs_wait                        # waits, then postprocess -> finish -> restore

## Configuration

Add one row to your DSH profile patch (see examples/config.example.yml):

    - id: dsh-labscript
      name: dsh-labscript
      config:
        python: /path/to/labscript-suite/python
        backend: labscript
        runmanager: { host: 127.0.0.1, port: 42523, timeoutS: 10 }
        protocolsDir: /path/to/protocols
        scriptsFile: /path/to/scripts.json
        hardLimitsFile: /path/to/hard_limits.json
        calibrationLog: /path/to/calibration.jsonl
        pathMap:
          - { remotePrefix: "E:/Experiments", localPrefix: "//lab-pc/Experiments" }
        reps: { global: dummy_iterator }
        postprocess:
          entry: my_lab.analysis:analyze
          pythonPaths: [/path/to/my_lab]
        approval: { engage: true, protocolRun: true }

`pathMap` rewrites paths reported by runmanager (remote drive letters such as
`E:/Experiments`) into paths this machine can open (UNC shares). Tool output is
already mapped: `labs_status`, `labs_engage`, `labs_wait`, `labs_data` and
`labs_protocol` show local paths, while `run_folder` keeps the raw runmanager
value next to `mapped_run_folder`.

The python interpreter must be able to import numpy and h5py; the real backend
also needs runmanager (labscript-suite). The plugin never stores credentials:
zprocess authentication is read by labscript itself from labconfig.

### Mock backend

    backend: mock-labscript

runs a built-in simulator (fake panel, fake HDF5, persistent state under
mock.stateDir). It exists for tests, examples and hardware-free development; it
is not a physics simulation.

## Install (local)

    git clone <this repo> && cd dsh-labscript
    npm install
    # add "dsh-labscript": "link:/path/to/dsh-labscript" to the profile package.json
    # add the bundle name and the cordis.patch.yml row; restart DSH

## Development

    npm install              # only runtime dependency: yaml
    npm test                 # node --test, 40 tests (fake bridge, no python needed)
    python python/tests/run_tests.py   # 22 tests (needs numpy + h5py)
    LABSCRIPT_PYTHON=/path/to/python3 node scripts/demo.mjs   # end-to-end mock demo

A fresh clone must run `npm install` before `npm test`; the CI workflow does this.

## Docs

- docs/README.zh.md - Chinese guide
- docs/INSTALL.zh.md - local DSH profile install

## Boundaries

- Real hardware: engage/protocol run ask for a DSH approval (one-shot) when the
  backend is labscript; abort and panel writes do not. The plugin is not a
  hardware interlock.
- Optional hard limits (limits/channels entries with min and/or max, plus
  run_limits) are checked on panel writes and protocol planning; boolean values
  count as 0/1. They are advisory configuration, not a safety interlock.
- labs_protocol show warns when a set/scan key is missing from the currently loaded
  labscript; protocol runs ask for confirmation before proceeding on real hardware.
- Completion is detected from the run folder on the filesystem (HDF5 count,
  stable file sizes, and a readable last shot); stock labscript has no
  run-state query.
- Cancelling labs_wait only stops waiting: the run keeps going. Call labs_abort
  explicitly when something must stop; labs_abort also cancels an in-flight labs_wait.
  labscript abort stops compiling/submitting new shots; shots already queued in BLACS
  or running are not stopped.
- labs_wait reports a diagnosis (complete, no_shots, compiling, compiled_not_executed,
  partial). compiled_not_executed means shots exist but show no acquisition data yet;
  BLACS may be paused or not executing the queue.
- Real runmanager engage does not return a run folder; the bridge captures the
  output folder before engage and reports it as the run folder. That shot output
  folder is the NEXT run's folder and does not exist until engage, so labs_data
  errors (RUN_FOLDER_MISSING) instead of reporting h5: 0 for a path that is not there.
- The panel is snapshotted before every write; labs_restore restores only the keys the
  write touched (full=true restores the whole panel), so unrelated concurrent edits survive.
- Actions are logged to events.jsonl next to the snapshots.

---

Development note: implemented with assistance from a DeepSeek Harness coding agent.
