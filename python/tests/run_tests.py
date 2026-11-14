"""Self-contained tests for the python bridge core (mock backend only).

Run: python python/tests/run_tests.py
No pytest, no labscript, no hardware: numpy and h5py are enough.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import types

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


def test_boolean_hard_limits():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-limits-bool-")
    try:
        cfg = make_config(tmp)
        limits_path = os.path.join(tmp, "limits.json")
        with open(limits_path, "w", encoding="utf-8") as handle:
            json.dump({"limits": {"KeepMOT": {"max": 0}, "do_acquire": {"min": 1}}}, handle)
        cfg["hardLimitsFile"] = limits_path
        for values, kind in (({"KeepMOT": True}, "over_max"), ({"do_acquire": False}, "under_min")):
            try:
                bridge.cmd_set(cfg, {"values": values})
            except bridge.BridgeError as exc:
                assert exc.code == "HARD_LIMIT_EXCEEDED", exc.message
                assert any(item["kind"] == kind for item in exc.detail["findings"]), exc.detail
            else:
                raise AssertionError("expected boolean hard-limit rejection for " + str(values))
        ok = bridge.cmd_set(cfg, {"values": {"KeepMOT": False, "do_acquire": True}})
        assert ok["n_shots"] >= 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_script_name_check():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-names-")
    try:
        path = os.path.join(tmp, "sequence.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("freq = 1.0\namplitude = 0.5\n# duration_old_us is a comment\n")
        info = bridge.script_name_check(path, ["freq", "old_duration_us", "amplitude"])
        assert info["checked"] is True, info
        assert info["missing"] == ["old_duration_us"], info
        assert isinstance(info["suggestions"]["old_duration_us"], list)
        missing_path = bridge.script_name_check(os.path.join(tmp, "nope.py"), ["freq"])
        assert missing_path["checked"] is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hard_limits_min_max():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-limits-mm-")
    try:
        cfg = make_config(tmp)
        limits_path = os.path.join(tmp, "limits.json")
        with open(limits_path, "w", encoding="utf-8") as handle:
            json.dump({"limits": {
                "amplitude": {"min": 0.2, "max": 0.8},
                "duration_test_us": {"min": 0.5, "max": 2.0},
            }}, handle)
        cfg["hardLimitsFile"] = limits_path
        for value, kind in ((0.1, "under_min"), (0.9, "over_max")):
            try:
                bridge.cmd_set(cfg, {"values": {"amplitude": value}})
            except bridge.BridgeError as exc:
                assert exc.code == "HARD_LIMIT_EXCEEDED", exc.message
                assert any(item["kind"] == kind for item in exc.detail["findings"]), exc.detail
            else:
                raise AssertionError("expected " + kind + " rejection")
        good = bridge.cmd_set(cfg, {"values": {"amplitude": 0.5}})
        assert good["n_shots"] >= 1
        try:
            bridge.cmd_set(cfg, {"values": {"duration_test_us": "np.linspace(0, 3, 3)"}, "raw": True})
        except bridge.BridgeError as exc:
            kinds = [item["kind"] for item in exc.detail["findings"]]
            assert "under_min_scan" in kinds and "over_max_scan" in kinds, kinds
        else:
            raise AssertionError("expected scan min/max rejection")
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
        data = bridge.cmd_data(cfg, {"run_folder": run_folder, "scan": "duration_test_us", "limit": 1})
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


def test_axis_heuristic_ignores_tuples_and_dicts():
    """runmanager does not expand tuples, so the scan-axes hint must not count them."""
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-axis-")
    try:
        cfg = make_config(tmp)
        out = bridge.cmd_set(cfg, {"values": {"pulse_shape": "(0.1, 0.2, 0.3)"}, "raw": True})
        assert out["missing"] == [], out
        status = bridge.cmd_status(cfg, {})
        assert "pulse_shape" not in status["scan_axes"], status["scan_axes"]
        assert status["n_shots"] == 1, status["n_shots"]
        assert status["n_shots_from_arrays"] == 1, status["n_shots_from_arrays"]
        # a list/array global is an axis, a dict is not
        bridge.cmd_set(cfg, {"values": {"freq": "np.arange(0.5, 6.7, 0.2)", "shape": "{'a': 1, 'b': 2}"}, "raw": True})
        status = bridge.cmd_status(cfg, {})
        assert status["scan_axes"].get("freq") == 31, status["scan_axes"]
        assert "shape" not in status["scan_axes"], status["scan_axes"]
        assert status["n_shots"] == 31, status["n_shots"]
        assert status["n_shots_from_arrays"] == 31, status["n_shots_from_arrays"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mock_error_in_globals_blocks_engage():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-mock-error-")
    try:
        cfg = make_config(tmp)
        assert bridge.cmd_status(cfg, {})["error_in_globals"] is False
        cfg["mock"]["errorInGlobals"] = True
        assert bridge.cmd_status(cfg, {})["error_in_globals"] is True
        try:
            bridge.cmd_engage(cfg, {})
        except bridge.BridgeError as exc:
            assert exc.code == "GLOBALS_ERROR", exc.code
        else:
            raise AssertionError("expected GLOBALS_ERROR")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mock_reports_shuffle():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-mock-shuffle-")
    try:
        cfg = make_config(tmp)
        assert bridge.cmd_status(cfg, {})["shuffle"] is False
        cfg["mock"]["shuffle"] = True
        assert bridge.cmd_status(cfg, {})["shuffle"] is True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_real_client_uses_labconfig_defaults():
    """Unset host/port/timeout must be resolved by runmanager.remote from labconfig."""
    class FakeClient(object):
        last = None

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.host = kwargs.get("host") or "localhost"
            self.port = kwargs.get("port") or 42523
            self.timeout = kwargs.get("timeout") or 60
            FakeClient.last = self

        def say_hello(self):
            return "hello"

        def get_shuffle(self):
            return True

    class NoShuffleClient(FakeClient):
        def get_shuffle(self):
            raise AttributeError("get_shuffle")

    fake_remote = types.ModuleType("runmanager.remote")
    fake_remote.Client = FakeClient
    fake_package = types.ModuleType("runmanager")
    fake_package.remote = fake_remote
    saved = dict((name, sys.modules.get(name)) for name in ("runmanager", "runmanager.remote"))
    sys.modules["runmanager"] = fake_package
    sys.modules["runmanager.remote"] = fake_remote
    try:
        bare = bridge.LabscriptBackend({})
        client = bare.client()
        assert client.kwargs == {}, client.kwargs
        assert bare.shuffle() is True
        explicit = bridge.LabscriptBackend({"runmanager": {"host": "10.0.0.5", "port": 43000, "timeoutS": 25}})
        assert explicit.client().kwargs == {"host": "10.0.0.5", "port": 43000, "timeout": 25.0}
        assert "10.0.0.5" in explicit.endpoint() and "config" in explicit.endpoint()
        fake_remote.Client = NoShuffleClient
        assert bridge.LabscriptBackend({}).shuffle() is None
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_wait_reports_extra_shot_files():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-extra-shots-")
    try:
        cfg = make_config(tmp)
        cfg["engage"] = {"timeoutS": 5, "pollS": 0.05, "settleS": 0.1}
        run_dir = os.path.join(tmp, "extra_run")
        os.makedirs(run_dir, exist_ok=True)
        for index in range(4):
            write_shot(os.path.join(run_dir, "shot_000%d.h5" % index), executed=True)
        result = bridge.wait_for_run(cfg, run_dir, 2, 4)
        assert result["complete"] is True, result
        assert result["n_h5"] == 4, result
        assert "Repeat all" in result["hint"], result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def write_shot(path, executed):
    """Write a shot h5 like labscript does: globals first, acquisition data later."""
    import h5py
    with h5py.File(path, "w") as handle:
        handle.create_group("globals")
        if executed:
            label = handle.create_group("images/camera/label")
            label.create_dataset("frame", data=[1.0, 2.0])


def test_wait_reports_stalled_compilation_early():
    """A compile that dies after one shot h5 must not block for the whole timeout."""
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-stalled-")
    try:
        cfg = make_config(tmp)
        cfg["engage"] = {"timeoutS": 10, "pollS": 0.05, "settleS": 0.1, "noShotS": 0.3}
        run_dir = os.path.join(tmp, "stalled_run")
        os.makedirs(run_dir, exist_ok=True)
        write_shot(os.path.join(run_dir, "shot_0000.h5"), executed=False)
        started = time.time()
        result = bridge.wait_for_run(cfg, run_dir, 64, 10)
        elapsed = time.time() - started
        assert result["diagnosis"] == "compile_stalled", result
        assert result["timed_out"] is False, result
        assert result["n_h5"] == 1, result
        assert result["n_executed"] == 0, result
        assert elapsed < 3.0, elapsed
        assert "aborted" in result["hint"], result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_wait_reports_no_shots_early():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-noshots-")
    try:
        cfg = make_config(tmp)
        cfg["engage"] = {"timeoutS": 10, "pollS": 0.05, "settleS": 0.1, "noShotS": 0.3}
        run_dir = os.path.join(tmp, "empty_run")
        os.makedirs(run_dir, exist_ok=True)
        started = time.time()
        result = bridge.wait_for_run(cfg, run_dir, 64, 10)
        elapsed = time.time() - started
        assert result["diagnosis"] == "no_shots", result
        assert result["timed_out"] is False, result
        assert result["n_h5"] == 0 and result["n_executed"] == 0, result
        assert elapsed < 3.0, elapsed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_progress_separates_files_from_executed_shots():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-progress-exec-")
    try:
        cfg = make_config(tmp)
        compiled = os.path.join(tmp, "compiled_run")
        os.makedirs(compiled, exist_ok=True)
        write_shot(os.path.join(compiled, "shot_0000.h5"), executed=True)
        write_shot(os.path.join(compiled, "shot_0001.h5"), executed=False)
        write_shot(os.path.join(compiled, "shot_0002.h5"), executed=False)
        out = bridge.cmd_progress(cfg, {"run_folder": compiled, "expected_n_shots": 3})
        assert out["n_h5"] == 3, out
        assert out["n_executed"] == 1, out
        assert out["complete"] is False, out
        done = os.path.join(tmp, "done_run")
        os.makedirs(done, exist_ok=True)
        for index in range(2):
            write_shot(os.path.join(done, "shot_000%d.h5" % index), executed=True)
        finished = bridge.cmd_progress(cfg, {"run_folder": done, "expected_n_shots": 2})
        assert finished["complete"] is True, finished
        assert finished["n_executed"] == 2, finished
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_real_backend_set_globals_survives_none_reply():
    """runmanager's set_globals returns None; the write must still be reported."""
    class FakeClient(object):
        def __init__(self):
            self.calls = []

        def set_globals(self, values, raw=False):
            self.calls.append((dict(values), raw))
            return None

    client = FakeClient()
    backend = bridge.LabscriptBackend({})
    backend._client = client
    written = backend.set_globals({"do_acquire": True, "amplitude": 0.5})
    assert written == {"do_acquire": True, "amplitude": 0.5}, written
    assert client.calls == [({"do_acquire": True, "amplitude": 0.5}, False)], client.calls


def test_cmd_set_accepts_backend_that_returns_none():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-set-none-")
    try:
        cfg = make_config(tmp)
        original = bridge.make_backend

        def make(config):
            backend = bridge.MockBackend(config)

            def set_globals(values, raw=False):
                bridge.MockBackend.set_globals(backend, values, raw=raw)
                return None

            backend.set_globals = set_globals
            return backend

        bridge.make_backend = make
        try:
            out = bridge.cmd_set(cfg, {"values": {"amplitude": 0.71}, "note": "None-returning client"})
        finally:
            bridge.make_backend = original
        assert out["missing"] == [], out
        assert out["readback"]["amplitude"] == 0.71, out
        assert out["written"] == {"amplitude": 0.71}, out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cmd_set_failure_reports_what_landed():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-set-partial-")
    try:
        cfg = make_config(tmp)
        original = bridge.make_backend

        def make(config):
            backend = bridge.MockBackend(config)

            def set_globals(values, raw=False):
                if "amplitude" in values:
                    bridge.MockBackend.set_globals(backend, {"amplitude": values["amplitude"]}, raw=raw)
                raise ValueError("Global nope not found in any active group")

            backend.set_globals = set_globals
            return backend

        bridge.make_backend = make
        try:
            bridge.cmd_set(cfg, {"values": {"amplitude": 0.71, "nope": 1}})
        except bridge.BridgeError as exc:
            assert exc.code == "SET_FAILED", exc.code
            assert "not found" in exc.message, exc.message
            assert exc.detail["snapshot"]["name"].endswith(".json"), exc.detail
            assert exc.detail["landed"]["amplitude"] == "0.71", exc.detail
            assert "labs_status" in exc.detail["hint"], exc.detail
        else:
            raise AssertionError("expected SET_FAILED")
    finally:
        bridge.make_backend = original
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


def test_status_maps_paths_from_runmanager():
    """labs_status must return paths the agent's machine can open."""
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-py-map-")
    try:
        cfg = make_config(tmp)
        cfg["pathMap"] = [{"remotePrefix": "E:\\Experiments", "localPrefix": "\\\\lab-pc\\Experiments"}]

        class FakeBackend(object):
            def get_globals(self, raw=False):
                return {}

            def labscript_file(self):
                return "E:\\Experiments\\2026\\x.py"

            def shot_output_folder(self):
                return "E:\\Experiments\\2026\\11\\12\\0177"

            def n_shots(self):
                return 1

            def error_in_globals(self):
                return False

            def run_shots(self):
                return True

            def version(self):
                return "fake"

        original = bridge.make_backend
        bridge.make_backend = lambda config: FakeBackend()
        try:
            out = bridge.cmd_status(cfg, {})
        finally:
            bridge.make_backend = original
        assert out["labscript"] == "\\\\lab-pc\\Experiments\\2026\\x.py", out["labscript"]
        assert out["shot_output_folder"] == "\\\\lab-pc\\Experiments\\2026\\11\\12\\0177", out["shot_output_folder"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_map_path_is_idempotent():
    cfg = {"pathMap": [{"remotePrefix": "E:\\Experiments", "localPrefix": "\\\\lab-pc\\Experiments"}]}
    once = bridge.map_path(cfg, "E:\\Experiments\\a\\b.h5")
    assert once == "\\\\lab-pc\\Experiments\\a\\b.h5", once
    assert bridge.map_path(cfg, once) == once


def test_progress_reports_mapped_run_folder():
    cfg = {"pathMap": [{"remotePrefix": "E:\\Experiments", "localPrefix": "\\\\lab-pc\\Experiments"}]}
    out = bridge.cmd_progress(cfg, {"run_folder": "E:\\Experiments\\nope", "expected_n_shots": 1})
    assert out["run_folder"] == "E:\\Experiments\\nope", out
    assert out["mapped_run_folder"] == "\\\\lab-pc\\Experiments\\nope", out
    assert out["folder_exists"] is False, out


def test_data_missing_run_folder():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-data-missing-")
    try:
        cfg = make_config(tmp)
        missing = os.path.join(tmp, "no_such_run")
        try:
            bridge.cmd_data(cfg, {"run_folder": missing})
        except bridge.BridgeError as exc:
            assert exc.code == "RUN_FOLDER_MISSING", exc.message
            assert exc.detail["mapped_run_folder"] == missing, exc.detail
            assert "next" in exc.detail["hint"].lower(), exc.detail
        else:
            raise AssertionError("expected RUN_FOLDER_MISSING")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_data_empty_run_folder():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-data-empty-")
    try:
        cfg = make_config(tmp)
        run_dir = os.path.join(tmp, "empty_run")
        os.makedirs(run_dir, exist_ok=True)
        out = bridge.cmd_data(cfg, {"run_folder": run_dir})
        assert out["n_h5"] == 0, out
        assert out.get("empty") is True, out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_status_drops_missing_keys():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-status-missing-")
    try:
        cfg = make_config(tmp)
        out = bridge.cmd_status(cfg, {"keys": ["amplitude", "Select_Sequence"], "raw": True})
        assert "amplitude" in out["globals"], out["globals"]
        assert "Select_Sequence" not in out["globals"], out["globals"]
        assert "Select_Sequence" not in out["raw_globals"], out["raw_globals"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_status_scan_axes():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-scan-axes-")
    try:
        cfg = make_config(tmp)
        bridge.cmd_set(cfg, {"values": {"duration_test_us": [0.0, 0.5, 1.0]}})
        out = bridge.cmd_status(cfg, {})
        assert out["scan_axes"].get("duration_test_us") == 3, out["scan_axes"]
        assert out["n_shots_from_arrays"] == 3, out["n_shots_from_arrays"]
        assert out["n_shots"] == 3, out["n_shots"]
        partial = bridge.cmd_status(cfg, {"keys": ["amplitude"]})
        assert "duration_test_us" not in partial["globals"], partial["globals"]
        assert partial["scan_axes"].get("duration_test_us") == 3, partial["scan_axes"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_data_keys_and_scan_summary():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-data-keys-")
    try:
        cfg = make_config(tmp)
        started = bridge.cmd_protocol_start(cfg, {"protocol": protocol_raw()})
        run_folder = started["engage"]["run_folder"]
        one = bridge.cmd_data(cfg, {"run_folder": run_folder, "keys": ["amplitude"]})
        assert list(one["globals"].keys()) == ["amplitude"], one["globals"]
        absent = bridge.cmd_data(cfg, {"run_folder": run_folder, "keys": ["nope"]})
        assert absent["globals"] == {}, absent["globals"]
        full = bridge.cmd_data(cfg, {"run_folder": run_folder, "keys": ["*"]})
        assert len(full["globals"]) > 1, full["globals"]
        summary = bridge.cmd_data(cfg, {"run_folder": run_folder, "scan": "duration_test_us"})
        table = summary["scan_table"]
        assert table["n_points"] == 6, table
        assert table["axis"]["points"] == 3, table["axis"]
        assert table["axis"]["min"] == 0.0 and table["axis"]["max"] == 2.0, table["axis"]
        assert table["axis"]["step"] == 1.0, table["axis"]
        assert "shot" not in table["columns"] and "duration_test_us" not in table["columns"], table["columns"]
        assert "rows" not in table, list(table.keys())
        limited = bridge.cmd_data(cfg, {"run_folder": run_folder, "scan": "duration_test_us", "limit": 2})
        assert len(limited["scan_table"]["rows"]) == 2, limited["scan_table"]
        assert limited["scan_table"]["rows"][0]["shot"], limited["scan_table"]["rows"][0]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_engage_with_unicode_note():
    import subprocess
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-unicode-")
    try:
        cfg = make_config(tmp)
        request = {"cmd": "engage", "config": cfg, "args": {"note": "中文备注：测试"}}
        env = dict((k, v) for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING"))
        proc = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(HERE), "bridge.py")],
            input=json.dumps(request).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        payload = json.loads(proc.stdout.decode("utf-8"))
        assert payload.get("ok") is True, payload
        assert payload["data"]["run_folder"], payload["data"]
        with open(os.path.join(tmp, "history", "events.jsonl"), "r", encoding="utf-8") as handle:
            assert "中文备注" in handle.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_append_event_never_raises():
    tmp = tempfile.mkdtemp(prefix="dsh-labscript-events-")
    try:
        blocked = os.path.join(tmp, "blocked")
        with open(blocked, "w", encoding="utf-8") as handle:
            handle.write("not a directory")
        cfg = {"panelHistoryDir": os.path.join(blocked, "history")}
        bridge.append_event(cfg, {"tool": "x", "note": "中文"})
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
