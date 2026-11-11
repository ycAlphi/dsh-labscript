/**
 * Runtime skill: workflow rules for controlling a labscript-suite experiment.
 * Tool descriptions carry parameter detail; this skill carries the order of
 * operations and the stop rules.
 */
export const SKILL_CONTENT = [
  '# Labscript experiment control',
  '',
  'You control a labscript-suite experiment through the labs_* tools. The panel state,',
  'not your memory, is the truth. Never guess a global value or a scan axis.',
  '',
  '## Before any write',
  '1. Call labs_status. Confirm the loaded labscript, n_shots, error_in_globals, run_shots',
  '   and the keys you are about to touch.',
  '2. Real hardware: the user must approve this turn before panel writes, engage or abort.',
  '   The plugin asks for its own one-shot approval on engage/protocol run in the real',
  '   backend; that approval does not replace the user conversation requirement.',
  '3. Prefer a fixed protocol (labs_protocol) for stable calibration flows. Use labs_set',
  '   and labs_engage only for exploratory measurements.',
  '',
  '## Running',
  '- labs_set writes the panel. It does not run anything. Always pass note: one sentence',
  '  saying what this batch is for (it is written to the event log).',
  '- labs_engage submits a run and returns immediately with n_shots_at_submit and',
  '  expected_n_shots. Do not poll in a loop; when the user wants results, call labs_wait.',
  '  After submitting you may end the turn or do independent work.',
  '- labs_protocol run submits a fixed protocol; labs_wait finalizes it (postprocess,',
  '  finish write-back, panel restore).',
  '- backend defaults to mock-labscript; real hardware is only used when the config says',
  '  backend: labscript explicitly.',
  '- On a new install or when a tool fails with a configuration error, run labs_doctor first.',
  '- labs_abort stops compiling/submitting new shots; shots already queued in BLACS or',
  '  running are not stopped. It is not a hardware interlock. Never call it unless',
  '  something looks wrong or the user asks.',
  '- A panel snapshot is taken before every write. labs_restore restores only the keys that',
  '  write touched (pass full=true for the whole panel), so concurrent edits elsewhere are kept.',
  '',
  '## Reading results',
  '- labs_data reads the run folder HDF5: globals of the first shot and, with scan: a',
  '  per-shot table. HDF5 is authoritative; the panel is only intermediate state.',
  '- Report facts: run folder, expected vs actual shots, postprocess numbers, warnings.',
  '  If a number is missing, say so instead of inventing one.',
  '',
  '## Stop rules',
  '- error_in_globals true: fix or ask; never engage.',
  '- wait timed out or diagnosis=compiled_not_executed: the shots may be compiled but BLACS',
  '  may be paused/not executing. Check BLACS and the panel, then abort or ask; do not resubmit blindly.',
  '- A run is already pending: wait or abort before submitting another.',
].join(String.fromCharCode(10))

export function registerSkill(ctx) {
  ctx.skills.register({
    name: 'labscript',
    description: 'Control a labscript-suite experiment through the labs_* tools: read the runmanager panel, write globals and scans, submit runs without blocking, wait for completion, run YAML fixed-operation protocols with postprocess write-back, read run HDF5 data, restore panel snapshots.',
    whenToUse: 'When the user asks to inspect or change the labscript panel, run shots or scans, run a fixed experiment protocol, or read results of a labscript run.',
    source: 'runtime',
    content: SKILL_CONTENT,
    invocation: { modelInvocable: true, userInvocable: true },
  })
}
