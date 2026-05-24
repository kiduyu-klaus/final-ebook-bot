"""
WSGI Entry Point for Ebook Bot on cPanel

Passenger spawns multiple worker processes — this module ensures
exactly ONE bot subprocess runs at all times using:

  1. fcntl advisory lock (tmp/bot.lock)  — cross-process mutex so only
     the "primary" Passenger worker manages the bot.

  2. kill_all_bot_processes() on every start — scans /proc for ANY
     surviving bot_runner.py processes (from previous workers, stale
     PIDs, etc.) and kills them all before spawning a fresh one.

  3. Single PID file (tmp/bot.pid) — records the live subprocess PID
     for health checks.
"""

import sys
import os
import signal
import subprocess
import logging
import threading
import time
import fcntl
import json
import shutil
import tempfile
import re
import hashlib
import hmac
import secrets
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# PIN Configuration
# ---------------------------------------------------------------------------
REQUIRED_PIN = "klausK2801"  # The PIN required for sensitive operations

def verify_pin(pin: str) -> bool:
    """Verify the provided PIN using constant-time comparison to prevent timing attacks."""
    if not pin:
        return False
    # Use hmac.compare_digest for constant-time comparison
    return hmac.compare_digest(pin, REQUIRED_PIN)

def generate_pin_hash() -> str:
    """Generate a secure hash of the PIN (for display purposes only)."""
    return hashlib.sha256(REQUIRED_PIN.encode()).hexdigest()[:16]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
base_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, base_dir)

LOG_DIR = os.path.join(base_dir, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

TMP_DIR     = os.path.join(base_dir, 'tmp')
os.makedirs(TMP_DIR, exist_ok=True)

BACKUP_DIR = os.path.join(base_dir, 'backups')
os.makedirs(BACKUP_DIR, exist_ok=True)


def _truncate_log_file(path: str) -> None:
    """Truncate a log file if it exists, or create it."""
    try:
        with open(path, 'w', encoding='utf-8') as f:
            pass
    except Exception:
        pass

PID_FILE    = os.path.join(TMP_DIR, 'bot.pid')
LOCK_FILE   = os.path.join(TMP_DIR, 'bot.lock')

BOT_RUNNER  = os.path.join(base_dir, 'run_bot.py')   # <-- actual entry point
STDERR_LOG_FILE = os.path.join(base_dir, 'stderr.log')

# ---------------------------------------------------------------------------
# Logging - with broken pipe protection
# ---------------------------------------------------------------------------
_truncate_log_file(os.path.join(LOG_DIR, 'wsgi.log'))
_truncate_log_file(STDERR_LOG_FILE)

class SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that safely handles broken pipes."""
    def emit(self, record):
        try:
            super().emit(record)
        except BrokenPipeError:
            pass
        except Exception:
            self.handleError(record)

# Set up file handler (always works)
file_handler = logging.FileHandler(os.path.join(LOG_DIR, 'wsgi.log'))

# Try to set up stream handler, fall back to file-only if it fails
handlers = [file_handler]
try:
    stream_handler = SafeStreamHandler(sys.stdout)
    handlers.append(stream_handler)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=handlers,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-worker state
# ---------------------------------------------------------------------------
bot_process          = None
bot_process_group_id = None
bot_log_handle       = None
_process_lock        = threading.Lock()
_lock_fh             = None
_is_primary_worker   = False


# ---------------------------------------------------------------------------
# Cross-process lock
# ---------------------------------------------------------------------------

def _acquire_worker_lock() -> bool:
    global _lock_fh, _is_primary_worker
    try:
        _lock_fh = open(LOCK_FILE, 'a')
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.seek(0)
        _lock_fh.truncate()
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
        _is_primary_worker = True
        logger.info(f"Worker {os.getpid()} is PRIMARY")
        return True
    except BlockingIOError:
        if _lock_fh:
            _lock_fh.close()
            _lock_fh = None
        _is_primary_worker = False
        logger.info(f"Worker {os.getpid()} is secondary")
        return False
    except Exception as e:
        logger.error(f"Lock error: {e}")
        return False


def _release_worker_lock():
    global _lock_fh, _is_primary_worker
    if _lock_fh:
        try:
            fcntl.flock(_lock_fh, fcntl.LOCK_UN)
            _lock_fh.close()
        except Exception:
            pass
        _lock_fh = None
    _is_primary_worker = False


# ---------------------------------------------------------------------------
# PID file
# ---------------------------------------------------------------------------

def _write_pid(pid: int):
    try:
        with open(PID_FILE, 'w') as f:
            f.write(str(pid))
    except Exception as e:
        logger.warning(f"Could not write PID file: {e}")


def _read_pid() -> int | None:
    try:
        return int(Path(PID_FILE).read_text().strip())
    except Exception:
        return None


def _clear_pid():
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ---------------------------------------------------------------------------
# Kill ALL surviving bot_runner.py processes (the key fix)
# ---------------------------------------------------------------------------

def _find_all_bot_pids() -> list[int]:
    """
    Return all PIDs running bot_runner.py (run_bot.py) except current worker.
    Uses pgrep for better cPanel compatibility.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python.*run_bot.py"],
            capture_output=True,
            text=True,
        )
        our_pid = os.getpid()
        pids = []
        for line in result.stdout.splitlines():
            try:
                pid = int(line.strip())
                if pid != our_pid:
                    pids.append(pid)
            except ValueError:
                pass
        return pids
    except Exception as e:
        logger.warning(f"Could not locate bot PIDs: {e}")
        return []


def kill_all_bot_processes():
    """
    Kill every bot_runner.py process before spawning a fresh one.
    Uses pgrep + os.killpg for reliable cleanup on cPanel.
    """
    pids = _find_all_bot_pids()
    if not pids:
        logger.info("No existing bot processes found")
        return

    logger.info(f"Stopping existing bot processes: {pids}")

    for pid in pids:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            logger.info(f"SIGTERM -> PID {pid}, PGID {pgid}")
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

    deadline = time.time() + 8
    while time.time() < deadline:
        if not _find_all_bot_pids():
            break
        time.sleep(0.5)

    remaining = _find_all_bot_pids()
    for pid in remaining:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
            logger.warning(f"SIGKILL -> PID {pid}, PGID {pgid}")
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass

    _clear_pid()
    logger.info("All bot processes terminated")


# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------

def start_bot():
    global bot_process, bot_process_group_id, bot_log_handle

    with _process_lock:
        kill_all_bot_processes()

        logger.info("Starting fresh bot process...")
        try:
            _truncate_log_file(os.path.join(LOG_DIR, 'bot.log'))
            _truncate_log_file(STDERR_LOG_FILE)
            bot_log_handle = open(
                os.path.join(LOG_DIR, 'bot.log'), 'w', encoding='utf-8'
            )
            bot_process = subprocess.Popen(
                [sys.executable, BOT_RUNNER],
                cwd=base_dir,
                stdout=bot_log_handle,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
            bot_process_group_id = os.getpgid(bot_process.pid)
            _write_pid(bot_process.pid)

            logger.info(f"Bot started - PID {bot_process.pid}, PGID {bot_process_group_id}")
            return bot_process

        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            if bot_log_handle:
                bot_log_handle.close()
                bot_log_handle = None
            return None


def stop_bot():
    global bot_process, bot_process_group_id, bot_log_handle

    with _process_lock:
        kill_all_bot_processes()
        bot_process = None
        bot_process_group_id = None
        if bot_log_handle:
            bot_log_handle.close()
            bot_log_handle = None


def restart_bot(reason: str = "manual"):
    logger.info(f"Restarting bot - reason: {reason}")
    stop_bot()
    time.sleep(2)
    return start_bot()


def _read_primary_worker_pid() -> int | None:
    try:
        return int(Path(LOCK_FILE).read_text().strip())
    except Exception:
        return None


def get_bot_status() -> dict:
    pid = _read_pid() if bot_process is None else bot_process.pid
    running = False
    if bot_process is not None:
        running = bot_process.poll() is None
    elif pid:
        running = _pid_is_alive(pid)
    primary_pid = _read_primary_worker_pid()
    primary_alive = _pid_is_alive(primary_pid) if primary_pid else False
    return {
        'running': running,
        'pid': pid,
        'primary_worker': primary_alive,
        'primary_worker_pid': primary_pid,
        'worker_pid': os.getpid(),
    }


# ---------------------------------------------------------------------------
# File upload and code replacement functionality with PIN protection
# ---------------------------------------------------------------------------

def validate_python_file(content: bytes) -> tuple[bool, str]:
    """
    Validate that the uploaded content is a valid Python file.
    Returns (is_valid, error_message)
    """
    try:
        # Try to decode as UTF-8
        decoded = content.decode('utf-8')
        
        # Check if it looks like Python code (basic heuristics)
        if not decoded.strip():
            return False, "File is empty"
        
        # Try to compile it (best validation)
        try:
            compile(decoded, '<uploaded>', 'exec')
        except SyntaxError as e:
            return False, f"Syntax error in Python file: {e}"
        
        # Check for common malicious patterns (basic security)
        dangerous_patterns = [
            r'os\.system\s*\(',
            r'subprocess\.call\s*\(',
            r'__import__\s*\(',
            r'eval\s*\(',
            r'exec\s*\(',
            r'__builtins__',
        ]
        
        for pattern in dangerous_patterns:
            if re.search(pattern, decoded, re.IGNORECASE):
                # Log but don't block - let admin decide
                logger.warning(f"Potentially dangerous pattern found: {pattern}")
        
        return True, "Valid"
    except UnicodeDecodeError:
        return False, "File must be UTF-8 encoded"
    except Exception as e:
        return False, f"Validation error: {e}"


def create_backup(file_path: str) -> str | None:
    """
    Create a timestamped backup of the file.
    Returns backup path or None if failed.
    """
    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_name = f"{os.path.basename(file_path)}.{timestamp}.bak"
        backup_path = os.path.join(BACKUP_DIR, backup_name)
        shutil.copy2(file_path, backup_path)
        logger.info(f"Created backup: {backup_path}")
        return backup_path
    except Exception as e:
        logger.error(f"Failed to create backup: {e}")
        return None


def replace_python_file(file_path: str, new_content: bytes) -> tuple[bool, str]:
    """
    Replace the Python file with new content after validation.
    Returns (success, message)
    """
    # Validate the new file
    is_valid, error_msg = validate_python_file(new_content)
    if not is_valid:
        return False, f"Validation failed: {error_msg}"
    
    # Create backup of existing file if it exists
    if os.path.exists(file_path):
        backup_path = create_backup(file_path)
        if not backup_path:
            logger.warning("Failed to create backup, but continuing...")
    
    # Write the new file using a temporary file for atomic operation
    temp_fd = None
    temp_path = None
    try:
        # Create temporary file in same directory
        fd, temp_path = tempfile.mkstemp(
            dir=os.path.dirname(file_path),
            prefix='.upload_',
            suffix='.py'
        )
        
        # Write new content
        with os.fdopen(fd, 'wb') as f:
            f.write(new_content)
        
        # Replace the original file
        shutil.move(temp_path, file_path)
        
        # Set proper permissions (read/write for owner)
        os.chmod(file_path, 0o644)
        
        logger.info(f"Successfully replaced {file_path}")
        return True, f"Successfully replaced {os.path.basename(file_path)}"
        
    except Exception as e:
        logger.error(f"Failed to replace file: {e}")
        # Clean up temp file if it exists
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except:
                pass
        return False, f"Failed to replace file: {e}"


def rollback_file(file_path: str, backup_name: str) -> tuple[bool, str]:
    """
    Rollback a file from a specific backup.
    """
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    if not os.path.exists(backup_path):
        return False, f"Backup file not found: {backup_name}"
    
    try:
        # Stop bot before rollback
        was_running = get_bot_status()['running']
        if was_running:
            stop_bot()
        
        # Restore from backup
        shutil.copy2(backup_path, file_path)
        logger.info(f"Rolled back {file_path} from {backup_name}")
        
        # Restart bot if it was running
        if was_running:
            start_bot()
        
        return True, f"Successfully rolled back to {backup_name}"
    except Exception as e:
        logger.error(f"Rollback failed: {e}")
        return False, f"Rollback failed: {e}"


def list_backups() -> list[dict]:
    """
    List available backups with metadata.
    """
    backups = []
    try:
        for backup_file in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if backup_file.endswith('.bak'):
                backup_path = os.path.join(BACKUP_DIR, backup_file)
                stat = os.stat(backup_path)
                backups.append({
                    'name': backup_file,
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'original_file': backup_file.split('.')[0] + '.py'
                })
    except Exception as e:
        logger.error(f"Failed to list backups: {e}")
    return backups


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

def _shutdown(signum, frame):
    logger.info(f"Signal {signum} - shutting down worker {os.getpid()}")
    stop_bot()
    _release_worker_lock()
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)


# ---------------------------------------------------------------------------
# HTML Dashboard with PIN-protected Upload Interface
# ---------------------------------------------------------------------------

def _render_dashboard() -> bytes:
    html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ebook Bot Dashboard</title>
    <style>
        :root {
            color-scheme: dark;
            --bg: #0e1015;
            --panel: #171b23;
            --panel-soft: #1f242d;
            --text: #e7e9ef;
            --muted: #94a1b2;
            --accent: #4fd1ff;
            --success: #38b2ac;
            --warning: #f6ad55;
            --danger: #f56565;
            font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            padding-top: 70px;
            min-height: 100vh;
            background: radial-gradient(circle at top, rgba(79,209,255,0.16), transparent 25%),
                        radial-gradient(circle at bottom right, rgba(56,178,172,0.14), transparent 20%),
                        var(--bg);
            color: var(--text);
        }
        .navbar {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 1000;
            background: rgba(23,27,35,0.85);
            backdrop-filter: blur(14px);
            border-bottom: 1px solid rgba(255,255,255,0.08);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 24px;
            height: 60px;
        }
        .navbar-brand {
            font-weight: 700;
            font-size: 1.25rem;
            letter-spacing: -0.03em;
            color: var(--text);
        }
        .navbar-links {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .navbar-links a,
        .navbar-links button {
            text-decoration: none;
            color: var(--muted);
            font-size: 0.95rem;
            padding: 8px 16px;
            border-radius: 12px;
            transition: background 0.2s, color 0.2s;
            background: transparent;
            border: none;
            cursor: pointer;
            white-space: nowrap;
        }
        .navbar-links a:hover,
        .navbar-links button:hover {
            background: rgba(255,255,255,0.06);
            color: var(--text);
        }
        .navbar-links a.active,
        .navbar-links button.active {
            background: rgba(79,209,255,0.16);
            color: #fff;
        }
        .navbar-actions {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .dropdown {
            position: relative;
        }
        .dropdown-content {
            display: none;
            position: absolute;
            right: 0;
            top: 100%;
            margin-top: 4px;
            background: var(--panel);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 14px;
            padding: 8px;
            min-width: 140px;
            z-index: 100;
        }
        .dropdown:hover .dropdown-content {
            display: block;
        }
        .dropdown-content button {
            width: 100%;
            text-align: left;
        }
        .hamburger {
            display: none;
            font-size: 1.6rem;
            background: none;
            border: none;
            color: var(--text);
            cursor: pointer;
        }
        .mobile-menu {
            display: none;
            position: fixed;
            top: 60px;
            left: 0;
            right: 0;
            background: rgba(23,27,35,0.95);
            backdrop-filter: blur(14px);
            border-bottom: 1px solid rgba(255,255,255,0.08);
            flex-direction: column;
            padding: 16px 24px;
            gap: 12px;
            z-index: 999;
        }
        .mobile-menu a,
        .mobile-menu button {
            width: 100%;
            text-align: left;
        }
        @media (max-width: 768px) {
            .navbar-links { display: none; }
            .hamburger { display: block; }
            .mobile-menu.show { display: flex; }
        }
        .container {
            max-width: 1000px;
            margin: 0 auto;
            padding: 32px 20px 48px;
        }
        .header {
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 28px;
        }
        .brand {
            font-size: clamp(1.8rem, 2.4vw, 2.4rem);
            font-weight: 700;
            letter-spacing: -0.03em;
        }
        .subtitle {
            color: var(--muted);
            max-width: 620px;
            line-height: 1.65;
        }
        .card {
            background: linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.01));
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 24px;
            padding: 28px;
            box-shadow: 0 24px 80px rgba(0,0,0,0.22);
            backdrop-filter: blur(18px);
        }
        .grid {
            display: grid;
            gap: 20px;
        }
        .status-grid {
            grid-template-columns: repeat(3, minmax(0, 1fr));
        }
        .panel {
            padding: 22px;
            border-radius: 20px;
            background: var(--panel-soft);
            border: 1px solid rgba(255,255,255,0.05);
        }
        .panel h3 {
            margin: 0 0 12px;
            font-size: 1rem;
            color: var(--muted);
        }
        .panel p {
            margin: 0;
            font-size: 1.9rem;
            font-weight: 700;
        }
        .actions {
            display: flex;
            flex-wrap: wrap;
            gap: 14px;
            margin-top: 18px;
        }
        .button {
            border: none;
            border-radius: 14px;
            padding: 14px 20px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s ease, box-shadow 0.2s ease, background 0.2s ease;
            box-shadow: 0 16px 36px rgba(0,0,0,0.18);
        }
        .button:hover { transform: translateY(-1px); }
        .button:active { transform: translateY(0); }
        .button--primary { background: var(--accent); color: #050d18; }
        .button--success { background: var(--success); color: #0b1417; }
        .button--warning { background: var(--warning); color: #202023; }
        .button--danger  { background: var(--danger);  color: #fff; }
        .button--ghost {
            background: transparent;
            border: 1px solid rgba(255,255,255,0.14);
            color: var(--text);
            box-shadow: none;
        }
        .tab-button.active {
            background: rgba(79,209,255,0.16);
            border-color: rgba(79,209,255,0.32);
            color: #fff;
        }
        .log {
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 18px;
            padding: 22px;
            min-height: 260px;
            color: #cbd5e1;
            overflow: auto;
            line-height: 1.7;
            white-space: pre-wrap;
        }
        .note {
            color: var(--muted);
            margin-top: 14px;
        }
        .upload-area {
            border: 2px dashed rgba(255,255,255,0.2);
            border-radius: 16px;
            padding: 32px;
            text-align: center;
            transition: all 0.3s ease;
            cursor: pointer;
        }
        .upload-area:hover, .upload-area.drag-over {
            border-color: var(--accent);
            background: rgba(79,209,255,0.05);
        }
        .upload-area input {
            display: none;
        }
        .backup-list {
            max-height: 300px;
            overflow-y: auto;
        }
        .backup-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px;
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        .backup-item:last-child {
            border-bottom: none;
        }
        .backup-info {
            flex: 1;
        }
        .backup-name {
            font-family: monospace;
            font-size: 0.85rem;
        }
        .backup-size {
            font-size: 0.75rem;
            color: var(--muted);
        }
        .toast {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: var(--panel);
            border-left: 4px solid var(--success);
            padding: 12px 20px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            z-index: 2000;
            animation: slideIn 0.3s ease;
        }
        .toast.error {
            border-left-color: var(--danger);
        }
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.8);
            z-index: 3000;
            justify-content: center;
            align-items: center;
        }
        .modal-content {
            background: var(--panel);
            border-radius: 24px;
            padding: 32px;
            max-width: 400px;
            width: 90%;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .modal-content h3 {
            margin: 0 0 20px;
        }
        .modal-content input {
            width: 100%;
            padding: 12px;
            border-radius: 12px;
            border: 1px solid rgba(255,255,255,0.2);
            background: rgba(255,255,255,0.05);
            color: var(--text);
            font-size: 1rem;
            margin-bottom: 20px;
        }
        .modal-content input:focus {
            outline: none;
            border-color: var(--accent);
        }
        .modal-buttons {
            display: flex;
            gap: 12px;
            justify-content: flex-end;
        }
        @keyframes slideIn {
            from {
                transform: translateX(100%);
                opacity: 0;
            }
            to {
                transform: translateX(0);
                opacity: 1;
            }
        }
        @media (max-width: 740px) {
            .status-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <!-- PIN Modal -->
    <div id="pinModal" class="modal">
        <div class="modal-content">
            <h3>🔐 PIN Required</h3>
            <p>Please enter the PIN to perform this operation.</p>
            <input type="password" id="pinInput" placeholder="Enter PIN" autocomplete="off">
            <div class="modal-buttons">
                <button class="button button--ghost" onclick="closePinModal()">Cancel</button>
                <button class="button button--primary" onclick="submitPin()">Submit</button>
            </div>
        </div>
    </div>

    <nav class="navbar">
        <div class="navbar-brand">📚 Ebook Bot</div>
        <div class="navbar-links" id="desktop-nav">
            <a href="#" class="nav-link active" data-tab="status" onclick="switchTab('status'); return false;">Status</a>
            <a href="#" class="nav-link" data-tab="bot-log" onclick="switchTab('bot-log'); return false;">Bot Log</a>
            <a href="#" class="nav-link" data-tab="wsgi-log" onclick="switchTab('wsgi-log'); return false;">WSGI Log</a>
            <a href="#" class="nav-link" data-tab="stderr-log" onclick="switchTab('stderr-log'); return false;">stderr.log</a>
            <a href="#" class="nav-link" data-tab="upload" onclick="switchTab('upload'); return false;">📤 Upload</a>
            <div class="dropdown">
                <button class="nav-link">Actions ▾</button>
                <div class="dropdown-content">
                    <button onclick="sendCommand('/start')">▶ Start Bot</button>
                    <button onclick="sendCommand('/stop')">⏹ Stop Bot</button>
                    <button onclick="sendCommand('/restart')">↻ Restart Bot</button>
                </div>
            </div>
            <button onclick="refreshStatus(); return false;" class="nav-link" title="Refresh">↻</button>
            <button onclick="toggleAutoRefresh(); return false;" class="nav-link" id="auto-refresh-btn" title="Auto-refresh">⏱</button>
        </div>
        <button class="hamburger" onclick="toggleMobileMenu()">☰</button>
    </nav>
    <div class="mobile-menu" id="mobile-menu">
        <a href="#" class="nav-link active" data-tab="status" onclick="switchTab('status'); toggleMobileMenu(); return false;">Status</a>
        <a href="#" class="nav-link" data-tab="bot-log" onclick="switchTab('bot-log'); toggleMobileMenu(); return false;">Bot Log</a>
        <a href="#" class="nav-link" data-tab="wsgi-log" onclick="switchTab('wsgi-log'); toggleMobileMenu(); return false;">WSGI Log</a>
        <a href="#" class="nav-link" data-tab="stderr-log" onclick="switchTab('stderr-log'); toggleMobileMenu(); return false;">stderr.log</a>
        <a href="#" class="nav-link" data-tab="upload" onclick="switchTab('upload'); toggleMobileMenu(); return false;">📤 Upload</a>
        <button onclick="sendCommand('/start'); toggleMobileMenu();">▶ Start Bot</button>
        <button onclick="sendCommand('/stop'); toggleMobileMenu();">⏹ Stop Bot</button>
        <button onclick="sendCommand('/restart'); toggleMobileMenu();">↻ Restart Bot</button>
        <button onclick="refreshStatus(); toggleMobileMenu();">↻ Refresh</button>
        <button onclick="toggleAutoRefresh(); toggleMobileMenu();" id="auto-refresh-btn-mobile">⏱ Auto-refresh</button>
    </div>

    <div class="container">
        <div class="header">
            <div>
                <div class="brand">Ebook Bot Control Panel</div>
                <div class="subtitle">Monitor your bot process, manage lifecycle operations, upload new bot code, and see live status from the WSGI service.</div>
            </div>
            <div class="actions">
                <button class="button button--primary" onclick="refreshStatus()">Refresh</button>
                <button class="button button--ghost" onclick="toggleAutoRefresh()">Auto-refresh</button>
            </div>
        </div>

        <div class="grid card status-grid">
            <div class="panel">
                <h3>Bot status</h3>
                <p id="status-running">Loading...</p>
                <div class="note">Shows whether the subprocess is currently active.</div>
            </div>
            <div class="panel">
                <h3>Bot PID</h3>
                <p id="status-pid">—</p>
                <div class="note">The current process ID recorded in tmp/bot.pid.</div>
            </div>
            <div class="panel">
                <h3>Primary worker PID</h3>
                <p id="status-primary">—</p>
                <div class="note">PID of the Passenger worker that owns the bot process.</div>
            </div>
        </div>

        <div class="card" style="margin-top: 20px;">
            <div style="display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 18px;">
                <div>
                    <h3 style="margin:0 0 10px; color: var(--muted);">Actions</h3>
                    <div class="note">Use these buttons to start, stop, or restart the bot from the dashboard.</div>
                </div>
                <div class="actions">
                    <button class="button button--success" onclick="sendCommand('/start')">Start</button>
                    <button class="button button--danger" onclick="sendCommand('/stop')">Stop</button>
                    <button class="button button--warning" onclick="sendCommand('/restart')">Restart</button>
                </div>
            </div>
        </div>

        <div class="card" style="margin-top: 20px;">
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap;">
                <div>
                    <h3 style="margin:0 0 10px; color: var(--muted);">Live response</h3>
                    <div class="note">JSON status fetched from the WSGI service and live logs from the bot and WSGI processes.</div>
                </div>
                <div id="status-updated" class="note">Last updated: --</div>
            </div>
            <div class="tabs" style="margin-bottom: 18px; display: flex; gap: 10px; flex-wrap: wrap;">
                <button class="button button--ghost tab-button active" data-tab="status" onclick="showTab('status')">Status</button>
                <button class="button button--ghost tab-button" data-tab="bot-log" onclick="showTab('bot-log')">Bot logs</button>
                <button class="button button--ghost tab-button" data-tab="wsgi-log" onclick="showTab('wsgi-log')">WSGI logs</button>
                <button class="button button--ghost tab-button" data-tab="stderr-log" onclick="showTab('stderr-log')">stderr.log</button>
                <button class="button button--ghost tab-button" data-tab="upload" onclick="showTab('upload')">Upload Bot</button>
                <button class="button button--ghost tab-button" data-tab="backups" onclick="showTab('backups')">Backups</button>
            </div>
            <pre id="status-json" class="log tab-panel" data-panel="status">Loading status...</pre>
            <pre id="bot-log" class="log tab-panel" data-panel="bot-log" style="display:none;">Loading bot logs...</pre>
            <pre id="wsgi-log" class="log tab-panel" data-panel="wsgi-log" style="display:none;">Loading wsgi logs...</pre>
            <pre id="stderr-log" class="log tab-panel" data-panel="stderr-log" style="display:none;">Loading stderr.log...</pre>
            <div id="upload-panel" class="tab-panel" data-panel="upload" style="display:none;">
                <div class="upload-area" id="uploadArea" onclick="requestPinThenUpload()">
                    <div>📁 Click or drag to upload run_bot.py</div>
                    <div class="note" style="margin-top: 10px;">Upload a Python file to replace the bot code. PIN required for security.</div>
                    <input type="file" id="fileInput" accept=".py" />
                </div>
                <div id="uploadStatus" style="margin-top: 20px;"></div>
            </div>
            <div id="backups-panel" class="tab-panel" data-panel="backups" style="display:none;">
                <div class="backup-list" id="backupList">
                    <div class="note">Loading backups...</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let autoRefresh = false;
        let refreshInterval = null;
        let pendingFile = null;
        let pendingRollback = null;
        const basePath = window.location.pathname.replace(/\\/ui\\/?$/, '') || '/';

        function joinPath(base, suffix) {
            const normalizedBase = base.endsWith('/') ? base.slice(0, -1) : base;
            const normalizedSuffix = suffix.startsWith('/') ? suffix.slice(1) : suffix;
            return normalizedBase === '' ? `/${normalizedSuffix}` : `${normalizedBase}/${normalizedSuffix}`;
        }

        async function fetchStatus() {
            try {
                const endpoint = joinPath(basePath, '/status');
                const res = await fetch(endpoint);
                if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
                const data = await res.json();
                const running = data.running ? 'Running' : 'Stopped';
                document.getElementById('status-running').textContent = running;
                document.getElementById('status-pid').textContent = data.pid || 'N/A';
                document.getElementById('status-primary').textContent = data.primary_worker_pid || 'N/A';
                document.getElementById('status-json').textContent = JSON.stringify(data, null, 2);
                document.getElementById('status-updated').textContent = 'Last updated: ' + new Date().toLocaleString();
            } catch (error) {
                document.getElementById('status-running').textContent = 'Error';
                document.getElementById('status-json').textContent = 'Unable to fetch status. ' + error;
            }
        }

        async function sendCommand(path) {
            try {
                const endpoint = joinPath(basePath, path);
                const res = await fetch(endpoint, { method: 'POST' });
                if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
                const text = await res.text();
                await fetchStatus();
                showToast(text);
            } catch (error) {
                showToast('Command failed: ' + error, true);
            }
        }

        function showPinModal() {
            document.getElementById('pinModal').style.display = 'flex';
            document.getElementById('pinInput').value = '';
            document.getElementById('pinInput').focus();
        }

        function closePinModal() {
            document.getElementById('pinModal').style.display = 'none';
            pendingFile = null;
            pendingRollback = null;
        }

        async function submitPin() {
            const pin = document.getElementById('pinInput').value;
            if (!pin) {
                showToast('PIN is required', true);
                return;
            }
            
            closePinModal();
            
            if (pendingFile) {
                await uploadFileWithPin(pendingFile, pin);
                pendingFile = null;
            } else if (pendingRollback) {
                await rollbackWithPin(pendingRollback, pin);
                pendingRollback = null;
            }
        }

        function requestPinThenUpload() {
            const fileInput = document.getElementById('fileInput');
            fileInput.click();
            fileInput.onchange = (e) => {
                if (e.target.files.length > 0) {
                    pendingFile = e.target.files[0];
                    showPinModal();
                }
            };
        }

        async function uploadFileWithPin(file, pin) {
            if (!file.name.endsWith('.py')) {
                showToast('Please upload a .py file', true);
                return;
            }
            
            const formData = new FormData();
            formData.append('file', file);
            formData.append('pin', pin);
            
            const uploadStatus = document.getElementById('uploadStatus');
            uploadStatus.innerHTML = '<div class="note">Uploading...</div>';
            
            try {
                const endpoint = joinPath(basePath, '/upload-bot');
                const res = await fetch(endpoint, { method: 'POST', body: formData });
                const result = await res.text();
                
                if (res.ok) {
                    showToast('Upload successful! Bot will be restarted.');
                    uploadStatus.innerHTML = '<div class="note" style="color: var(--success);">✓ ' + result + '</div>';
                    setTimeout(() => {
                        fetchStatus();
                        refreshLogs();
                        loadBackups();
                    }, 2000);
                } else {
                    showToast('Upload failed: ' + result, true);
                    uploadStatus.innerHTML = '<div class="note" style="color: var(--danger);">✗ ' + result + '</div>';
                }
            } catch (error) {
                showToast('Upload error: ' + error, true);
                uploadStatus.innerHTML = '<div class="note" style="color: var(--danger);">✗ Upload failed: ' + error + '</div>';
            }
        }
        
        function requestRollback(backupName) {
            if (confirm('Rollback to ' + backupName + '? PIN required for this operation.')) {
                pendingRollback = backupName;
                showPinModal();
            }
        }
        
        async function rollbackWithPin(backupName, pin) {
            try {
                const endpoint = joinPath(basePath, '/rollback');
                const res = await fetch(endpoint, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ backup: backupName, pin: pin })
                });
                const result = await res.text();
                
                if (res.ok) {
                    showToast('Rollback successful!');
                    loadBackups();
                    setTimeout(() => {
                        fetchStatus();
                        refreshLogs();
                    }, 2000);
                } else {
                    showToast('Rollback failed: ' + result, true);
                }
            } catch (error) {
                showToast('Rollback error: ' + error, true);
            }
        }
        
        async function loadBackups() {
            try {
                const endpoint = joinPath(basePath, '/list-backups');
                const res = await fetch(endpoint);
                const backups = await res.json();
                
                const backupList = document.getElementById('backupList');
                if (backups.length === 0) {
                    backupList.innerHTML = '<div class="note">No backups available</div>';
                    return;
                }
                
                backupList.innerHTML = backups.map(backup => `
                    <div class="backup-item">
                        <div class="backup-info">
                            <div class="backup-name">${escapeHtml(backup.name)}</div>
                            <div class="backup-size">${formatBytes(backup.size)} - ${new Date(backup.modified).toLocaleString()}</div>
                        </div>
                        <button class="button button--ghost" onclick="requestRollback('${escapeHtml(backup.name)}')">Rollback (PIN)</button>
                    </div>
                `).join('');
            } catch (error) {
                document.getElementById('backupList').innerHTML = '<div class="note">Failed to load backups: ' + error + '</div>';
            }
        }
        
        function formatBytes(bytes) {
            if (bytes === 0) return '0 Bytes';
            const k = 1024;
            const sizes = ['Bytes', 'KB', 'MB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        function showToast(message, isError = false) {
            const toast = document.createElement('div');
            toast.className = 'toast' + (isError ? ' error' : '');
            toast.textContent = message;
            document.body.appendChild(toast);
            setTimeout(() => toast.remove(), 3000);
        }

        function refreshStatus() {
            fetchStatus();
        }

        function refreshLogs() {
            fetchBotLog();
            fetchWsgiLog();
            fetchStderrLog();
        }

        function toggleAutoRefresh() {
            autoRefresh = !autoRefresh;
            const desktopBtn = document.getElementById('auto-refresh-btn');
            const mobileBtn = document.getElementById('auto-refresh-btn-mobile');
            if (desktopBtn) desktopBtn.textContent = autoRefresh ? '⏱ ON' : '⏱';
            if (mobileBtn) mobileBtn.textContent = autoRefresh ? '⏱ ON' : '⏱';
            if (autoRefresh) {
                refreshInterval = setInterval(fetchStatus, 5000);
            } else if (refreshInterval) {
                clearInterval(refreshInterval);
                refreshInterval = null;
            }
        }

        function showTab(tabName) {
            document.querySelectorAll('.nav-link').forEach(link => {
                link.classList.toggle('active', link.dataset.tab === tabName);
            });
            document.querySelectorAll('.tab-button').forEach(button => {
                button.classList.toggle('active', button.dataset.tab === tabName);
            });
            document.querySelectorAll('.tab-panel').forEach(panel => {
                panel.style.display = panel.dataset.panel === tabName ? 'block' : 'none';
            });
            if (tabName === 'bot-log') fetchBotLog();
            else if (tabName === 'wsgi-log') fetchWsgiLog();
            else if (tabName === 'stderr-log') fetchStderrLog();
            else if (tabName === 'backups') loadBackups();
        }

        function switchTab(tabName) {
            showTab(tabName);
        }

        function toggleMobileMenu() {
            document.getElementById('mobile-menu').classList.toggle('show');
        }

        async function fetchBotLog() {
            try {
                const endpoint = joinPath(basePath, '/bot-log');
                const res = await fetch(endpoint);
                if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
                const text = await res.text();
                document.getElementById('bot-log').textContent = text || 'No bot log output yet.';
            } catch (error) {
                document.getElementById('bot-log').textContent = 'Unable to fetch bot logs. ' + error;
            }
        }

        async function fetchWsgiLog() {
            try {
                const endpoint = joinPath(basePath, '/wsgi-log');
                const res = await fetch(endpoint);
                if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
                const text = await res.text();
                document.getElementById('wsgi-log').textContent = text || 'No WSGI log output yet.';
            } catch (error) {
                document.getElementById('wsgi-log').textContent = 'Unable to fetch wsgi logs. ' + error;
            }
        }

        async function fetchStderrLog() {
            try {
                const endpoint = joinPath(basePath, '/stderr-log');
                const res = await fetch(endpoint);
                if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
                const text = await res.text();
                document.getElementById('stderr-log').textContent = text || 'No stderr.log output yet.';
            } catch (error) {
                document.getElementById('stderr-log').textContent = 'Unable to fetch stderr.log. ' + error;
            }
        }

        // Drag and drop handlers
        const uploadArea = document.getElementById('uploadArea');
        
        if (uploadArea) {
            uploadArea.addEventListener('dragover', (e) => {
                e.preventDefault();
                uploadArea.classList.add('drag-over');
            });
            
            uploadArea.addEventListener('dragleave', () => {
                uploadArea.classList.remove('drag-over');
            });
            
            uploadArea.addEventListener('drop', (e) => {
                e.preventDefault();
                uploadArea.classList.remove('drag-over');
                const files = e.dataTransfer.files;
                if (files.length > 0) {
                    pendingFile = files[0];
                    showPinModal();
                }
            });
        }

        window.addEventListener('load', () => {
            fetchStatus();
            refreshLogs();
            setInterval(refreshLogs, 5000);
            
            // Close modal when clicking outside
            document.getElementById('pinModal').addEventListener('click', (e) => {
                if (e.target === document.getElementById('pinModal')) {
                    closePinModal();
                }
            });
            
            // Submit on Enter key
            document.getElementById('pinInput').addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    submitPin();
                }
            });
        });
    </script>
</body>
</html>'''

    return html.encode('utf-8')


# ---------------------------------------------------------------------------
# WSGI application
# ---------------------------------------------------------------------------

def application(environ, start_response):
    path   = environ.get('PATH_INFO', '')
    method = environ.get('REQUEST_METHOD', 'GET')
    
    # Parse multipart form data for file uploads
    if path in ('/upload-bot', '/upload-bot/', '/ui/upload-bot', '/ui/upload-bot/') and method == 'POST':
        content_type = environ.get('CONTENT_TYPE', '')
        if 'multipart/form-data' not in content_type:
            start_response('400 Bad Request', [('Content-Type', 'text/plain')])
            return [b'Expected multipart/form-data']
        
        try:
            # Parse multipart data
            import cgi
            form = cgi.FieldStorage(fp=environ['wsgi.input'], environ=environ, keep_blank_values=True)
            
            # Verify PIN
            pin = form.getvalue('pin', '')
            if not verify_pin(pin):
                logger.warning(f"Failed PIN attempt from {environ.get('REMOTE_ADDR', 'unknown')}")
                start_response('401 Unauthorized', [('Content-Type', 'text/plain')])
                return [b'Invalid PIN. Unauthorized operation.']
            
            if 'file' not in form:
                start_response('400 Bad Request', [('Content-Type', 'text/plain')])
                return [b'No file uploaded']
            
            file_item = form['file']
            if not file_item.filename:
                start_response('400 Bad Request', [('Content-Type', 'text/plain')])
                return [b'No file selected']
            
            # Read file content
            file_content = file_item.file.read()
            
            # Validate and replace
            success, message = replace_python_file(BOT_RUNNER, file_content)
            
            if success:
                # Restart the bot with the new code
                kill_all_bot_processes()
                start_bot()
                logger.info(f"Bot code updated successfully by {environ.get('REMOTE_ADDR', 'unknown')}")
                start_response('200 OK', [('Content-Type', 'text/plain')])
                return [message.encode('utf-8')]
            else:
                start_response('400 Bad Request', [('Content-Type', 'text/plain')])
                return [message.encode('utf-8')]
                
        except Exception as e:
            logger.error(f"Upload error: {e}")
            start_response('500 Internal Server Error', [('Content-Type', 'text/plain')])
            return [f'Upload failed: {e}'.encode('utf-8')]
    
    # Rollback endpoint with PIN protection
    if path in ('/rollback', '/rollback/', '/ui/rollback', '/ui/rollback/') and method == 'POST':
        try:
            content_length = int(environ.get('CONTENT_LENGTH', 0))
            if content_length > 0:
                body = environ['wsgi.input'].read(content_length)
                data = json.loads(body.decode('utf-8'))
                backup_name = data.get('backup')
                pin = data.get('pin', '')
                
                if not verify_pin(pin):
                    logger.warning(f"Failed PIN attempt for rollback from {environ.get('REMOTE_ADDR', 'unknown')}")
                    start_response('401 Unauthorized', [('Content-Type', 'text/plain')])
                    return [b'Invalid PIN. Unauthorized operation.']
                
                if not backup_name:
                    start_response('400 Bad Request', [('Content-Type', 'text/plain')])
                    return [b'Backup name required']
                
                success, message = rollback_file(BOT_RUNNER, backup_name)
                
                if success:
                    logger.info(f"Rollback to {backup_name} successful from {environ.get('REMOTE_ADDR', 'unknown')}")
                    start_response('200 OK', [('Content-Type', 'text/plain')])
                    return [message.encode('utf-8')]
                else:
                    start_response('400 Bad Request', [('Content-Type', 'text/plain')])
                    return [message.encode('utf-8')]
            else:
                start_response('400 Bad Request', [('Content-Type', 'text/plain')])
                return [b'No request body']
        except Exception as e:
            start_response('500 Internal Server Error', [('Content-Type', 'text/plain')])
            return [f'Rollback failed: {e}'.encode('utf-8')]
    
    # List backups endpoint (no PIN required for viewing)
    if path in ('/list-backups', '/list-backups/', '/ui/list-backups', '/ui/list-backups/'):
        backups = list_backups()
        start_response('200 OK', [('Content-Type', 'application/json')])
        return [json.dumps(backups).encode('utf-8')]

    if path in ('/', '/ui', '/ui/'):
        body = _render_dashboard()
        start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8')])
        return [body]

    if path in ('/health', '/health/', '/ui/health', '/ui/health/'):
        s    = get_bot_status()
        body = f'OK running={s["running"]} pid={s["pid"]} primary={s["primary_worker"]}'.encode()
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [body]

    if path in ('/status', '/status/', '/ui/status', '/ui/status/'):
        start_response('200 OK', [('Content-Type', 'application/json')])
        return [json.dumps(get_bot_status()).encode()]

    if path in ('/bot-log', '/bot-log/', '/ui/bot-log', '/ui/bot-log/'):
        try:
            log_path = os.path.join(LOG_DIR, 'bot.log')
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                body = f.read().encode('utf-8')
        except FileNotFoundError:
            body = b'Bot log file not found.'
        except Exception as e:
            body = f'Error reading bot log: {e}'.encode('utf-8')
        start_response('200 OK', [('Content-Type', 'text/plain; charset=utf-8')])
        return [body]

    if path in ('/wsgi-log', '/wsgi-log/', '/ui/wsgi-log', '/ui/wsgi-log/'):
        try:
            log_path = os.path.join(LOG_DIR, 'wsgi.log')
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                body = f.read().encode('utf-8')
        except FileNotFoundError:
            body = b'WSGI log file not found.'
        except Exception as e:
            body = f'Error reading wsgi log: {e}'.encode('utf-8')
        start_response('200 OK', [('Content-Type', 'text/plain; charset=utf-8')])
        return [body]

    if path in ('/stderr-log', '/stderr-log/', '/ui/stderr-log', '/ui/stderr-log/'):
        try:
            with open(STDERR_LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
                body = f.read().encode('utf-8')
        except FileNotFoundError:
            body = b'stderr.log file not found.'
        except Exception as e:
            body = f'Error reading stderr.log: {e}'.encode('utf-8')
        start_response('200 OK', [('Content-Type', 'text/plain; charset=utf-8')])
        return [body]

    if path in ('/start', '/start/', '/ui/start', '/ui/start/') and method == 'POST':
        start_bot()
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [b'Bot start requested']

    if path in ('/stop', '/stop/', '/ui/stop', '/ui/stop/') and method == 'POST':
        stop_bot()
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [b'Bot stop requested']

    if path in ('/restart', '/restart/', '/ui/restart', '/ui/restart/') and method == 'POST':
        restart_bot(reason="HTTP request")
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [b'Bot restart requested']

    # Default
    if _is_primary_worker and not get_bot_status()['running']:
        start_bot()

    start_response('200 OK', [('Content-Type', 'text/plain')])
    return [b'Ebook Bot WSGI is running']


# ---------------------------------------------------------------------------
# Init - with error handling
# ---------------------------------------------------------------------------

def initialize():
    try:
        logger.info(f"Initializing worker PID {os.getpid()}")
        if _acquire_worker_lock():
            start_bot()
            logger.info("Primary worker ready")
        else:
            logger.info("Secondary worker ready")
    except Exception as e:
        print(f"Initialization error: {e}", file=sys.stderr)
        try:
            with open(os.path.join(LOG_DIR, 'wsgi.log'), 'a') as f:
                f.write(f"Initialization error: {e}\n")
        except:
            pass

initialize()