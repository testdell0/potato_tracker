import os
import time
import subprocess
from datetime import datetime
import getpass
import platform
import threading
import queue
import logging
from logging.handlers import RotatingFileHandler

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

oracledb.defaults.thin = True

# Logging Setup
LOG_FILE = os.getenv("DESKTRACKER_LOG_FILE", "desktracker.log")


_log_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(_log_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)

log = logging.getLogger("desktracker")
log.setLevel(logging.DEBUG)
log.addHandler(_file_handler)
log.addHandler(_console_handler)

# Configuration
POLL_SECONDS = 1
IDLE_THRESHOLD = int(os.getenv("DESKTRACKER_IDLE_THRESHOLD"))
BATCH_INTERVAL_SECONDS = int(os.getenv("DESKTRACKER_BATCH_INTERVAL"))
MAX_TOTAL_SECONDS = 8 * 60 * 60

# Oracle DB Connection Details
DB_USER = os.getenv("DB_USER", "your_username")
DB_PASS = os.getenv("DB_PASS", "your_password")
DB_DSN = os.getenv("DB_DSN", "your_dsn")

# Oracle Pool Settings
POOL_MIN = 1
POOL_MAX = 4
POOL_INCREMENT = 1
TCP_CONNECT_TIMEOUT = 5
OPEN_RETRY_COUNT = 3
OPEN_RETRY_DELAY = 2
PING_INTERVAL = 60
PING_TIMEOUT_MS = 5000
POOL_WAIT_TIMEOUT_MS = 5000

MAX_QUEUE_SIZE = 1000
MAX_INSERT_RETRIES = 3

# System Info
# def get_machine_guid():
#     if not winreg:
#         return ""
#     try:
#         with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
#                             r"SOFTWARE\Microsoft\Cryptography") as key:
#             val, _ = winreg.QueryValueEx(key, "MachineGuid")
#             return str(val)
#     except Exception:
#         return ""

# def get_system_uuid():
#     try:
#         out = subprocess.check_output(
#             ["wmic", "csproduct", "get", "uuid"], universal_newlines=True)
#         lines = [l.strip() for l in out.splitlines()
#                 if l.strip() and l.lower() != "uuid"]
#         return lines[0] if lines else ""
#     except Exception:
#         try:
#             out = subprocess.check_output(
#                 ["powershell", "-NoProfile", "-Command",
#                 "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
#                 universal_newlines=True)
#             return out.strip()
#         except Exception:
#             return ""

def get_system_info():
    return {
        "device_name": platform.node(),
        "username": getpass.getuser(),
        # "machine_guid": get_machine_guid(),
        # "system_uuid": get_system_uuid(),
        "os": platform.platform(),
    }

# Window Info
def get_active_window_info():
    hwnd = win32gui.GetForegroundWindow()
    if hwnd == 0:
        return None
    title = win32gui.GetWindowText(hwnd)
    pid = None
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc_name = psutil.Process(pid).name()
    except Exception:
        proc_name = "<unknown>"
    return {
        "hwnd": hwnd,
        "title": title,
        "pid": pid if pid else -1,
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

# Oracle Pool + Writer Thread
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
                # thin=True,
                wait_timeout=POOL_WAIT_TIMEOUT_MS,
                ping_interval=PING_INTERVAL,
                ping_timeout=PING_TIMEOUT_MS,
                retry_count=OPEN_RETRY_COUNT,
                retry_delay=OPEN_RETRY_DELAY,
                tcp_connect_timeout=TCP_CONNECT_TIMEOUT,
            )
            log.info("Oracle connection pool started")
            return
        except oracledb.Error as e:
            log.error(f"Pool startup failed: {e}. Retrying in 3s...")
            time.sleep(3)

def enqueue_row(row):
    """Non-blocking enqueue; drops oldest entry on overflow."""
    try:
        WRITE_Q.put(row, timeout=2)
    except queue.Full:
        log.warning("Write queue full — dropping oldest row to keep up")
        try:
            WRITE_Q.get_nowait()
            WRITE_Q.task_done()
        except queue.Empty:
            pass
        WRITE_Q.put(row)

def db_writer():
    sql = """
        MERGE INTO app_activity_daily dst
        USING (
            SELECT
                TO_DATE(:activity_date, 'YYYY-MM-DD') AS activity_date,
                :app_name AS app_name,
                :total_seconds AS total_seconds,
                :active_seconds AS active_seconds,
                :session_count AS session_count,
                :username AS username,
                :device_name AS device_name,
                :os AS os,
                TO_TIMESTAMP(:first_seen, 'YYYY-MM-DD HH24:MI:SS') AS first_seen,
                TO_TIMESTAMP(:last_seen,  'YYYY-MM-DD HH24:MI:SS') AS last_seen
            FROM DUAL
        ) src
        ON (
            dst.activity_date = src.activity_date
            AND dst.app_name  = src.app_name
            AND dst.username  = src.username
            AND dst.device_name = src.device_name
        )
        WHEN MATCHED THEN UPDATE SET
            dst.total_seconds  = dst.total_seconds  + src.total_seconds,
            dst.active_seconds = dst.active_seconds + src.active_seconds,
            dst.session_count  = dst.session_count  + src.session_count,
            dst.last_seen      = src.last_seen
        WHEN NOT MATCHED THEN INSERT (
            activity_date, app_name, total_seconds, active_seconds, session_count, username, device_name, os, first_seen, last_seen
        ) VALUES (
            src.activity_date, src.app_name, src.total_seconds, src.active_seconds, src.session_count,
            src.username, src.device_name, src.os,
            src.first_seen, src.last_seen
        )
    """

    while True:
        row = WRITE_Q.get()
        binds = {
            "activity_date": row[0],
            "app_name": row[1],
            "total_seconds": row[2],
            "active_seconds": row[3],
            "session_count": row[4],
            "username": row[5],
            "device_name": row[6],
            "os": row[7],
            "first_seen": row[8],
            "last_seen": row[9],
            # "system_uuid": row[5],
            # "machine_guid": row[4],
        }

        for attempt in range(MAX_INSERT_RETRIES + 1):
            try:
                with DB_POOL.acquire() as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, binds)
                    conn.commit()
                log.info(
                    f"DB write OK: {row[1]} | {row[0]} | "
                    f"total={row[7]}s active={row[8]}s sessions={row[9]}"
                )
                break

            except oracledb.Error as e:
                msg = str(e)
                if ("DPY-4011" in msg) or ("DPI-1010" in msg) or ("not connected" in msg.lower()):
                    backoff = 1.5 * (attempt + 1)
                    log.warning(
                        f"DB retry {attempt+1}/{MAX_INSERT_RETRIES} "
                        f"after connection error: {msg} (backoff {backoff}s)"
                    )
                    time.sleep(backoff)
                    continue
                else:
                    log.error(f"DB write failed: {msg}")
                    break

        WRITE_Q.task_done()


# Activity Detection
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

_batch_accumulator = {}
_acc_lock = threading.Lock()

def accumulate(app_name, window_start, window_end, active_sec):
    total_sec  = min((window_end - window_start).total_seconds(), MAX_TOTAL_SECONDS)
    active_sec = min(active_sec, total_sec)

    with _acc_lock:
        if app_name not in _batch_accumulator:
            _batch_accumulator[app_name] = {
                "total_seconds":  0,
                "active_seconds": 0,
                "session_count":  0,
                "first_seen":     window_start,
                "last_seen":      window_end,
            }
        entry = _batch_accumulator[app_name]
        entry["total_seconds"]  += total_sec
        entry["active_seconds"] += active_sec
        entry["session_count"]  += 1
        entry["last_seen"]       = window_end

    log.debug(
        f"Accumulated '{app_name}': +{total_sec:.0f}s total / "
        f"+{active_sec:.0f}s active (window #{entry['session_count']})"
    )

def flush_batch(sysinfo, batch_date):
    with _acc_lock:
        snapshot = dict(_batch_accumulator)
        _batch_accumulator.clear()

    if not snapshot:
        log.debug("Batch flush: nothing to flush")
        return

    date_str = batch_date.strftime("%Y-%m-%d")
    log.info(f"Batch flush: pushing {len(snapshot)} app(s) for {date_str}")

    for app_name, entry in snapshot.items():
        if entry["total_seconds"] < 10:
            log.info(
                f"  Skipped: '{app_name}' | {date_str} | "
                f"total={entry['total_seconds']:.0f}s (<10s threshold)"
            )
            continue
        
        row = (
            date_str,
            app_name,
            round(entry["total_seconds"],  2),
            round(entry["active_seconds"], 2),
            entry["session_count"],
            sysinfo["username"],
            sysinfo["device_name"],
            sysinfo["os"],
            entry["first_seen"].strftime("%Y-%m-%d %H:%M:%S"),
            entry["last_seen"].strftime("%Y-%m-%d %H:%M:%S"),
            # sysinfo["machine_guid"],
            # sysinfo["system_uuid"],
        )
        enqueue_row(row)
        log.info(
            f"  Queued: '{app_name}' | {date_str} | "
            f"total={entry['total_seconds']:.0f}s "
            f"active={entry['active_seconds']:.0f}s "
            f"windows={entry['session_count']}"
        )

# Main Loop
def main():
    sysinfo = get_system_info()
    log.info(
        f"Tracker starting — user={sysinfo['username']} "
        f"device={sysinfo['device_name']} "
        f"idle_threshold={IDLE_THRESHOLD}s "
        f"batch_interval={BATCH_INTERVAL_SECONDS}s"
    )

    start_pool_blocking()
    threading.Thread(target=db_writer, daemon=True).start()

    last_info      = get_active_window_info()
    last_start     = datetime.now()
    active_seconds = 0
    batch_start    = datetime.now()

    try:
        while True:
            now  = datetime.now()
            info = get_active_window_info()

            #15-minute batch flush 
            if (now - batch_start).total_seconds() >= BATCH_INTERVAL_SECONDS:
                if last_info:
                    accumulate(
                        derive_app_name(last_info["process"]),
                        last_start, now, active_seconds
                    )
                    last_start     = now
                    active_seconds = 0

                flush_batch(sysinfo, batch_start.date())
                batch_start = now

            # Window switch
            if info and last_info and info["hwnd"] != last_info["hwnd"]:
                old_app = derive_app_name(last_info["process"])
                new_app = derive_app_name(info["process"]) if info else "<unknown>"
                elapsed = (now - last_start).total_seconds()
                log.debug(
                    f"Window switch: '{old_app}' ({elapsed:.0f}s) → '{new_app}'"
                )
                accumulate(old_app, last_start, now, active_seconds)
                last_info      = info
                last_start     = now
                active_seconds = 0

            if is_user_active():
                active_seconds += POLL_SECONDS

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        log.info("Exiting — flushing remaining batch data...")
        now = datetime.now()
        if last_info:
            accumulate(
                derive_app_name(last_info["process"]),
                last_start, now, active_seconds
            )
        flush_batch(sysinfo, batch_start.date())

        remaining = WRITE_Q.qsize()
        if remaining:
            log.info(f"Waiting for {remaining} DB write(s) to finish...")
        WRITE_Q.join()

        _kb_listener.stop()
        _ms_listener.stop()

        if DB_POOL:
            try:
                DB_POOL.close()
                log.info("DB pool closed")
            except Exception as e:
                log.warning(f"DB pool close error: {e}")

        log.info("Tracker stopped")

if __name__ == "__main__":
    main()
