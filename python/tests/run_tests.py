"""Self-contained tests for the python bridge core (mock backend only).

Run: python python/tests/run_tests.py
No pytest, no labscript, no hardware: numpy and h5py are enough.
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(HERE)), "examples")

import bridge


def make_config(tmpdir):
    return {
        "backend": "mock-labscript",
        "mock": {"stateDir": os.path.join(tmpdir, "mock")},
        "panelHistoryDir": os.path.join(tmpdir, "history"),
        "calibrationLog": os.path.join(tmpdir, "calibration.jsonl"),
        "engage": {"timeoutS": 10, "pollS": 0.05, "settleS": 0.05},
        "postprocess": {"entry": "postprocess_demo:analyze", "pythonPaths": [EXAMPLES]},
    }


def protocol_raw():
    return {
        "name": "demo_scan",
        "labscript": "/tmp/mock_sequence.py",
        "set": {"amplitude": 0.5},
        "scan": {"duration_test_us": "np.linspace(0, 2, 3)"},
        "reps": 2,
        "postprocess": "demo.peaks",
        "finish": {
            "restore": True,
            "panel": {"duration_test_us": "fit.center"},
            "history": {"demo.peaks.center_us": "fit.center"},
        },
    }


def test_scan_expressions():
    assert bridge.eval_scan_expr("np.arange(0, 1, 0.25)").size == 4
    assert bridge.eval_scan_expr("np.linspace(0, 2, 5)").size == 5
    assert bridge.eval_scan_expr([0, 1, 2]).size == 3
    assert bridge.eval_scan_expr(3).size == 1
    for bad in ["os.system('x')", "__import__('os')", "np.ones(3)" ]:
        try:
            bridge.eval_scan_expr(bad)
        except bridge.BridgeError as exc:
            assert exc.code == "BAD_SCAN_EXPR", exc.message
        else:
            raise AssertionError("expected rejection for " + bad)


def test_mock_status_and_set():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-py-")
    try:
        cfg = make_config(tmp)
        status = bridge.cmd_status(cfg, {})
        assert status["n_shots"] == 1
        out = bridge.cmd_set(cfg, {"values": {"amplitude": 0.7}, "reps": 2, "note": "test"})
        assert out["n_shots"] == 2
        assert out["missing"] == []
        assert out["snapshot"]["name"].endswith(".json")
        assert out["readback"]["amplitude"] == 0.7
        raw = bridge.cmd_status(cfg, {"raw": True})
        assert raw["raw_globals"]["amplitude"] == "0.7"
        restored = bridge.cmd_restore(cfg, {"snapshot": "latest"})
        assert restored["restored_from"]["file"]
        assert bridge.cmd_status(cfg, {})["n_shots"] == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_protocol_end_to_end():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-protocol-")
    try:
        cfg = make_config(tmp)
        planned = bridge.cmd_protocol_plan(cfg, {"protocol": protocol_raw()})
        assert planned["plan"]["expected_n_shots"] == 6
        started = bridge.cmd_protocol_start(cfg, {"protocol": protocol_raw(), "note": "cal demo"})
        assert started["n_shots_at_submit"] == 6
        run_folder = started["engage"]["run_folder"]
        waited = bridge.wait_for_run(cfg, run_folder, 6, 10)
        assert waited["complete"] is True, waited
        assert waited["n_h5"] == 6
        final = bridge.cmd_protocol_finalize(cfg, {
            "protocol": protocol_raw(),
            "run_folder": run_folder,
            "snapshot": started["snapshot"],
        })
        assert final["postprocess"]["fit"]["center"] == 2.5
        assert final["restore"]["restored"] is True
        panel = bridge.cmd_status(cfg, {})["globals"]
        assert panel["duration_test_us"] == 2.5
        assert panel["amplitude"] == 0.5
        with open(cfg["calibrationLog"], "r", encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        assert len(rows) == 1
        assert rows[0]["key"] == "demo.peaks.center_us"
        assert rows[0]["value"] == 2.5
        events = os.path.join(cfg["panelHistoryDir"], "events.jsonl")
        assert os.path.isfile(events)
        assert len(open(events, encoding="utf-8").readlines()) >= 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hard_limits():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-limits-")
    try:
        cfg = make_config(tmp)
        limits_path = os.path.join(tmp, "limits.json")
        with open(limits_path, "w", encoding="utf-8") as handle:
            json.dump({"limits": {"amplitude": {"max": 0.4}}, "run_limits": {"max_total_shots": 4}}, handle)
        cfg["hardLimitsFile"] = limits_path
        raw = protocol_raw()
        try:
            bridge.cmd_protocol_plan(cfg, {"protocol": raw})
        except bridge.BridgeError as exc:
            assert exc.code == "HARD_LIMIT_EXCEEDED", exc.message
            findings = exc.detail["findings"]
            assert any(item["kind"] == "over_max" for item in findings)
        else:
            raise AssertionError("expected hard limit rejection")
        raw["set"] = {}
        raw["scan"] = {"duration_test_us": "np.linspace(0, 2, 3)"}
        raw["reps"] = 3
        try:
            bridge.cmd_protocol_plan(cfg, {"protocol": raw})
        except bridge.BridgeError as exc:
            findings = exc.detail["findings"]
            assert any(item["kind"] == "over_max_total_shots" for item in findings)
        else:
            raise AssertionError("expected shot limit rejection")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_array_values_through_set():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-array-")
    try:
        cfg = make_config(tmp)
        out = bridge.cmd_set(cfg, {"values": {"duration_test_us": [0.0, 0.5, 1.0]}, "note": "array set"})
        assert out["n_shots"] == 3, out
        raw = out["raw_readback"]["duration_test_us"]
        assert raw.startswith("np.linspace(") or raw.startswith("np.array(["), raw
        raw = bridge.cmd_status(cfg, {"raw": True})
        assert raw["globals"] is not None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_data_reading():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-data-")
    try:
        cfg = make_config(tmp)
        started = bridge.cmd_protocol_start(cfg, {"protocol": protocol_raw()})
        run_folder = started["engage"]["run_folder"]
        data = bridge.cmd_data(cfg, {"run_folder": run_folder, "scan": "duration_test_us"})
        assert data["n_h5"] == 6
        assert data["scan_table"]["n_points"] == 6
        assert data["globals"]["amplitude"] == 0.5
        keys = data["scan_table"]["rows"][0].keys()
        assert any(str(key).startswith("demo.") for key in keys), list(keys)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_selective_restore():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-selective-")
    try:
        cfg = make_config(tmp)
        bridge.cmd_set(cfg, {"values": {"amplitude": 0.61}})
        second = bridge.cmd_set(cfg, {"values": {"do_acquire": False}})
        bridge.cmd_set(cfg, {"values": {"amplitude": 0.66}})
        restored = bridge.cmd_restore(cfg, {"snapshot": second["snapshot"]["name"]})
        panel = bridge.cmd_status(cfg, {})["globals"]
        assert restored["selective"] is True, restored
        assert panel["amplitude"] == 0.66, panel
        assert panel["do_acquire"] is True, panel
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_wait_diagnosis_compiled_not_executed():
    import h5py
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-diag-")
    try:
        cfg = make_config(tmp)
        cfg["engage"] = {"timeoutS": 5, "pollS": 0.05, "settleS": 0.1, "stallS": 0.3}
        run_dir = os.path.join(tmp, "compiled_run")
        os.makedirs(run_dir, exist_ok=True)
        with h5py.File(os.path.join(run_dir, "shot_0000.h5"), "w") as handle:
            group = handle.create_group("globals")
            group.attrs["dummy_iterator"] = 0
        result = bridge.wait_for_run(cfg, run_dir, 1, 4)
        assert result["complete"] is False, result
        assert result["diagnosis"] == "compiled_not_executed", result
        assert result["executed"] is False, result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_doctor_mock():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-doctor-")
    try:
        cfg = make_config(tmp)
        out = bridge.cmd_doctor(cfg, {})
        names = dict((check["name"], check["status"]) for check in out["checks"])
        assert names.get("python") == "ok", names
        assert names.get("numpy") == "ok", names
        assert names.get("h5py") == "ok", names
        assert names.get("backend") == "ok", names
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print("ok  " + test.__name__)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print("FAIL " + test.__name__ + ": " + repr(exc))
    print(str(len(tests)) + " tests, " + str(failures) + " failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
