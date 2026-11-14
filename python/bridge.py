"""dsh-labscript bridge: one JSON request on stdin, one JSON envelope on stdout.

commands: status, set, engage, wait, abort, data, restore,
          protocol_plan, protocol_start, protocol_finalize, progress

The node plugin spawns this script with one python interpreter configured by the
user; it must be able to import numpy and h5py (the labscript environment has
both). The labscript backend additionally imports runmanager.remote lazily.
"""
import ast
import glob
import json
import os
import sys
import subprocess
import time
import datetime
import importlib
import importlib.util
import inspect
import traceback


class BridgeError(Exception):
    def __init__(self, code, message, detail=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def expand(path):
    if not path:
        return path
    return os.path.expanduser(str(path))


def jsonable(value):
    if hasattr(value, "tolist"):
        return jsonable(value.tolist())
    if hasattr(value, "item"):
        return jsonable(value.item())
    if isinstance(value, dict):
        return dict((str(k), jsonable(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)



_H5_LOCK_LOADED = False


def ensure_h5_lock():
    """Import labscript_utils.h5_lock before h5py (labscript requirement)."""
    global _H5_LOCK_LOADED
    if _H5_LOCK_LOADED:
        return True
    try:
        import labscript_utils.h5_lock  # noqa: F401
        _H5_LOCK_LOADED = True
        return True
    except Exception:
        return False


def shot_ready(path):
    """True when a shot h5 is complete enough to read (best effort, never raises)."""
    ensure_h5_lock()
    try:
        import h5py
        with h5py.File(path, "r", locking=False) as handle:
            return "globals" in handle
    except Exception:
        return False


def shot_executed(path):
    """True/False when a shot shows acquisition data; None when unknown.

    labscript compiles shot h5 files before BLACS executes them. A compiled but
    not executed shot has no images/results yet; this is how a stalled queue
    (for example BLACS paused) can be told apart from a normal run in progress.
    """
    ensure_h5_lock()
    try:
        import h5py
        with h5py.File(path, "r", locking=False) as handle:
            if "images" in handle:
                cameras = handle["images"]
                if isinstance(cameras, h5py.Group):
                    for camera in cameras:
                        camera_group = cameras[camera]
                        if isinstance(camera_group, h5py.Group):
                            for label in camera_group:
                                label_group = camera_group[label]
                                if isinstance(label_group, h5py.Group) and len(label_group) > 0:
                                    return True
            if "results" in handle:
                results = handle["results"]
                if isinstance(results, h5py.Group):
                    for name in results:
                        group = results[name]
                        if isinstance(group, h5py.Group) and (len(group) > 0 or len(group.attrs) > 0):
                            return True
        return False
    except Exception:
        return None


def executed_prefix(shots):
    """How many shot h5 files already hold acquisition data (binary search).

    labscript compiles and submits shots in file order, so executed shots are a prefix
    of the sorted file list. Opening a handful of files keeps this cheap even when the
    run folder is a network share.
    """
    low, high = 0, len(shots)
    while low < high:
        middle = (low + high) // 2
        if shot_executed(shots[middle]) is True:
            low = middle + 1
        else:
            high = middle
    return low


def globals_expansion(handle):
    """Real scan structure from a shot file.

    runmanager writes, inside each globals group, an 'expansion' group whose attrs map
    a global name to 'outer' or to a zip group name; the group's own attrs hold the
    values of the sequence global, whose length is the axis length. Every other source
    is a guess: the panel shows arrays, and two 50-point globals may be one zip axis.
    """
    import h5py
    try:
        globals_group = handle["globals"]
    except Exception:
        return None
    outer = []
    zips = {}

    def visit(name, obj):
        if name.split("/")[-1] != "expansion" or not isinstance(obj, h5py.Group):
            return
        group = obj.parent
        for key, raw in dict(obj.attrs).items():
            kind = raw.decode() if isinstance(raw, bytes) else str(raw)
            if not kind:
                continue
            size = 1
            if group is not None and key in group.attrs:
                value = group.attrs[key]
                if hasattr(value, "size") and not isinstance(value, (str, bytes)):
                    size = int(value.size) or 1
            if kind == "outer":
                outer.append({"kind": "outer", "name": str(key), "n": size})
                continue
            entry = zips.get(kind)
            if entry is None:
                entry = {"kind": "zip", "name": kind, "members": [], "n": size}
                zips[kind] = entry
            entry["members"].append(str(key))

    try:
        globals_group.visititems(visit)
    except Exception:
        return None
    axes = outer + list(zips.values())
    if not axes:
        return None
    total = 1
    for axis in axes:
        total *= int(axis.get("n") or 1)
    return {"axes": axes, "n_shots": total}

# ---------------------------------------------------------------- scan expr

_ALLOWED_CALLS = {"arange", "linspace", "array", "logspace"}
_ALLOWED_NODES = (
    ast.Expression, ast.Call, ast.Attribute, ast.Name, ast.Load, ast.Constant,
    ast.List, ast.Tuple, ast.UnaryOp, ast.USub, ast.UAdd, ast.BinOp, ast.Add,
    ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
)


def _num_arg(node):
    value = ast.literal_eval(node)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError("BAD_SCAN_EXPR", "scan expression argument is not numeric")
    return value


def eval_scan_expr(expr):
    """Evaluate the small scan-expression subset used by protocols (np.arange...)."""
    import numpy as np
    if isinstance(expr, bool):
        raise BridgeError("BAD_SCAN_EXPR", "scan expression cannot be a bool")
    if isinstance(expr, (int, float)):
        return np.atleast_1d(np.asarray(expr, dtype=float))
    if isinstance(expr, (list, tuple)):
        return np.asarray([float(v) for v in expr], dtype=float)
    text = str(expr).strip()
    if text == "":
        raise BridgeError("BAD_SCAN_EXPR", "scan expression is empty")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise BridgeError("BAD_SCAN_EXPR", "cannot parse scan expression: " + str(exc))
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise BridgeError("BAD_SCAN_EXPR", "scan expression node not allowed: " + type(node).__name__)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Attribute):
                raise BridgeError("BAD_SCAN_EXPR", "only np.* calls are allowed")
            if not isinstance(node.func.value, ast.Name) or node.func.value.id != "np":
                raise BridgeError("BAD_SCAN_EXPR", "only np.* calls are allowed")
            if node.func.attr not in _ALLOWED_CALLS:
                raise BridgeError("BAD_SCAN_EXPR", "np." + node.func.attr + " is not allowed")
            for arg in node.args:
                if isinstance(arg, ast.Constant):
                    _num_arg(arg)
                elif isinstance(arg, (ast.List, ast.Tuple)):
                    for item in arg.elts:
                        if isinstance(item, ast.Constant):
                            _num_arg(item)
                        elif not isinstance(item, (ast.UnaryOp, ast.BinOp)):
                            raise BridgeError("BAD_SCAN_EXPR", "unsupported scan expression argument")
                elif not isinstance(arg, (ast.UnaryOp, ast.BinOp)):
                    raise BridgeError("BAD_SCAN_EXPR", "unsupported scan expression argument")
    env = {"np": np, "__builtins__": {}}
    try:
        value = eval(compile(tree, "<scan>", "eval"), env)
    except Exception as exc:
        raise BridgeError("BAD_SCAN_EXPR", "scan expression evaluation failed: " + str(exc))
    return np.atleast_1d(np.asarray(value, dtype=float))


def eval_panel_value(raw):
    """Evaluate a raw panel string the way runmanager does (best effort)."""
    import numpy as np
    text = str(raw).strip()
    if text == "":
        return ""
    if "np." in text or text.startswith("["):
        try:
            arr = eval_scan_expr(text)
            return arr if arr.size > 1 else arr[0]
        except BridgeError:
            pass
    try:
        return ast.literal_eval(text)
    except Exception:
        return str(raw)


def axis_length(value):
    """Length of the axis runmanager expands from this global, or None.

    Heuristic: runmanager's automatic expansion turns list/array globals into axes
    ("outer") and passes tuples through whole, so a tuple is not an axis. Expansion
    settings (none / zip group) are not visible from runmanager.remote, so a list that
    was switched to "none" still looks like an axis here. The panel's n_shots is the
    authoritative shot count; this is only a hint.
    """
    if isinstance(value, (str, bytes, dict, tuple)):
        return None
    if not hasattr(value, "__len__"):
        return None
    ndim = getattr(value, "ndim", None)
    if ndim is not None:
        return int(value.size) if ndim >= 1 else None
    try:
        return int(len(value))
    except Exception:
        return None


def raw_repr(value):
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return repr(list(value))
    return repr(value)


def values_to_raw(values):
    """Convert JSON values to runmanager raw expressions (arrays included)."""
    out = {}
    for key, value in values.items():
        if isinstance(value, (list, tuple)):
            out[str(key)] = scan_expr_from_value(list(value))
        else:
            out[str(key)] = raw_repr(value)
    return out


def has_array_value(values):
    return any(isinstance(value, (list, tuple)) for value in values.values())


def to_raw_values(values):
    """Stringify evaluated globals into runmanager raw expressions."""
    out = {}
    for key, value in values.items():
        if isinstance(value, str):
            out[str(key)] = value
        elif isinstance(value, (list, tuple)):
            out[str(key)] = "np.array([" + ",".join(repr(float(item)) for item in value) + "])"
        else:
            out[str(key)] = raw_repr(value)
    return out


def as_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def as_limit_value(value):
    """Numeric value for hard-limit checks; booleans count as 0/1."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return as_number(value)


# ---------------------------------------------------------------- backends


def map_path(config, path):
    if not path:
        return path
    text = str(path)
    for rule in config.get("pathMap") or []:
        remote = rule.get("remotePrefix") or ""
        local = rule.get("localPrefix") or ""
        if remote and local and text.startswith(remote):
            return local + text[len(remote):]
    return text


class LabscriptBackend(object):
    def __init__(self, config):
        self.config = config
        self._client = None

    def client(self):
        if self._client is not None:
            return self._client
        try:
            from labscript_utils import h5_lock  # noqa: F401  (must precede h5py)
        except Exception:
            pass
        try:
            from runmanager.remote import Client
        except Exception as exc:
            raise BridgeError("BACKEND_UNAVAILABLE", "labscript python environment not importable: " + type(exc).__name__ + ": " + str(exc))
        # Anything left unset is resolved by runmanager.remote.Client from labscript's
        # labconfig ([servers] runmanager, [ports] runmanager, [timeouts]
        # communication_timeout), the same way the runmanager GUI does it. Explicit
        # config always wins.
        rm = self.config.get("runmanager") or {}
        kwargs = {}
        if rm.get("host"):
            kwargs["host"] = rm["host"]
        if rm.get("port"):
            kwargs["port"] = int(rm["port"])
        if rm.get("timeoutS"):
            kwargs["timeout"] = float(rm["timeoutS"])
        try:
            client = Client(**kwargs)
        except Exception as exc:
            raise BridgeError("BACKEND_UNAVAILABLE", "cannot create a runmanager client: " + type(exc).__name__ + ": " + str(exc))
        try:
            client.say_hello()
        except Exception as exc:
            raise BridgeError("BACKEND_UNAVAILABLE", "runmanager not reachable at " + str(getattr(client, "host", "?")) + ":" + str(getattr(client, "port", "?")) + ": " + str(exc))
        self._client = client
        return client

    def endpoint(self):
        """Where this client points, and whether the values came from config or labconfig."""
        rm = self.config.get("runmanager") or {}
        source = "config" if (rm.get("host") or rm.get("port")) else "labconfig"
        try:
            client = self.client()
            return str(client.host) + ":" + str(client.port) + " (" + source + ", timeout " + str(client.timeout) + "s)"
        except Exception as exc:
            return "<unknown> (" + source + "): " + str(exc)

    def version(self):
        try:
            return str(self.client().get_version())
        except Exception:
            return None

    def get_globals(self, raw=False):
        return dict(self.client().get_globals(raw=raw))

    def set_globals(self, values, raw=False):
        # runmanager's set_globals is a side-effect RPC: its handler returns None, so
        # never feed the reply to dict(). Report what was requested; the caller reads
        # the panel back to verify.
        written = self.client().set_globals(values, raw=raw)
        return dict(written) if isinstance(written, dict) else dict(values)

    def n_shots(self):
        return int(self.client().n_shots())

    def error_in_globals(self):
        return bool(self.client().error_in_globals())

    def run_shots(self):
        try:
            return bool(self.client().get_run_shots())
        except Exception:
            return None

    def shuffle(self):
        # Older runmanager versions have no get_shuffle; report unknown instead of failing.
        try:
            return bool(self.client().get_shuffle())
        except Exception:
            return None

    def labscript_file(self):
        try:
            return self.client().get_labscript_file()
        except Exception:
            return None

    def shot_output_folder(self):
        try:
            return self.client().get_shot_output_folder()
        except Exception:
            return None

    def engage(self):
        # runmanager advances the sequence index on engage: capture the folder first.
        run_folder = self.shot_output_folder()
        n_shots = self.n_shots()
        try:
            self.client().engage()
        except Exception as exc:
            raise BridgeError("ENGAGE_REJECTED", "runmanager engage failed: " + type(exc).__name__ + ": " + str(exc))
        return {"accepted": True, "n_shots": n_shots, "run_folder": run_folder, "mode": "runmanager"}

    def abort(self):
        return self.client().abort()


MOCK_DEFAULT_PANEL = {
    "do_acquire": True,
    "duration_test_us": 1.0,
    "amplitude": 0.5,
    "dummy_iterator": 0,
}


class MockBackend(object):
    """In-memory simulator used for tests, examples and hardware-free development."""

    def __init__(self, config):
        self.config = config
        mock = config.get("mock") or {}
        self.state_dir = os.path.abspath(expand(mock.get("stateDir") or "~/.dsh-labscript/mock"))
        os.makedirs(self.state_dir, exist_ok=True)
        self.panel_path = os.path.join(self.state_dir, "panel.json")
        self.state_path = os.path.join(self.state_dir, "state.json")
        self.runs_dir = os.path.join(self.state_dir, "runs")
        os.makedirs(self.runs_dir, exist_ok=True)

    # ---- state helpers
    def read_panel(self):
        if os.path.isfile(self.panel_path):
            with open(self.panel_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        else:
            data = dict((k, raw_repr(v)) for k, v in MOCK_DEFAULT_PANEL.items())
        return data

    def write_panel(self, raw_panel):
        with open(self.panel_path, "w", encoding="utf-8") as handle:
            json.dump(raw_panel, handle, indent=1)

    def next_run_folder(self):
        state = {}
        if os.path.isfile(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        index = int(state.get("last_run") or 0) + 1
        state["last_run"] = index
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        folder = os.path.join(self.runs_dir, "run_" + ("%06d" % index))
        os.makedirs(folder, exist_ok=True)
        return folder

    # ---- backend interface
    def get_globals(self, raw=False):
        panel = self.read_panel()
        if raw:
            return dict(panel)
        # Keep Python types (tuple vs list matters to the expansion rules); callers
        # jsonable() values before they leave the bridge.
        return dict((k, eval_panel_value(v)) for k, v in panel.items())

    def set_globals(self, values, raw=False):
        panel = self.read_panel()
        for key, value in values.items():
            panel[str(key)] = str(value) if raw else raw_repr(value)
        self.write_panel(panel)
        return dict((k, panel[k]) for k in values)

    def n_shots(self):
        total = 1
        for value in self.get_globals(raw=False).values():
            length = axis_length(value)
            if length is None or length <= 0:
                continue
            total *= length
        return int(total)

    def error_in_globals(self):
        # Simulator switch: mock.errorInGlobals makes the mock panel report an evaluation
        # error, so the GLOBALS_ERROR refusal path can be tested without real hardware.
        return bool((self.config.get("mock") or {}).get("errorInGlobals"))

    def shuffle(self):
        return bool((self.config.get("mock") or {}).get("shuffle"))

    def run_shots(self):
        return True

    def labscript_file(self):
        return "mock_sequence.py"

    def shot_output_folder(self):
        return self.runs_dir

    def engage(self):
        import h5py
        import numpy as np
        values = self.get_globals(raw=False)
        arrays = {}
        for key, value in values.items():
            if (axis_length(value) or 0) > 1:
                arrays[key] = np.asarray(value).ravel()
        total = 1
        for arr in arrays.values():
            total *= int(arr.size)
        folder = self.next_run_folder()
        for index in range(total):
            attrs = {}
            for key, value in values.items():
                if key in arrays:
                    arr = arrays[key]
                    attrs[key] = arr.flat[index % arr.size]
                elif isinstance(value, str):
                    attrs[key] = value
                else:
                    attrs[key] = value
            shot_path = os.path.join(folder, "shot_" + ("%04d" % index) + ".h5")
            with h5py.File(shot_path, "w") as handle:
                group = handle.create_group("globals")
                for key, value in attrs.items():
                    group.attrs[key] = value
                results = handle.create_group("results/demo")
                results.attrs["value"] = float(index)
                results.create_dataset("mean", data=float(index))
        return {"accepted": True, "n_shots": total, "run_folder": folder, "mode": "mock-labscript"}

    def abort(self):
        return {"aborted": True, "mode": "mock-labscript"}


def make_backend(config):
    backend = (config.get("backend") or "mock-labscript").lower()
    if backend == "mock-labscript":
        return MockBackend(config)
    if backend == "labscript":
        return LabscriptBackend(config)
    raise BridgeError("BAD_CONFIG", "unknown backend: " + backend)


# ---------------------------------------------------------------- snapshots / logs


def history_dir(config):
    mock = config.get("mock") or {}
    base = config.get("panelHistoryDir") or os.path.join(expand("~/.dsh-labscript"), "panel_history")
    if (config.get("backend") or "").lower() == "mock-labscript" and mock.get("historyDir"):
        base = mock["historyDir"]
    path = os.path.abspath(expand(base))
    os.makedirs(path, exist_ok=True)
    return path


def snapshot_panel(backend, config, tool, args, touched=None):
    panel = backend.get_globals(raw=True)
    folder = history_dir(config)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = stamp + "-" + tool + "-" + os.urandom(3).hex() + ".json"
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({
            "ts": now_iso(),
            "tool": tool,
            "args": jsonable(args),
            "backend": config.get("backend"),
            "raw_globals": jsonable(panel),
            "globals": jsonable(backend.get_globals(raw=False)),
            "touched": sorted(str(key) for key in touched) if touched is not None else None,
        }, handle, indent=1)
    prune_keep = 200
    files = sorted(glob.glob(os.path.join(folder, "*.json")))
    for old in files[:-prune_keep]:
        try:
            os.remove(old)
        except OSError:
            pass
    return {"file": path, "name": name, "dir": folder}


def append_event(config, event):
    try:
        folder = history_dir(config)
        path = os.path.join(folder, "events.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(jsonable(event), ensure_ascii=False) + "\n")
    except Exception as exc:
        sys.stderr.write("append_event failed: %s\n" % (exc,))


def load_snapshot(config, name):
    folder = history_dir(config)
    if not name or name == "latest":
        files = sorted(glob.glob(os.path.join(folder, "*.json")))
        if not files:
            raise BridgeError("NO_SNAPSHOT", "no panel snapshot found in " + folder)
        path = files[-1]
    else:
        path = name if os.path.isabs(name) else os.path.join(folder, name)
    if not os.path.isfile(path):
        raise BridgeError("NO_SNAPSHOT", "snapshot not found: " + str(path))
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return path, data


# ---------------------------------------------------------------- hard limits


def load_limits(config):
    path = config.get("hardLimitsFile")
    if not path:
        return None
    path = os.path.abspath(expand(path))
    if not os.path.isfile(path):
        raise BridgeError("HARD_LIMITS_UNREADABLE", "hardLimitsFile does not exist: " + path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except Exception as exc:
        raise BridgeError("HARD_LIMITS_UNREADABLE", "cannot read hardLimitsFile: " + str(exc))
    limits = {}
    for name, meta in (doc.get("limits") or {}).items():
        if isinstance(meta, dict):
            limits[str(name)] = dict(meta)
    for channel, meta in (doc.get("channels") or {}).items():
        if not isinstance(meta, dict):
            continue
        expanded = dict((k, v) for k, v in meta.items() if k != "globals")
        expanded["channel"] = channel
        for name in meta.get("globals") or []:
            limits[str(name)] = dict(expanded)
    return {"doc": doc, "limits": limits}


def check_limits(config, set_values, scan_values, expected_n_shots):
    loaded = load_limits(config)
    if loaded is None:
        return {"checked": False, "warning": "hardLimitsFile not configured"}
    findings = []
    limits = loaded["limits"]
    for key, raw in (set_values or {}).items():
        meta = limits.get(key)
        number = as_limit_value(raw)
        if meta is None or number is None:
            continue
        minimum = as_number(meta.get("min"))
        if minimum is not None and number < minimum:
            findings.append({"kind": "under_min", "key": key, "value": number, "min": minimum})
        maximum = as_number(meta.get("max"))
        if maximum is not None and number > maximum:
            findings.append({"kind": "over_max", "key": key, "value": number, "max": maximum})
    for key, expr in (scan_values or {}).items():
        meta = limits.get(key)
        if meta is None:
            continue
        minimum = as_number(meta.get("min"))
        maximum = as_number(meta.get("max"))
        if minimum is None and maximum is None:
            continue
        arr = eval_scan_expr(expr)
        if not arr.size:
            continue
        if minimum is not None and float(arr.min()) < minimum:
            findings.append({"kind": "under_min_scan", "key": key, "min_in_scan": float(arr.min()), "min": minimum})
        if maximum is not None and float(arr.max()) > maximum:
            findings.append({"kind": "over_max_scan", "key": key, "max_in_scan": float(arr.max()), "max": maximum})
    doc = loaded["doc"]
    max_shots = None
    run_limits = doc.get("run_limits") or {}
    if isinstance(run_limits, dict):
        max_shots = run_limits.get("maxTotalShots") or run_limits.get("max_total_shots")
    if isinstance(max_shots, int) and expected_n_shots is not None and expected_n_shots > max_shots:
        findings.append({"kind": "over_max_total_shots", "expected": expected_n_shots, "max": max_shots})
    if findings:
        raise BridgeError("HARD_LIMIT_EXCEEDED", "hard limit exceeded", {"findings": findings})
    return {"checked": True, "findings": []}


# ---------------------------------------------------------------- postprocess


def run_postprocess(config, plan, run_folder, scan_globals, reps):
    name = plan.get("postprocess")
    if not name:
        return None
    spec = config.get("postprocess") or {}
    entry = spec.get("entry")
    if not entry:
        raise BridgeError("POSTPROCESS_NOT_CONFIGURED", "protocol asks for postprocess but config.postprocess.entry is not set")
    for extra in spec.get("pythonPaths") or []:
        path = os.path.abspath(expand(extra))
        if path not in sys.path:
            sys.path.insert(0, path)
    if ":" in entry:
        module_name, func_name = entry.split(":", 1)
    else:
        module_name, func_name = entry.rsplit(".", 1)
    try:
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)
    except Exception as exc:
        raise BridgeError("POSTPROCESS_IMPORT_FAILED", "cannot import postprocess " + entry + ": " + str(exc))
    axes = [str(key) for key in (scan_globals or [])]
    scan_global = axes[0] if axes else None
    kwargs = {"run_folder": run_folder, "scan_global": scan_global, "reps": reps, "name": name}
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        params = {}
    if "scan_globals" in params or any(param.kind is param.VAR_KEYWORD for param in params.values()):
        kwargs["scan_globals"] = axes
    if not all(key in params for key in kwargs if key != "scan_globals"):
        # Positional-only or differently named parameters: keep the historical call.
        kwargs = {}
    try:
        result = func(**kwargs) if kwargs else func(run_folder, scan_global, reps, name)
    except Exception as exc:
        raise BridgeError("POSTPROCESS_FAILED", "postprocess raised: " + type(exc).__name__ + ": " + str(exc))
    if not isinstance(result, dict):
        raise BridgeError("POSTPROCESS_BAD_RESULT", "postprocess must return a dict, got " + type(result).__name__)
    return jsonable(result)


_MISSING = object()


def resolve_dotted(obj, dotted, default=None):
    current = obj
    for part in str(dotted).split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def finish_value(result, dotted):
    """(number, None) for a usable finish.* value, else (None, reason)."""
    resolved = resolve_dotted(result, dotted, _MISSING)
    if resolved is _MISSING:
        return None, "missing"
    value = as_number(resolved)
    if value is None:
        return None, "not a number (" + type(resolved).__name__ + ")"
    return value, None


def apply_finish(backend, config, plan, result, run_folder):
    out = {}
    skipped = []
    finish = plan.get("finish") or {}
    writes = {}
    for key, dotted in (finish.get("panel") or {}).items():
        value, reason = finish_value(result, dotted)
        if value is None:
            skipped.append({"target": "panel", "key": str(key), "dotted": str(dotted), "reason": reason})
            continue
        writes[str(key)] = value
    if writes:
        out["panel"] = jsonable(backend.set_globals(writes, raw=False))
    history = finish.get("history") or {}
    rows = []
    log_path = config.get("calibrationLog")
    if history and log_path:
        log_path = os.path.abspath(expand(log_path))
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            for key, dotted in history.items():
                value, reason = finish_value(result, dotted)
                if value is None:
                    skipped.append({"target": "history", "key": str(key), "dotted": str(dotted), "reason": reason})
                    continue
                row = {
                    "ts": now_iso(),
                    "protocol": plan.get("name") or plan.get("labscript"),
                    "run_folder": run_folder,
                    "key": str(key),
                    "value": value,
                    "postprocess": plan.get("postprocess"),
                }
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                rows.append(row)
    if rows:
        out["history"] = rows
    if skipped:
        out["skipped"] = skipped
    return out


def restore_panel(backend, config, snapshot_file, protected, only_touched=True):
    _, data = load_snapshot(config, snapshot_file) if snapshot_file else load_snapshot(config, "latest")
    protected = protected or set()
    touched = data.get("touched")
    use_raw = isinstance(backend, LabscriptBackend)
    if use_raw:
        values = to_raw_values(data.get("raw_globals") or {})
    else:
        values = data.get("globals")
        if not values:
            values = to_raw_values(data.get("raw_globals") or {})
            use_raw = True
    if not values:
        return {"restored": False, "reason": "snapshot has no values"}
    if only_touched and touched:
        wanted = set(str(key) for key in touched)
        values = dict((k, v) for k, v in values.items() if k in wanted)
    if protected:
        values = dict((k, v) for k, v in values.items() if k not in protected)
    if not values:
        return {"restored": False, "reason": "nothing to restore after filtering touched/protected keys"}
    written = backend.set_globals(values, raw=use_raw)
    readback = backend.get_globals(raw=True)
    return {
        "restored": True,
        "selective": bool(only_touched and touched),
        "keys": sorted(str(key) for key in values),
        "from": data.get("ts"),
        "written": jsonable(written),
        "readback": jsonable(readback),
    }


# ---------------------------------------------------------------- protocol plan


def normalize_plan(raw):
    if not isinstance(raw, dict):
        raise BridgeError("BAD_PROTOCOL", "protocol must be a mapping")
    plan = {}
    plan["name"] = raw.get("name") or raw.get("protocol") or ""
    plan["labscript"] = raw.get("labscript")
    plan["joint_script"] = raw.get("joint_script")
    plan["set"] = dict(raw.get("set") or {})
    plan["scan"] = dict(raw.get("scan") or {})
    plan["reps"] = raw.get("reps", 1)
    plan["engage"] = bool(raw.get("engage", True))
    plan["postprocess"] = raw.get("postprocess") or raw.get("analysis")
    finish = raw.get("finish") or {}
    plan["finish"] = {
        "restore": bool(finish.get("restore", True)),
        "panel": dict(finish.get("panel") or {}),
        "history": dict(finish.get("history") or {}),
    }
    if not plan["labscript"] and not plan["joint_script"]:
        raise BridgeError("BAD_PROTOCOL", "protocol needs labscript or joint_script")
    reps = plan["reps"]
    if isinstance(reps, bool) or not isinstance(reps, int) or reps <= 0:
        raise BridgeError("BAD_PROTOCOL", "reps must be a positive integer")
    return plan


def scan_expr_from_value(value):
    """Protocol YAML scan value -> runmanager raw expression string."""
    import numpy as np
    if isinstance(value, bool):
        raise BridgeError("BAD_PROTOCOL", "scan value cannot be a bool")
    if isinstance(value, (int, float)):
        return repr(float(value))
    if isinstance(value, str):
        arr = eval_scan_expr(value)
        if arr.size < 1:
            raise BridgeError("BAD_PROTOCOL", "scan expression is empty: " + value)
        return value
    if isinstance(value, (list, tuple)):
        if not value:
            raise BridgeError("BAD_PROTOCOL", "scan list is empty")
        numbers = [float(item) for item in value]
        if len(numbers) == 1:
            return repr(numbers[0])
        steps = [numbers[index + 1] - numbers[index] for index in range(len(numbers) - 1)]
        mean = sum(steps) / len(steps)
        tolerance = 1e-9 * max(1.0, abs(mean))
        if all(abs(step - mean) <= tolerance for step in steps):
            return "np.linspace(" + repr(numbers[0]) + ", " + repr(numbers[-1]) + ", " + str(len(numbers)) + ")"
        return "np.array([" + ",".join(repr(number) for number in numbers) + "])"
    raise BridgeError("BAD_PROTOCOL", "unsupported scan value: " + repr(value))


def resolve_plan(config, plan):
    resolved = dict(plan)
    if not resolved.get("labscript") and resolved.get("joint_script"):
        path = config.get("scriptsFile")
        if not path:
            raise BridgeError("UNKNOWN_SCRIPT", "protocol uses joint_script but config.scriptsFile is not set")
        path = os.path.abspath(expand(path))
        if not os.path.isfile(path):
            raise BridgeError("UNKNOWN_SCRIPT", "scriptsFile not found: " + path)
        with open(path, "r", encoding="utf-8") as handle:
            registry = json.load(handle)
        entry = registry.get(resolved["joint_script"])
        if not entry:
            raise BridgeError("UNKNOWN_SCRIPT", "joint_script not registered: " + str(resolved["joint_script"]))
        resolved["labscript"] = entry.get("labscript")
    scan_exprs = {}
    scan_raw = {}
    n_points = 1
    for key, value in resolved["scan"].items():
        expr = scan_expr_from_value(value)
        arr = eval_scan_expr(expr)
        if arr.size < 1:
            raise BridgeError("BAD_PROTOCOL", "scan axis is empty: " + key)
        scan_exprs[key] = arr
        scan_raw[key] = expr
        n_points *= int(arr.size)
    resolved["scan_exprs"] = scan_exprs
    resolved["scan_raw"] = scan_raw
    resolved["expected_n_shots"] = int(resolved["reps"]) * n_points
    return resolved


# ---------------------------------------------------------------- commands


def cmd_status(config, args):
    backend = make_backend(config)
    keys = args.get("keys")
    raw = bool(args.get("raw"))
    panel_values = backend.get_globals(raw=False)
    raw_values = backend.get_globals(raw=True)
    scan_axes = {}
    n_shots_from_arrays = 1
    for name, value in panel_values.items():
        size = axis_length(value)
        if size is None or size <= 1:
            continue
        scan_axes[str(name)] = size
        n_shots_from_arrays *= size
    values = dict(panel_values)
    if keys:
        values = dict((k, panel_values[k]) for k in keys if k in panel_values)
        raw_values = dict((k, raw_values[k]) for k in keys if k in raw_values)
    out = {
        "backend": config.get("backend"),
        "version": backend.version() if hasattr(backend, "version") else None,
        "labscript": map_path(config, backend.labscript_file()),
        "shot_output_folder": map_path(config, backend.shot_output_folder()),
        "globals": jsonable(values),
        "scan_axes": scan_axes,
        "n_shots_from_arrays": n_shots_from_arrays,
        "n_shots": backend.n_shots(),
        "error_in_globals": backend.error_in_globals(),
        "run_shots": backend.run_shots(),
        "shuffle": backend.shuffle() if hasattr(backend, "shuffle") else None,
        "panel_history": panel_history_info(config),
    }
    if raw:
        out["raw_globals"] = jsonable(raw_values)
    return out


def panel_history_info(config):
    folder = history_dir(config)
    files = sorted(glob.glob(os.path.join(folder, "*.json")))
    latest = os.path.basename(files[-1]) if files else None
    return {"dir": folder, "latest": latest, "count": len(files)}


def cmd_set(config, args):
    backend = make_backend(config)
    values = dict(args.get("values") or {})
    reps = args.get("reps")
    raw = bool(args.get("raw"))
    if reps is not None:
        if isinstance(reps, bool) or not isinstance(reps, int) or reps <= 0:
            raise BridgeError("BAD_ARGS", "reps must be a positive integer")
        reps_global = ((config.get("reps") or {}).get("global") or "dummy_iterator")
        values[reps_global] = "np.arange(" + str(reps) + ")" if raw else list(range(reps))
        raw = True
    if not values:
        raise BridgeError("BAD_ARGS", "values must not be empty")
    if raw:
        values = dict((k, v) if isinstance(v, str) else (k, values_to_raw({k: v})[k]) for k, v in values.items()) if False else dict((k, v if isinstance(v, str) else values_to_raw({k: v})[k]) for k, v in values.items())
    elif has_array_value(values):
        values = values_to_raw(values)
        raw = True
    check_limits(config, dict((k, v) for k, v in values.items() if not raw), dict((k, v) for k, v in values.items()) if raw else {}, None)
    snap = snapshot_panel(backend, config, "set", {"keys": sorted(values.keys()), "raw": raw, "note": args.get("note")}, touched=values.keys())
    try:
        written = backend.set_globals(values, raw=raw)
    except Exception as exc:
        detail = dict(getattr(exc, "detail", None) or {})
        detail["snapshot"] = snap
        # A failed write can still have landed (runmanager writes each key in turn),
        # so report what the panel holds now instead of guessing in the hint.
        try:
            now = backend.get_globals(raw=True)
            detail["landed"] = dict((k, jsonable(now.get(k))) for k in values)
        except Exception:
            pass
        detail["hint"] = "the write may have landed partially or not at all: read the keys back with labs_status before retrying, and only labs_restore if they are wrong"
        raise BridgeError(getattr(exc, "code", None) or "SET_FAILED", "set_globals failed: " + type(exc).__name__ + ": " + str(exc), detail)
    readback = backend.get_globals(raw=False)
    raw_readback = backend.get_globals(raw=True)
    missing = [k for k in values if k not in readback]
    out = {
        # Report what was asked for even if a backend returns something else (runmanager's
        # RPC returns nothing); the readback below carries the truth from the panel.
        "written": jsonable(written) if isinstance(written, dict) else dict((k, jsonable(v)) for k, v in values.items()),
        "readback": dict((k, jsonable(readback.get(k))) for k in values),
        "raw_readback": dict((k, jsonable(raw_readback.get(k))) for k in values),
        "missing": missing,
        "n_shots": backend.n_shots(),
        "error_in_globals": backend.error_in_globals(),
        "snapshot": snap,
    }
    append_event(config, {"ts": now_iso(), "tool": "set", "backend": config.get("backend"), "note": args.get("note"), "keys": sorted(values.keys()), "snapshot": snap["name"]})
    return out


def cmd_engage(config, args):
    backend = make_backend(config)
    if backend.error_in_globals():
        raise BridgeError("GLOBALS_ERROR", "panel globals are in an error state; refusing to engage")
    run_shots = backend.run_shots()
    if run_shots is False:
        raise BridgeError("RUN_SHOTS_OFF", "the runmanager 'Run shot(s)' checkbox is off; engage would only compile and never execute")
    n_shots = backend.n_shots()
    result = backend.engage()
    run_folder = None
    if isinstance(result, dict):
        run_folder = result.get("run_folder")
    else:
        run_folder = getattr(result, "run_folder", None)
    out = {
        "accepted": True,
        "run_folder": run_folder,
        "mapped_run_folder": map_path(config, run_folder),
        "n_shots_at_submit": n_shots,
        "expected_n_shots": int(args.get("expected_n_shots") or n_shots),
        "labscript": map_path(config, backend.labscript_file()),
        "submitted_at": now_iso(),
    }
    append_event(config, {"ts": out["submitted_at"], "tool": "engage", "backend": config.get("backend"), "note": args.get("note"), "run_folder": run_folder, "expected_n_shots": out["expected_n_shots"]})
    return out


# Same cause list for the stall branch and the timeout branch: BLACS did not run the shot.
BLACS_NO_DATA_HINT = (
    "shot files exist but show no acquisition data: BLACS may be paused, may have kept the shot out of its queue "
    "(a shot's connection table must be a subset of the lab connection table) or re-queued it after a device "
    "programming timeout. Check the BLACS GUI, then labs_abort if needed."
)


def wait_for_run(config, run_folder, expected, timeout_s):
    if not run_folder:
        raise BridgeError("BAD_ARGS", "run_folder is required")
    folder = map_path(config, run_folder)
    engage_cfg = config.get("engage") or {}
    timeout = float(timeout_s or engage_cfg.get("timeoutS") or 900)
    poll = float(engage_cfg.get("pollS") or 1.0)
    settle = float(engage_cfg.get("settleS") or 2.0)
    stall = float(engage_cfg.get("stallS") or 20.0)
    # labscript writes each shot h5 before compiling it, so a healthy compilation keeps
    # adding files. When nothing has appeared for this long the compile almost certainly
    # aborted; returning a diagnosis beats blocking for the full timeout_s.
    no_shot = float(engage_cfg.get("noShotS") or 25.0)
    want = int(expected or 0)
    start = time.time()
    last_progress = start
    stable_since = None
    compiled_since = None
    last_count = -1
    last_size = -1
    shots = []

    def result(complete, timed_out, diagnosis, elapsed, hint=None):
        payload = {
            "run_folder": run_folder,
            "mapped_run_folder": folder,
            "folder_exists": os.path.isdir(folder),
            "expected_n_shots": expected,
            "n_h5": len(shots),
            "n_executed": executed_prefix(shots),
            "complete": complete,
            "timed_out": timed_out,
            "diagnosis": diagnosis,
            "executed": shot_executed(shots[-1]) if shots else None,
            "elapsed": round(elapsed, 1),
        }
        if hint:
            payload["hint"] = hint
        return payload

    while True:
        shots = sorted(glob.glob(os.path.join(folder, "**", "*.h5"), recursive=True))
        count = len(shots)
        total = sum(os.path.getsize(p) for p in shots) if shots else 0
        if count != last_count or total != last_size:
            last_progress = time.time()
        ready = False
        if count >= (want or 0) and count == last_count and total == last_size and shots:
            ready = shot_ready(shots[-1])
        executed = None
        if ready:
            executed = shot_executed(shots[-1])
        if ready and executed is True:
            compiled_since = None
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= settle:
                hint = None
                if want and len(shots) != want:
                    hint = ("finished with " + str(len(shots)) + " shot files for expected_n_shots=" + str(want) +
                            ": the panel's n_shots changed after submit, or shots were re-run (BLACS Repeat all / "
                            "Repeat last re-executes existing shot files and does not create new ones).")
                return result(True, False, "complete", time.time() - start, hint)
        else:
            stable_since = None
            if ready and executed is False:
                if compiled_since is None:
                    compiled_since = time.time()
                elif time.time() - compiled_since >= stall:
                    return result(False, False, "compiled_not_executed", time.time() - start, BLACS_NO_DATA_HINT)
            else:
                compiled_since = None
        last_count = count
        last_size = total
        quiet = time.time() - last_progress
        if want and count < want and quiet >= no_shot:
            if count == 0:
                diagnosis = "no_shots"
                hint = ("no shot h5 appeared in " + str(int(no_shot)) + " s: the compilation probably aborted "
                        "(runmanager shows the error) or BLACS is not running. Check runmanager, call labs_wait "
                        "again to keep waiting, or labs_abort.")
            else:
                diagnosis = "compile_stalled"
                hint = ("compilation stopped at " + str(count) + " of " + str(want) + " shot h5 files and "
                        "nothing changed for " + str(int(no_shot)) + " s: it probably aborted (runmanager shows "
                        "the error). Check runmanager, call labs_wait again to keep waiting, or labs_abort.")
            return result(False, False, diagnosis, time.time() - start, hint)
        if time.time() - start >= timeout:
            if count == 0:
                diagnosis = "no_shots"
                hint = "no shot h5 appeared: check that the run was accepted, that BLACS is running, and that pathMap points at the run folder."
            elif count < want:
                diagnosis = "compiling"
                hint = "only " + str(count) + " of " + str(expected) + " shots appeared before the timeout; the run may still be compiling or blocked."
            elif executed is False:
                diagnosis = "compiled_not_executed"
                hint = BLACS_NO_DATA_HINT
            else:
                diagnosis = "partial"
                hint = "shots appeared but the run was not confirmed complete before the timeout."
            return result(False, True, diagnosis, time.time() - start, hint)
        time.sleep(poll)


def cmd_wait(config, args):
    return wait_for_run(config, args.get("run_folder"), int(args.get("expected_n_shots") or 0), args.get("timeout_s"))


def cmd_abort(config, args):
    backend = make_backend(config)
    backend_name = (config.get("backend") or "").lower()
    if backend_name == "labscript":
        note = ("labscript abort stops compiling/submitting new shots. Shots that are already compiled and queued "
                "in BLACS, or currently running, are not stopped by it. This is not a hardware interlock.")
    else:
        note = "mock backend abort is a no-op."
    result = backend.abort()
    append_event(config, {"ts": now_iso(), "tool": "abort", "backend": config.get("backend"), "note": note, "result": jsonable(result)})
    return {"aborted": True, "note": note, "result": jsonable(result)}


def cmd_progress(config, args):
    run_folder = args.get("run_folder")
    expected = int(args.get("expected_n_shots") or 0)
    folder = map_path(config, run_folder)
    shots = sorted(glob.glob(os.path.join(folder, "**", "*.h5"), recursive=True)) if folder else []
    # n_h5 counts files, which labscript writes before compiling each shot; n_executed
    # counts the shots that actually acquired data. A run is only complete when the
    # last shot has acquisition data, not merely when the file count matches.
    executed = executed_prefix(shots)
    complete = bool(expected and len(shots) >= expected and executed == len(shots))
    return {
        "run_folder": run_folder,
        "mapped_run_folder": folder,
        "folder_exists": bool(folder) and os.path.isdir(folder),
        "n_h5": len(shots),
        "n_executed": executed,
        "expected_n_shots": expected,
        "complete": complete,
    }



def script_name_check(script_path, keys):
    """Check that every protocol key appears as an identifier in the labscript source.

    Heuristic by design: tokenize strips comments/strings; dynamic globals lookups
    are not detected. Returns checked=False when the source cannot be read.
    """
    if not script_path:
        return {"checked": False, "reason": "no labscript file", "script": None, "missing": [], "suggestions": {}}
    path = os.path.abspath(expand(script_path))
    if not os.path.isfile(path):
        return {"checked": False, "reason": "labscript file not readable: " + path, "script": script_path, "missing": [], "suggestions": {}}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            source = handle.read()
        import io
        import tokenize
        names = set()
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.NAME:
                names.add(token.string)
    except Exception as exc:
        return {"checked": False, "reason": "cannot parse labscript: " + str(exc), "script": script_path, "missing": [], "suggestions": {}}
    import difflib
    missing = []
    suggestions = {}
    for key in keys:
        if key in names:
            continue
        missing.append(key)
        suggestions[key] = difflib.get_close_matches(str(key), sorted(names), n=3, cutoff=0.6)
    return {"checked": True, "script": script_path, "missing": missing, "suggestions": suggestions}


def check_protocol_names(config, plan):
    backend = make_backend(config)
    loaded = backend.labscript_file()
    if not loaded:
        return {"checked": False, "reason": "runmanager did not report a labscript file", "script": None, "missing": [], "suggestions": {}}
    reps_global = ((config.get("reps") or {}).get("global") or "dummy_iterator")
    keys = sorted((set(plan.get("set") or {}) | set(plan.get("scan") or {})) - {reps_global})
    info = script_name_check(map_path(config, loaded), keys)
    info["script"] = loaded
    return info

def cmd_data(config, args):
    ensure_h5_lock()
    import h5py
    run_folder = args.get("run_folder")
    if not run_folder:
        raise BridgeError("BAD_ARGS", "run_folder is required")
    folder = map_path(config, run_folder)
    if not os.path.isdir(folder):
        raise BridgeError("RUN_FOLDER_MISSING", "run folder does not exist: " + str(folder), {
            "run_folder": run_folder,
            "mapped_run_folder": folder,
            "hint": "runmanager's shot output folder is the folder the NEXT run will use; it does not exist until engage. Pass a historical run folder explicitly.",
        })
    shots = sorted(glob.glob(os.path.join(folder, "**", "*.h5"), recursive=True))
    out = {"run_folder": run_folder, "mapped_run_folder": folder, "n_h5": len(shots)}
    if shots:
        out["n_executed"] = executed_prefix(shots)
    if not shots:
        out["empty"] = True
        return out
    scan = args.get("scan")
    with h5py.File(shots[0], "r", locking=False) as handle:
        globals_attrs = dict(handle["globals"].attrs) if "globals" in handle else {}
        if args.get("globals", True):
            selected = jsonable(globals_attrs)
            keys = args.get("keys")
            if keys and list(map(str, keys)) != ["*"]:
                wanted = [str(key) for key in keys]
                selected = dict((key, selected[key]) for key in wanted if key in selected)
            out["globals"] = selected
        if args.get("results"):
            layout = {}
            if "results" in handle:
                for group_name in handle["results"]:
                    group = handle["results"][group_name]
                    if isinstance(group, h5py.Group):
                        layout[group_name] = sorted(list(group.keys()) + list(group.attrs.keys()))
            out["results_layout"] = layout
        expansion = globals_expansion(handle)
        if expansion:
            out["expansion"] = expansion
    if scan:
        rows = []
        for path in shots:
            with h5py.File(path, "r", locking=False) as handle:
                attrs = handle["globals"].attrs if "globals" in handle else {}
                row = {"shot": os.path.basename(path)}
                if scan in attrs:
                    row[scan] = jsonable(attrs[scan])
                if "results" in handle:
                    for group_name in handle["results"]:
                        group = handle["results"][group_name]
                        if not isinstance(group, h5py.Group):
                            continue
                        for key in list(group.attrs.keys()):
                            value = as_number(group.attrs[key])
                            if value is not None:
                                row[group_name + "." + key] = value
                        for key in group.keys():
                            item = group[key]
                            if isinstance(item, h5py.Dataset) and item.size <= 16:
                                try:
                                    value = item[()]
                                    if hasattr(value, "item"):
                                        value = value.item()
                                except Exception:
                                    continue
                                number = as_number(value)
                                if number is not None:
                                    row[group_name + "." + key] = number
                rows.append(row)
        axis_values = [float(row[scan]) for row in rows if isinstance(row.get(scan), (int, float))]
        unique = sorted(set(axis_values))
        steps = [round(b - a, 9) for a, b in zip(unique, unique[1:])]
        columns = sorted(set(key for row in rows for key in row) - {"shot", scan})
        table = {
            "scan": scan,
            "n_points": len(rows),
            "axis": {
                "points": len(unique),
                "min": unique[0] if unique else None,
                "max": unique[-1] if unique else None,
                "step": (sorted(steps)[len(steps) // 2] if steps else None),
            },
            "columns": columns,
        }
        limit = args.get("limit")
        if limit:
            try:
                limit = max(1, min(20, int(limit)))
            except Exception:
                limit = 0
            if limit:
                table["rows"] = rows[:limit]
        out["scan_table"] = table
    return out


def cmd_restore(config, args):
    backend = make_backend(config)
    snap = args.get("snapshot")
    full = bool(args.get("full"))
    path, data = load_snapshot(config, snap)
    touched = data.get("touched")
    use_raw = isinstance(backend, LabscriptBackend)
    if use_raw:
        values = to_raw_values(data.get("raw_globals") or {})
    else:
        values = data.get("globals") or to_raw_values(data.get("raw_globals") or {})
        if not data.get("globals"):
            use_raw = True
    if not full and touched:
        wanted = set(str(key) for key in touched)
        values = dict((k, v) for k, v in values.items() if k in wanted)
    written = backend.set_globals(values, raw=use_raw)
    readback = backend.get_globals(raw=True)
    append_event(config, {"ts": now_iso(), "tool": "restore", "backend": config.get("backend"), "snapshot": os.path.basename(path)})
    return {
        "restored_from": {"file": path, "ts": data.get("ts"), "tool": data.get("tool")},
        "selective": bool(not full and touched),
        "keys": sorted(str(key) for key in values),
        "written": jsonable(written),
        "readback": jsonable(readback),
    }


def cmd_protocol_plan(config, args):
    plan = resolve_plan(config, normalize_plan(args.get("protocol") or {}))
    limits = check_limits(config, plan["set"], dict((k, v) for k, v in plan["scan_raw"].items()), plan["expected_n_shots"])
    public = {
        "name": plan.get("name"),
        "labscript": plan.get("labscript"),
        "joint_script": plan.get("joint_script"),
        "set": jsonable(plan["set"]),
        "scan": dict((k, {"expr": plan["scan_raw"][k], "n": int(plan["scan_exprs"][k].size)}) for k in plan["scan"]),
        "reps": plan["reps"],
        "engage": plan["engage"],
        "postprocess": plan.get("postprocess"),
        "finish": jsonable(plan["finish"]),
        "expected_n_shots": plan["expected_n_shots"],
    }
    warnings = []
    if not plan.get("postprocess"):
        warnings.append("protocol has no postprocess")
    name_check = check_protocol_names(config, plan)
    return {"plan": public, "hard_limits": limits, "name_check": name_check, "warnings": warnings}


def cmd_protocol_start(config, args):
    plan = resolve_plan(config, normalize_plan(args.get("protocol") or {}))
    backend = make_backend(config)
    check_limits(config, plan["set"], dict((k, v) for k, v in plan["scan_raw"].items()), plan["expected_n_shots"])
    reps_global = ((config.get("reps") or {}).get("global") or "dummy_iterator")
    touched = set(plan["set"]) | set(plan["scan"]) | {reps_global}
    snap = snapshot_panel(backend, config, "protocol", {"name": plan.get("name"), "labscript": plan.get("labscript"), "note": args.get("note")}, touched=touched)
    steps = {}
    try:
        if plan["set"]:
            steps["set"] = jsonable(backend.set_globals(plan["set"], raw=False))
        scan_values = dict((k, expr) for k, expr in plan["scan_raw"].items())
        scan_values[reps_global] = "np.arange(" + str(plan["reps"]) + ")"
        steps["scan"] = jsonable(backend.set_globals(scan_values, raw=True))
    except BridgeError as exc:
        detail = dict(exc.detail or {})
        detail["snapshot"] = snap
        detail["hint"] = "protocol panel write failed; the panel may be partially written. Use labs_restore to undo."
        raise BridgeError(exc.code, exc.message, detail)
    except Exception as exc:
        raise BridgeError("PROTOCOL_WRITE_FAILED", "protocol panel write failed: " + type(exc).__name__ + ": " + str(exc), {
            "snapshot": snap,
            "hint": "the panel may be partially written; use labs_restore to undo",
        })
    panel_n_shots = backend.n_shots()
    if panel_n_shots != plan["expected_n_shots"]:
        raise BridgeError("N_SHOTS_MISMATCH", "panel n_shots does not match protocol plan", {"panel_n_shots": panel_n_shots, "expected": plan["expected_n_shots"]})
    if not plan["engage"]:
        return {"plan": public_plan(plan), "snapshot": snap, "steps": steps, "engage": None, "n_shots_at_submit": panel_n_shots}
    result = backend.engage()
    run_folder = result.get("run_folder") if isinstance(result, dict) else getattr(result, "run_folder", None)
    out = {
        "plan": public_plan(plan),
        "snapshot": snap,
        "steps": steps,
        "engage": {"accepted": True, "run_folder": run_folder, "mapped_run_folder": map_path(config, run_folder), "n_shots_at_submit": panel_n_shots},
        "n_shots_at_submit": panel_n_shots,
        "expected_n_shots": plan["expected_n_shots"],
        "submitted_at": now_iso(),
    }
    append_event(config, {"ts": out["submitted_at"], "tool": "protocol", "backend": config.get("backend"), "note": args.get("note"), "protocol": plan.get("name"), "run_folder": run_folder, "expected_n_shots": plan["expected_n_shots"]})
    return out


def public_plan(plan):
    return {
        "name": plan.get("name"),
        "labscript": plan.get("labscript"),
        "joint_script": plan.get("joint_script"),
        "set": jsonable(plan["set"]),
        "scan": dict((k, {"expr": plan["scan"][k], "n": int(plan["scan_exprs"][k].size)}) for k in plan["scan"]),
        "reps": plan["reps"],
        "engage": plan["engage"],
        "postprocess": plan.get("postprocess"),
        "finish": jsonable(plan["finish"]),
        "expected_n_shots": plan["expected_n_shots"],
    }


def cmd_protocol_finalize(config, args):
    plan = resolve_plan(config, normalize_plan(args.get("protocol") or {}))
    run_folder = args.get("run_folder")
    if not run_folder:
        raise BridgeError("BAD_ARGS", "run_folder is required")
    backend = make_backend(config)
    out = {"run_folder": run_folder}
    result = None
    if plan.get("postprocess"):
        scan_globals = [str(key) for key in plan["scan"]]
        result = run_postprocess(config, plan, map_path(config, run_folder), scan_globals, plan["reps"])
        out["postprocess"] = result
    if plan.get("finish"):
        out["finish"] = apply_finish(backend, config, plan, result or {}, run_folder)
        if plan["finish"].get("restore"):
            protected = set((plan["finish"].get("panel") or {}).keys())
            out["restore"] = restore_panel(backend, config, (args.get("snapshot") or {}).get("file"), protected)
    append_event(config, {"ts": now_iso(), "tool": "protocol_finalize", "backend": config.get("backend"), "note": args.get("note"), "protocol": plan.get("name"), "run_folder": run_folder, "postprocess": bool(result), "restore": (out.get("restore") or {}).get("restored")})
    return out


def _add_check(checks, name, status, detail, hint=""):
    checks.append({"name": name, "status": status, "detail": str(detail), "hint": hint})


def probe_child(config):
    """Run cmd_probe in a child interpreter and return its data.

    Importing labscript_utils installs a process-wide import hook (its double-import
    denier), and labs_doctor has already imported h5py by the time it asks for the
    panel; in-process imports after that fail with a misleading KeyError. Every tool
    call already runs in its own process, so probing there reports the environment the
    tools actually use.
    """
    request = json.dumps({"cmd": "probe", "config": config, "args": {}}, ensure_ascii=False)
    timeout = 15 + float((config.get("runmanager") or {}).get("timeoutS") or 60)
    try:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__)],
            input=request.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except Exception as exc:
        raise BridgeError("BACKEND_UNAVAILABLE", "child probe did not run: " + type(exc).__name__ + ": " + str(exc))
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace").strip() or "{}")
    except Exception:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        raise BridgeError("BACKEND_UNAVAILABLE", "child probe printed no JSON" + (": " + tail[0] if tail else ""))
    if not payload.get("ok"):
        error = payload.get("error") or {}
        raise BridgeError(str(error.get("code") or "BACKEND_UNAVAILABLE"), str(error.get("message") or "child probe failed"))
    return payload.get("data") or {}


def cmd_probe(config, args):
    """Panel facts for labs_doctor; labs_doctor runs this in a child process."""
    backend = make_backend(config)
    if not isinstance(backend, LabscriptBackend):
        status = cmd_status(config, {})
        return {"backend": config.get("backend"), "n_shots": status.get("n_shots"), "labscript": status.get("labscript")}
    client = backend.client()
    version = client.get_version() if hasattr(client, "get_version") else "unknown"
    return {
        "backend": config.get("backend"),
        "version": str(version),
        "endpoint": backend.endpoint(),
        "n_shots": backend.n_shots(),
        "run_shots": backend.run_shots(),
        "error_in_globals": backend.error_in_globals(),
        "shot_output_folder": backend.shot_output_folder(),
    }


def cmd_doctor(config, args):
    """Read-only environment/config diagnostic (no panel writes, no engage)."""
    checks = []
    _add_check(checks, "python", "ok", sys.version.split()[0])
    try:
        import numpy as np
        _add_check(checks, "numpy", "ok", np.__version__)
    except Exception as exc:
        _add_check(checks, "numpy", "fail", str(exc), "install numpy (the labscript-suite environment has it)")
    try:
        import h5py
        _add_check(checks, "h5py", "ok", h5py.__version__)
    except Exception as exc:
        _add_check(checks, "h5py", "fail", str(exc), "install h5py")

    backend = (config.get("backend") or "mock-labscript").lower()
    _add_check(checks, "backend", "ok", backend)
    _add_check(checks, "python-executable", "ok", sys.executable)
    if backend == "labscript":
        # Presence only: importing labscript in this process has side effects (its
        # double-import denier walks the stack and leaves half-initialised modules
        # behind), which used to break the runmanager checks below and turn a working
        # environment into a false negative. Nothing here imports labscript itself.
        missing = []
        for module in ("labscript", "runmanager", "labscript_utils", "zprocess"):
            try:
                present = importlib.util.find_spec(module) is not None
            except Exception as exc:
                missing.append(module + ": " + type(exc).__name__ + ": " + str(exc))
                continue
            if not present:
                missing.append(module + ": not installed")
        if missing:
            _add_check(checks, "labscript-suite", "fail", "; ".join(missing), "use the labscript-suite python environment")
        else:
            _add_check(checks, "labscript-suite", "ok", "labscript / runmanager / labscript_utils / zprocess installed")
        try:
            data = probe_child(config)
            _add_check(checks, "runmanager", "ok", str(data.get("version")) + " at " + str(data.get("endpoint")))
            _add_check(checks, "panel", "ok", "n_shots=" + str(data.get("n_shots")) + ", run_shots=" + str(data.get("run_shots")) + ", error_in_globals=" + str(data.get("error_in_globals")))
            _add_check(checks, "shot-output", "ok", str(data.get("shot_output_folder")))
        except Exception as exc:
            _add_check(checks, "runmanager", "fail", type(exc).__name__ + ": " + str(exc), "check runmanager host/port and the zprocess shared secret in labconfig")
    else:
        try:
            probe = make_backend(config)
            status = cmd_status(config, {})
            _add_check(checks, "backend-status", "ok", "n_shots=" + str(status.get("n_shots")) + ", labscript=" + str(status.get("labscript")))
        except Exception as exc:
            _add_check(checks, "backend-status", "fail", str(exc), "check the mock/simulator configuration")

    protocols_dir = config.get("protocolsDir")
    if protocols_dir:
        path = os.path.abspath(expand(protocols_dir))
        _add_check(checks, "protocols-dir", "ok" if os.path.isdir(path) else "warn", path, "" if os.path.isdir(path) else "directory does not exist")
    scripts_file = config.get("scriptsFile")
    if scripts_file and os.path.isfile(os.path.abspath(expand(scripts_file))):
        try:
            with open(os.path.abspath(expand(scripts_file)), "r", encoding="utf-8") as handle:
                json.load(handle)
            _add_check(checks, "scripts-file", "ok", scripts_file)
        except Exception as exc:
            _add_check(checks, "scripts-file", "fail", str(exc), "scriptsFile must be JSON")
    hard_limits = config.get("hardLimitsFile")
    if hard_limits:
        try:
            with open(os.path.abspath(expand(hard_limits)), "r", encoding="utf-8") as handle:
                json.load(handle)
            _add_check(checks, "hard-limits-file", "ok", hard_limits)
        except Exception as exc:
            _add_check(checks, "hard-limits-file", "warn", str(exc), "hard limits are not enforced if this file cannot be read")
    try:
        base = history_dir(config)
        _add_check(checks, "state-dir", "ok", base)
    except Exception as exc:
        _add_check(checks, "state-dir", "fail", str(exc), "cannot create the panel history directory")

    spec = config.get("postprocess") or {}
    entry = spec.get("entry")
    if entry:
        for extra in spec.get("pythonPaths") or []:
            extra_path = os.path.abspath(expand(extra))
            if extra_path not in sys.path:
                sys.path.insert(0, extra_path)
        try:
            module_name, _, function_name = entry.partition(":")
            module = importlib.import_module(module_name)
            if not hasattr(module, function_name):
                raise AttributeError("module has no attribute " + function_name)
            _add_check(checks, "postprocess", "ok", entry)
        except Exception as exc:
            _add_check(checks, "postprocess", "fail", str(exc), "postprocess.entry must be module:function and importable from pythonPaths")
    return {"checks": checks, "backend": backend, "python": sys.version.split()[0]}


HANDLERS = {
    "status": cmd_status,
    "set": cmd_set,
    "engage": cmd_engage,
    "wait": cmd_wait,
    "abort": cmd_abort,
    "progress": cmd_progress,
    "data": cmd_data,
    "restore": cmd_restore,
    "protocol_plan": cmd_protocol_plan,
    "protocol_start": cmd_protocol_start,
    "protocol_finalize": cmd_protocol_finalize,
    "doctor": cmd_doctor,
    "probe": cmd_probe,
}


def main():
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        request = json.load(sys.stdin)
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": {"code": "BAD_REQUEST", "message": "invalid JSON on stdin: " + str(exc)}}))
        return 0
    command = request.get("cmd") or ""
    handler = HANDLERS.get(command)
    if handler is None:
        sys.stdout.write(json.dumps({"ok": False, "error": {"code": "UNKNOWN_COMMAND", "message": "unknown bridge command: " + str(command)}}))
        return 0
    try:
        data = handler(request.get("config") or {}, request.get("args") or {})
        sys.stdout.write(json.dumps({"ok": True, "data": jsonable(data)}, ensure_ascii=True))
        return 0
    except BridgeError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message, "detail": jsonable(exc.detail)}}, ensure_ascii=True))
        return 0
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        sys.stdout.write(json.dumps({"ok": False, "error": {"code": "INTERNAL", "message": type(exc).__name__ + ": " + str(exc)}}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
