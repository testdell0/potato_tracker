# -*- coding: utf-8 -*-
"""
Windows App Usage Logger with Robust Oracle Auto-Reconnect (Pool + Writer)
- Tracks active window usage (Windows-only)
- Detects inactivity if no input for >= IDLE_THRESHOLD seconds
- Logs sessions into Oracle DB reliably with auto-reconnect and retries
- Optional: Excel/CSV (left stubbed, can be re-enabled)
- Caps total time per session at 8 hours (28,800 seconds)

Customizations in this version:
- Enforces MIN_SESSION_SECONDS >= 10 (sessions < 10s are not logged)
- Enforces IDLE_THRESHOLD >= 300s (idle starts at 5 minutes or later)
- Finalizes current session on shutdown only if it meets min duration
"""

import os
import time
import subprocess
from datetime import datetime
import getpass
import platform
import threading
import queue
import csv

import psutil
from psutil import NoSuchProcess, AccessDenied

import win32gui
import win32process

# from openpyxl import Workbook, load_workbook   # optional if you use Excel

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

# Enforce policy: idle must be >= 5 minutes (300s). You can raise it via env var.
IDLE_THRESHOLD = max(300, int(os.getenv("DESKTRACKER_IDLE_THRESHOLD", "300")))

# Enforce policy: session must be >= 10s to be logged. You can raise it via env var.
MIN_SESSION_SECONDS = max(10, int(os.getenv("DESKTRACKER_MIN_SESSION_SECONDS", "10")))

FLUSH_INTERVAL = 60           # used if you re-enable Excel/CSV periodic flush
MAX_TOTAL_SECONDS = 8 * 60 * 60

# Output (optional Excel/CSV if you need them)
EXCEL_FILE = "app_usage.xlsx"
CSV_BACKUP = "app_usage_sessions.csv"

# Oracle DB connection/pool settings
DB_USER = os.getenv("DB_USER", "your_username")
DB_PASS = os.getenv("DB_PASS", "your_password")
DB_DSN = os.getenv("DB_DSN", "your_dsn")

# Pool tuning: fail fast and recover quickly
POOL_MIN = 1
POOL_MAX = 4
POOL_INCREMENT = 1
TCP_CONNECT_TIMEOUT = 5       # seconds
OPEN_RETRY_COUNT = 3
OPEN_RETRY_DELAY = 2          # seconds
PING_INTERVAL = 60            # validate idle conns every 60s
PING_TIMEOUT_MS = 5000        # ms
POOL_WAIT_TIMEOUT_MS = 5000   # ms waiting for a free connection

# Writer thread and retry behavior
MAX_QUEUE_SIZE = 1000
MAX_INSERT_RETRIES = 3

SESSIONS_HEADERS = [
    "Start Time", "End Time", "Duration (seconds)", "Duration (minutes)",
    "Active Duration (seconds)", "Active Duration (minutes)",
    "Window Title", "App (Process)", "App Name", "PID", "HWND",
    "Device Name", "Username", "Machine GUID", "System UUID", "OS"
]

# --------------------------
# System Info
# --------------------------
def get_machine_guid():
    if not winreg:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            val, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(val)
    except Exception:
        return ""

def get_system_uuid():
    try:
        out = subprocess.check_output(["wmic", "csproduct", "get", "uuid"], universal_newlines=True)
        lines = [l.strip() for l in out.splitlines() if l.strip() and l.lower() != "uuid"]
        return lines[0] if lines else ""
    except Exception:
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
                universal_newlines=True
            )
            return out.strip()
        except Exception:
            return ""

def get_system_info():
    return {
        "device_name": platform.node(),
        "username": getpass.getuser(),
        "machine_guid": get_machine_guid(),
        "system_uuid": get_system_uuid(),
        "os": platform.platform(),
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
        "hwnd": hwnd,
        "title": title,
        "pid": pid if pid is not None else -1,
        "process": proc_name
    }

FRIENDLY_NAMES = {
    "WindowsTerminal": "Windows Terminal",
    "WebViewHost": "Copilot",
    "Code": "Visual Studio Code",
    "msedge": "Microsoft Edge",
    "SearchHost": "Windows Search",
    "notepad": "Notepad",
    "excel": "Microsoft Excel",
    "chrome": "Google Chrome"
}

def derive_app_name(process_name):
    if not process_name or process_name == "<unknown>":
        return "<unknown>"
    base = process_name.replace(".exe", "")
    return FRIENDLY_NAMES.get(base, base.capitalize())

# --------------------------
# Optional Excel/CSV Helpers (disabled by default)
# --------------------------
# def ensure_workbook(path):
#     try:
#         if os.path.isfile(path):
#             wb = load_workbook(path)
#             if "Sessions" not in wb.sheetnames:
#                 ws = wb.create_sheet("Sessions")
#                 ws.append(SESSIONS_HEADERS)
#             return wb
#     except Exception:
#         print("⚠ Invalid Excel file detected. Creating a new one...")
#     wb = Workbook()
#     ws1 = wb.active
#     ws1.title = "Sessions"
#     ws1.append(SESSIONS_HEADERS)
#     wb.save(path)
#     return wb

# def append_session_row(wb, session_row):
#     ws = wb["Sessions"]
#     ws.append(session_row)
#     wb.save(EXCEL_FILE)

# def ensure_csv_backup_header(path):
#     if not os.path.isfile(path):
#         with open(path, mode="w", newline="", encoding="utf-8") as f:
#             csv.writer(f).writerow(SESSIONS_HEADERS)

# def append_csv_backup(path, session_row):
#     with open(path, mode="a", newline="", encoding="utf-8") as f:
#         csv.writer(f).writerow(session_row)

# --------------------------
# Oracle Pool + Writer Thread
# --------------------------
DB_POOL = None
WRITE_Q = queue.Queue(maxsize=MAX_QUEUE_SIZE)

def start_pool_blocking():
    """
    Start the connection pool; keep retrying until it comes up.
    Using Thin mode (default). You can switch to Thick by calling
    oracledb.init_oracle_client(...) before creating the pool.
    """
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
                tcp_connect_timeout=TCP_CONNECT_TIMEOUT
            )
            print("✅ Oracle connection pool started")
            return
        except oracledb.Error as e:
            print(f"❌ Pool startup failed: {e}. Retrying in 3s...")
            time.sleep(3)

def enqueue_session(row):
    """Non-blocking enqueue with light backpressure handling."""
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
    Dedicated writer thread:
    - acquires fresh connection per write from the pool
    - retries on DPY-4011 / DPI-1010 (connection closed / not connected)
    - commits on success
    """
    sql = """
        INSERT INTO app_usage_sessions (
            start_time, end_time, duration_seconds, active_seconds,
            window_title, app_process, app_name, pid, hwnd,
            device_name, username, machine_guid, system_uuid, os
        ) VALUES (
            TO_TIMESTAMP(:1, 'YYYY-MM-DD HH24:MI:SS'),
            TO_TIMESTAMP(:2, 'YYYY-MM-DD HH24:MI:SS'),
            :3, :4, :5, :6, :7, :8, :9, :10, :11, :12, :13, :14
        )
    """
    while True:
        row = WRITE_Q.get()
        # Bind order matches SQL placeholders
        binds = [
            row[0], row[1], row[2], row[4],
            row[6], row[7], row[8], row[9], row[10],
            row[11], row[12], row[13], row[14], row[15]
        ]
        for attempt in range(MAX_INSERT_RETRIES + 1):
            try:
                with DB_POOL.acquire() as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, binds)
                    conn.commit()
                # Success
                break
            except oracledb.Error as e:
                msg = str(e)
                if ("DPY-4011" in msg) or ("DPI-1010" in msg) or ("not connected" in msg.lower()):
                    backoff = 1.5 * (attempt + 1)
                    print(f"⚠️ DB write retry {attempt+1}/{MAX_INSERT_RETRIES} after connection error: {msg} (backing off {backoff:.1f}s)")
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

def on_key_press(key):
    global last_input_time
    last_input_time = datetime.now()

def on_mouse_click(x, y, button, pressed):
    global last_input_time
    last_input_time = datetime.now()

keyboard.Listener(on_press=on_key_press).start()
mouse.Listener(on_click=on_mouse_click).start()

def is_user_active():
    # User considered active only if inactivity < IDLE_THRESHOLD
    return (datetime.now() - last_input_time).total_seconds() < IDLE_THRESHOLD

# --------------------------
# Helpers
# --------------------------
def build_session_row(start_dt, end_dt, total_sec, active_sec, last_info, sysinfo):
    duration_sec = min(total_sec, MAX_TOTAL_SECONDS)
    duration_min = round(duration_sec / 60.0, 2)
    active_sec_capped = min(active_sec, MAX_TOTAL_SECONDS)
    active_min = round(active_sec_capped / 60.0, 2)

    return [
        start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        round(duration_sec, 2),
        duration_min,
        round(active_sec_capped, 2),
        active_min,
        last_info["title"],
        last_info["process"],
        derive_app_name(last_info["process"]),
        last_info["pid"],
        last_info["hwnd"],
        sysinfo["device_name"],
        sysinfo["username"],
        sysinfo["machine_guid"],
        sysinfo["system_uuid"],
        sysinfo["os"],
    ]

def maybe_enqueue_session(start_dt, end_dt, active_sec, last_info, sysinfo, reason):
    """Only enqueue if session meets the minimum length policy."""
    total_sec = (end_dt - start_dt).total_seconds()
    if total_sec >= MIN_SESSION_SECONDS:
        session_row = build_session_row(start_dt, end_dt, total_sec, active_sec, last_info, sysinfo)
        enqueue_session(session_row)
        print(f"[{last_info['title']}] total: {total_sec:.0f}s, active: {min(active_sec, total_sec):.0f}s ({reason})")
        return True
    else:
        print(f"⏭️ Skipped short session ({total_sec:.1f}s < {MIN_SESSION_SECONDS}s) for '{last_info['title']}' ({reason})")
        return False

# --------------------------
# Main Logic
# --------------------------
def main():
    sysinfo = get_system_info()
    # wb = ensure_workbook(EXCEL_FILE)
    # ensure_csv_backup_header(CSV_BACKUP)

    # Start pool and writer
    start_pool_blocking()
    threading.Thread(target=db_writer, daemon=True).start()

    last_info = get_active_window_info()
    last_start = datetime.now()
    active_seconds = 0

    try:
        while True:
            info = get_active_window_info()
            now = datetime.now()

            # On window switch, finalize previous session and enqueue for DB write (if >= MIN_SESSION_SECONDS)
            if info and last_info and info["hwnd"] != last_info["hwnd"]:
                maybe_enqueue_session(last_start, now, active_seconds, last_info, sysinfo, reason="switch")
                # Move on to next
                last_info = info
                last_start = now
                active_seconds = 0

            if is_user_active():
                active_seconds += POLL_SECONDS

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        print("\nExiting...")
        # Finalize the current session on exit if it meets minimum duration
        now = datetime.now()
        if last_info:
            maybe_enqueue_session(last_start, now, active_seconds, last_info, sysinfo, reason="exit")

        # Drain queue gracefully
        remaining = WRITE_Q.qsize()
        if remaining:
            print(f"Waiting for {remaining} DB writes to finish...")
        WRITE_Q.join()

        # Close pool
        if DB_POOL:
            try:
                DB_POOL.close()
                print("✅ DB pool closed.")
            except Exception as e:
                print(f"⚠️ DB pool close error: {e}")
        print("Bye.")

if __name__ == "__main__":
    main()