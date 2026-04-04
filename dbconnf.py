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
import atexit

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
from logging.handlers import TimedRotatingFileHandler

LOG_FILE = os.getenv("DESKTRACKER_LOG_FILE", "desktracker.log")

_log_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_file_handler = TimedRotatingFileHandler(
    LOG_FILE,
    when="midnight",
    interval=1,
    backupCount=30,
    encoding="utf-8",
    utc=False
)

_file_handler.suffix = "%Y-%m-%d"
_file_handler.setFormatter(_log_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)

log = logging.getLogger("desktracker")
log.setLevel(logging.DEBUG)
log.addHandler(_file_handler)

log.addHandler(_console_handler)

# Configuration
POLL_SECONDS = 1
WINDOW_POLL_SECONDS = 2     # CPU optimization
IDLE_THRESHOLD = int(os.getenv("DESKTRACKER_IDLE_THRESHOLD"))
BATCH_INTERVAL_SECONDS = int(os.getenv("DESKTRACKER_BATCH_INTERVAL"))
MAX_TOTAL_SECONDS = 8 * 60 * 60
MAX_BATCH_SIZE = 300

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

MAX_INSERT_RETRIES = 3
MAX_QUEUE_SIZE = 1000

DB_POOL = None
WRITE_Q = queue.Queue(maxsize=MAX_QUEUE_SIZE)

# System Info
def get_system_info():
    return {
        "device_name": platform.node(),
        "username": getpass.getuser(),
        "os": platform.platform(),
    }

FRIENDLY_NAMES = {
    "windows terminal": "Windows Terminal",
    "webviewhost": "Copilot",
    "code": "Visual Studio Code",
    "msedge": "Microsoft Edge",
    "msedgewebview2": "Microsoft Edge",
    "searchhost": "Windows Search",
    "notepad": "Notepad",
    "excel": "Microsoft Excel",
    "chrome": "Google Chrome",
    "outlook": "Outlook",
    "olk": "Outlook",
    "pangpa": "GlobalProtect",
}

def derive_app_name(process_name: str) -> str:
    if not process_name:
        return "unknown"

    base = process_name.replace(".exe", "").lower()

    for key in FRIENDLY_NAMES:
        if key in base:  # improved fuzzy match
            return FRIENDLY_NAMES[key]

    return base.capitalize()


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


# Oracle DB Pool 
def create_pool():
    return oracledb.create_pool(
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

def start_pool_blocking():
    global DB_POOL
    while True:
        try:
            DB_POOL = create_pool()
            log.info("Oracle connection pool started")
            return
        except oracledb.Error as e:
            log.error(f"Pool startup failed: {e}. Retrying in 3s...")
            time.sleep(3)

def ensure_pool_reliable():
    global DB_POOL
    try:
        with DB_POOL.acquire() as conn:
            conn.ping()
    except Exception:
        log.warning("Reinitializing Oracle pool...")
        start_pool_blocking()

# Queue + Writer Thread
def enqueue_row(row):
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
            activity_date, app_name, total_seconds, active_seconds, session_count,
            username, device_name, os, first_seen, last_seen
        ) VALUES (
            src.activity_date, src.app_name, src.total_seconds, src.active_seconds, 
            src.session_count, src.username, src.device_name, src.os,
            src.first_seen, src.last_seen
        )
    """

    while True:
        row = WRITE_Q.get()
        binds = {
            "activity_date": row["activity_date"],
            "app_name": row["app_name"],
            "total_seconds": row["total_seconds"],
            "active_seconds": row["active_seconds"],
            "session_count": row["session_count"],
            "username": row["username"],
            "device_name": row["device_name"],
            "os": row["os"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
        }

        for attempt in range(MAX_INSERT_RETRIES + 1):
            try:
                ensure_pool_reliable()
                with DB_POOL.acquire() as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, binds)
                    conn.commit()
                log.info(
                    f"DB write OK | {row['app_name']} | total={row['total_seconds']} "
                    f"active={row['active_seconds']} sessions={row['session_count']}"
                )
                break

            except oracledb.Error as e:
                msg = str(e)
                if ("DPY-4011" in msg) or ("DPI-1010" in msg) or ("not connected" in msg.lower()):
                    time.sleep(1.5 * (attempt + 1))
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

keyboard.Listener(on_press=on_key_press).start()
mouse.Listener(on_click=on_mouse_click).start()

def is_user_active():
    with _input_lock:
        return (datetime.now() - last_input_time).total_seconds() < IDLE_THRESHOLD

# Batching
batch_accumulator = {}
acc_lock = threading.Lock()

def accumulate(app_name, window_start, window_end, active_sec):
    total_sec = min((window_end - window_start).total_seconds(), MAX_TOTAL_SECONDS)
    active_sec = min(active_sec, total_sec)

    with acc_lock:
        # Early flush warning to protect memory
        if len(batch_accumulator) >= MAX_BATCH_SIZE:
            log.warning("Batch size exceeded — flushing early")

        # Cleaner, atomic initialization
        entry = batch_accumulator.setdefault(app_name, {
            "total_seconds": 0,
            "active_seconds": 0,
            "session_count": 0,
            "first_seen": window_start,
            "last_seen": window_end,
        })

        # Update stats
        entry["total_seconds"] += total_sec
        entry["active_seconds"] += active_sec
        entry["session_count"] += 1
        entry["last_seen"] = window_end

    log.debug(
        f"Accumulated app='{app_name}' | "
        f"+{total_sec:.0f}s total, +{active_sec:.0f}s active | "
        f"sessions={entry['session_count']} | "
        # f"first_seen={entry['first_seen']} | last_seen={entry['last_seen']}"
    )

def flush_batch(sysinfo, date):
    with acc_lock:
        snapshot = dict(batch_accumulator)
        batch_accumulator.clear()

    if not snapshot:
        log.debug("Batch flush: nothing to flush")
        return

    date_str = date.strftime("%Y-%m-%d")
    log.info(f"Flushing {len(snapshot)} entries for {date_str}")

    for app_name, e in snapshot.items():
        if e["total_seconds"] < 30:
            log.info(
                f"  Skipped: '{app_name}' | {date_str} | "
                f"total={e['total_seconds']:.0f}s (<10s threshold)"
            )
            continue
        
        row = {
            "activity_date": date_str,
            "app_name": app_name,
            "total_seconds": round(e["total_seconds"]),
            "active_seconds": round(e["active_seconds"]),
            "session_count": e["session_count"],
            "username": sysinfo["username"],
            "device_name": sysinfo["device_name"],
            "os": sysinfo["os"],
            "first_seen": e["first_seen"].strftime("%Y-%m-%d %H:%M:%S"),
            "last_seen": e["last_seen"].strftime("%Y-%m-%d %H:%M:%S"),
        }
        
        enqueue_row(row)
        log.info(
            f"  Queued: '{app_name}' | {date_str} | "
            f"total={e['total_seconds']:.0f}s "
            f"active={e['active_seconds']:.0f}s "
            f"windows={e['session_count']}"
        )

# Main Loop
def main():
    sysinfo = get_system_info()
    log.info(
        f"Tracker starting — user={sysinfo['username']} "
        f"device={sysinfo['device_name']} "
        # f"idle_threshold={IDLE_THRESHOLD}s "
        # f"batch_interval={BATCH_INTERVAL_SECONDS}s"
    )

    start_pool_blocking()
    threading.Thread(target=db_writer, daemon=True).start()

    last_window = get_active_window_info()
    last_window_poll = time.time()
    last_start = datetime.now()
    active_seconds = 0
    batch_start = datetime.now()

    try:
        while True:
            try:
                now = datetime.now()

                # Window polling optimization
                if time.time() - last_window_poll >= WINDOW_POLL_SECONDS:
                    info = get_active_window_info()
                    last_window_poll = time.time()
                else:
                    info = last_window

                # Time-based batch flush
                if (now - batch_start).total_seconds() >= BATCH_INTERVAL_SECONDS:
                    if last_window:
                        accumulate(
                            derive_app_name(last_window["process"]),
                            last_start, now, active_seconds
                        )
                    flush_batch(sysinfo, batch_start)
                    last_start = now
                    active_seconds = 0
                    batch_start = now

                # Window switch detection
                if info and last_window and info["hwnd"] != last_window["hwnd"]:
                    old_app = derive_app_name(last_window["process"])
                    new_app = derive_app_name(info["process"])
                    elapsed = (now - last_start).total_seconds()
                    log.debug(
                        f"Window switch: '{old_app}' → '{new_app}' | " f"duration={elapsed:.0f}s | "
                        # f"Window switch: '{old_app}' ({elapsed:.0f}s) → '{new_app}'"
                        # f"from={last_start.strftime('%H:%M:%S')} "
                        # f"to={now.strftime('%H:%M:%S')}"
                        )
                    # Accumulate time spent on old window
                    accumulate(old_app, last_start, now, active_seconds)    # Reset trackers
                    last_window = info
                    last_start = now
                    active_seconds = 0

                if is_user_active():
                    active_seconds += POLL_SECONDS

                time.sleep(POLL_SECONDS)

            except Exception as e:
                log.error(f"Main loop error: {e}")

    except KeyboardInterrupt:
        log.info("Shutting down...")
        safe_shutdown(sysinfo, last_window, last_start, active_seconds, batch_start)


# Safe Shutdown
def safe_shutdown(sysinfo, last_window, last_start, active_seconds, batch_start):
    now = datetime.now()

    if last_window:
        accumulate(
            derive_app_name(last_window["process"]),
            last_start, now, active_seconds
        )

    flush_batch(sysinfo, batch_start)

    remaining = WRITE_Q.qsize()
    if remaining:
        log.info(f"Waiting for {remaining} pending writes...")

    WRITE_Q.join()

    
    try:
        if DB_POOL:
            DB_POOL.close()
            # log.info("DB pool closed")
    except Exception as e:
        log.warning(f"DB pool close error: {e}")

    log.info("Tracker stopped cleanly")


# Register for sudden exit
atexit.register(lambda: log.info("Program exited sudden"))


if __name__ == "__main__":
    main()