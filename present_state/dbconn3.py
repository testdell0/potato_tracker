# -*- coding: utf-8 -*-
"""
Windows App Usage Logger — Daily Aggregation Mode

- Tracks active window usage (Windows-only)
- Detects inactivity if no input for >= IDLE_THRESHOLD seconds
- Clubs all time spent in the same application within a calendar day
  into a single in-memory record
- Pushes to Oracle DB only when a day ends (midnight rollover or exit)
  AND the app's total time for that day is >= MIN_PUSH_SECONDS (5 minutes)
- Auto-reconnect via connection pool with retries
"""

import os
import time
import subprocess
from datetime import datetime
import getpass
import platform
import threading
import queue

import psutil
from psutil import NoSuchProcess, AccessDenied

import win32gui
import win32process

from pynput import keyboard, mouse
import oracledb

try:
    import winreg
except ImportError:
    winreg = None

# --------------------------
# Configuration
# --------------------------
POLL_SECONDS = 1

# Idle detection: no keyboard/mouse input for >= IDLE_THRESHOLD seconds => inactive
IDLE_THRESHOLD = max(300, int(os.getenv("DESKTRACKER_IDLE_THRESHOLD", "300")))

# Only push app records that accumulated >= 5 minutes (300s) of total time in a day
MIN_PUSH_SECONDS = max(300, int(os.getenv("DESKTRACKER_MIN_PUSH_SECONDS", "300")))

MAX_TOTAL_SECONDS = 8 * 60 * 60  # cap per individual window focus period

# Oracle DB connection/pool settings
DB_USER = os.getenv("DB_USER", "your_username")
DB_PASS = os.getenv("DB_PASS", "your_password")
DB_DSN  = os.getenv("DB_DSN",  "your_dsn")

POOL_MIN          = 1
POOL_MAX          = 4
POOL_INCREMENT    = 1
TCP_CONNECT_TIMEOUT  = 5      # seconds
OPEN_RETRY_COUNT     = 3
OPEN_RETRY_DELAY     = 2      # seconds
PING_INTERVAL        = 60     # validate idle connections every 60s
PING_TIMEOUT_MS      = 5000
POOL_WAIT_TIMEOUT_MS = 5000

MAX_QUEUE_SIZE    = 1000
MAX_INSERT_RETRIES = 3

# --------------------------
# System Info
# --------------------------
def get_machine_guid():
    if not winreg:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as key:
            val, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(val)
    except Exception:
        return ""

def get_system_uuid():
    try:
        out = subprocess.check_output(
            ["wmic", "csproduct", "get", "uuid"], universal_newlines=True)
        lines = [l.strip() for l in out.splitlines()
                 if l.strip() and l.lower() != "uuid"]
        return lines[0] if lines else ""
    except Exception:
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
                universal_newlines=True)
            return out.strip()
        except Exception:
            return ""

def get_system_info():
    return {
        "device_name":  platform.node(),
        "username":     getpass.getuser(),
        "machine_guid": get_machine_guid(),
        "system_uuid":  get_system_uuid(),
        "os":           platform.platform(),
    }

# --------------------------
# Window Info
# --------------------------
def get_active_window_info():
    hwnd = win32gui.GetForegroundWindow()
    if hwnd == 0:
        return None
    title = win32gui.GetWindowText(hwnd)
    pid = None
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc_name = psutil.Process(pid).name()
    except (NoSuchProcess, AccessDenied, Exception):
        proc_name = "<unknown>"
    return {
        "hwnd":    hwnd,
        "title":   title,
        "pid":     pid if pid is not None else -1,
        "process": proc_name,
    }

FRIENDLY_NAMES = {
    "WindowsTerminal": "Windows Terminal",
    "WebViewHost":     "Copilot",
    "Code":            "Visual Studio Code",
    "msedge":          "Microsoft Edge",
    "SearchHost":      "Windows Search",
    "notepad":         "Notepad",
    "excel":           "Microsoft Excel",
    "chrome":          "Google Chrome",
    "Olk":             "Outlook",
    "Pangpa":          "GlobalProtect",
}

def derive_app_name(process_name):
    if not process_name or process_name == "<unknown>":
        return "<unknown>"
    base = process_name.replace(".exe", "")
    return FRIENDLY_NAMES.get(base, base.capitalize())

# --------------------------
# Oracle Pool + Writer Thread
# --------------------------
DB_POOL = None
WRITE_Q = queue.Queue(maxsize=MAX_QUEUE_SIZE)

def start_pool_blocking():
    global DB_POOL
    while True:
        try:
            DB_POOL = oracledb.create_pool(
                user=DB_USER, password=DB_PASS, dsn=DB_DSN,
                min=POOL_MIN, max=POOL_MAX, increment=POOL_INCREMENT,
                timeout=60,
                wait_timeout=POOL_WAIT_TIMEOUT_MS,
                ping_interval=PING_INTERVAL,
                ping_timeout=PING_TIMEOUT_MS,
                retry_count=OPEN_RETRY_COUNT,
                retry_delay=OPEN_RETRY_DELAY,
                tcp_connect_timeout=TCP_CONNECT_TIMEOUT,
            )
            print("✅ Oracle connection pool started")
            return
        except oracledb.Error as e:
            print(f"❌ Pool startup failed: {e}. Retrying in 3s...")
            time.sleep(3)

def enqueue_row(row):
    """Non-blocking enqueue; drops oldest entry on overflow."""
    try:
        WRITE_Q.put(row, timeout=2)
    except queue.Full:
        print("⚠️ Write queue full; dropping oldest row to keep up.")
        try:
            WRITE_Q.get_nowait()
            WRITE_Q.task_done()
        except queue.Empty:
            pass
        WRITE_Q.put(row)

def db_writer():
    """
    Dedicated writer thread targeting app_activity_daily.

    Row tuple order:
        (activity_date, app_name, username, device_name, machine_guid,
         system_uuid, os, total_seconds, active_seconds, session_count,
         first_seen, last_seen)
    """
    sql = """
        INSERT INTO app_activity_daily (
            activity_date, app_name, username, device_name, machine_guid,
            system_uuid, os, total_seconds, active_seconds, session_count,
            first_seen, last_seen
        ) VALUES (
            TO_DATE(:1, 'YYYY-MM-DD'),
            :2, :3, :4, :5, :6, :7, :8, :9, :10,
            TO_TIMESTAMP(:11, 'YYYY-MM-DD HH24:MI:SS'),
            TO_TIMESTAMP(:12, 'YYYY-MM-DD HH24:MI:SS')
        )
    """
    while True:
        row = WRITE_Q.get()
        binds = list(row)
        for attempt in range(MAX_INSERT_RETRIES + 1):
            try:
                with DB_POOL.acquire() as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, binds)
                    conn.commit()
                break
            except oracledb.Error as e:
                msg = str(e)
                if ("DPY-4011" in msg) or ("DPI-1010" in msg) or \
                        ("not connected" in msg.lower()):
                    backoff = 1.5 * (attempt + 1)
                    print(f"⚠️ DB write retry {attempt+1}/{MAX_INSERT_RETRIES} "
                          f"after connection error: {msg} (backing off {backoff:.1f}s)")
                    time.sleep(backoff)
                    continue
                else:
                    print(f"❌ DB write failed (non-connection error): {msg}")
                    break
        WRITE_Q.task_done()

# --------------------------
# Activity Detection
# --------------------------
last_input_time = datetime.now()
_input_lock = threading.Lock()

def on_key_press(key):
    global last_input_time
    with _input_lock:
        last_input_time = datetime.now()

def on_mouse_click(x, y, button, pressed):
    global last_input_time
    with _input_lock:
        last_input_time = datetime.now()

_kb_listener = keyboard.Listener(on_press=on_key_press)
_ms_listener = mouse.Listener(on_click=on_mouse_click)
_kb_listener.start()
_ms_listener.start()

def is_user_active():
    with _input_lock:
        return (datetime.now() - last_input_time).total_seconds() < IDLE_THRESHOLD

# --------------------------
# Daily Per-App Accumulator
# --------------------------
# Key:   (date_str "YYYY-MM-DD", app_name)
# Value: { total_seconds, active_seconds, session_count, first_seen, last_seen }
_daily_accumulator = {}
_acc_lock = threading.Lock()

def accumulate(app_name, window_start, window_end, active_sec):
    """Add one window focus period into the daily accumulator for app_name."""
    date_str  = window_start.strftime("%Y-%m-%d")
    total_sec = min((window_end - window_start).total_seconds(), MAX_TOTAL_SECONDS)
    active_sec = min(active_sec, total_sec)

    key = (date_str, app_name)
    with _acc_lock:
        if key not in _daily_accumulator:
            _daily_accumulator[key] = {
                "total_seconds":  0.0,
                "active_seconds": 0.0,
                "session_count":  0,
                "first_seen":     window_start,
                "last_seen":      window_end,
            }
        entry = _daily_accumulator[key]
        entry["total_seconds"]  += total_sec
        entry["active_seconds"] += active_sec
        entry["session_count"]  += 1
        entry["last_seen"]       = window_end

def flush_entries(sysinfo, date_filter=None):
    """
    Push qualifying accumulator entries to the write queue.

    Only entries with total_seconds >= MIN_PUSH_SECONDS are pushed.
    date_filter: "YYYY-MM-DD" string to flush a specific day, or None for all.
    """
    with _acc_lock:
        keys_to_flush = [
            k for k in list(_daily_accumulator.keys())
            if (date_filter is None or k[0] == date_filter)
            and _daily_accumulator[k]["total_seconds"] >= MIN_PUSH_SECONDS
        ]
        rows_to_push = [(k, _daily_accumulator.pop(k)) for k in keys_to_flush]

    for (date_str, app_name), entry in rows_to_push:
        row = (
            date_str,
            app_name,
            sysinfo["username"],
            sysinfo["device_name"],
            sysinfo["machine_guid"],
            sysinfo["system_uuid"],
            sysinfo["os"],
            round(entry["total_seconds"],  2),
            round(entry["active_seconds"], 2),
            entry["session_count"],
            entry["first_seen"].strftime("%Y-%m-%d %H:%M:%S"),
            entry["last_seen"].strftime("%Y-%m-%d %H:%M:%S"),
        )
        enqueue_row(row)
        print(f"  ↑ Queued: {app_name} | {date_str} | "
              f"total={entry['total_seconds']:.0f}s "
              f"active={entry['active_seconds']:.0f}s "
              f"windows={entry['session_count']}")

# --------------------------
# Main Loop
# --------------------------
def main():
    sysinfo      = get_system_info()
    start_pool_blocking()
    threading.Thread(target=db_writer, daemon=True).start()

    last_info    = get_active_window_info()
    last_start   = datetime.now()
    active_seconds = 0
    current_date = datetime.now().date()

    try:
        while True:
            now  = datetime.now()
            info = get_active_window_info()

            # ---- Day rollover: flush yesterday's accumulated data ----
            if now.date() != current_date:
                yesterday = current_date.strftime("%Y-%m-%d")
                print(f"[Day rollover] Flushing data for {yesterday}...")
                if last_info:
                    accumulate(derive_app_name(last_info["process"]),
                               last_start, now, active_seconds)
                flush_entries(sysinfo, date_filter=yesterday)
                current_date   = now.date()
                last_start     = now
                active_seconds = 0

            # ---- Window switch: accumulate completed focus period ----
            if info and last_info and info["hwnd"] != last_info["hwnd"]:
                accumulate(derive_app_name(last_info["process"]),
                           last_start, now, active_seconds)
                last_info      = info
                last_start     = now
                active_seconds = 0

            if is_user_active():
                active_seconds += POLL_SECONDS

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        print("\nExiting — flushing all accumulated data...")
        now = datetime.now()
        if last_info:
            accumulate(derive_app_name(last_info["process"]),
                       last_start, now, active_seconds)
        flush_entries(sysinfo)   # flush all dates

        remaining = WRITE_Q.qsize()
        if remaining:
            print(f"Waiting for {remaining} DB writes to finish...")
        WRITE_Q.join()

        _kb_listener.stop()
        _ms_listener.stop()

        if DB_POOL:
            try:
                DB_POOL.close()
                print("✅ DB pool closed.")
            except Exception as e:
                print(f"⚠️ DB pool close error: {e}")
        print("Bye.")

if __name__ == "__main__":
    main()
