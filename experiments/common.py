"""Shared tooling of the experiments: one directory per run, never overwritten, with everything needed to reproduce it.

    experiments/runs/<YYYYmmdd-HHMMSS>_<name>_<git short hash>[_k]/
        config.json        what was run: script, arguments, parameters
        environment.json   git commit (+ dirty flag and diff hash), Python and package versions, host, CPU, RAM, GPU,
                           Slurm job, thread limits, input data files with size and modification time
        log.txt            everything the script printed
        metrics.json       the results (list of records) + summaries
        metrics.csv        the same records, flat
        resources.json     wall time, CPU time, peak memory, energy (measured or estimated, said which) per phase
        *.png              figures

Energy: Intel RAPL counters when readable (package + DRAM, whole socket: an upper bound when the node is shared), else an
estimate = CPU seconds x (CPU TDP / hardware threads) — labelled ``estimated``. GPU energy integrates ``nvidia-smi``
power readings sampled every 0.2 s. The estimate is coarse (TDP is a maximum) and is used only to compare
configurations measured the same way.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import resource
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "experiments" / "runs"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

# TDP (W) per socket for the CPUs of the cluster; used only for the energy estimate when RAPL is not readable
TDP_W = {"Xeon(R) Gold 6430": 270, "Xeon(R) Platinum 8480": 350, "Xeon(R) Platinum 8480C": 350, "Xeon(R) Platinum 8468": 350}


def _sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20, cwd=REPO).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def cpu_info() -> dict:
    model, threads, sockets = "", os.cpu_count() or 0, 1
    try:
        txt = Path("/proc/cpuinfo").read_text()
        model = next((l.split(":", 1)[1].strip() for l in txt.splitlines() if l.startswith("model name")), "")
        sockets = len({l.split(":", 1)[1].strip() for l in txt.splitlines() if l.startswith("physical id")}) or 1
    except OSError:
        pass
    tdp = next((w for k, w in TDP_W.items() if k in model), None)
    avail = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else threads
    return {"model": model, "hardware_threads": threads, "sockets": sockets, "threads_available_to_job": avail,
            "tdp_w_per_socket": tdp, "watts_per_thread_estimate": round(tdp * sockets / threads, 2) if tdp and threads else None}


def mem_info() -> dict:
    try:
        kv = {l.split(":")[0]: int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines()}
        return {"total_gb": round(kv["MemTotal"] / 2**20, 1), "available_gb": round(kv["MemAvailable"] / 2**20, 1)}
    except (OSError, KeyError, ValueError):
        return {}


def gpu_info() -> list[dict]:
    out = _sh(["nvidia-smi", "--query-gpu=index,name,memory.total,power.limit,driver_version", "--format=csv,noheader,nounits"])
    gpus = []
    for l in out.splitlines():
        p = [x.strip() for x in l.split(",")]
        if len(p) == 5:
            gpus.append({"index": p[0], "name": p[1], "memory_mb": p[2], "power_limit_w": p[3], "driver": p[4]})
    return gpus


def packages() -> dict:
    from importlib import metadata

    names = ["numpy", "scipy", "shapely", "networkx", "geopandas", "pyproj", "rasterio", "torch", "fastapi", "scikit-learn", "matplotlib"]
    out = {}
    for n in names:
        try:
            out[n] = metadata.version(n)
        except metadata.PackageNotFoundError:
            pass
    return out


def git_state() -> dict:
    diff = _sh(["git", "diff", "HEAD"])
    return {"commit": _sh(["git", "rev-parse", "HEAD"]), "branch": _sh(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
            "dirty": bool(_sh(["git", "status", "--porcelain", "--untracked-files=no"])),
            "diff_sha256": hashlib.sha256(diff.encode()).hexdigest()[:16] if diff else None}


def file_ref(p: str | Path) -> dict:
    p = Path(p)
    if not p.is_absolute():
        p = REPO / p
    try:
        st = p.stat()
        return {"path": str(p.relative_to(REPO)) if p.is_relative_to(REPO) else str(p), "bytes": st.st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}
    except OSError:
        return {"path": str(p), "missing": True}


# ------------------------------------------------------------------------------------------------- energy / resources

class _Rapl:
    def __init__(self):
        self.files = []
        base = Path("/sys/class/powercap")
        for d in sorted(base.glob("intel-rapl:*")):
            f = d / "energy_uj"
            try:
                int(f.read_text())
                self.files.append((f, int((d / "max_energy_range_uj").read_text())))
            except (OSError, ValueError):
                pass

    @property
    def ok(self) -> bool:
        return bool(self.files)

    def read(self) -> list[int]:
        return [int(f.read_text()) for f, _ in self.files]

    def joules(self, a: list[int], b: list[int]) -> float:
        return sum(((y - x) % m) / 1e6 for (x, y, (_, m)) in zip(a, b, self.files))


class _GpuPower(threading.Thread):
    def __init__(self, period: float = 0.2):
        super().__init__(daemon=True)
        self.period, self.stop_flag, self.joules, self.samples, self.peak_w = period, False, 0.0, 0, 0.0
        self.mem_peak_mb = 0.0

    def run(self):
        idx = os.environ.get("CUDA_VISIBLE_DEVICES", "")
        q = ["nvidia-smi", "--query-gpu=power.draw,memory.used", "--format=csv,noheader,nounits"] + (["-i", idx] if idx and idx != "" else [])
        last = time.perf_counter()
        while not self.stop_flag:
            out = _sh(q)
            now = time.perf_counter()
            try:
                vals = [[float(x) for x in l.split(",")] for l in out.splitlines() if l.strip()]
                w = sum(v[0] for v in vals)
                self.joules += w * (now - last)
                self.peak_w = max(self.peak_w, w)
                self.mem_peak_mb = max(self.mem_peak_mb, sum(v[1] for v in vals))
                self.samples += 1
            except (ValueError, IndexError):
                pass
            last = now
            time.sleep(self.period)


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def current_rss_mb() -> float:
    try:
        for l in Path("/proc/self/status").read_text().splitlines():
            if l.startswith("VmRSS"):
                return int(l.split()[1]) / 1024
    except OSError:
        pass
    return float("nan")


class Run:
    """An experiment run: ``with Run("name", config) as R: ... R.record({...}); with R.phase("x"): ...``."""

    def __init__(self, name: str, config: dict, gpu: bool = False):
        self.name, self.config, self.gpu = name, config, gpu
        self.records: list[dict] = []
        self.phases: dict[str, dict] = {}
        self.summary: dict = {}
        g = git_state()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = RUNS / f"{stamp}_{name}_{(g['commit'] or 'nogit')[:7]}"
        d, k = base, 1
        while d.exists():  # never overwrite
            d = Path(f"{base}_{k}")
            k += 1
        d.mkdir(parents=True)
        self.dir = d
        self.cpu = cpu_info()
        self.env = {"git": g, "python": platform.python_version(), "packages": packages(), "host": socket.gethostname(),
                    "cpu": self.cpu, "memory": mem_info(), "gpus": gpu_info(), "slurm_job": os.environ.get("SLURM_JOB_ID"),
                    "slurm_cpus": os.environ.get("SLURM_CPUS_PER_TASK"),
                    "threads": {k: os.environ.get(k) for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")},
                    "command": " ".join(sys.argv), "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.rapl = _Rapl()
        self.env["energy_method"] = "RAPL (package+DRAM, whole sockets)" if self.rapl.ok else "estimated from CPU time x TDP per thread"
        (d / "config.json").write_text(json.dumps(config, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        if g["dirty"]:  # uncommitted code: keep the exact diff with the run
            (d / "code.diff").write_text(_sh(["git", "diff", "HEAD"]) + "\n", encoding="utf-8")
        (d / "environment.json").write_text(json.dumps(self.env, indent=1, default=str), encoding="utf-8")
        self._log = open(d / "log.txt", "a", encoding="utf-8")

    def __enter__(self):
        self._t0, self._c0 = time.perf_counter(), time.process_time()
        self._r0 = self.rapl.read() if self.rapl.ok else None
        self._gp = _GpuPower() if self.gpu else None
        if self._gp:
            self._gp.start()
        self.log(f"run {self.dir.name}")
        return self

    def log(self, *a):
        msg = " ".join(str(x) for x in a)
        print(msg, flush=True)
        self._log.write(time.strftime("%H:%M:%S ") + msg + "\n")
        self._log.flush()

    def _energy(self, cpu_s: float, r0, r1) -> dict:
        if r0 is not None and r1 is not None:
            return {"cpu_energy_j": round(self.rapl.joules(r0, r1), 1), "cpu_energy_method": "RAPL"}
        w = self.cpu.get("watts_per_thread_estimate")
        return {"cpu_energy_j": round(cpu_s * w, 1) if w else None, "cpu_energy_method": "estimated"}

    @contextmanager
    def phase(self, name: str):
        t, c = time.perf_counter(), time.process_time()
        r0 = self.rapl.read() if self.rapl.ok else None
        gj = self._gp.joules if self._gp else None
        yield
        wall, cpu = time.perf_counter() - t, time.process_time() - c
        r1 = self.rapl.read() if self.rapl.ok else None
        ph = self.phases.setdefault(name, {"calls": 0, "wall_s": 0.0, "cpu_s": 0.0, "cpu_energy_j": 0.0})
        e = self._energy(cpu, r0, r1)
        ph["calls"] += 1
        ph["wall_s"] = round(ph["wall_s"] + wall, 3)
        ph["cpu_s"] = round(ph["cpu_s"] + cpu, 3)
        ph["cpu_energy_j"] = round(ph["cpu_energy_j"] + (e["cpu_energy_j"] or 0), 1)
        ph["cpu_energy_method"] = e["cpu_energy_method"]
        ph["rss_mb_after"] = round(current_rss_mb(), 1)
        if self._gp:
            ph["gpu_energy_j"] = round(ph.get("gpu_energy_j", 0.0) + (self._gp.joules - gj), 1)

    def measure(self, fn, *a, **kw):
        """(result, {wall_s, cpu_s, cpu_energy_j, rss_mb}) of one call."""
        t, c = time.perf_counter(), time.process_time()
        r0 = self.rapl.read() if self.rapl.ok else None
        out = fn(*a, **kw)
        wall, cpu = time.perf_counter() - t, time.process_time() - c
        r1 = self.rapl.read() if self.rapl.ok else None
        return out, {"wall_s": round(wall, 4), "cpu_s": round(cpu, 4), **self._energy(cpu, r0, r1), "rss_mb": round(current_rss_mb(), 1)}

    def record(self, rec: dict):
        self.records.append(rec)

    def save(self):
        (self.dir / "metrics.json").write_text(json.dumps({"summary": self.summary, "records": self.records}, indent=1, ensure_ascii=False, default=str),
                                               encoding="utf-8")
        if self.records:
            keys = list(dict.fromkeys(k for r in self.records for k in r))
            with open(self.dir / "metrics.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, keys)
                w.writeheader()
                for r in self.records:
                    w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in r.items()})

    def __exit__(self, et, ev, tb):
        wall, cpu = time.perf_counter() - self._t0, time.process_time() - self._c0
        r1 = self.rapl.read() if self.rapl.ok else None
        if self._gp:
            self._gp.stop_flag = True
            self._gp.join(timeout=2)
        res = {"wall_s": round(wall, 2), "cpu_s": round(cpu, 2), "peak_rss_mb": round(peak_rss_mb(), 1), **self._energy(cpu, self._r0, r1),
               "phases": self.phases, "status": "failed" if et else "ok", "error": repr(ev) if et else None}
        if self._gp:
            res.update(gpu_energy_j=round(self._gp.joules, 1), gpu_peak_w=round(self._gp.peak_w, 1), gpu_mem_peak_mb=self._gp.mem_peak_mb,
                       gpu_power_samples=self._gp.samples)
        (self.dir / "resources.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        self.save()
        self.log(f"done in {wall:.1f} s ({'ok' if not et else 'FAILED: ' + repr(ev)}): {self.dir}")
        self._log.close()
        return False


def exercises():
    """The 10 PDF exercises of the Hackathon sheet as dicts (points, order, objective, ...)."""
    from interface.backend import amiad_api as A

    r = A.pdf_from_sheet("hackathon")
    return json.loads(r.body)["exercises"] if hasattr(r, "body") else r["exercises"]
