"""Example postprocess entry: after a run, return the numbers that finish.panel
and finish.history write back. Replace the fake model with your lab analysis.

Signature: analyze(run_folder=..., scan_global=..., reps=..., name=..., scan_globals=...)
scan_global is the first axis; scan_globals is every axis in order (a 2D scan needs both).
Returns a JSON-serializable dict; finish.panel reads the numbers back out of it.
"""


def analyze(run_folder, scan_global=None, reps=None, name=None, scan_globals=None):
    # Placeholder numbers so the write-back path is demonstrable end to end.
    return {
        "fit": {"center": 2.5, "pi_duration_us": 1.25, "r2": 0.99},
        "meta": {
            "run_folder": run_folder,
            "scan_global": scan_global,
            "reps": reps,
            "name": name,
        },
    }
