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
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
base_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, base_dir)

LOG_DIR = os.path.join(base_dir, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

TMP_DIR     = os.path.join(base_dir, 'tmp')
os.makedirs(TMP_DIR, exist_ok=True)


def _truncate_log_file(path: str) -> None:
    """Truncate a log file if it exists, or create it."""
    try:
        with open(path, 'w', encoding='utf-8'):
            pass
    except Exception:
        pass

PID_FILE    = os.path.join(TMP_DIR, 'bot.pid')
LOCK_FILE   = os.path.join(TMP_DIR, 'bot.lock')
RESTART_TXT = os.path.join(TMP_DIR, 'restart.txt')

BOT_RUNNER  = os.path.join(base_dir, 'run_bot.py')   # <-- actual entry point
STDERR_LOG_FILE = os.path.join(base_dir, 'stderr.log')

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_truncate_log_file(os.path.join(LOG_DIR, 'wsgi.log'))
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'wsgi.log')),
        logging.StreamHandler(sys.stdout),
    ],
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
        _lock_fh = open(LOCK_FILE, 'w')
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            ["pgrep", "-f", "python.*run_bot.py"],    # <-- matches our runner
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

    # Graceful stop
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

    # Wait up to 8 seconds for graceful exit
    deadline = time.time() + 8
    while time.time() < deadline:
        if not _find_all_bot_pids():
            break
        time.sleep(0.5)

    # Force-kill anything still running
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

    # No longer require primary worker – any worker can start the bot
    # because we kill everything first.
    with _process_lock:
        kill_all_bot_processes()

        logger.info("Starting fresh bot process...")
        try:
            _truncate_log_file(os.path.join(LOG_DIR, 'bot.log'))
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


def get_bot_status() -> dict:
    pid = _read_pid() if bot_process is None else bot_process.pid
    running = False
    if bot_process is not None:
        running = bot_process.poll() is None
    elif pid:
        running = _pid_is_alive(pid)
    return {
        'running': running,
        'pid': pid,
        'primary_worker': _is_primary_worker,
        'worker_pid': os.getpid(),
    }


# ---------------------------------------------------------------------------
# File watcher
# ---------------------------------------------------------------------------

def _collect_py_mtimes(directory: str) -> dict:
    mtimes = {}
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for fname in files:
            if fname.endswith('.py'):
                full = os.path.join(root, fname)
                try:
                    mtimes[full] = os.path.getmtime(full)
                except OSError:
                    pass
    return mtimes


def _touch_restart_txt():
    try:
        Path(RESTART_TXT).touch()
        logger.info("Touched tmp/restart.txt - Passenger will reload on next request")
    except Exception as e:
        logger.warning(f"Could not touch restart.txt: {e}")


def _file_watcher(poll_interval: float = 2.0):
    logger.info(f"File watcher started (every {poll_interval}s)")
    snapshot = _collect_py_mtimes(base_dir)

    while True:
        time.sleep(poll_interval)
        try:
            current = _collect_py_mtimes(base_dir)
        except Exception as e:
            logger.warning(f"Watcher scan error: {e}")
            continue

        changed = [p for p, m in current.items() if snapshot.get(p) != m] + \
                  [p for p in snapshot if p not in current]

        if changed:
            for p in changed:
                logger.info(f"Changed: {p}")
            restart_bot(reason="file change")
            _touch_restart_txt()
            snapshot = _collect_py_mtimes(base_dir)
        else:
            snapshot = current


def _start_file_watcher():
    t = threading.Thread(target=_file_watcher, name="py-file-watcher", daemon=True)
    t.start()


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
# WSGI application
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
            min-height: 100vh;
            background: radial-gradient(circle at top, rgba(79,209,255,0.16), transparent 25%),
                        radial-gradient(circle at bottom right, rgba(56,178,172,0.14), transparent 20%),
                        var(--bg);
            color: var(--text);
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
        @media (max-width: 740px) {
            .status-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <div class="brand">Ebook Bot Control Panel</div>
                <div class="subtitle">Monitor your bot process, manage lifecycle operations, and see live status from the WSGI service.</div>
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
                <h3>Primary worker</h3>
                <p id="status-primary">—</p>
                <div class="note">Indicates whether this Passenger worker owns the bot process.</div>
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
            </div>
            <pre id="status-json" class="log tab-panel" data-panel="status">Loading status…</pre>
            <pre id="bot-log" class="log tab-panel" data-panel="bot-log" style="display:none;">Loading bot logs…</pre>
            <pre id="wsgi-log" class="log tab-panel" data-panel="wsgi-log" style="display:none;">Loading wsgi logs…</pre>
            <pre id="stderr-log" class="log tab-panel" data-panel="stderr-log" style="display:none;">Loading stderr log…</pre>
        </div>
    </div>

    <script>
        let autoRefresh = false;
        let refreshInterval = null;
        const basePath = window.location.pathname.replace(/\/ui\/?$/, '') || '/';

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
                document.getElementById('status-primary').textContent = data.primary_worker ? 'Yes' : 'No';
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
                alert(text);
            } catch (error) {
                alert('Command failed: ' + error);
            }
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
            const button = document.querySelector('.button--ghost');
            button.textContent = autoRefresh ? 'Auto-refresh ON' : 'Auto-refresh';
            if (autoRefresh) {
                refreshInterval = setInterval(fetchStatus, 5000);
            } else if (refreshInterval) {
                clearInterval(refreshInterval);
                refreshInterval = null;
            }
        }

        function showTab(tabName) {
            document.querySelectorAll('.tab-button').forEach(button => {
                button.classList.toggle('active', button.dataset.tab === tabName);
            });
            document.querySelectorAll('.tab-panel').forEach(panel => {
                panel.style.display = panel.dataset.panel === tabName ? 'block' : 'none';
            });
            if (tabName === 'bot-log') {
                fetchBotLog();
            } else if (tabName === 'wsgi-log') {
                fetchWsgiLog();
            } else if (tabName === 'stderr-log') {
                fetchStderrLog();
            }
        }

        async function fetchBotLog() {
            try {
                const endpoint = joinPath(basePath, '/bot-log');
                const res = await fetch(endpoint);
                if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
                const text = await res.text();
                document.getElementById('bot-log').textContent = text || 'No bot log output yet.';
                document.getElementById('status-updated').textContent = 'Last updated: ' + new Date().toLocaleString();
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
                document.getElementById('status-updated').textContent = 'Last updated: ' + new Date().toLocaleString();
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
                document.getElementById('status-updated').textContent = 'Last updated: ' + new Date().toLocaleString();
            } catch (error) {
                document.getElementById('stderr-log').textContent = 'Unable to fetch stderr.log. ' + error;
            }
        }

        window.addEventListener('load', () => {
            fetchStatus();
            refreshLogs();
            setInterval(refreshLogs, 5000);
        });
    </script>
</body>
</html>'''

    return html.encode('utf-8')


def application(environ, start_response):
    path   = environ.get('PATH_INFO', '')
    method = environ.get('REQUEST_METHOD', 'GET')

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
# Init
# ---------------------------------------------------------------------------

def initialize():
    logger.info(f"Initializing worker PID {os.getpid()}")
    if _acquire_worker_lock():
        start_bot()
        _start_file_watcher()
        logger.info("Primary worker ready")
    else:
        logger.info("Secondary worker ready")


initialize()
