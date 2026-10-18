#!/usr/bin/env python3
"""Webová aplikace pro nastavení, spuštění a zastavení rtl_tcp na Raspberry Pi. Bez závislostí (pouze stdlib)."""
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("MANAGER_HOST", "0.0.0.0")
PORT = int(os.environ.get("MANAGER_PORT", "8080"))
TOKEN = os.environ.get("MANAGER_TOKEN", "")  # pokud je nastaven, vyžaduje se v hlavičce X-Token
CONFIG_FILE = Path(__file__).with_name("config.json")
RTL_SDR_IMAGE = Path(__file__).with_name("assets") / "rtl-sdr-v3.jpg"

# klíč: (přepínač rtl_tcp, typ, min, max, výchozí)
FIELDS = {
    "address": ("-a", "addr", None, None, "0.0.0.0"),
    "port": ("-p", int, 1, 65535, 1234),
    "frequency": ("-f", int, 24000000, 1766000000, 100000000),
    "sample_rate": ("-s", int, 225001, 3200000, 2048000),
    "gain": ("-g", float, 0, 60, 0),  # 0 = AGC
    "ppm": ("-P", int, -1000, 1000, 0),
    "device": ("-d", int, 0, 16, 0),
    "buffers": ("-b", int, 1, 500, 15),
}

lock = threading.Lock()
metrics_lock = threading.Lock()
proc = None
rtl_started_at = None
host_cpu_sample = None
host_network_sample = None
rtl_cpu_sample = None
log = deque(maxlen=200)
rtl_status_lock = threading.Lock()
rtl_status = {
    "device_count": None, "device": None, "tuner": None,
    "direct_sampling": None, "direct_input": None,
    "pll_locked": None, "frequency": None,
    "listening": False, "client_connected": False,
    "gain_mode": None, "bias_tee": None, "warning": None,
}
history_samples = deque(maxlen=361)
last_history_sample = None
HISTORY_INTERVAL = 10
HISTORY_WINDOW = 3600


def defaults():
    return {k: v[4] for k, v in FIELDS.items()}


def validate(data):
    out = {}
    for key, (_, typ, lo, hi, dflt) in FIELDS.items():
        raw = data.get(key, dflt)
        if typ == "addr":
            s = str(raw).strip()
            parts = s.split(".")
            if len(parts) != 4 or not all(p.isdigit() and int(p) <= 255 for p in parts):
                raise ValueError("Neplatná adresa")
            out[key] = s
        else:
            try:
                val = typ(raw)
            except (TypeError, ValueError):
                raise ValueError(f"Neplatná hodnota: {key}")
            if not lo <= val <= hi:
                raise ValueError(f"{key} mimo rozsah {lo}..{hi}")
            out[key] = val
    return out


def load_config():
    try:
        return validate(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        return defaults()


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def reset_rtl_status():
    with rtl_status_lock:
        rtl_status.update({
            "device_count": None, "device": None, "tuner": None,
            "direct_sampling": None, "direct_input": None,
            "pll_locked": None, "frequency": None,
            "listening": False, "client_connected": False,
            "gain_mode": None, "bias_tee": None, "warning": None,
        })


def update_rtl_status(line):
    lower = line.lower()
    with rtl_status_lock:
        match = re.search(r"Found (\d+) device\(s\)", line, re.IGNORECASE)
        if match:
            rtl_status["device_count"] = int(match.group(1))
        elif "no supported devices found" in lower:
            rtl_status["device_count"] = 0
            rtl_status["device"] = None

        match = re.search(r"Using device (\d+):\s*(.+)", line, re.IGNORECASE)
        if match:
            rtl_status["device"] = {"index": int(match.group(1)), "name": match.group(2).strip()}

        match = re.search(r"(?:Found|Using)\s+(.+?)\s+tuner\b", line, re.IGNORECASE)
        if match:
            tuner = match.group(1).strip()
            for prefix in ("Rafael Micro ", "Elonics ", "Fitipower ", "FCI "):
                if tuner.startswith(prefix):
                    tuner = tuner[len(prefix):]
                    break
            rtl_status["tuner"] = tuner

        if "enabled direct sampling mode" in lower:
            match = re.search(r"input\s+(\d+)", line, re.IGNORECASE)
            rtl_status["direct_sampling"] = True
            rtl_status["direct_input"] = int(match.group(1)) if match else None
        elif "disabled direct sampling mode" in lower or "direct sampling mode disabled" in lower:
            rtl_status["direct_sampling"] = False
            rtl_status["direct_input"] = None
        elif "failed to set direct sampling mode" in lower:
            rtl_status["direct_sampling"] = None
            rtl_status["warning"] = line

        if "pll not locked" in lower or "no valid pll" in lower or "failed to set center freq" in lower:
            rtl_status["pll_locked"] = False
        match = re.search(r"Tuned to (\d+) Hz", line, re.IGNORECASE)
        if match:
            rtl_status["frequency"] = int(match.group(1))
            rtl_status["pll_locked"] = True

        if "listening..." in lower:
            rtl_status["listening"] = True
            rtl_status["client_connected"] = False
        elif "client accepted!" in lower:
            rtl_status["listening"] = True
            rtl_status["client_connected"] = True
        elif "all threads dead" in lower or "client disconnected" in lower:
            rtl_status["listening"] = True
            rtl_status["client_connected"] = False
        elif "signal caught, exiting" in lower or lower.strip() == "bye!":
            rtl_status["listening"] = False
            rtl_status["client_connected"] = False

        if "tuner gain set to automatic" in lower or re.search(r"set gain mode 0\b", lower):
            rtl_status["gain_mode"] = "agc"
        elif re.search(r"tuner gain set to [-+\d.]+ db", lower) or re.search(r"set gain mode 1\b", lower):
            rtl_status["gain_mode"] = "manual"

        if "activated bias-t" in lower or re.search(r"set bias tee 1\b", lower):
            rtl_status["bias_tee"] = True
        elif re.search(r"set bias tee 0\b", lower):
            rtl_status["bias_tee"] = False

        if "warning:" in lower or "i2c" in lower and "failed" in lower or "no valid pll" in lower or "pll not locked" in lower:
            rtl_status["warning"] = line


def reader(p):
    for line in p.stdout:
        line = line.rstrip()
        log.append(line)
        update_rtl_status(line)
    return_code = p.wait()
    with rtl_status_lock:
        rtl_status["listening"] = False
        rtl_status["client_connected"] = False
    log.append(f"[proces skončil, kód {return_code}]")


def is_running():
    return proc is not None and proc.poll() is None


def parse_cpu_stat(text):
    fields = text.splitlines()[0].split()
    if fields[0] != "cpu" or len(fields) < 5:
        raise ValueError("Neplatný /proc/stat")
    values = [int(value) for value in fields[1:]]
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def parse_memory_info(text):
    values = {}
    for line in text.splitlines():
        name, value = line.split(":", 1)
        values[name] = int(value.split()[0]) * 1024
    total = values["MemTotal"]
    available = values.get("MemAvailable", values.get("MemFree"))
    if available is None:
        raise ValueError("V /proc/meminfo chybí dostupná paměť")
    return available, total


def parse_network_stats(text):
    received = 0
    transmitted = 0
    for line in text.splitlines():
        if ":" not in line:
            continue
        interface, counters = line.split(":", 1)
        if interface.strip() == "lo":
            continue
        values = [int(value) for value in counters.split()]
        if len(values) < 9:
            raise ValueError("Neplatný /proc/net/dev")
        received += values[0]
        transmitted += values[8]
    return received, transmitted


def host_metrics():
    global host_cpu_sample, host_network_sample
    metrics = {"cpu_percent": None, "memory_available_bytes": None,
               "memory_total_bytes": None, "load_average": None,
               "download_bytes_per_second": None, "upload_bytes_per_second": None}
    try:
        total, idle = parse_cpu_stat(Path("/proc/stat").read_text())
        now = time.monotonic()
        with metrics_lock:
            previous = host_cpu_sample
            host_cpu_sample = (total, idle)
        if previous is not None:
            total_delta = total - previous[0]
            idle_delta = idle - previous[1]
            if total_delta > 0:
                metrics["cpu_percent"] = max(0.0, min(100.0, 100 * (total_delta - idle_delta) / total_delta))
    except (OSError, ValueError, IndexError):
        pass
    try:
        available, total = parse_memory_info(Path("/proc/meminfo").read_text())
        metrics["memory_available_bytes"] = available
        metrics["memory_total_bytes"] = total
    except (OSError, ValueError, IndexError, KeyError):
        pass
    try:
        metrics["load_average"] = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        received, transmitted = parse_network_stats(Path("/proc/net/dev").read_text())
        now = time.monotonic()
        with metrics_lock:
            previous = host_network_sample
            host_network_sample = (received, transmitted, now)
        if previous is not None and now > previous[2]:
            elapsed = now - previous[2]
            metrics["download_bytes_per_second"] = max(0.0, (received - previous[0]) / elapsed)
            metrics["upload_bytes_per_second"] = max(0.0, (transmitted - previous[1]) / elapsed)
    except (OSError, ValueError, IndexError):
        pass
    return metrics


def rtl_metrics(process):
    global rtl_cpu_sample
    if process is None or process.poll() is not None:
        return None
    try:
        fields = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
        if len(fields) < 22:
            return None
        cpu_ticks = int(fields[11]) + int(fields[12])
        rss_bytes = int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        now = time.monotonic()
        cpu_seconds = cpu_ticks / ticks_per_second
        with metrics_lock:
            previous = rtl_cpu_sample
            rtl_cpu_sample = (process.pid, cpu_seconds, now)
        cpu_percent = None
        if previous is not None and previous[0] == process.pid and now > previous[2]:
            cpu_percent = max(0.0, 100 * (cpu_seconds - previous[1]) / (now - previous[2]))
        uptime = int(now - rtl_started_at) if rtl_started_at is not None else None
        return {"pid": process.pid, "cpu_percent": cpu_percent,
                "memory_bytes": rss_bytes, "uptime_seconds": uptime}
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def metric_history(host, rtl):
    global last_history_sample
    now = time.time()
    sample_time = time.monotonic()
    values = {
        "host_cpu": host["cpu_percent"],
        "host_memory": host["memory_available_bytes"],
        "host_load": host["load_average"],
        "rtl_cpu": rtl["cpu_percent"] if rtl else None,
        "rtl_memory": rtl["memory_bytes"] if rtl else None,
        "rtl_uptime": rtl["uptime_seconds"] if rtl else None,
        "download_rate": host["download_bytes_per_second"],
        "upload_rate": host["upload_bytes_per_second"],
    }
    with metrics_lock:
        if last_history_sample is None or sample_time - last_history_sample >= HISTORY_INTERVAL:
            history_samples.append({"time": now, **values})
            last_history_sample = sample_time
        while history_samples and now - history_samples[0]["time"] > HISTORY_WINDOW:
            history_samples.popleft()
        return {key: [[sample["time"], sample[key]] for sample in history_samples]
                for key in values}


def start(cfg):
    global proc, rtl_started_at, rtl_cpu_sample
    with lock:
        if is_running():
            raise RuntimeError("rtl_tcp již běží")
        exe = shutil.which("rtl_tcp")
        if not exe:
            raise RuntimeError("rtl_tcp nenalezen (sudo apt install rtl-sdr)")
        cmd = [exe]
        for key, (flag, *_rest) in FIELDS.items():
            cmd += [flag, str(cfg[key])]
        log.append("$ " + " ".join(cmd))
        reset_rtl_status()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True)
        rtl_started_at = time.monotonic()
        rtl_cpu_sample = None
        threading.Thread(target=reader, args=(proc,), daemon=True).start()


def stop():
    global proc
    with lock:
        if not is_running():
            return
        log.append(f"[odesílám SIGTERM procesu rtl_tcp, pid {proc.pid}]")
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            log.append(f"[rtl_tcp nereaguje, vynucuji ukončení pid {proc.pid}]")
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                proc.kill()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                raise RuntimeError("rtl_tcp se nepodařilo ukončit ani signálem SIGKILL")
        log.append(f"[rtl_tcp ukončen, kód {proc.returncode}]")


def status():
    running = is_running()
    active_proc = proc if running else None
    host = host_metrics()
    rtl = rtl_metrics(active_proc)
    with rtl_status_lock:
        radio = dict(rtl_status)
    return {"running": running, "pid": active_proc.pid if active_proc else None,
            "config": load_config(), "log": list(log),
            "host": host, "rtl_tcp": rtl, "radio": radio,
            "history": metric_history(host, rtl)}


PAGE = """<!doctype html>
<html lang="cs">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#f3f5f2">
<title>RTL_TCP server manager</title>
<style>
:root{color-scheme:light;--paper:#f3f5f2;--surface:#fff;--ink:#182522;--muted:#71807b;--line:#e1e8e3;--green:#16735b;--green-dark:#10513f;--lime:#d5f06d;--red:#ba493c;--shadow:0 16px 48px #20392d12}
:root[data-theme="dark"]{color-scheme:dark;--paper:#080f0c;--surface:#101a15;--ink:#d7ffd0;--muted:#82a38b;--line:#294534;--green:#9bff58;--green-dark:#5cda3f;--lime:#d5f06d;--red:#ff766b;--shadow:0 18px 48px #0009}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:"Aptos", "Segoe UI", sans-serif;font-size:15px;line-height:1.5}
:root[data-theme="dark"] body{background:radial-gradient(ellipse at 50% -15%,#17281d 0%,var(--paper) 65%)}
button,input{font:inherit}
.shell{width:min(1120px,100% - 48px);margin:0 auto;padding:38px 0 48px}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:24px;padding-bottom:28px;border-bottom:1px solid #dce4de}
.brand{display:flex;align-items:center;gap:14px}
.brand-mark{display:block;width:100px;height:44px;flex:0 0 auto;object-fit:contain}
.brand-mark img{display:block;width:100%;height:100%;object-fit:contain}
.topbar-tools{display:flex;align-items:center;gap:12px}
:root[data-theme="dark"] .topbar{padding:13px 16px;border:1px solid #59645e;border-bottom-color:#111714;border-radius:6px 6px 0 0;background:linear-gradient(180deg,#56605b 0%,#343c38 12%,#202824 38%,#151c18 100%);box-shadow:inset 0 1px #a6afa9,0 8px 24px #0008}
.radio-display{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;margin:16px 0 26px;padding:9px;border:1px solid var(--line);border-radius:6px;background:linear-gradient(180deg,#66706a,#353c38 14%,#202622 100%);box-shadow:inset 0 1px #a8afa9,0 5px 12px #0004}
.radio-lcd{display:grid;grid-template-columns:minmax(0,1fr);min-width:0;padding:12px 17px 9px;border:4px solid #39433b;border-radius:4px;background:#030805;color:#a5ff6a;box-shadow:inset 0 0 20px #58b83244,0 0 0 1px #131a15;font-family:"Cascadia Code",Consolas,monospace}
.lcd-caption{display:block;margin-bottom:6px;color:#7bb557;font-size:10px;font-weight:700}
.lcd-main{display:grid;grid-template-columns:minmax(0,1fr) minmax(220px,1fr);align-items:center;gap:18px}
.lcd-frequency-panel{min-width:0}
.lcd-readout{display:flex;align-items:baseline;gap:10px;min-width:0}
.lcd-frequency{overflow:hidden;font:400 38px/1 "Courier New",monospace;font-variant-numeric:tabular-nums;text-shadow:0 0 8px #8aff5b,0 0 18px #67e84099;white-space:nowrap}
.lcd-unit{color:#9be56e;font-size:13px}
.lcd-mode{display:none;padding:2px 5px;border:1px solid #527b40;border-radius:2px;color:#a5ff6a;font:700 10px/1 "Cascadia Code",Consolas,monospace;text-shadow:0 0 6px #8aff5b}
.lcd-mode.is-active{display:inline-block}
.frequency-scale{height:9px;margin-top:11px;border-top:1px solid #38652c;background:repeating-linear-gradient(90deg,#81c95a 0 1px,transparent 1px 12px);opacity:.72}
.radio-lamps{display:flex;min-width:100px;flex-direction:column;justify-content:center;gap:9px;padding:8px 15px;color:#adb9b1;font:700 10px "Cascadia Code",Consolas,monospace}
.radio-lamp{display:flex;align-items:center;gap:8px}
.radio-lamp-dot{width:8px;height:8px;border-radius:50%;background:#343b36;box-shadow:inset 0 1px 2px #000}
.radio-lamp.active .radio-lamp-dot{background:#a4ff5c;box-shadow:0 0 5px #a4ff5c,0 0 12px #79f146}
.radio-lamp.active{color:#c8ffad}
.lcd-indicators{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:7px 12px;min-height:15px;padding:1px 2px 3px}
.lcd-indicator{display:inline-flex;align-items:center;gap:4px;color:#718078;font-size:9px;line-height:1;text-shadow:none;white-space:nowrap}
.lcd-indicator-light{width:6px;height:6px;border-radius:50%;background:#303a33;box-shadow:inset 0 1px 2px #000}
.lcd-indicator.is-active{color:#baff91;text-shadow:0 0 5px #85ff6555}
.lcd-indicator.is-active .lcd-indicator-light{background:#a4ff5c;box-shadow:0 0 4px #a4ff5c,0 0 9px #79f146}
.lcd-indicator.is-waiting{color:#f2c968}
.lcd-indicator.is-waiting .lcd-indicator-light{background:#f2c968;box-shadow:0 0 6px #f2c968}
.lcd-indicator.is-warning{color:#ff8176;text-shadow:0 0 6px #ff514755}
.lcd-indicator.is-warning .lcd-indicator-light{background:#ff574c;box-shadow:0 0 5px #ff574c,0 0 9px #ff332b}
.radio-meters{display:grid;grid-template-columns:minmax(0,1fr);gap:6px;padding:0}
.radio-meter{min-width:0;color:#d0d8d2;font:700 10px "Cascadia Code",Consolas,monospace}
.radio-meter-head,.radio-meter-ruler{display:flex;align-items:center;justify-content:space-between;gap:8px}
.radio-meter-value{color:#a5ff6a;text-shadow:0 0 6px #7aff5566;font-variant-numeric:tabular-nums}
.radio-meter-track{position:relative;height:12px;margin-top:3px;overflow:hidden;border:2px solid #647068;border-radius:2px;background:repeating-linear-gradient(90deg,#202923 0 9%,#101612 9% 10%);box-shadow:inset 0 2px 4px #000d}
.radio-meter-zone{position:absolute;inset:0 0 0 70%;background:#871d1d4d}
.radio-meter-fill{position:absolute;inset:0 auto 0 0;width:0;background:linear-gradient(180deg,#b4ff75,#63d33b 52%,#286d27);box-shadow:0 0 8px #8dff5f;transition:width .25s ease}
.radio-meter-threshold{position:absolute;top:-2px;bottom:-2px;left:70%;width:2px;background:#ff574c;box-shadow:0 0 5px #ff332b}
.radio-meter-ruler{margin-top:2px;color:#96a59a;font-size:9px}
.radio-meter-ruler .threshold-label{position:absolute;left:70%;transform:translateX(-50%);color:#ff8176}
.radio-meter-ruler{position:relative;padding-right:1px}
.theme-toggle{display:flex;align-items:center;gap:8px;min-height:36px;padding:0;border:0;background:transparent;color:var(--muted);font-size:11px;font-weight:700}
.toggle-track{display:flex;align-items:center;width:34px;height:19px;padding:2px;border:1px solid var(--line);border-radius:999px;background:var(--surface);transition:background .18s,border-color .18s}
.toggle-knob{width:13px;height:13px;border-radius:50%;background:var(--muted);transition:transform .18s,background .18s}
.theme-toggle[aria-checked="true"] .toggle-track{border-color:var(--green);background:#27502e}
.theme-toggle[aria-checked="true"] .toggle-knob{transform:translateX(15px);background:var(--green);box-shadow:0 0 8px var(--green)}
.eyebrow,.section-kicker{margin:0;color:var(--muted);font-size:11px;font-weight:700;letter-spacing:1.1px;text-transform:uppercase}
h1,h2,p{margin-top:0}
h1{margin:1px 0 0;font-family:Georgia,serif;font-size:29px;font-weight:400;letter-spacing:0}
.status{display:flex;align-items:center;gap:10px;padding:9px 14px;border:1px solid var(--line);border-radius:999px;background:#fff;font-size:13px;font-weight:650;white-space:nowrap}
.status-dot{width:9px;height:9px;border-radius:50%;background:#a8b3ad;box-shadow:0 0 0 4px #a8b3ad20}
.status.is-running .status-dot{background:#2b9a70;box-shadow:0 0 0 4px #2b9a7020}
.status.is-running{color:var(--green-dark)}
.status.is-stopped .status-dot{background:#d68a36;box-shadow:0 0 0 4px #d68a3620}
main{padding-top:30px}
.intro{display:flex;align-items:end;justify-content:space-between;gap:24px;margin-bottom:22px}
.intro h2{margin:4px 0 0;font-family:Georgia,serif;font-size:25px;font-weight:400}
.intro-note{margin:0;color:var(--muted);font-size:13px}
.workspace{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(300px,.75fr);gap:22px;align-items:start}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin-bottom:22px;border-block:1px solid var(--line);background:#ffffff80}
:root[data-theme="dark"] .metrics{background:linear-gradient(145deg,#0b1610e8,#0d1712d9);box-shadow:inset 0 0 24px #78ff1410,0 10px 34px #0005}
.metric{min-width:0;padding:15px 16px;border-right:1px solid var(--line)}
.metric:nth-child(4n),.metric:last-child{border-right:0}
.metric:nth-child(-n+4){border-bottom:1px solid var(--line)}
.metric-label{display:block;margin-bottom:5px;color:var(--muted);font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase}
.metric strong{display:block;overflow:hidden;color:var(--ink);font-size:20px;font-weight:650;text-overflow:ellipsis;white-space:nowrap}
:root[data-theme="dark"] .metric strong{color:#b9ff8c;text-shadow:0 0 10px #78ff1466}
.metric small{display:block;min-height:17px;margin-top:3px;color:var(--muted);font-size:10px;white-space:nowrap}
.sparkline{display:block;width:100%;height:28px;margin-top:7px;overflow:visible}
.sparkline polyline{fill:none;stroke:var(--green);stroke-width:2;stroke-linecap:round;stroke-linejoin:round;vector-effect:non-scaling-stroke}
.sparkline circle{fill:var(--green)}
:root[data-theme="dark"] .sparkline polyline,:root[data-theme="dark"] .sparkline circle{filter:drop-shadow(0 0 4px #8bff85)}
.panel{min-width:0;overflow:hidden;border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--shadow)}
:root[data-theme="dark"] .panel{border-color:#535c57;border-radius:5px;background:linear-gradient(180deg,#414944 0,#262d29 12px,#151c18 13px);box-shadow:inset 0 1px #929b95,0 3px #090d0b,0 12px 22px #0007}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:19px 22px;border-bottom:1px solid var(--line)}
:root[data-theme="dark"] .panel-head{border-bottom-color:#070b08;background:linear-gradient(180deg,#4b544f,#2c332f);text-shadow:0 1px #000}
.panel-head h3{margin:0;font-size:15px;font-weight:700}
.panel-head span{color:var(--muted);font-size:12px}
.config-panel .panel-head{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"title toggle" "summary summary";align-items:center;gap:4px 12px}
.config-panel .panel-head h3{grid-area:title}
.config-summary{grid-area:summary;color:var(--muted);font-size:11px}
.config-toggle{grid-area:toggle;min-height:30px;padding:0 10px;border:1px solid var(--line);border-radius:4px;background:var(--surface);color:var(--ink);font-size:11px;font-weight:700}
.config-panel.is-collapsed .config-body{display:none}
:root[data-theme="dark"] .config-toggle{border-color:#646d67;background:linear-gradient(#555e59,#353c38);color:#eef3ef}
.service-panel .panel-head{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"title toggle" "summary summary";align-items:center;gap:4px 12px}
.service-panel .panel-head h3{grid-area:title}
.service-summary{grid-area:summary;color:var(--muted);font-size:11px}
.service-toggle{grid-area:toggle}
.service-panel.is-collapsed .service-body{display:none}
#f{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:17px 18px;padding:21px 22px 24px}
#f label{display:block;min-width:0;color:#45534e;font-size:12px;font-weight:650}
#f input{display:block;width:100%;height:42px;margin-top:7px;padding:0 11px;border:1px solid #dce4de;border-radius:6px;background:#fbfcfb;color:var(--ink);outline:none;transition:border-color .15s,box-shadow .15s}
#f input:focus{border-color:var(--green);box-shadow:0 0 0 3px #16735b1c}
.panel-actions{display:flex;align-items:center;gap:10px;padding:15px 22px;border-top:1px solid var(--line);background:#fbfcfb}
button{min-height:40px;padding:0 15px;border:1px solid transparent;border-radius:6px;cursor:pointer;font-size:13px;font-weight:700;transition:background .15s,border-color .15s,transform .15s}
button:hover{transform:translateY(-1px)}
button:focus-visible{outline:3px solid #16735b40;outline-offset:2px}
.button-primary{background:var(--green);color:#fff}.button-primary:hover{background:var(--green-dark)}
.button-secondary{border-color:var(--line);background:#fff;color:var(--ink)}.button-secondary:hover{border-color:#b7c5bc;background:#f5f8f5}
.button-stop{border-color:#f0dedb;background:#fff;color:var(--red)}.button-stop:hover{background:#fff5f3}
.ops{padding:21px}
.ops-label{margin-bottom:14px;color:var(--muted);font-size:12px}
.ops-actions{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.ops-actions button{padding:0 9px}
.message{min-height:23px;margin:11px 0 0;color:var(--muted);font-size:12px}
.message.is-error{color:var(--red)}
:root[data-theme="dark"] .status,:root[data-theme="dark"] #f input,:root[data-theme="dark"] .panel-actions,:root[data-theme="dark"] .button-secondary,:root[data-theme="dark"] .button-stop{background:var(--surface);color:var(--ink)}
:root[data-theme="dark"] .topbar{border-color:var(--line)}
:root[data-theme="dark"] #f label{color:#b0cbb4}
:root[data-theme="dark"] #f input{border-color:var(--line)}
:root[data-theme="dark"] .panel-actions{border-color:var(--line)}
:root[data-theme="dark"] .button-secondary,:root[data-theme="dark"] .button-stop{border-color:var(--line)}
:root[data-theme="dark"] #f input{border-color:#657069;border-radius:3px;background:#070b08;color:#baff88;box-shadow:inset 0 2px 5px #000b}
:root[data-theme="dark"] button{border-radius:4px;box-shadow:inset 0 1px #ffffff35,0 3px #090c0a,0 4px 7px #0005;text-shadow:0 1px #0005}
:root[data-theme="dark"] .button-primary{border-color:#67865c;background:linear-gradient(#82bd55,#39712d);color:#f0ffe4}
:root[data-theme="dark"] .button-primary:hover{background:linear-gradient(#9ad568,#4b8738)}
:root[data-theme="dark"] .button-secondary{border-color:#646d67;background:linear-gradient(#555e59,#353c38);color:#eef3ef}
:root[data-theme="dark"] .button-stop{border-color:#74534f;background:linear-gradient(#664944,#3e302d);color:#ffd1c8}
:root[data-theme="dark"] pre{background:#050a07;color:#baff9a;text-shadow:0 0 5px #76ff3755}
.log-head{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.log-head h3{margin:0;font-size:13px}
.log-head span{color:var(--muted);font-size:11px}
pre{height:280px;margin:0;padding:16px 18px;overflow:auto;background:#192521;color:#d5e7db;font:12px/1.65 "Cascadia Code",Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.footer{display:flex;justify-content:space-between;gap:16px;margin-top:17px;color:var(--muted);font-size:11px}
@media(max-width:760px){.shell{width:min(100% - 30px,560px);padding-top:22px}.topbar{align-items:flex-start;flex-wrap:wrap;padding-bottom:16px}.brand{min-width:0;flex:1 1 100%}.topbar-tools{width:100%;flex-direction:row;align-items:center;justify-content:space-between;gap:10px}.radio-display{margin:12px 0 22px}.radio-lcd{padding:11px 13px 8px}.lcd-main{grid-template-columns:minmax(0,1fr)}.radio-meters{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 12px}.lcd-frequency{font-size:32px}.radio-lamps{min-width:78px;padding:7px}.brand-mark{width:62px;height:40px}.status{gap:8px;padding:8px 10px;font-size:11px}.workspace{grid-template-columns:1fr}.intro{align-items:flex-start;flex-direction:column;gap:5px}.intro h2{font-size:23px}.ops{padding:18px}pre{height:220px}}
@media(max-width:430px){.shell{width:calc(100% - 24px)}h1{font-size:20px;white-space:nowrap}.eyebrow{font-size:9px}.brand{gap:9px}.radio-lcd{padding:10px 9px 7px}.lcd-readout{gap:5px}.lcd-frequency{font-size:19px}.lcd-unit{font-size:11px}.radio-lamps{min-width:68px;padding:6px}.radio-meters{gap:8px 10px}.radio-meter{font-size:9px}.radio-meter-ruler{font-size:8px}.status{max-width:122px;white-space:normal}.panel-head{padding:16px}.panel-head span{font-size:10px}#f{gap:14px 10px;padding:17px 15px 20px}#f input{padding:0 8px;font-size:13px}.panel-actions{padding:13px 15px}.panel-actions button{flex:1;padding:0 8px;font-size:12px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.metric{padding:13px 10px}.metric:nth-child(4n){border-right:1px solid var(--line)}.metric:nth-child(2n){border-right:0}.metric:nth-child(-n+4){border-bottom:0}.metric:nth-child(-n+6){border-bottom:1px solid var(--line)}.metric strong{font-size:17px}.metric-label{font-size:9px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}
</style>
<div class="shell">
    <header class="topbar">
        <div class="brand">
            <img class="brand-mark" src="/assets/rtl-sdr-v3.jpg" alt="">
            <div><p class="eyebrow">Radio receiver · Raspberry Pi</p><h1>RTL_TCP server manager</h1></div>
        </div>
        <div class="topbar-tools">
            <button class="theme-toggle" id="theme-toggle" type="button" role="switch" aria-checked="false" aria-label="Tmavý vzhled" title="Přepnout tmavý vzhled"><span class="toggle-track" aria-hidden="true"><span class="toggle-knob"></span></span><span>Tmavý vzhled</span></button>
            <div class="status is-stopped" id="status-pill" role="status" aria-live="polite"><span class="status-dot"></span><span id="st">Načítám stav…</span></div>
        </div>
    </header>
    <section class="radio-display" aria-label="Frekvence a stav přijímače">
        <div class="radio-lcd"><span class="lcd-caption">VFO A · FREKVENCE</span><div class="lcd-main"><div class="lcd-frequency-panel"><div class="lcd-readout"><strong class="lcd-frequency" id="lcd-frequency">000.000000</strong><span class="lcd-unit">MHz</span><span class="lcd-mode" id="ds-indicator" aria-label="Direct sampling mode">DS</span></div><div class="frequency-scale" aria-hidden="true"></div></div>
            <div class="radio-meters" aria-label="Síťový provoz">
                <div class="radio-meter"><div class="radio-meter-head"><span>DOWNLOAD</span><span class="radio-meter-value" id="download-value">—</span></div><div class="radio-meter-track" id="download-meter" role="meter" aria-label="Download rychlost, maximum 100 Mb/s, červená hranice 70 Mb/s" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span class="radio-meter-zone"></span><span class="radio-meter-fill" id="download-fill"></span><span class="radio-meter-threshold"></span></div><div class="radio-meter-ruler"><span>0</span><span class="threshold-label">70</span><span>100 Mb/s</span></div></div>
                <div class="radio-meter"><div class="radio-meter-head"><span>UPLOAD</span><span class="radio-meter-value" id="upload-value">—</span></div><div class="radio-meter-track" id="upload-meter" role="meter" aria-label="Upload rychlost, maximum 100 Mb/s, červená hranice 70 Mb/s" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span class="radio-meter-zone"></span><span class="radio-meter-fill" id="upload-fill"></span><span class="radio-meter-threshold"></span></div><div class="radio-meter-ruler"><span>0</span><span class="threshold-label">70</span><span>100 Mb/s</span></div></div>
            </div></div>
        <div class="lcd-indicators" aria-label="Stav RTL-SDR">
            <span class="lcd-indicator" id="status-device" title="Zařízení"><i class="lcd-indicator-light"></i><b class="lcd-indicator-label">DEV</b></span>
            <span class="lcd-indicator" id="status-tuner" title="Tuner"><i class="lcd-indicator-light"></i><b class="lcd-indicator-label">TUN</b></span>
            <span class="lcd-indicator" id="status-pll" title="PLL"><i class="lcd-indicator-light"></i><b class="lcd-indicator-label">PLL</b></span>
            <span class="lcd-indicator" id="status-network" title="Síťový klient"><i class="lcd-indicator-light"></i><b class="lcd-indicator-label">NET</b></span>
            <span class="lcd-indicator" id="status-gain" title="Zisk"><i class="lcd-indicator-light"></i><b class="lcd-indicator-label">GAIN</b></span>
            <span class="lcd-indicator" id="status-bias" title="Bias-T"><i class="lcd-indicator-light"></i><b class="lcd-indicator-label">BT</b></span>
            <span class="lcd-indicator" id="status-warning" title="Varování"><i class="lcd-indicator-light"></i><b class="lcd-indicator-label">WARN</b></span>
        </div>
        </div>
        <div class="radio-lamps"><span class="radio-lamp" id="rx-lamp"><span class="radio-lamp-dot"></span>RX</span><span class="radio-lamp"><span class="radio-lamp-dot"></span>SDR</span></div>
    </section>
    <main>
        <div class="intro"><div><p class="section-kicker">Ovládací panel</p><h2>Nastavení přijímače</h2></div><p class="intro-note">Konfigurace se ukládá lokálně vedle aplikace.</p></div>
        <section class="metrics" aria-label="Výkon hosta a rtl_tcp">
            <div class="metric"><span class="metric-label">CPU hosta</span><strong id="host-cpu">—</strong><small>Vytížení systému</small><svg class="sparkline" viewBox="0 0 120 28" preserveAspectRatio="none" role="img" aria-label="Historie CPU hosta za poslední hodinu"><polyline id="chart-host-cpu"></polyline><circle id="dot-host-cpu" r="2"></circle></svg></div>
            <div class="metric"><span class="metric-label">Dostupná RAM</span><strong id="host-memory">—</strong><small id="host-memory-detail">Hostitel</small><svg class="sparkline" viewBox="0 0 120 28" preserveAspectRatio="none" role="img" aria-label="Historie dostupné RAM za poslední hodinu"><polyline id="chart-host-memory"></polyline><circle id="dot-host-memory" r="2"></circle></svg></div>
            <div class="metric"><span class="metric-label">Load average</span><strong id="host-load">—</strong><small>Průměr za 1 minutu</small><svg class="sparkline" viewBox="0 0 120 28" preserveAspectRatio="none" role="img" aria-label="Historie load average za poslední hodinu"><polyline id="chart-host-load"></polyline><circle id="dot-host-load" r="2"></circle></svg></div>
            <div class="metric"><span class="metric-label">CPU rtl_tcp</span><strong id="rtl-cpu">—</strong><small id="rtl-cpu-detail">Proces neběží</small><svg class="sparkline" viewBox="0 0 120 28" preserveAspectRatio="none" role="img" aria-label="Historie CPU rtl_tcp za poslední hodinu"><polyline id="chart-rtl-cpu"></polyline><circle id="dot-rtl-cpu" r="2"></circle></svg></div>
            <div class="metric"><span class="metric-label">RAM rtl_tcp</span><strong id="rtl-memory">—</strong><small>Rezidentní paměť</small><svg class="sparkline" viewBox="0 0 120 28" preserveAspectRatio="none" role="img" aria-label="Historie RAM rtl_tcp za poslední hodinu"><polyline id="chart-rtl-memory"></polyline><circle id="dot-rtl-memory" r="2"></circle></svg></div>
            <div class="metric"><span class="metric-label">Doba běhu</span><strong id="rtl-uptime">—</strong><small>Aktuální proces</small><svg class="sparkline" viewBox="0 0 120 28" preserveAspectRatio="none" role="img" aria-label="Historie doby běhu za poslední hodinu"><polyline id="chart-rtl-uptime"></polyline><circle id="dot-rtl-uptime" r="2"></circle></svg></div>
            <div class="metric"><span class="metric-label">Download</span><strong id="network-download">—</strong><small>Přijato rozhraními</small><svg class="sparkline" viewBox="0 0 120 28" preserveAspectRatio="none" role="img" aria-label="Historie download rychlosti za poslední hodinu"><polyline id="chart-download-rate"></polyline><circle id="dot-download-rate" r="2"></circle></svg></div>
            <div class="metric"><span class="metric-label">Upload</span><strong id="network-upload">—</strong><small>Odesláno rozhraními</small><svg class="sparkline" viewBox="0 0 120 28" preserveAspectRatio="none" role="img" aria-label="Historie upload rychlosti za poslední hodinu"><polyline id="chart-upload-rate"></polyline><circle id="dot-upload-rate" r="2"></circle></svg></div>
        </section>
        <div class="workspace">
            <section class="panel config-panel" id="config-panel" aria-labelledby="config-title">
                <div class="panel-head"><h3 id="config-title">Parametry rtl_tcp</h3><span class="config-summary" id="config-summary">Konfigurace zařízení</span><button class="config-toggle" id="config-toggle" type="button" aria-expanded="true" aria-controls="config-body">Skrýt</button></div>
                <div class="config-body" id="config-body"><form id="f"></form>
                    <div class="panel-actions"><button class="button-secondary" id="save" type="button">Uložit nastavení</button></div>
                </div>
            </section>
            <section class="panel service-panel" id="service-panel" aria-labelledby="service-title">
                <div class="panel-head"><h3 id="service-title">Ovládání služby</h3><span class="service-summary" id="service-summary">Server zastaven</span><button class="config-toggle service-toggle" id="service-toggle" type="button" aria-expanded="true" aria-controls="service-body">Skrýt</button></div>
                <div class="service-body" id="service-body">
                    <div class="ops">
                        <p class="section-kicker">Služba</p>
                        <p class="ops-label">Spusťte nebo zastavte stream přijímače.</p>
                        <div class="ops-actions"><button class="button-primary" id="start" type="button"><span aria-hidden="true">▶</span> &nbsp;Spustit</button><button class="button-stop" id="stop" type="button"><span aria-hidden="true">■</span> &nbsp;Zastavit</button></div>
                        <p class="message" id="msg" role="status" aria-live="polite"></p>
                    </div>
                    <div class="log-head"><h3>Provozní výpis</h3><span>Posledních 200 zpráv</span></div>
                    <pre id="log" aria-live="polite"></pre>
                </div>
            </section>
        </div>
        <footer class="footer"><span>RTL_TCP server manager</span><span>Stav každou 1 s · graf po 10 s</span></footer>
    </main>
</div>
<script>
const L={address:"Adresa (-a)",port:"Port (-p)",frequency:"Frekvence Hz (-f)",sample_rate:"Vzorkovací frekvence Hz (-s)",
gain:"Zesílení dB, 0 = AGC (-g)",ppm:"Korekce ppm (-P)",device:"Index zařízení (-d)",buffers:"Počet bufferů (-b)"};
const H={"Content-Type":"application/json"};const tok=localStorage.token||"";if(tok)H["X-Token"]=tok;
const themeToggle=document.querySelector("#theme-toggle");const savedTheme=localStorage.getItem("theme")||"light";document.documentElement.dataset.theme=savedTheme;themeToggle.setAttribute("aria-checked",String(savedTheme==="dark"));themeToggle.onclick=()=>{const dark=document.documentElement.dataset.theme!=="dark";document.documentElement.dataset.theme=dark?"dark":"light";themeToggle.setAttribute("aria-checked",String(dark));localStorage.setItem("theme",dark?"dark":"light");};
const configPanel=document.querySelector("#config-panel"),configToggle=document.querySelector("#config-toggle"),configSummary=document.querySelector("#config-summary"),servicePanel=document.querySelector("#service-panel"),serviceToggle=document.querySelector("#service-toggle"),serviceSummary=document.querySelector("#service-summary");let previousRunning=null;function setConfigCollapsed(collapsed){configPanel.classList.toggle("is-collapsed",collapsed);configToggle.setAttribute("aria-expanded",String(!collapsed));configToggle.textContent=collapsed?"Zobrazit":"Skrýt";}function setServiceCollapsed(collapsed){servicePanel.classList.toggle("is-collapsed",collapsed);serviceToggle.setAttribute("aria-expanded",String(!collapsed));serviceToggle.textContent=collapsed?"Zobrazit":"Skrýt";}configToggle.onclick=()=>setConfigCollapsed(!configPanel.classList.contains("is-collapsed"));serviceToggle.onclick=()=>setServiceCollapsed(!servicePanel.classList.contains("is-collapsed"));
async function api(p,b){const r=await fetch(p,{method:b?"POST":"GET",headers:H,body:b?JSON.stringify(b):undefined});
if(r.status==401){localStorage.token=prompt("Token");location.reload();}return r.json();}
const form=()=>Object.fromEntries([...document.querySelectorAll("#f input")].map(i=>[i.name,i.value]));
const formatBytes=n=>n==null?"—":n>=1073741824?(n/1073741824).toFixed(1)+" GiB":(n/1048576).toFixed(0)+" MiB";
const formatRate=n=>n==null?"—":n>=1048576?(n/1048576).toFixed(1)+" MiB/s":n>=1024?(n/1024).toFixed(0)+" KiB/s":n.toFixed(0)+" B/s";
const RADIO_METER_MAX_MBPS=100;
function updateRadioMeter(name,bytesPerSecond){const mbps=bytesPerSecond==null?null:bytesPerSecond*8/1000000;const value=document.querySelector("#"+name+"-value"),fill=document.querySelector("#"+name+"-fill"),meter=document.querySelector("#"+name+"-meter");value.textContent=mbps==null?"—":mbps.toFixed(1)+" Mb/s";fill.style.width=(mbps==null?0:Math.min(100,Math.max(0,mbps/RADIO_METER_MAX_MBPS*100)))+"%";meter.setAttribute("aria-valuenow",mbps==null?"0":Math.min(RADIO_METER_MAX_MBPS,Math.max(0,mbps)).toFixed(1));meter.setAttribute("aria-valuetext",mbps==null?"Nedostupné":mbps.toFixed(1)+" Mb/s");}
const formatUptime=n=>{if(n==null)return"—";const d=Math.floor(n/86400),h=Math.floor(n%86400/3600),m=Math.floor(n%3600/60);return d?d+" d "+h+" h":h? h+" h "+m+" min":m+" min";};
const HISTORY_KEYS={host_cpu:"host-cpu",host_memory:"host-memory",host_load:"host-load",rtl_cpu:"rtl-cpu",rtl_memory:"rtl-memory",rtl_uptime:"rtl-uptime",download_rate:"download-rate",upload_rate:"upload-rate"};
function drawHistory(history){const now=Date.now()/1000;for(const [key,id] of Object.entries(HISTORY_KEYS)){const data=(history[key]||[]).filter(([,value])=>value!=null);const line=document.querySelector("#chart-"+id),dot=document.querySelector("#dot-"+id);if(!data.length){line.setAttribute("points","");dot.setAttribute("r","0");continue;}const values=data.map(([,value])=>value),low=Math.min(...values),high=Math.max(...values),padding=(high-low)*.1||Math.max(Math.abs(high)*.05,1);const points=data.map(([timestamp,value])=>{const x=Math.max(0,Math.min(120,(timestamp-(now-3600))/3600*120));const y=25-(value-low+padding)/(high-low+padding*2)*22;return x.toFixed(1)+","+y.toFixed(1);});line.setAttribute("points",points.join(" "));const [x,y]=points[points.length-1].split(",");dot.setAttribute("cx",x);dot.setAttribute("cy",y);dot.setAttribute("r","2");}}
function setIndicator(id,state,label,title){const element=document.querySelector("#"+id);element.classList.remove("is-active","is-waiting","is-warning");if(state!=="off")element.classList.add(state.startsWith("is-")?state:"is-"+state);element.querySelector(".lcd-indicator-label").textContent=label;element.title=title;}
function updateRadioIndicators(s){
    const radio=s.radio||{};
    const device=radio.device;
    const directSampling=s.running&&radio.direct_sampling===true;
    setIndicator("status-device",radio.device_count>0?"active":radio.device_count===0?"warning":"off",device?"DEV "+device.index:"DEV",device?device.name:(radio.device_count===0?"Nenalezeno žádné RTL-SDR zařízení":"Čeká se na zařízení"));
    setIndicator("status-tuner",radio.tuner?"active":"off",radio.tuner||"TUN",radio.tuner||"Tuner zatím nebyl rozpoznán");
    setIndicator("status-pll",radio.pll_locked===false?"warning":radio.pll_locked===true?"active":"off",radio.pll_locked===false?"PLL!":"PLL",radio.pll_locked===false?(radio.warning||"PLL není zamčená"):radio.pll_locked===true?"Tuned signal":"Stav PLL neznámý");
    setIndicator("status-network",radio.client_connected?"active":radio.listening?"is-waiting":"off",radio.client_connected?"NET":radio.listening?"WAIT":"NET",radio.client_connected?"Klient připojen":radio.listening?"Čeká na klienta":"Server neposlouchá");
    setIndicator("status-gain",radio.gain_mode==="agc"?"active":"off",radio.gain_mode==="agc"?"AGC":radio.gain_mode==="manual"?"MAN":"GAIN",radio.gain_mode==="agc"?"Automatické řízení zisku":radio.gain_mode==="manual"?"Manuální zisk":"Režim zisku neznámý");
    setIndicator("status-bias",radio.bias_tee===true?"active":"off","BT",radio.bias_tee===true?"Bias-T zapnutý":radio.bias_tee===false?"Bias-T vypnutý":"Stav Bias-T neznámý");
    setIndicator("status-warning",radio.warning?"warning":"off",radio.warning?"WARN":"OK",radio.warning||"Bez zachyceného varování");
    const dsIndicator=document.querySelector("#ds-indicator");
    dsIndicator.classList.toggle("is-active",directSampling);
    dsIndicator.title=directSampling?"Direct sampling input "+(radio.direct_input??"?"):"Direct sampling vypnutý";
}
function updateMetrics(s){const h=s.host||{},r=s.rtl_tcp;document.querySelector("#lcd-frequency").textContent=((s.config.frequency||0)/1000000).toFixed(6);document.querySelector("#rx-lamp").classList.toggle("active",s.running);updateRadioIndicators(s);updateRadioMeter("download",h.download_bytes_per_second);updateRadioMeter("upload",h.upload_bytes_per_second);document.querySelector("#host-cpu").textContent=h.cpu_percent==null?"—":h.cpu_percent.toFixed(0)+"%";document.querySelector("#host-memory").textContent=formatBytes(h.memory_available_bytes);document.querySelector("#host-memory-detail").textContent=h.memory_total_bytes==null?"Hostitel":formatBytes(h.memory_total_bytes)+" celkem";document.querySelector("#host-load").textContent=h.load_average==null?"—":h.load_average.toFixed(2);document.querySelector("#rtl-cpu").textContent=!r||r.cpu_percent==null?"—":r.cpu_percent.toFixed(0)+"%";document.querySelector("#rtl-cpu-detail").textContent=r?"PID "+r.pid:"Proces neběží";document.querySelector("#rtl-memory").textContent=r?formatBytes(r.memory_bytes):"—";document.querySelector("#rtl-uptime").textContent=r?formatUptime(r.uptime_seconds):"—";document.querySelector("#network-download").textContent=formatRate(h.download_bytes_per_second);document.querySelector("#network-upload").textContent=formatRate(h.upload_bytes_per_second);drawHistory(s.history||{});}
function updateConfigPanel(s){if(previousRunning!==s.running){setConfigCollapsed(s.running);setServiceCollapsed(s.running);previousRunning=s.running;}const cfg=s.config;configSummary.textContent=s.running?((cfg.frequency/1000000).toFixed(3)+" MHz · "+(cfg.sample_rate/1000000).toFixed(3)+" Msps"):"Konfigurace zařízení";serviceSummary.textContent=s.running?"PID "+s.pid+" · běží":"Server zastaven";}
let built=false;
async function refresh(){const s=await api("/api/status");
if(!built){f.innerHTML=Object.keys(L).map(k=>`<label>${L[k]}<input name="${k}" value="${s.config[k]}"></label>`).join("");built=true;}
st.textContent=s.running?"Běží · PID "+s.pid:"Zastaveno";document.querySelector("#status-pill").className="status "+(s.running?"is-running":"is-stopped");updateConfigPanel(s);updateMetrics(s);log.textContent=s.log.join("\\n");log.scrollTop=log.scrollHeight;}
async function act(p,b){const r=await api(p,b);msg.textContent=r.error||"Hotovo";msg.className="message"+(r.error?" is-error":"");refresh();}
save.onclick=()=>act("/api/config",form());
start.onclick=async()=>{await act("/api/config",form());act("/api/start",{});};
stop.onclick=()=>act("/api/stop",{});
refresh();setInterval(refresh,1000);
</script>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return not TOKEN or self.headers.get("X-Token") == TOKEN

    def do_GET(self):
        if self.path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/assets/rtl-sdr-v3.jpg":
            return self._send(200, RTL_SDR_IMAGE.read_bytes(), "image/jpeg")
        if self.path == "/api/status":
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, status())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 4096)
            data = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/config":
                save_config(validate(data))
            elif self.path == "/api/start":
                start(load_config())
            elif self.path == "/api/stop":
                stop()
            else:
                return self._send(404, {"error": "not found"})
            self._send(200, {"ok": True})
        except (ValueError, RuntimeError) as e:
            self._send(400, {"error": str(e)})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"http://{HOST}:{PORT}")
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    finally:
        stop()
