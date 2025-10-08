import os, sys, time, socket, shutil, threading, subprocess, psutil, base64, hmac, platform
import re
from typing import Optional, Set
from collections import deque
from datetime import timedelta
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session, make_response
from functools import wraps

# === PocketComfy portable configuration ===
from pathlib import Path

CONFIG_FILE = Path(__file__).with_suffix(".env")

def _load_env():
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip(); v = v.strip()
            if v and ((v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'"))):
                v = v[1:-1]
            v = os.path.expanduser(os.path.expandvars(v))
            if k and k not in os.environ:
                os.environ[k] = v
_load_env()

def _intenv(name, default):
    try: return int(os.getenv(name, str(default)))
    except: return default

# Defaults. All overridable via PocketComfy.env or environment variables
LOGIN_PASS                    = os.getenv("LOGIN_PASS", "")
DELETE_PASSWORD               = os.getenv("DELETE_PASSWORD", "")
DELETE_PATH                   = os.getenv("DELETE_PATH", "").strip()

COMFY_PATH                    = os.getenv("COMFY_PATH", "").strip()
MINI_PATH                     = os.getenv("MINI_PATH", "").strip()
SMART_GALLERY_PATH            = os.getenv("SMART_GALLERY_PATH", "").strip()

COMFY_PORT_DEFAULT            = _intenv("COMFY_PORT", 8188)
MINI_PORT_DEFAULT             = _intenv("MINI_PORT", 3000)
SMART_GALLERY_PORT_DEFAULT    = _intenv("SMART_GALLERY_PORT", 8189)
WAIT_FOR_GALLERY_SECS         = _intenv("WAIT_FOR_GALLERY_SECS", 60)
WAIT_FOR_COMFY_SECS           = _intenv("WAIT_FOR_COMFY_SECS", 120)
FALLBACK_MINI_DELAY_SECS      = _intenv("FALLBACK_MINI_DELAY_SECS", 30)
FLASK_PORT                    = _intenv("FLASK_PORT", 5000)

FORCE_FREE_COMFY_PORT         = os.getenv("FORCE_FREE_COMFY_PORT", "1") != "0"
FORCE_FREE_MINI_PORT          = os.getenv("FORCE_FREE_MINI_PORT", "1") != "0"
FORCE_FREE_SMART_GALLERY_PORT = os.getenv("FORCE_FREE_SMART_GALLERY_PORT", "1") != "0"


# ========================= APP/STATE ======================
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))

app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(hours=1),
    SESSION_REFRESH_EACH_REQUEST=False,
)
SESSION_IDLE_TIMEOUT = 3600

processes = {"comfy": None, "mini": None, "gallery": None}
lock = threading.Lock()
detected_ports = {"comfy": None, "mini": None, "gallery": None}

PYDIR = os.path.dirname(sys.executable)
PYTHON_EXE  = os.path.join(PYDIR, "python.exe")
PYTHONW_EXE = os.path.join(PYDIR, "pythonw.exe")
SCRIPT_PATH = os.path.abspath(__file__)

START_DELAY = int(os.environ.get("PC_START_DELAY", "0"))
SKIP_LAUNCH = os.environ.get("PC_SKIP_LAUNCH", "0") == "1"

# Static assets
BRAND_MASCOT_FILE = "comfy-mascot.png"
HERO_FILE         = "pocket-comfy-hero.png"

# ============= Security: CSRF + rate limiting =============
CSRF_TOKEN = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")

@app.before_request
def _csrf():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if request.path in ("/login", "/activity"):
            return
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        if not token or not hmac.compare_digest(token, CSRF_TOKEN):
            return ("Forbidden", 403)

RATE_WINDOW_SEC = 10
RATE_MAX_HITS  = 30
_rate = {}
@app.before_request
def _rate_limit_posts():
    if request.method != "POST": return
    ip = request.remote_addr or "?"
    now = time.time()
    q = _rate.setdefault(ip, deque())
    while q and (now - q[0]) > RATE_WINDOW_SEC: q.popleft()
    if len(q) >= RATE_MAX_HITS: return ("Too Many Requests", 429)
    q.append(now)

# ==================== Auth & Idle Timeout =================
def _safe_eq(a, b): return hmac.compare_digest(a, b)

def login_required(fn):
    @wraps(fn)
    def _wrap(*a, **k):
        if session.get("auth_ok"): return fn(*a, **k)
        return redirect(url_for("login", next=request.path))
    return _wrap

ACTIVITY_PATHS = {"/", "/mini", "/comfyui", "/activity"}
@app.before_request
def _idle_timeout_guard():
    if not session.get("auth_ok"):
        return
    now = time.time()
    last = session.get("last_seen", now)
    if now - last > SESSION_IDLE_TIMEOUT:
        session.clear()
        return redirect(url_for("login"))
    if request.path in ACTIVITY_PATHS or request.method == "POST" or request.headers.get("X-Activity") == "1":
        session["last_seen"] = now
        session.modified = True

@app.route("/activity", methods=["POST"])
def activity():
    if session.get("auth_ok"):
        session["last_seen"] = time.time()
        session.modified = True
        return "ok"
    return ("no", 401)

# ===================== Helper Functions ===================
def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(0.1)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"

def taskkill_tree(pid: Optional[int]):
    if not pid: return
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        else:
            try:
                proc = psutil.Process(pid)
            except psutil.NoSuchProcess:
                return
            for child in proc.children(recursive=True):
                try: child.kill()
                except Exception: continue
            try: proc.kill()
            except Exception: pass
    except Exception as e:
        print(f"[ERROR] taskkill failed for PID {pid}: {e}")

def kill_proc_handle(proc: Optional[subprocess.Popen]):
    if proc and proc.poll() is None: taskkill_tree(proc.pid)

def comfy_running_by_handle() -> bool:
    with lock:
        p = processes.get("comfy")
        return bool(p and p.poll() is None)

def mini_running_by_handle() -> bool:
    with lock:
        p = processes.get("mini")
        return bool(p and p.poll() is None)

def _inet_conns(p):
    fn = getattr(p, "net_connections", None)
    return fn(kind="inet") if fn else p.connections(kind="inet")

def _listen_ports(proc: psutil.Process) -> Set[int]:
    ports: Set[int] = set()
    try:
        for c in _inet_conns(proc):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port:
                ports.add(c.laddr.port)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    for ch in proc.children(recursive=True):
        try:
            for c in _inet_conns(ch):
                if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port:
                    ports.add(c.laddr.port)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return ports

def detect_port_for(key: str, preferred: Optional[int]) -> Optional[int]:
    with lock:
        p = processes.get(key)
        if not p or p.poll() is not None: return None
        try: root = psutil.Process(p.pid)
        except psutil.NoSuchProcess: return None
    ports = _listen_ports(root)
    if not ports: return None
    if preferred and preferred in ports: return preferred
    return sorted(ports)[0]

def pids_listening_on(port: int) -> set[int]:
    pids = set()
    for c in psutil.net_connections(kind="inet"):
        if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
            pids.add(c.pid)
    return pids

def is_port_in_use(port: int) -> bool:
    return bool(pids_listening_on(port))

def free_port(port: int, label: str):
    pids = pids_listening_on(port)
    if not pids: return
    print(f"[WARN] {label}: port {port} busy; terminating PIDs {sorted(pids)} …")
    for pid in list(pids):
        try: taskkill_tree(pid)
        except Exception as e: print(f"[ERROR] kill PID {pid} on port {port}: {e}")
    time.sleep(1.0)

def launch_comfy() -> bool:
    if platform.system() != "Windows":
        print("[WARN] launch_comfy skipped: not running on Windows.")
        return False
    if not COMFY_PATH:
        print("[INFO] Comfy launcher not configured. Skipping start."); return True
    if not os.path.exists(COMFY_PATH):
        print(f"[WARN] Comfy launcher not found: {COMFY_PATH}. Skipping."); return True
    try:
        if FORCE_FREE_COMFY_PORT and is_port_in_use(COMFY_PORT_DEFAULT):
            free_port(COMFY_PORT_DEFAULT, "ComfyUI")
        with lock:
            print(f"[INFO] Launching ComfyUI: {COMFY_PATH}")
            processes["comfy"] = subprocess.Popen(COMFY_PATH, shell=True, cwd=os.path.dirname(COMFY_PATH))
        return True
    except Exception as e:
        print(f"[ERROR] Failed to launch ComfyUI: {e}"); return False

def launch_mini() -> bool:
    if platform.system() != "Windows":
        print("[WARN] launch_mini skipped: not running on Windows.")
        return False
    if not MINI_PATH:
        print("[INFO] Mini launcher not configured. Skipping start."); return True
    if not os.path.exists(MINI_PATH):
        print(f"[WARN] Mini launcher not found: {MINI_PATH}. Skipping."); return True
    try:
        if FORCE_FREE_MINI_PORT and is_port_in_use(MINI_PORT_DEFAULT):
            free_port(MINI_PORT_DEFAULT, "Mini")
        with lock:
            print(f"[INFO] Launching ComfyUI Mini: {MINI_PATH}")
            processes["mini"] = subprocess.Popen(MINI_PATH, shell=True, cwd=os.path.dirname(MINI_PATH))
        return True
    except Exception as e:
        print(f"[ERROR] Failed to launch Mini: {e}"); return False

def wait_for_comfy_ready(timeout_secs: int) -> bool:
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if is_port_in_use(COMFY_PORT_DEFAULT):
            detected_ports["comfy"] = COMFY_PORT_DEFAULT; return True
        if not comfy_running_by_handle():
            time.sleep(1); continue
        with lock: p = processes.get("comfy")
        try: proc = psutil.Process(p.pid)
        except Exception: time.sleep(1); continue
        ports = _listen_ports(proc)
        if ports:
            best = COMFY_PORT_DEFAULT if COMFY_PORT_DEFAULT in ports else sorted(ports)[0]
            detected_ports["comfy"] = best; return True
        time.sleep(2)
    return is_port_in_use(COMFY_PORT_DEFAULT)

def gallery_running_by_handle() -> bool:
    with lock:
        p = processes.get("gallery")
        return bool(p and p.poll() is None)

def wait_for_gallery_ready(timeout_secs: int) -> bool:
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if is_port_in_use(SMART_GALLERY_PORT_DEFAULT):
            detected_ports["gallery"] = SMART_GALLERY_PORT_DEFAULT
            return True
        if not gallery_running_by_handle():
            time.sleep(1)
            continue
        with lock:
            p = processes.get("gallery")
        try:
            proc = psutil.Process(p.pid)
        except Exception:
            time.sleep(1)
            continue
        ports = _listen_ports(proc)
        if ports:
            best = SMART_GALLERY_PORT_DEFAULT if SMART_GALLERY_PORT_DEFAULT in ports else sorted(ports)[0]
            detected_ports["gallery"] = best
            return True
        time.sleep(2)
    return is_port_in_use(SMART_GALLERY_PORT_DEFAULT)

# Helper to patch invalid escape sequences in the Smart Gallery script
def _patch_invalid_escape_sequences(py_file: str) -> None:
    """
    Attempt to patch known invalid escape sequences in a Python file.

    Some ComfyUI Smart Gallery distributions embed absolute Windows paths in
    string literals without escaping backslashes (e.g. BASE_OUTPUT_PATH = 'C:\\ComfyUI\\path').
    On Python 3.12+, unrecognised escape sequences like '\\C' produce a SyntaxWarning and may become
    fatal in future versions. To avoid this, convert assignments of the form
    `<NAME> = '<drive>:\\...'` to raw strings by inserting an 'r' prefix before the opening quote.
    For example, `BASE_OUTPUT_PATH = 'C:\\path'` becomes `BASE_OUTPUT_PATH = r'C:\\path'`.

    The patch is idempotent: lines already using a raw string prefix are left unchanged.
    Any errors during patching are ignored.
    """
    try:
        with open(py_file, 'r', encoding='utf-8') as _f:
            lines = _f.readlines()
    except Exception:
        return
    modified = False
    patched_lines: list[str] = []
    assignment_re = re.compile(r"^(\s*\w+\s*=\s*)'([A-Za-z]:\\\\[^']*)'")
    for line in lines:
        m = assignment_re.match(line)
        if m and " r'" not in line:
            prefix, path_str = m.groups()
            patched_lines.append(f"{prefix}r'{path_str}'\n")
            modified = True
        else:
            patched_lines.append(line)
    if modified:
        try:
            with open(py_file, 'w', encoding='utf-8') as _f:
                _f.writelines(patched_lines)
        except Exception:
            pass

def launch_gallery() -> bool:
    """
    Launch the Smart Gallery process if configured.

    On Windows the Smart Gallery script may embed absolute Windows paths in
    string literals without escaping backslashes, which triggers
    `SyntaxWarning: invalid escape sequence` under Python 3.12+. To avoid
    this, the Smart Gallery file is patched immediately before launch to
    convert assignments like `BASE_OUTPUT_PATH = 'C:\\foo'` to raw strings
    (`BASE_OUTPUT_PATH = r'C\\foo'`). If the patch fails or the file is
    missing, the original script is used as-is.

    Additionally, the `PYTHONWARNINGS` environment variable is set to
    ignore `SyntaxWarning` for the Smart Gallery process to suppress any
    remaining warnings. This environment variable only applies to the child
    process.
    """
    if platform.system() != "Windows":
        print("[WARN] launch_gallery skipped: not running on Windows.")
        return False
    if not SMART_GALLERY_PATH:
        print("[INFO] Smart Gallery launcher not configured. Skipping start"); return True
    if not os.path.exists(SMART_GALLERY_PATH):
        print(f"[WARN] Smart Gallery script not found: {SMART_GALLERY_PATH}. Skipping."); return True

    # Patch the Smart Gallery script to escape backslashes in string literals
    _patch_invalid_escape_sequences(SMART_GALLERY_PATH)

    try:
        if FORCE_FREE_SMART_GALLERY_PORT and is_port_in_use(SMART_GALLERY_PORT_DEFAULT):
            free_port(SMART_GALLERY_PORT_DEFAULT, "Smart Gallery")
        exe = sys.executable  # use same interpreter/console
        print(f"[INFO] Launching Smart Gallery: {SMART_GALLERY_PATH}")
        # Prepare environment for the child process; suppress SyntaxWarning
        env = os.environ.copy()
        env.setdefault('PYTHONWARNINGS', 'ignore::SyntaxWarning')
        with lock:
            processes["gallery"] = subprocess.Popen(
                [exe, "-u", SMART_GALLERY_PATH],
                cwd=os.path.dirname(SMART_GALLERY_PATH),
                env=env,
            )
        return True
    except Exception as e:
        print(f"[ERROR] Failed to launch Smart Gallery: {e}")
        return False

    # Note: Any fallback logic to probe ComfyUI ports is removed from launch_gallery.
    # The function returns above on success or failure.

def launch_both():
    if not launch_comfy(): return
    def _start_mini_when_ready():
        print(f"[INFO] Waiting for ComfyUI to expose a port (<= {WAIT_FOR_COMFY_SECS}s)…")
        if wait_for_comfy_ready(WAIT_FOR_COMFY_SECS):
            print(f"[INFO] ComfyUI ready on port {detected_ports.get('comfy', COMFY_PORT_DEFAULT)} — starting Mini.")
            launch_mini()
        else:
            if is_port_in_use(COMFY_PORT_DEFAULT) or comfy_running_by_handle():
                print(f"[WARN] Probe inconclusive; launching Mini in {FALLBACK_MINI_DELAY_SECS}s…")
                time.sleep(FALLBACK_MINI_DELAY_SECS); launch_mini()
            else:
                print("[WARN] Skipping Mini: ComfyUI stopped during wait.")
    threading.Thread(target=_start_mini_when_ready, daemon=True).start()
def launch_all():
    # Launch Comfy + Mini using existing logic
    launch_both()
    # Start Gallery in parallel
    threading.Thread(target=lambda: (launch_gallery(), wait_for_gallery_ready(WAIT_FOR_GALLERY_SECS)), daemon=True).start()


def ensure_mini():
    need_comfy = not (is_port_in_use(COMFY_PORT_DEFAULT) or comfy_running_by_handle())
    if need_comfy:
        launch_comfy()
        wait_for_comfy_ready(WAIT_FOR_COMFY_SECS)
    if not (is_port_in_use(MINI_PORT_DEFAULT) or mini_running_by_handle()):
        launch_mini()

def stop_all():
    with lock:
        print("[INFO] Stopping Mini (if running)…");     kill_proc_handle(processes.get("mini"));     processes["mini"] = None
        print("[INFO] Stopping ComfyUI (if running)…");  kill_proc_handle(processes.get("comfy"));    processes["comfy"] = None
        print("[INFO] Stopping Gallery (if running)…");  kill_proc_handle(processes.get("gallery"));  processes["gallery"] = None
    try:
        free_port(MINI_PORT_DEFAULT, "Mini")
        free_port(COMFY_PORT_DEFAULT, "ComfyUI")
        free_port(SMART_GALLERY_PORT_DEFAULT, "Smart Gallery")
    except Exception as e:
        print(f"[WARN] free_port during stop_all: {e}")

def kill_other_controller_instances():
    this_pid = os.getpid()
    this_path = os.path.abspath(__file__)
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] == this_pid: continue
            name = (proc.info.get("name") or "").lower()
            if "python" not in name: continue
            cmd = proc.info.get("cmdline") or []
            for arg in cmd:
                try:
                    if os.path.abspath(arg) == this_path:
                        taskkill_tree(proc.info["pid"]); break
                except Exception:
                    continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

# ================= Relaunch Hidden/Visible =================
CREATE_NEW_CONSOLE   = 0x00000010
DETACHED_PROCESS     = 0x00000008
CREATE_NO_WINDOW     = 0x08000000

def _spawn_relaunch(exe_path: str, creationflags: int, env=None):
    try:
        subprocess.Popen([exe_path, "-u", SCRIPT_PATH],
                         cwd=os.path.dirname(SCRIPT_PATH),
                         creationflags=creationflags,
                         close_fds=True,
                         env=env)
        return True
    except Exception as e:
        print(f"[ERROR] Relaunch failed: {e}")
        return False

def relaunch_hidden_core():
    exe = PYTHONW_EXE if os.path.exists(PYTHONW_EXE) else sys.executable
    env = os.environ.copy()
    env["PC_START_DELAY"] = "2"
    ok = _spawn_relaunch(exe, DETACHED_PROCESS | CREATE_NO_WINDOW, env=env)
    if ok: time.sleep(0.3); os._exit(0)

def relaunch_visible_core_autostart():
    exe = PYTHON_EXE if os.path.exists(PYTHON_EXE) else sys.executable
    env = os.environ.copy()
    env["PC_START_DELAY"] = "2"
    ok = _spawn_relaunch(exe, CREATE_NEW_CONSOLE, env=env)
    if ok: time.sleep(0.3); os._exit(0)

def is_hidden_mode() -> bool:
    return "pythonw" in os.path.basename(sys.executable).lower()

# ======================= Login Page ========================
LOGIN_TEMPLATE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<title>Pocket Comfy • Login</title>
<link rel="apple-touch-icon" sizes="180x180" href="{{ apple_icon }}">
<link rel="icon" type="image/png" sizes="32x32" href="{{ favicon32 }}">
<link rel="icon" type="image/png" sizes="16x16" href="{{ favicon16 }}">
<link rel="preload" as="image" href="{{ hero_logo }}" imagesrcset="{{ hero_logo }}">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black">
<style>
:root{
  --bg:#070814; --panel:#121336; --txt:#eaf6ff; --ink:#0b0c1a;
  --purp1:#a86eff; --purp2:#5e36ff; --cyan:#00d9ff;
  --kb-shift: 0px; /* dynamic shift when keyboard shows */
}

/* ===== Fill the entire viewport with our gradient (fixes black top/bottom bands) ===== */
*{box-sizing:border-box
  margin:0; color:var(--txt);
  font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif;
  -webkit-text-size-adjust:100%;
}
html,body{height:100%;}
body{ /* backdrop lives on body now to avoid iOS/Safari gaps */
  min-height:100svh; min-height:100lvh; min-height:100dvh;
  background:
    radial-gradient(1600px 1100px at 50% -280px, rgba(168,110,255,.20), transparent 70%),
    radial-gradient(1400px 1000px at 100% -160px, rgba(0,217,255,.12), transparent 65%),
    radial-gradient(1600px 1200px at 50% 120%, rgba(0,217,255,.08), transparent 70%),
    linear-gradient(180deg, rgba(7,8,20,.98), rgba(7,8,20,.98)),
    var(--bg);
  background-repeat:no-repeat;
  background-attachment:fixed;
  background-size:cover;
  -webkit-transform:translateZ(0);
  will-change:transform;
}




/* layout */
.wrap{max-width:520px;margin:9vh auto 8vh;padding:0 20px}
.hero{position:relative;display:flex;justify-content:center;margin:0 0 18px;transform:translateZ(0);will-change:transform}
.heroWrap{position:relative; width:75vw; max-width:520px; aspect-ratio: 1 / 1; transform:translateZ(0); will-change:transform;}
.heroWrap img{position:absolute; inset:0; width:100%; height:100%; object-fit:contain; display:block;
  -webkit-transform:translateZ(0); transform:translateZ(0); -webkit-backface-visibility:hidden; backface-visibility:hidden;
  image-rendering:auto; pointer-events:none; will-change:transform,opacity;}
.aura{
  position:absolute; inset:-10%; border-radius:28px; pointer-events:none; opacity:.95; mix-blend-mode:screen;
  background: radial-gradient(120% 80% at 50% 15%, rgba(0,217,255,.28), transparent 60%),
             conic-gradient(from 0deg, rgba(168,110,255,.00) 0deg, rgba(168,110,255,.38) 50deg,
             rgba(0,217,255,.50) 120deg, rgba(146,72,255,.55) 210deg, rgba(0,217,255,.00) 320deg);
  filter: blur(22px) saturate(1.12); -webkit-transform: translateZ(0) scale(1.02); transform: translateZ(0) scale(1.02);
  will-change: transform, opacity, filter; animation: auraSpin 7.2s linear infinite, auraBreathe 5.6s ease-in-out infinite;
}
@keyframes auraSpin { from { transform:rotate(0deg) scale(1.02);} to { transform:rotate(360deg) scale(1.02);} }
@keyframes auraBreathe { 0%,100% { filter: blur(20px) saturate(1.05); opacity:.92; }
  50% { filter: blur(26px) saturate(1.20); opacity:1; } }
@supports (-webkit-touch-callout: none){ .aura{ filter: blur(18px) saturate(1.18); } }

.card{
  background:#121336; border:1px solid rgba(0,217,255,.24); border-radius:18px; padding:20px;
  box-shadow:0 0 0 1px rgba(0,217,255,.10), 0 14px 36px rgba(0,0,0,.55);
  transform: translateY(var(--kb-shift));
  transition: transform .20s cubic-bezier(.22,.61,.36,1);
  will-change: transform;
}
h1{margin:0 0 14px;text-align:center;font-size:1.65rem;color:#d7c9ff}
.row{display:flex;flex-direction:column;gap:12px}
label{font-size:.95rem;color:#c7d0ff}

/* prevent iOS auto-zoom */
input, button, select, textarea{font-size:16px;}

.vh{position:absolute;left:-10000px;width:1px;height:1px;opacity:0;pointer-events:none}
.pwdwrap{display:flex;align-items:center;gap:8px}
input{ width:100%;padding:14px;border-radius:12px;border:1px solid #2f3472; background:#141549;color:#eaf6ff;text-align:center}
.eye{ width:46px;height:46px;border-radius:10px;border:1px solid #2f3472; background:#141549;cursor:pointer;display:flex;align-items:center;justify-content:center;line-height:1;}
.eye svg{width:22px;height:22px;stroke:#cfe3ff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
button.submit{ width:100%;padding:14px;margin-top:6px;border:0;border-radius:12px;font-weight:800;cursor:pointer;
  background:linear-gradient(45deg,#a86eff,#5e36ff);color:#0b0c1a; box-shadow:0 10px 18px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.06);}
.note{margin-top:10px;text-align:center;color:#8aa;font-size:.9rem}

/* subtle hint while keyboard is up */
body.kb-open .hero{opacity:.96; transition:opacity .2s ease;}

/* extra room for the iOS accessory/“Not Secure” bar only while keyboard is open */
body.kb-open .wrap{ padding-bottom: calc(env(safe-area-inset-bottom) + 90px); }
@supports not (padding-bottom: env(safe-area-inset-bottom)){
  body.kb-open .wrap{ padding-bottom: 90px; }
}

/* --- Stability for sticky header & vivid green lock text --- */
.header{transform:translateZ(0);-webkit-transform:translateZ(0);backface-visibility:hidden;-webkit-backface-visibility:hidden;will-change:transform;contain:paint}
.statusHot{color:#40f19a;font-weight:800}


/* soften all status dots a bit */
.mode-dot{opacity:.55}

/* shutdown status styling */
#headerBar.statusComplete{color:#ff5f6d;font-weight:800}
</style></head><body>
<div class="wrap">
  <div class="hero">
    <div class="heroWrap">
      <div class="aura"></div>
      <img src="{{ hero_logo }}" alt="Pocket Comfy" decoding="async" fetchpriority="high">
    </div>
  </div>
  <div class="card" id="loginCard">
    <h1>Pocket Comfy • Login</h1>
    <form method="post" action="/login" autocomplete="on">
      <input class="vh" id="username" name="username" value="admin" autocomplete="username">
      <div class="row">
        <div class="pwdwrap">
          <input id="pwd" name="password" type="password" autocomplete="current-password" placeholder="Password" inputmode="text"/>
          <button class="eye" id="eyeBtn" title="Show/Hide" type="button" aria-label="Toggle password visibility">
            <svg id="eyeIcon" viewBox="0 0 24 24">
              <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/>
              <circle cx="12" cy="12" r="3"/>
            </svg>
          </button>
        </div>
        <button class="submit" type="submit">Enter</button>
      </div>
    </form>
    <div class="note">Note: Connection is not secure. Credentials travel in clear text over the internet.</div>
  </div>
</div>
<script>
// Fresh page after mode switch
if (new URLSearchParams(location.search).has('switched')) {
  setTimeout(()=>{ location.replace('/login?r='+Date.now()); }, 250);
}

const pwd = document.getElementById('pwd');
const eyeBtn = document.getElementById('eyeBtn');
const eyeIcon = document.getElementById('eyeIcon');
function setEye(show){
  if(show){
    pwd.type='text';
    eyeIcon.innerHTML='<path d="M3 3L21 21"/><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>';
  }else{
    pwd.type='password';
    eyeIcon.innerHTML='<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>';
  }
}
eyeBtn.addEventListener('click', e=>{ e.preventDefault(); setEye(pwd.type==='password'); });

/* ===== Smooth & reliable mobile keyboard handling ===== */
(function(){
  const root = document.documentElement;
  const card = document.getElementById('loginCard');
  const form = card.closest('form') || card;
  const vv = window.visualViewport;
  const ua = navigator.userAgent;
  const isIOS = /iP(?:hone|ad|od)/.test(ua) && /Safari/.test(ua) && !/CriOS|FxiOS/.test(ua);

  // Baselines per orientation (iOS toolbars change height)
  let basePortrait = vv ? vv.height : window.innerHeight;
  let baseLandscape = basePortrait;

  let current = 0;  // current applied shift
  let target = 0;   // target shift
  let raf = null;
  let kbOpen = false;

  function lerp(a,b,t){ return a + (b-a)*t; }

  function setVar(px){
    root.style.setProperty('--kb-shift', Math.round(px) + 'px');
  }

  function animateTo(next){
    target = next;
    if (raf) return;
    const step = () => {
      const diff = target - current;
      if (Math.abs(diff) < 0.6){
        current = target;
        setVar(current);
        raf = null;
        return;
      }
      current = lerp(current, target, 0.22); // smooth, springy
      setVar(current);
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
  }

  function orientationBase(){
    const portrait = (window.orientation === 0 || window.orientation === 180 || window.matchMedia('(orientation: portrait)').matches);
    return portrait ? basePortrait : baseLandscape;
  }

  function recordBase(){
    if (!vv) return;
    const portrait = (window.orientation === 0 || window.orientation === 180 || window.matchMedia('(orientation: portrait)').matches);
    if (portrait){
      basePortrait = Math.max(basePortrait, vv.height);
    } else {
      baseLandscape = Math.max(baseLandscape, vv.height);
    }
  }

  function isKeyboardOpen(){
    if (!vv) return false;
    recordBase();
    const baseline = orientationBase();
    // Threshold tuned for iOS/Android; counts “Not Secure” accessory bar as part of keyboard
    return (baseline - vv.height) > 120;
  }

  function computeShift(){
    const el = document.activeElement;
    if (!el || !card.contains(el)) return 0;

    const rect = card.getBoundingClientRect();
    const viewportBottom = vv ? (vv.height + vv.offsetTop) : window.innerHeight;

    // iOS http accessory bar: give a little more room while open
    const insecureExtra = (isIOS && location.protocol === 'http:' && kbOpen) ? 64 : 0;
    const margin = 12 + insecureExtra;

    const overlap = rect.bottom + margin - viewportBottom;
    if (overlap <= 0) return 0;

    const topCushion = 10;
    const maxUp = Math.max(0, rect.top - topCushion);
    return -Math.min(overlap, maxUp);
  }

  function update(){
    const open = isKeyboardOpen();
    kbOpen = open;
    document.body.classList.toggle('kb-open', open);
    const next = open ? computeShift() : 0;
    animateTo(next);

    // If we just closed, make absolutely sure we settle at 0
    if (!open){
      setTimeout(()=>animateTo(0), 120);
    }
  }

  // Focus management
  form.addEventListener('focusin', () => setTimeout(update, 50));
  form.addEventListener('focusout', () => setTimeout(update, 80));

  // Tap outside to close (helps Safari settle)
  document.addEventListener('touchstart', (e)=>{
    if (kbOpen && !card.contains(e.target)){
      try{ document.activeElement.blur(); }catch(e){}
      setTimeout(update, 80);
    }
  }, {passive:true});

  if (vv){
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
  }
  window.addEventListener('resize', update);
  window.addEventListener('orientationchange', ()=> setTimeout(()=>{ basePortrait = baseLandscape = vv ? vv.height : window.innerHeight; update(); }, 180));
  window.addEventListener('pageshow', ()=> setTimeout(update, 50));
  document.addEventListener('visibilitychange', ()=> { if (!document.hidden) setTimeout(update, 50); });

  // Nudge into view when focusing
  pwd.addEventListener('focus', () => { setTimeout(()=>{ try{ pwd.scrollIntoView({block:'center', behavior:'smooth'});}catch(e){} }, 80); }, {passive:true});
})();
</script>
</body></html>
"""

# ---------- MINI PAGE (frame + toolbar) ----------
MINI_TEMPLATE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>Pocket Comfy • Mini</title>
<meta name="theme-color" content="#000000">
<link rel="apple-touch-icon" sizes="180x180" href="{{ apple_icon }}">
<link rel="icon" type="image/png" sizes="32x32" href="{{ favicon32 }}">
<link rel="icon" type="image/png" sizes="16x16" href="{{ favicon16 }}">
<style>
:root{
  --bg:#0a0b1e;
  --panel:#0b0c1a;
  --ink:#eaf6ff;

  /* neon + aurora palette */
  --magenta-rgb: 255, 43, 214;
  --magenta: rgb(var(--magenta-rgb));
  --c-cyan:#00eaff;
  --c-mint:#7cffc4;
  --c-violet:#b28dff;
  --c-blue:#00b3ff;
  --c-red:#ff5f6d;

  --barH:45px;
  --barRadius:16px;
  --sideBorder:3px;
  --bottomBorder:2px;          /* ↓ slightly thinner for more space */
  --glassBlur:12px;
  --barSidePad:12px;

  /* Aurora tuning */
  --aurora-blur:8px;
  --aurora-opacity:.30;
  --aurora-red-opacity:.60;
  --aurora-speed-base:12s;
  --aurora-speed-red:7.5s;
}

*{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body{ height:100%; }
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif;
}

/* ===== Top bar wrapper ===== */
.topbar-wrap{
  position:fixed; left:0; right:0; top:0; z-index:1000;
  padding:
    calc(env(safe-area-inset-top) + 3px)   /* ↑ nudge bar closer to top */
    calc(max(env(safe-area-inset-right),0px) + var(--barSidePad))
    6px
    calc(max(env(safe-area-inset-left),0px) + var(--barSidePad));
}
.topbar{
  position:relative;
  height:var(--barH);
  display:flex; align-items:center;
  border-radius:var(--barRadius);
  background:linear-gradient(180deg, rgba(0,0,0,.92), rgba(0,0,0,.96) 60%, #000 100%);
  border:1px solid rgba(255,255,255,.06);
  box-shadow:0 10px 26px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.06), inset 0 -1px 0 rgba(255,255,255,.03);
  backdrop-filter:blur(var(--glassBlur)) saturate(130%);
  -webkit-backdrop-filter:blur(var(--glassBlur)) saturate(130%);
  overflow:visible;
}

/* Neon pulse ring (unchanged) */
.topbar::after{
  content:"";
  position:absolute; inset:-6px; border-radius:inherit;
  background:radial-gradient(closest-side, rgba(0,234,255,.55), rgba(0,234,255,.25) 55%, rgba(0,234,255,0) 70%);
  opacity:0; transform:scale(.96);
  pointer-events:none;
}
.topbar.pulse::after{ animation:barPulse 1.6s ease-out .2s 1 forwards; }
@keyframes barPulse{
  0%   { opacity:.0;  transform:scale(.96); }
  15%  { opacity:.9;  transform:scale(.985); }
  60%  { opacity:.25; transform:scale(1.06); }
  100% { opacity:0;   transform:scale(1.08); }
}

.bar-inner{
  display:grid; grid-template-columns:44px 1fr 44px;
  align-items:center; width:100%; padding:0 4px;
}

/* ===== Flat deep-black circular buttons, magenta ring slightly toned down ===== */
.iconbtn{
  appearance:none; cursor:pointer;
  position:relative; isolation:isolate;
  width:36px; height:36px; border-radius:999px;
  display:inline-grid; place-items:center; overflow:hidden;

  background:#000;
  border:1px solid rgba(var(--magenta-rgb), .82); /* ↓ “smidge” less glow */
  box-shadow:none;
  backdrop-filter:none; -webkit-backdrop-filter:none;

  color:#fff;
  transition:transform .08s ease, opacity .12s ease;
  -webkit-mask-image: radial-gradient(closest-side, #000 99.7%, transparent 100%);
          mask-image: radial-gradient(closest-side, #000 99.7%, transparent 100%);
  -webkit-mask-repeat:no-repeat; mask-repeat:no-repeat;
  -webkit-mask-position:center; mask-position:center;
  -webkit-mask-size:100% 100%; mask-size:100% 100%;
}

/* Aurora layers INSIDE the circle, fully clipped */
.iconbtn .aurora{
  position:absolute; inset:0;
  border-radius:inherit;
  clip-path:circle(50% at 50% 50%);
  -webkit-mask-image: radial-gradient(closest-side, #000 99.7%, transparent 100%);
          mask-image: radial-gradient(closest-side, #000 99.7%, transparent 100%);
  filter:blur(var(--aurora-blur)); mix-blend-mode:screen;
  will-change:transform; transform:translateZ(0);
  pointer-events:none;
}
.iconbtn .aurora.base{
  opacity:var(--aurora-opacity);
  background:
    radial-gradient(60% 70% at 20% 30%, var(--c-cyan) 0%, transparent 60%),
    radial-gradient(55% 65% at 78% 35%, var(--c-mint) 0%, transparent 68%),
    radial-gradient(65% 55% at 52% 80%, var(--c-violet) 0%, transparent 68%),
    radial-gradient(55% 70% at 32% 72%, var(--c-blue) 0%, transparent 80%);
  animation:auroraDrift var(--aurora-speed-base) ease-in-out infinite;
}
.iconbtn .aurora.red{
  opacity:var(--aurora-red-opacity);
  background:radial-gradient(70% 50% at 70% 25%, rgba(255,95,109,.95) 0%, rgba(255,95,109,0) 60%);
  animation:auroraRibbon var(--aurora-speed-red) linear infinite;
}

/* subtle inner specular */
.iconbtn::after{
  content:""; position:absolute; inset:0; border-radius:inherit;
  background:linear-gradient(180deg, rgba(255,255,255,.12), rgba(255,255,255,0) 45%);
  mix-blend-mode:soft-light; pointer-events:none;
}

/* icon */
.iconbtn svg{
  position:relative; z-index:1; width:20px; height:20px;
  stroke:currentColor; fill:none; stroke-width:2; stroke-linecap:round; stroke-linejoin:round;
}

/* interactions */
.iconbtn:hover{ opacity:.96; }
.iconbtn:active{ transform:translateY(1px); }
.iconbtn:focus-visible{ outline:2px solid rgba(var(--magenta-rgb), .85); outline-offset:2px; }

/* Motion */
@keyframes auroraDrift{
  0%   { transform:translate(-12%, -9%) scale(1.04) rotate(0deg); }
  50%  { transform:translate(12%, 9%)  scale(1.12) rotate(180deg); }
  100% { transform:translate(-12%, -9%) scale(1.04) rotate(360deg); }
}
@keyframes auroraRibbon{
  0%   { transform:translate(-18%, -6%) scale(1.05) rotate(0deg); }
  50%  { transform:translate(18%, 8%)  scale(1.18) rotate(180deg); }
  100% { transform:translate(-18%, -6%) scale(1.05) rotate(360deg); }
}

@media (prefers-reduced-motion: reduce){
  .iconbtn .aurora{ animation:none; }
  .topbar.pulse::after{ animation:none; opacity:0; }
}

/* Title */
.title{
  text-align:center; font-weight:700; font-size:.9rem; line-height:1;
  letter-spacing:.2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  text-shadow:0 1px 1px rgba(0,0,0,.35); padding:0 4px;
}
.title a{ color:#eaf6ff; text-decoration:none; }

/* Frame — moved up slightly */
.frameWrap{
  position:fixed;
  top:calc(var(--barH) + env(safe-area-inset-top) + 6px); /* ↓ was +12px */
  left:0; right:0; bottom:max(env(safe-area-inset-bottom), 0px);
  background:var(--panel);
  border-left:var(--sideBorder) solid #000;
  border-right:var(--sideBorder) solid #000;
  border-top:1px solid #000;
  border-bottom:var(--bottomBorder) solid #000; /* thinner */
  overflow:hidden;
}
iframe{ width:100%; height:100%; border:0; display:block; background:#0b0c1a; }
.sr-only{
  position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
  clip:rect(0,0,0,0); white-space:nowrap; border:0;
}
@supports (-webkit-touch-callout: none) { .frameWrap{ bottom:0; } }
@media (max-width:360px){ .title{ font-size:.86rem; } }

/* --- Stability for sticky header & vivid green lock text --- */
.header{transform:translateZ(0);-webkit-transform:translateZ(0);backface-visibility:hidden;-webkit-backface-visibility:hidden;will-change:transform;contain:paint}
.statusHot{color:#40f19a;font-weight:800}
/* shutdown status styling */
#headerBar.statusComplete{color:#ff5f6d;font-weight:800}


</style></head><body>

  <div class="topbar-wrap">
    <div class="topbar" id="topbar">
      <div class="bar-inner">
        <button class="iconbtn" id="backBtn" title="Back" aria-label="Back">
          <span class="aurora base"></span>
          <span class="aurora red"></span>
          <svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>
        </button>

        <div class="title">
          ComfyUI Mini • <a href="https://github.com/ImDarkTom" target="_blank" rel="noopener">ImDarkTom</a>
        </div>

        <button class="iconbtn" id="refreshBtn" title="Refresh" aria-label="Refresh">
          <span class="aurora base"></span>
          <span class="aurora red"></span>
          <svg viewBox="0 0 24 24">
            <path d="M3 12a9 9 0 0 1 14.5-6.36M21 12a9 9 0 0 1-14.5 6.36"/>
            <path d="M3 5v6h6"/>
          </svg>
        </button>
      </div>
    </div>
  </div>

  <div class="frameWrap">
    <iframe id="miniFrame" src="about:blank" referrerpolicy="no-referrer"></iframe>
  </div>

  <div class="sr-only" id="miniStatus" aria-live="polite">Loading Comfy Mini…</div>

<script>
const CSRF = "{{ csrf_token }}";
const miniURL = location.protocol + '//' + location.hostname + ':3000/';

async function ensureMini(){
  try { await fetch('/ensure_mini', { method:'POST', headers:{ 'X-CSRF-Token': CSRF } }); }
  catch(e) {}
}
function bustURL(u){ return u + (u.includes('?') ? '&' : '?') + 'r=' + Date.now(); }

/* Trigger the neon pulse on the bar (on demand) */
function triggerBarPulse(){
  const bar = document.getElementById('topbar');
  if(!bar) return;
  bar.classList.remove('pulse');
  void bar.offsetWidth;
  bar.classList.add('pulse');
}

function loadMini(){
  const f = document.getElementById('miniFrame');
  const st = document.getElementById('miniStatus');
  f.onload = () => { st.textContent = 'Comfy Mini Ready'; };
  f.src = bustURL(miniURL);
}

document.getElementById('backBtn').addEventListener('click', () => { location.href = '/'; });

document.getElementById('refreshBtn').addEventListener('click', () => {
  const f = document.getElementById('miniFrame');
  f.src = bustURL(miniURL);
  document.getElementById('miniStatus').textContent = 'Refreshing…';
  triggerBarPulse(); // pulse on refresh tap
});

(async () => {
  await ensureMini();
  loadMini();
  triggerBarPulse();   // pulse once on first load
})();
</script>
</body></html>
"""


# ---------- COMFYUI PAGE (frame + toolbar) ---------- 
COMFYUI_TEMPLATE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>Pocket Comfy • ComfyUI</title>
<meta name="theme-color" content="#000000">
<link rel="apple-touch-icon" sizes="180x180" href="{{ apple_icon }}">
<link rel="icon" type="image/png" sizes="32x32" href="{{ favicon32 }}">
<link rel="icon" type="image/png" sizes="16x16" href="{{ favicon16 }}">
<style>
:root{
  --bg:#0a0b1e;
  --panel:#0b0c1a;
  --ink:#eaf6ff;

  /* neon + aurora palette (slightly bluer for ComfyUI) */
  --magenta-rgb: 80, 140, 255;       /* blue ring */
  --magenta: rgb(var(--magenta-rgb));
  --c-cyan:#00eaff;
  --c-mint:#7cffc4;
  --c-violet:#b28dff;
  --c-blue:#00b3ff;
  --c-red:#ff5f6d;

  --barH:45px;
  --barRadius:16px;
  --sideBorder:3px;
  --bottomBorder:2px;
  --glassBlur:12px;
  --barSidePad:12px;

  /* Aurora tuning */
  --aurora-blur:8px;
  --aurora-opacity:.30;
  --aurora-red-opacity:.60;
  --aurora-speed-base:12s;
  --aurora-speed-red:7.5s;
}

*{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body{ height:100%; }
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif;
}

/* ===== Top bar wrapper ===== */
.topbar-wrap{
  position:fixed; left:0; right:0; top:0; z-index:1000;
  padding:
    calc(env(safe-area-inset-top) + 3px)
    calc(max(env(safe-area-inset-right),0px) + var(--barSidePad))
    6px
    calc(max(env(safe-area-inset-left),0px) + var(--barSidePad));
}
.topbar{
  position:relative;
  height:var(--barH);
  display:flex; align-items:center;
  border-radius:var(--barRadius);
  background:linear-gradient(180deg, rgba(0,0,0,.92), rgba(0,0,0,.96) 60%, #000 100%);
  border:1px solid rgba(255,255,255,.06);
  box-shadow:0 10px 26px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.06), inset 0 -1px 0 rgba(255,255,255,.03);
  backdrop-filter:blur(var(--glassBlur)) saturate(130%);
  -webkit-backdrop-filter:blur(var(--glassBlur)) saturate(130%);
  overflow:visible;
}

/* Neon pulse ring */
.topbar::after{
  content:"";
  position:absolute; inset:-6px; border-radius:inherit;
  background:radial-gradient(closest-side, rgba(0,184,255,.55), rgba(0,184,255,.25) 55%, rgba(0,184,255,0) 70%);
  opacity:0; transform:scale(.96);
  pointer-events:none;
}
.topbar.pulse::after{ animation:barPulse 1.6s ease-out .2s 1 forwards; }
@keyframes barPulse{
  0%   { opacity:.0;  transform:scale(.96); }
  15%  { opacity:.9;  transform:scale(.985); }
  60%  { opacity:.25; transform:scale(1.06); }
  100% { opacity:0;   transform:scale(1.08); }
}

.bar-inner{
  display:grid; grid-template-columns:44px 1fr 44px;
  align-items:center; width:100%; padding:0 4px;
}

/* ===== Flat deep-black circular buttons (blue ring for ComfyUI) ===== */
.iconbtn{
  appearance:none; cursor:pointer;
  position:relative; isolation:isolate;
  width:36px; height:36px; border-radius:999px;
  display:inline-grid; place-items:center; overflow:hidden;

  background:#000;
  border:1px solid rgba(var(--magenta-rgb), .86);
  box-shadow:none; backdrop-filter:none; -webkit-backdrop-filter:none;

  color:#fff;
  transition:transform .08s ease, opacity .12s ease;
  -webkit-mask-image: radial-gradient(closest-side, #000 99.7%, transparent 100%);
          mask-image: radial-gradient(closest-side, #000 99.7%, transparent 100%);
  -webkit-mask-repeat:no-repeat; mask-repeat:no-repeat;
  -webkit-mask-position:center; mask-position:center;
  -webkit-mask-size:100% 100%; mask-size:100% 100%;
}

/* Aurora layers INSIDE the circle, fully clipped */
.iconbtn .aurora{
  position:absolute; inset:0; border-radius:inherit;
  clip-path:circle(50% at 50% 50%);
  -webkit-mask-image: radial-gradient(closest-side, #000 99.7%, transparent 100%);
          mask-image: radial-gradient(closest-side, #000 99.7%, transparent 100%);
  filter:blur(var(--aurora-blur)); mix-blend-mode:screen;
  will-change:transform; transform:translateZ(0);
  pointer-events:none;
}
.iconbtn .aurora.base{
  opacity:var(--aurora-opacity);
  background:
    radial-gradient(60% 70% at 20% 30%, var(--c-cyan) 0%, transparent 60%),
    radial-gradient(55% 65% at 78% 35%, var(--c-mint) 0%, transparent 68%),
    radial-gradient(65% 55% at 52% 80%, var(--c-violet) 0%, transparent 68%),
    radial-gradient(55% 70% at 32% 72%, var(--c-blue) 0%, transparent 80%);
  animation:auroraDrift var(--aurora-speed-base) ease-in-out infinite;
}
.iconbtn .aurora.red{
  opacity:var(--aurora-red-opacity);
  background:radial-gradient(70% 50% at 70% 25%, rgba(255,95,109,.95) 0%, rgba(255,95,109,0) 60%);
  animation:auroraRibbon var(--aurora-speed-red) linear infinite;
}

/* subtle inner specular */
.iconbtn::after{
  content:""; position:absolute; inset:0; border-radius:inherit;
  background:linear-gradient(180deg, rgba(255,255,255,.12), rgba(255,255,255,0) 45%);
  mix-blend-mode:soft-light; pointer-events:none;
}

/* icon */
.iconbtn svg{
  position:relative; z-index:1; width:20px; height:20px;
  stroke:currentColor; fill:none; stroke-width:2; stroke-linecap:round; stroke-linejoin:round;
}

/* interactions */
.iconbtn:hover{ opacity:.96; }
.iconbtn:active{ transform:translateY(1px); }
.iconbtn:focus-visible{ outline:2px solid rgba(var(--magenta-rgb), .85); outline-offset:2px; }

/* Motion */
@keyframes auroraDrift{
  0%   { transform:translate(-12%, -9%) scale(1.04) rotate(0deg); }
  50%  { transform:translate(12%, 9%)  scale(1.12) rotate(180deg); }
  100% { transform:translate(-12%, -9%) scale(1.04) rotate(360deg); }
}
@keyframes auroraRibbon{
  0%   { transform:translate(-18%, -6%) scale(1.05) rotate(0deg); }
  50%  { transform:translate(18%, 8%)  scale(1.18) rotate(180deg); }
  100% { transform:translate(-18%, -6%) scale(1.05) rotate(360deg); }
}

@media (prefers-reduced-motion: reduce){
  .iconbtn .aurora{ animation:none; }
  .topbar.pulse::after{ animation:none; opacity:0; }
}

/* Title */
.title{
  text-align:center; font-weight:700; font-size:.9rem; line-height:1;
  letter-spacing:.2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  text-shadow:0 1px 1px rgba(0,0,0,.35); padding:0 4px;
}
.title a{ color:#eaf6ff; text-decoration:none; }

/* Frame */
.frameWrap{
  position:fixed;
  top:calc(var(--barH) + env(safe-area-inset-top) + 6px);
  left:0; right:0; bottom:max(env(safe-area-inset-bottom), 0px);
  background:var(--panel);
  border-left:var(--sideBorder) solid #000;
  border-right:var(--sideBorder) solid #000;
  border-top:1px solid #000;
  border-bottom:var(--bottomBorder) solid #000;
  overflow:hidden;
}
iframe{ width:100%; height:100%; border:0; display:block; background:#0b0c1a; }
.sr-only{
  position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
  clip:rect(0,0,0,0); white-space:nowrap; border:0;
}
@supports (-webkit-touch-callout: none) { .frameWrap{ bottom:0; } }
@media (max-width:360px){ .title{ font-size:.86rem; } }

/* === Auto-hide border + topbar in landscape to maximize canvas === */
@media (orientation: landscape){
  .topbar-wrap{ display:none !important; }
  .frameWrap{ top:0 !important; border:0 !important; }
}

/* --- Stability for sticky header & vivid green lock text --- */
.header{transform:translateZ(0);-webkit-transform:translateZ(0);backface-visibility:hidden;-webkit-backface-visibility:hidden;will-change:transform;contain:paint}
.statusHot{color:#40f19a;font-weight:800}
/* shutdown status styling */
#headerBar.statusComplete{color:#ff5f6d;font-weight:800}


</style></head><body>

  <div class="topbar-wrap">
    <div class="topbar" id="topbar">
      <div class="bar-inner">
        <button class="iconbtn" id="backBtn" title="Back" aria-label="Back">
          <span class="aurora base"></span>
          <span class="aurora red"></span>
          <svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>
        </button>

       <div class="title">
  <a href="https://www.comfy.org/" target="_blank" rel="noopener">ComfyUI</a>
</div>
        <button class="iconbtn" id="refreshBtn" title="Refresh" aria-label="Refresh">
          <span class="aurora base"></span>
          <span class="aurora red"></span>
          <svg viewBox="0 0 24 24">
            <path d="M3 12a9 9 0 0 1 14.5-6.36M21 12a9 9 0 0 1-14.5 6.36"/>
            <path d="M3 5v6h6"/>
          </svg>
        </button>
      </div>
    </div>
  </div>

  <div class="frameWrap">
    <iframe id="comfyFrame" src="about:blank" referrerpolicy="no-referrer"></iframe>
  </div>

  <div class="sr-only" id="comfyStatus" aria-live="polite">Loading ComfyUI…</div>

<script>
const CSRF = "{{ csrf_token }}";
async function getComfyURL(){
  try{
    const n = await (await fetch('/netinfo')).json();
    const port = (n && n.comfy_port) ? n.comfy_port : 8188;
    const host = location.hostname || '127.0.0.1';
    // direct http is expected (same-LAN). If you serve over https, ensure a proxy to avoid mixed content.
    return location.protocol + '//' + host + ':' + port + '/';
  }catch(_){
    const host = location.hostname || '127.0.0.1';
    return location.protocol + '//' + host + ':8188/';
  }
}

async function ensureComfy(){
  try { await fetch('/ensure_comfy', { method:'POST', headers:{ 'X-CSRF-Token': CSRF } }); } catch(e){}
}
function bustURL(u){ return u + (u.includes('?') ? '&' : '?') + 'r=' + Date.now(); }
function triggerBarPulse(){
  const bar = document.getElementById('topbar');
  if(!bar) return;
  bar.classList.remove('pulse'); void bar.offsetWidth; bar.classList.add('pulse');
}
async function loadComfy(){
  const f = document.getElementById('comfyFrame');
  const st = document.getElementById('comfyStatus');
  const url = await getComfyURL();
  f.onload = () => { st.textContent = 'ComfyUI Ready'; };
  f.src = bustURL(url);
}

document.getElementById('backBtn').addEventListener('click', () => { location.href = '/'; });
document.getElementById('refreshBtn').addEventListener('click', async () => {
  const f = document.getElementById('comfyFrame');
  const url = await getComfyURL();
  f.src = bustURL(url);
  document.getElementById('comfyStatus').textContent = 'Refreshing…';
  triggerBarPulse();
});

(async () => {
  await ensureComfy();
  await loadComfy();
  triggerBarPulse();
})();
</script>
</body></html>
"""



# ---------- SMART GALLERY PAGE (frame + toolbar) ----------
GALLERY_TEMPLATE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>Pocket Comfy • Gallery</title>
<meta name="theme-color" content="#000000">
<link rel="apple-touch-icon" sizes="180x180" href="{{ apple_icon }}">
<link rel="icon" type="image/png" sizes="32x32" href="{{ favicon32 }}">
<link rel="icon" type="image/png" sizes="16x16" href="{{ favicon16 }}">
<style>
:root{
  --bg:#0a0b1e;
  --panel:#0b0c1a;
  --ink:#eaf6ff;

  /* neon + aurora palette (warmer ring for Gallery) */
  --magenta-rgb: 255, 120, 90;
  --magenta: rgb(var(--magenta-rgb));
  --c-cyan:#00eaff;
  --c-mint:#7cffc4;
  --c-violet:#b28dff;
  --c-blue:#00b3ff;
  --c-red:#ff5f6d;

  --barH:45px;
  --barRadius:16px;
  --sideBorder:3px;
  --bottomBorder:2px;
  --glassBlur:12px;
  --barSidePad:12px;

  /* Aurora tuning */
  --aurora-blur:8px;
  --aurora-opacity:.30;
  --aurora-red-opacity:.60;
  --aurora-speed-base:12s;
  --aurora-speed-red:7.5s;
}

*{ box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body{ height:100%; }
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif;
}

/* ===== Top bar wrapper ===== */
.topbar-wrap{
  position:fixed; left:0; right:0; top:0; z-index:1000;
  padding:
    calc(env(safe-area-inset-top) + 3px)
    calc(max(env(safe-area-inset-right),0px) + var(--barSidePad))
    6px
    calc(max(env(safe-area-inset-left),0px) + var(--barSidePad));
}
.topbar{
  position:relative;
  height:var(--barH);
  display:flex; align-items:center;
  border-radius:var(--barRadius);
  background:linear-gradient(180deg, rgba(0,0,0,.92), rgba(0,0,0,.96) 60%, #000 100%);
  border:1px solid rgba(255,255,255,.06);
  box-shadow:0 10px 26px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.06), inset 0 -1px 0 rgba(255,255,255,.03);
  backdrop-filter:blur(var(--glassBlur)) saturate(130%);
  -webkit-backdrop-filter:blur(var(--glassBlur)) saturate(130%);
  overflow:visible;
}

/* Neon pulse ring */
.topbar::after{
  content:"";
  position:absolute; inset:-6px; border-radius:inherit;
  background:radial-gradient(closest-side, rgba(255,160,120,.55), rgba(255,160,120,.25) 55%, rgba(255,160,120,0) 70%);
  opacity:0; transform:scale(.96);
  pointer-events:none;
}
.topbar.pulse::after{ animation:barPulse 1.6s ease-out .2s 1 forwards; }
@keyframes barPulse{
  0%   { opacity:.0;  transform:scale(.96); }
  15%  { opacity:.9;  transform:scale(.985); }
  60%  { opacity:.25; transform:scale(1.06); }
  100% { opacity:0;   transform:scale(1.08); }
}

.bar-inner{
  display:grid; grid-template-columns:44px 1fr 44px;
  align-items:center; width:100%; padding:0 4px;
}

/* ===== Flat deep-black circular buttons ===== */
.iconbtn{
  appearance:none; cursor:pointer;
  position:relative; isolation:isolate;
  width:36px; height:36px; border-radius:999px;
  display:inline-grid; place-items:center; overflow:hidden;

  background:#000;
  border:1px solid rgba(var(--magenta-rgb), .86);
  box-shadow:none; backdrop-filter:none; -webkit-backdrop-filter:none;

  color:#fff;
  transition:transform .08s ease, opacity .12s ease;
  -webkit-mask-image: radial-gradient(closest-side, #000 99.7%, transparent 100%);
          mask-image: radial-gradient(closest-side, #000 99.7%, transparent 100%);
  -webkit-mask-repeat:no-repeat; mask-repeat:no-repeat;
  -webkit-mask-position:center; mask-position:center;
  -webkit-mask-size:100% 100%; mask-size:100% 100%;
}

/* Aurora layers INSIDE the circle, fully clipped */
.iconbtn .aurora{
  position:absolute; inset:0;
  border-radius:inherit;
  clip-path:circle(50% at 50% 50%);
  -webkit-mask-image: radial-gradient(closest-side, #000 99.7%, transparent 100%);
          mask-image: radial-gradient(closest-side, #000 99.7%, transparent 100%);
  filter:blur(var(--aurora-blur)); mix-blend-mode:screen;
  will-change:transform; transform:translateZ(0);
  pointer-events:none;
}
.iconbtn .aurora.base{
  opacity:var(--aurora-opacity);
  background:
    radial-gradient(60% 70% at 20% 30%, var(--c-cyan) 0%, transparent 60%),
    radial-gradient(55% 65% at 78% 35%, var(--c-mint) 0%, transparent 68%),
    radial-gradient(65% 55% at 52% 80%, var(--c-violet) 0%, transparent 68%),
    radial-gradient(55% 70% at 32% 72%, var(--c-blue) 0%, transparent 80%);
  animation:auroraDrift var(--aurora-speed-base) ease-in-out infinite;
}
.iconbtn .aurora.red{
  opacity:var(--aurora-red-opacity);
  background:radial-gradient(70% 50% at 70% 25%, rgba(255,95,109,.95) 0%, rgba(255,95,109,0) 60%);
  animation:auroraRibbon var(--aurora-speed-red) linear infinite;
}

/* subtle inner specular */
.iconbtn::after{
  content:""; position:absolute; inset:0; border-radius:inherit;
  background:linear-gradient(180deg, rgba(255,255,255,.12), rgba(255,255,255,0) 45%);
  mix-blend-mode:soft-light; pointer-events:none;
}

/* icon */
.iconbtn svg{
  position:relative; z-index:1; width:20px; height:20px;
  stroke:currentColor; fill:none; stroke-width:2; stroke-linecap:round; stroke-linejoin:round;
}

/* interactions */
.iconbtn:hover{ opacity:.96; }
.iconbtn:active{ transform:translateY(1px); }
.iconbtn:focus-visible{ outline:2px solid rgba(var(--magenta-rgb), .85); outline-offset:2px; }

/* Motion */
@keyframes auroraDrift{
  0%   { transform:translate(-12%, -9%) scale(1.04) rotate(0deg); }
  50%  { transform:translate(12%, 9%)  scale(1.12) rotate(180deg); }
  100% { transform:translate(-12%, -9%) scale(1.04) rotate(360deg); }
}
@keyframes auroraRibbon{
  0%   { transform:translate(-18%, -6%) scale(1.05) rotate(0deg); }
  50%  { transform:translate(18%, 8%)  scale(1.18) rotate(180deg); }
  100% { transform:translate(-18%, -6%) scale(1.05) rotate(360deg); }
}

@media (prefers-reduced-motion: reduce){
  .iconbtn .aurora{ animation:none; }
  .topbar.pulse::after{ animation:none; opacity:0; }
}

/* Title */
.title{
  text-align:center; font-weight:700; font-size:.9rem; line-height:1;
  letter-spacing:.2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  text-shadow:0 1px 1px rgba(0,0,0,.35); padding:0 4px;
}
.title a{ color:#eaf6ff; text-decoration:none; }

/* Frame */
.frameWrap{
  position:fixed;
  top:calc(var(--barH) + env(safe-area-inset-top) + 6px);
  left:0; right:0; bottom:max(env(safe-area-inset-bottom), 0px);
  background:var(--panel);
  border-left:var(--sideBorder) solid #000;
  border-right:var(--sideBorder) solid #000;
  border-top:1px solid #000;
  border-bottom:var(--bottomBorder) solid #000;
  overflow:hidden;
}
iframe{ width:100%; height:100%; border:0; display:block; background:#0b0c1a; }
.sr-only{
  position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
  clip:rect(0,0,0,0); white-space:nowrap; border:0;
}
@supports (-webkit-touch-callout: none) { .frameWrap{ bottom:0; } }
@media (max-width:360px){ .title{ font-size:.86rem; } }

/* === Auto-hide border + topbar in landscape === */
@media (orientation: landscape){
  .topbar-wrap{ display:none !important; }
  .frameWrap{ top:0 !important; border:0 !important; }
}

/* --- Stability for sticky header & vivid green lock text --- */
.header{transform:translateZ(0);-webkit-transform:translateZ(0);backface-visibility:hidden;-webkit-backface-visibility:hidden;will-change:transform;contain:paint}
.statusHot{color:#40f19a;font-weight:800}
/* shutdown status styling */
#headerBar.statusComplete{color:#ff5f6d;font-weight:800}


</style></head><body>

  <div class="topbar-wrap">
    <div class="topbar" id="topbar">
      <div class="bar-inner">
        <button class="iconbtn" id="backBtn" title="Back" aria-label="Back">
          <span class="aurora base"></span>
          <span class="aurora red"></span>
          <svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>
        </button>

       <div class="title">
  <a href="https://github.com/biagiomaf/smart-comfyui-gallery" target="_blank" rel="noopener">Smart Gallery • biagiomaf</a>
</div>

        <button class="iconbtn" id="refreshBtn" title="Refresh" aria-label="Refresh">
          <span class="aurora base"></span>
          <span class="aurora red"></span>
          <svg viewBox="0 0 24 24">
            <path d="M3 12a9 9 0 0 1 14.5-6.36M21 12a9 9 0 0 1-14.5 6.36"/>
            <path d="M3 5v6h6"/>
          </svg>
        </button>
      </div>
    </div>
  </div>

  <div class="frameWrap">
    <iframe id="galleryFrame" src="about:blank" referrerpolicy="no-referrer"></iframe>
  </div>

  <div class="sr-only" id="galleryStatus" aria-live="polite">Loading Smart Gallery…</div>

<script>
const CSRF = "{{ csrf_token }}";
async function getGalleryURL(){
  try{
    const n = await (await fetch('/netinfo')).json();
    const port = (n && n.gallery_port) ? n.gallery_port : 8189;
    const host = location.hostname || '127.0.0.1';
    // direct http is expected (same-LAN). If you serve over https, proxy to avoid mixed content.
    return location.protocol + '//' + host + ':' + port + '/';
  }catch(_){
    const host = location.hostname || '127.0.0.1';
    return location.protocol + '//' + host + ':8189/';
  }
}

async function ensureGallery(){
  try { await fetch('/ensure_gallery', { method:'POST', headers:{ 'X-CSRF-Token': CSRF } }); } catch(e){}
}
function bustURL(u){ return u + (u.includes('?') ? '&' : '?') + 'r=' + Date.now(); }

/* Trigger the neon pulse on the bar */
function triggerBarPulse(){
  const bar = document.getElementById('topbar');
  if(!bar) return;
  bar.classList.remove('pulse'); void bar.offsetWidth; bar.classList.add('pulse');
}

async function loadGallery(){
  const f = document.getElementById('galleryFrame');
  const st = document.getElementById('galleryStatus');
  const url = await getGalleryURL();
  f.onload = () => { st.textContent = 'Smart Gallery Ready'; };
  f.src = bustURL(url);
}

document.getElementById('backBtn').addEventListener('click', () => { location.href = '/'; });
document.getElementById('refreshBtn').addEventListener('click', async () => {
  const f = document.getElementById('galleryFrame');
  const url = await getGalleryURL();
  f.src = bustURL(url);
  document.getElementById('galleryStatus').textContent = 'Refreshing…';
  triggerBarPulse();
});

(async () => {
  await ensureGallery();
  await loadGallery();
  triggerBarPulse();
})();
</script>
</body></html>
"""

# ---------- MAIN CONTROL PAGE ----------         
TEMPLATE = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Pocket Comfy</title>
<link rel="apple-touch-icon" sizes="180x180" href="{{ apple_icon }}">
<link rel="icon" type="image/png" sizes="32x32" href="{{ favicon32 }}">
<link rel="icon" type="image/png" sizes="16x16" href="{{ favicon16 }}">
<link rel="preload" as="image" href="{{ url_for('static', filename='comfy-mascot.webp') }}{% if request.args.get('switched') %}?v={{ request.args.get('r','') }}{% endif %}">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black">
<style>
:root{
  --bg0:#0a0b1e; --panel:#121436; --text:#eaf6ff; --muted:#a7b6ff;
  --accent1:#a86eff; --accent2:#5e36ff; --accent3:#00d9ff;
  --accent4:#17d5ff; --accent5:#21a0ff; --accent6:#7a5cff;
  --radius:16px; --btn-radius:14px;

  /* ===== GALLERY ICON NUDGE — TWEAK HERE =====
     Increase/decrease to move the gallery logo horizontally. */
  --gallery-icon-nudge-x: 4px;

  /* ===== MINI ALIGN TWEAKS — TWEAK HERE =====
     Shift Mini icon+label left and control Mini icon size. */
  --mini-group-nudge-x: 15px;   /* move Mini icon+text left (increase to go further left) */
  --mini-icon-size: 2.85em;     /* Mini icon size (was ~2.35em) */
}
*{box-sizing:border-box}
body{
  margin:0;color:var(--text);
  font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif;
  background:
    radial-gradient(1200px 600px at 50% -120px, rgba(168,110,255,.12), transparent),
    radial-gradient(900px 480px at 100% 0, rgba(0,217,255,.10), transparent),
    var(--bg0);
}

/* header/status */
.header{
  position:sticky;top:0;z-index:1000;
  background:linear-gradient(180deg, rgba(18,19,54,.95), rgba(18,19,54,.85));
  padding:8px 10px; display:flex; align-items:center; justify-content:center; gap:10px; flex-wrap:wrap;
  box-shadow:0 2px 10px rgba(0,0,0,.5)
}
.badge{display:inline-flex;align-items:center;gap:8px;padding:6px 10px;border-radius:999px;font-weight:700;font-size:.95rem;border:1px solid rgba(255,255,255,.16)}
.badge svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.badge.ok{color:#0b2;background:linear-gradient(180deg, rgba(45,185,120,.15), rgba(45,185,120,.08));border-color:rgba(45,185,120,.35)}
.badge.bad{color:#f36;background:linear-gradient(180deg, rgba(255,86,120,.17), rgba(255,86,120,.08));border-color:rgba(255,86,120,.35)}
.sep{opacity:.45;color:#bcd}

h1{margin:18px 0 8px;text-align:center;font-size:1.7rem;color:#e8edff;font-weight:800;letter-spacing:.2px}
.brand{display:inline-flex;align-items:center;gap:14px;justify-content:center}

/* mascot next to title */
.mascot{position:relative;width:76px;height:76px;border-radius:50%;flex:0 0 auto;opacity:.9;transform:translateZ(0);will-change:transform,opacity}
.mascot a{
  display:block; width:100%; height:100%;
  border-radius:50%; overflow:hidden;
  -webkit-tap-highlight-color: transparent;
  outline:none;
}
.mascot a:focus{ outline:none; }
.mascot a:focus-visible{
  outline:2px solid rgba(23,213,255,.55);
  outline-offset:3px;
}
.mascot img{width:100%;height:100%;border-radius:50%;display:block;object-fit:cover;pointer-events:none;user-select:none}
.mascot::before,.mascot::after{content:"";position:absolute;inset:-14px;border-radius:50%;z-index:-1;filter:blur(16px);will-change:transform,opacity,background-position,filter;transform:translateZ(0)}
.mascot::before{background:conic-gradient(from 0deg, rgba(0,220,255,.55), rgba(120,60,255,.45), rgba(0,220,255,.55), rgba(120,60,255,.45), rgba(0,220,255,.55));animation:auroraSpin 7.5s linear infinite}
.mascot::after{background:radial-gradient(60% 60% at 30% 30%, rgba(255,255,255,.25), rgba(255,255,255,0) 60%),radial-gradient(70% 70% at 70% 60%, rgba(0,255,200,.22), rgba(0,255,200,0) 65%),radial-gradient(65% 65% at 40% 75%, rgba(120,80,255,.22), rgba(120,80,255,0) 60%);animation:auroraFlow 3.6s cubic-bezier(.22,.61,.36,1) infinite alternate}
@keyframes auroraSpin{0%{transform:translate3d(0,0,0) rotate(0deg) scale(1)}50%{transform:translate3d(0,0,0) rotate(180deg) scale(1.05)}100%{transform:translate3d(0,0,0) rotate(360deg) scale(1)}}
@keyframes auroraFlow{0%{opacity:.75;background-position:0% 0%,40% 60%,60% 80%}50%{opacity:.95;background-position:60% 40%,80% 20%,30% 30%}100%{opacity:.70;background-position:100% 80%,0% 100%,80% 0%}}

.brand .title{display:inline-block;line-height:1;text-shadow:0 1px 0 rgba(255,255,255,.04)}

.container{max-width:520px;margin:0 auto;padding:16px;display:flex;flex-direction:column;gap:14px}
.panel{background:linear-gradient(180deg, rgba(255,255,255,.02), transparent), var(--panel);border:1px solid #2a2f73;border-radius:var(--radius);padding:14px;box-shadow:0 8px 18px rgba(0,0,0,.45)}
.panel.warn{border-color:#5a1a2a;background:linear-gradient(180deg, rgba(255,30,77,.05), transparent), #151020}

/* default CTA link (Open Comfy Mini keeps gradient) */
a.btnlink,a.btnlink:visited{
  display:block;width:100%;text-align:center;text-decoration:none;color:#0b0c1a;
  font-weight:800;padding:18px;border-radius:var(--btn-radius);
  box-shadow:0 10px 18px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.06);
  background:linear-gradient(90deg, var(--accent4), var(--accent6));
  -webkit-user-select:none; user-select:none; -webkit-touch-callout:none;
  -webkit-tap-highlight-color:transparent; touch-action:manipulation;
}
a.btnlink:hover{transform:translateY(-1px)}

/* buttons */
button{
  width:100%;padding:18px;font-size:1.06rem;font-weight:800;border:0;border-radius:14px;color:#0b0c1a;
  cursor:pointer;position:relative;overflow:hidden;box-shadow:0 10px 18px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.06);
  transition:transform .12s ease, box-shadow .12s ease, filter .12s ease;will-change:transform,filter;transform:translateZ(0);
  -webkit-user-select:none; user-select:none; -webkit-touch-callout:none; -webkit-tap-highlight-color:transparent; touch-action:manipulation;
}
button::after{content:"";position:absolute;inset:0;border-radius:inherit;background:radial-gradient(120% 60% at 50% -10%, rgba(255,255,255,.14), transparent 42%);pointer-events:none}
button:hover{transform:translate3d(0,-1px,0);filter:brightness(1.02)}
button:active{transform:translate3d(0,0,0) scale(.998)}
button + button{margin-top:12px}
button:disabled{opacity:.45;filter:grayscale(.25);cursor:not-allowed;transform:none!important;box-shadow:none!important}
.hold-fill{position:absolute;inset:0 0 auto 0;height:100%;width:0%;background:rgba(255,255,255,.15);border-radius:14px;pointer-events:none}
.restart{background:linear-gradient(90deg, var(--accent1), var(--accent2));color:#fff}
.stop{background:linear-gradient(180deg,#d4d9e3,#b7becb);color:#0a0e14}
.shutdown{background:linear-gradient(90deg, #ff1e4d, #ff4b2b);color:#fff}
.delete{background:linear-gradient(90deg, #ff8a5c, #ffb86b);color:#2a1f00}
.recreate{background:linear-gradient(90deg, #67f39c, #2cf5e0);color:#0c1b18}
/* Run Hidden / Visible baseline (use for ALL other buttons) */
.vis{ background:linear-gradient(180deg,#323640,#232731); color:#f3f6ff; }

/* Disabled state for anchor buttons (Comfy Mini / ComfyUI) */
a.btnlink.disabled,
a.btnlink[aria-disabled="true"]{
  opacity:.45; filter:grayscale(.25);
  pointer-events:none; cursor:not-allowed;
  transform:none!important; box-shadow:none!important;
}

.ico{display:inline-flex;vertical-align:-0.2em;margin-right:.55rem}
.ico svg{width:1.15em;height:1.15em;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

/* --- Non-selectable descriptive text --- */
#headerBar, h1, .brand, .brand .title, .tiny, .muted, .statusLine, .sigLine, #netinfo, label, .modeBadge, .sep{
  -webkit-user-select:none; user-select:none;
}

/* mode dots on Run Hidden/Visible */
.mode-dot{position:absolute;right:10px;bottom:10px;width:10px;height:10px;border-radius:50%;border:2px solid rgba(255,255,255,.6);background:transparent}
.mode-dot.on{background:#40f19a;border-color:#2cc67a}
.mode-dot.off{ background:#ff4d6d; border-color:#ff2a4a; }
.mode-dot.hidden{display:none}
/* ensure dots above overlays */
#visHideBtn .mode-dot, #visShowBtn .mode-dot{ z-index:3; }
#visHideBtn .hold-fill, #visShowBtn .hold-fill{ z-index:1; }
#visHideBtn::after, #visShowBtn::after{ z-index:0; }
/* gallery dot layering */
#openGalleryBtn .mode-dot{ z-index:3; }

/* Open Mini/Open ComfyUI bracket sizing */
.container>.panel:first-of-type{
  margin-top:-42px;
  padding-top:16px; 
  padding-bottom:12px;
}
.container>.panel:first-of-type .btnlink{
  display:flex; align-items:center; justify-content:center; gap:14px;
  padding:34px 26px; font-size:1.40rem;
}
.container>.panel:first-of-type .btnlink .ico{margin:0}

/* PRIMARY: Comfy Mini — FLAT black + thin aurora ribbons */
#openMiniBtn,
#openMiniBtn:visited{
  padding:34px 26px;
  font-size:1.40rem;
  position:relative;

  overflow:hidden;
  isolation:isolate;
  contain:paint;
  clip-path: inset(0 round var(--btn-radius));

  background:#000;
  color:#fff !important;
  border-radius:var(--btn-radius);
  border:1px solid rgba(255,255,255,.06);
  box-shadow:none !important;
  backdrop-filter:none !important;
  text-shadow:none;
  transform:translateZ(0);
}
#openMiniBtn .ico{ margin-right:.65rem; }
#openMiniBtn .ico svg{ width:1.85em; height:1.85em; }
/* Mini icon size now controlled by variable */
#openMiniBtn .ico img{ width:var(--mini-icon-size); height:var(--mini-icon-size); display:block; }
#openMiniBtn{ gap:2px; }
#openMiniBtn .label{
  display:flex; align-items:center; line-height:1; margin-top:-1px; letter-spacing:.2px;
  margin-left:-8px;
}
/* Shift BOTH the Mini icon and its label left a smidge */
#openMiniBtn > .ico,
#openMiniBtn > .label{
  transform: translateX(calc(-1 * var(--mini-group-nudge-x)));
}
#openMiniBtn:hover, #openMiniBtn:active{ transform:none; filter:none; }
#openMiniBtn:focus-visible{
  outline:2px solid rgba(23,213,255,.55);
  outline-offset:3px;
  box-shadow:none;
}
#openMiniBtn::before, #openMiniBtn::after{
  content:"";
  position:absolute;
  inset:0;
  border-radius:inherit; pointer-events:none;
  mix-blend-mode:screen; will-change:transform;
  clip-path: inset(0 round var(--btn-radius));
}
#openMiniBtn::before{
  background:
    radial-gradient(120% 28% at -10% 40%, rgba(0,230,255,0) 44%, rgba(0,230,255,.65) 50%, rgba(0,230,255,0) 56%),
    radial-gradient(120% 26% at 110% 60%, rgba(120,200,255,0) 44%, rgba(120,200,255,.55) 50%, rgba(120,200,255,0) 56%);
  filter:blur(6px) saturate(180%); opacity:.55;
  transform:translate3d(-12%,-10%,0) rotate(8deg) scale(1.05);
  animation:ribbonA 14s ease-in-out infinite;
}
#openMiniBtn::after{
  background:
    radial-gradient(120% 24% at   0% 70%, rgba(255,95,109,0) 44%, rgba(255,95,109,.75) 50%, rgba(255,95,109,0) 56%),
    radial-gradient(120% 22% at 100% 25%, rgba(178,141,255,0) 44%, rgba(178,141,255,.60) 50%, rgba(178,141,255,0) 56%);
  filter:blur(7px) saturate(160%); opacity:.55;
  transform:translate3d(10%,-6%,0) rotate(-12deg) scale(1.08);
  animation:ribbonB 11s linear infinite reverse;
}
@keyframes ribbonA{
  0%   { transform:translate3d(-12%,-10%,0) rotate(  8deg)  scale(1.05); }
  50%  { transform:translate3d( 12%, 10%,0) rotate(188deg)  scale(1.12); }
  100% { transform:translate3d(-12%,-10%,0) rotate(368deg)  scale(1.05); }
}
@keyframes ribbonB{
  0%   { transform:translate3d( 10%, -6%,0) rotate(-12deg)  scale(1.08); }
  50%  { transform:translate3d(-10%,  6%,0) rotate(168deg)  scale(1.18); }
  100% { transform:translate3d( 10%, -6%,0) rotate(348deg)  scale(1.08); }
}
@media (prefers-reduced-motion: reduce){
  #openMiniBtn::before, #openMiniBtn::after{ animation:none; opacity:.35; }
}

/* SECONDARY: ComfyUI — BLACK, centered logo + LIGHT-BLUE FOG
   Seamless loop: movement + a full fade-out at the end, fade-in at start */
#openComfyUiBtn,
#openComfyUiBtn:visited{
  background:#000 !important;
  color:#fff !important;
  padding:30px 24px;
  border-radius:var(--btn-radius);
  border:1px solid rgba(255,255,255,.28);
  box-shadow:none !important;
  backdrop-filter:none !important;
  text-shadow:none;
  transform:none !important;

  position:relative;
  overflow:hidden;
  isolation:isolate;
  contain:paint;
  clip-path: inset(0 round var(--btn-radius));

  display:flex; align-items:center; justify-content:center; gap:0;

  /* Positions */
  --fog-offset: 38%;       /* bottom bank anchor (you already tuned this) */
  --fog-top-lift: 16%;     /* how much higher the top wisps ride vs bottom */
}
#openComfyUiBtn .ico{
  margin:0; flex:1 1 auto;
  display:flex; align-items:center; justify-content:center;
  position:relative; z-index:1;
}
#openComfyUiBtn .ico img{
  width:clamp(170px,66%,320px); height:auto; display:block; transform:translateX(6px);
}

/* Fog layers (bottom bank + higher wisps) */
#openComfyUiBtn::before,
#openComfyUiBtn::after{
  content:"";
  position:absolute; left:-6%; right:-6%;
  bottom:-22%; /* seam hidden below button */
  border-radius:inherit;
  pointer-events:none; mix-blend-mode:screen;
  clip-path: inset(-30% round var(--btn-radius));
  will-change: transform, background-position, opacity, filter;
  background-repeat:no-repeat;
  -webkit-mask-image: radial-gradient(150% 120% at 50% 50%, #000 78%, transparent 100%);
          mask-image: radial-gradient(150% 120% at 50% 50%, #000 78%, transparent 100%);
  mask-mode:alpha;
  opacity:0;
}

/* Bottom bank (~lower half). Duplicate stack for smooth slide. */
#openComfyUiBtn::before{
  height:72%;
  transform: translateY(var(--fog-offset));
  background:
    radial-gradient(58% 40% at 10% 115%, rgba(140,210,255,.55) 0%, rgba(140,210,255,.30) 40%, rgba(140,210,255,0) 70%),
    radial-gradient(64% 44% at 38% 112%, rgba(150,220,255,.52) 0%, rgba(150,220,255,.26) 42%, rgba(150,220,255,0) 72%),
    radial-gradient(60% 42% at 66% 114%, rgba(140,210,255,.50) 0%, rgba(140,210,255,.25) 44%, rgba(140,210,255,0) 72%),
    radial-gradient(58% 38% at 90% 116%, rgba(130,205,255,.46) 0%, rgba(130,205,255,.22) 40%, rgba(130,205,255,0) 70%),
    radial-gradient(58% 40% at 10% 115%, rgba(140,210,255,.55) 0%, rgba(140,210,255,.30) 40%, rgba(140,210,255,0) 70%),
    radial-gradient(64% 44% at 38% 112%, rgba(150,220,255,.52) 0%, rgba(150,220,255,.26) 42%, rgba(150,220,255,0) 72%),
    radial-gradient(60% 42% at 66% 114%, rgba(140,210,255,.50) 0%, rgba(140,210,255,.25) 44%, rgba(140,210,255,0) 72%),
    radial-gradient(58% 38% at 90% 116%, rgba(130,205,255,.46) 0%, rgba(130,205,255,.22) 40%, rgba(130,205,255,0) 70%);
  background-size: 220% 130%;
  background-position:
    -40% 92%,   0% 90%,  40% 92%,  80% 94%,
    -240% 92%, -200% 90%, -160% 92%, -120% 94%;
  filter: blur(14px) saturate(200%) brightness(1.16);
  animation:
    fogBottomSlide 70s linear infinite,
    fogBottomFade  70s ease-in-out infinite;
}

/* Higher, lighter wisps. Ride higher toward button middle. */
#openComfyUiBtn::after{
  height:110%; /* give the wisps more vertical reach */
  transform: translateY(calc(var(--fog-offset) - var(--fog-top-lift)));

  /* shift the gradient centers upward a bit */
  background:
    radial-gradient(40% 34% at 20% 62%, rgba(180,240,255,.30) 8%, rgba(180,240,255,.16) 38%, rgba(180,240,255,0) 64%),
    radial-gradient(36% 30% at 55% 52%, rgba(190,245,255,.28) 8%, rgba(190,245,255,.14) 36%, rgba(190,245,255,0) 62%),
    radial-gradient(34% 28% at 82% 58%, rgba(160,225,255,.26) 8%, rgba(160,225,255,.12) 34%, rgba(160,225,255,0) 60%),
    radial-gradient(30% 26% at 40% 40%, rgba(190,245,255,.22) 8%, rgba(190,245,255,.10) 34%, rgba(190,245,255,0) 58%),
    radial-gradient(28% 24% at 68% 36%, rgba(170,235,255,.20) 8%, rgba(170,235,255,.10) 32%, rgba(170,235,255,0) 56%),
    radial-gradient(40% 34% at 20% 62%, rgba(180,240,255,.30) 8%, rgba(180,240,255,.16) 38%, rgba(180,240,255,0) 64%),
    radial-gradient(36% 30% at 55% 52%, rgba(190,245,255,.28) 8%, rgba(190,245,255,.14) 36%, rgba(190,245,255,0) 62%),
    radial-gradient(34% 28% at 82% 58%, rgba(160,225,255,.26) 8%, rgba(160,225,255,.12) 34%, rgba(160,225,255,0) 60%),
    radial-gradient(30% 26% at 40% 40%, rgba(190,245,255,.22) 8%, rgba(190,245,255,.10) 34%, rgba(190,245,255,0) 58%),
    radial-gradient(28% 24% at 68% 36%, rgba(170,235,255,.20) 8%, rgba(170,235,255,.10) 32%, rgba(170,235,255,0) 56%);
  background-size: 240% 160%, 240% 160%, 240% 160%, 260% 170%, 260% 170%, 240% 160%, 240% 160%, 240% 160%, 260% 170%, 260% 170%;
  background-position:
    -80% 72%,  -30% 64%,   20% 68%,   60% 54%,  100% 50%,
    -280% 72%, -230% 64%, -180% 68%, -140% 54%, -100% 50%;
  filter: blur(16px) saturate(190%) brightness(1.10);
  animation:
    fogTopSlide 45s linear infinite,
    fogTopFade  45s ease-in-out infinite,
    fogTopBreath 7s ease-in-out infinite alternate;
}

/* --- Movement (seamless) --- */
@keyframes fogBottomSlide{
  0%{
    background-position:
      -40% 92%,   0% 90%,  40% 92%,  80% 94%,
      -240% 92%, -200% 90%, -160% 92%, -120% 94%;
  }
  100%{
    background-position:
      160% 92%, 200% 90%, 240% 92%, 280% 94%,
      -40% 92%,   0% 90%,  40% 92%,  80% 94%;
  }
}
@keyframes fogTopSlide{
  0%{
    background-position:
      -80% 72%,  -30% 64%,  20% 68%,  60% 54%, 100% 50%,
      -280% 72%, -230% 64%, -180% 68%, -140% 54%, -100% 50%;
  }
  100%{
    background-position:
      120% 72%, 170% 64%, 220% 68%, 260% 54%, 300% 50%,
      -80% 72%,  -30% 64%,  20% 68%,  60% 54%, 100% 50%;
  }
}

/* --- Fade envelopes --- */
@keyframes fogBottomFade{
  0%   { opacity:0; }
  10%  { opacity:.52; }
  90%  { opacity:.52; }
  100% { opacity:0; }
}
@keyframes fogTopFade{
  0%   { opacity:0; }
  12%  { opacity:.38; }
  88%  { opacity:.40; }
  100% { opacity:0; }
}

/* Subtle breathing for the top wisps */
@keyframes fogTopBreath{
  0%   { filter:blur(15px) saturate(185%) brightness(1.08); }
  100% { filter:blur(18px) saturate(200%) brightness(1.12); }
}

@media (prefers-reduced-motion: reduce){
  #openComfyUiBtn::before, #openComfyUiBtn::after{
    animation:none; opacity:.24;
  }
}

/* Unify ALL other buttons to dark (keep Open Mini aurora, Shutdown red) */
.restart,.stop,.delete,.recreate,.vis{
  background:linear-gradient(180deg,#323640,#232731) !important;
  color:#f3f6ff !important; border:1px solid rgba(255,255,255,.14);
}

/* === GALLERY button — MAGENTA × DARK BLUE aura around icon+title === */
#openGalleryBtn{
  padding:27px 24px;
  font-size:1.22rem;
  border-radius:var(--btn-radius);
  display:flex; align-items:center; justify-content:center; gap:10px;
  box-shadow:none !important;
  backdrop-filter:none !important;
}

/* Core button */
.gallery{
  position:relative;
  color:#fff !important;
  background:#000;                    /* solid base */
  border:1px solid rgba(255,255,255,.16);
  overflow:hidden;                     /* confine glow */
  isolation:isolate;
  clip-path: inset(0 round var(--btn-radius));
  animation:none !important;           /* stop old paint-splash animation */
}

/* Aura layers (confined to button) */
.gallery::before,
.gallery::after{
  content:"";
  position:absolute; inset:-8%;
  border-radius:inherit;
  pointer-events:none;
  mix-blend-mode:screen;
  will-change:transform, opacity, filter;
  clip-path: inset(0 round var(--btn-radius));
}

/* Inner magenta core + soft ring */
.gallery::before{
  background:
    radial-gradient(60% 60% at var(--gallery-aura-x,52%) var(--gallery-aura-y,50%),
      rgba(255, 40, 170, .55) 0%,
      rgba(255, 40, 170, .28) 26%,
      rgba(255, 40, 170, .10) 48%,
      rgba(255, 40, 170, 0)   64%),
    radial-gradient(95% 95% at var(--gallery-aura-x,52%) var(--gallery-aura-y,50%),
      rgba(255, 40, 170, .18) 12%,
      rgba(255, 40, 170, 0)   58%);
  filter: blur(12px) saturate(180%);
  opacity:.42;
  transform:scale(.92);
  animation:galleryAuraPulse 6.5s ease-in-out infinite;
}

/* Outer dark-blue halo + faint magenta echo */
.gallery::after{
  background:
    radial-gradient(110% 110% at var(--gallery-aura-x,52%) var(--gallery-aura-y,50%),
      rgba(30, 56, 255, .34) 8%,
      rgba(30, 56, 255, .16) 40%,
      rgba(30, 56, 255, 0)   70%),
    radial-gradient(130% 130% at var(--gallery-aura-x,52%) var(--gallery-aura-y,50%),
      rgba(255, 40, 170, .10) 0%,
      rgba(255, 40, 170, 0)   60%);
  filter: blur(16px) saturate(180%);
  opacity:.30;
  transform:scale(.88);
  animation:galleryAuraHalo 9s ease-in-out infinite;
}

/* Icon sizing + tiny horizontal nudge stays */
.gallery .ico{ margin-right:.55rem; }
.gallery .ico img,
.gallery .ico svg{
  width:2.0em; height:2.0em; display:block;
  transform:translateX(var(--gallery-icon-nudge-x));
}

/* Pulse outward, then breathe back in */
@keyframes galleryAuraPulse{
  0%   { transform:scale(.90); opacity:.36; filter:blur(11px) saturate(170%); }
  50%  { transform:scale(1.06); opacity:.50; filter:blur(14px) saturate(190%); }
  100% { transform:scale(.90); opacity:.36; filter:blur(11px) saturate(170%); }
}

/* Slower, larger halo for depth */
@keyframes galleryAuraHalo{
  0%   { transform:scale(.86); opacity:.26; filter:blur(15px) saturate(170%); }
  50%  { transform:scale(1.10); opacity:.34; filter:blur(18px) saturate(190%); }
  100% { transform:scale(.86); opacity:.26; filter:blur(15px) saturate(170%); }
}

@media (prefers-reduced-motion: reduce){
  .gallery::before, .gallery::after{ animation:none; opacity:.26; }
}

label{font-size:.9rem;color:#c7d0ff;text-align:left;display:block;margin:0 4px 8px}
.pwdwrap{display:flex;align-items:center;gap:8px;margin-bottom:10px}
input[type=password],input[type=text]{flex:1;padding:14px;font-size:1rem;border-radius:12px;border:1px solid #2f3472;outline:none;background:#141549;color:var(--text);text-align:center}
.eye{width:46px;height:46px;border-radius:10px;border:1px solid #2f3472;background:#141549;cursor:pointer;display:flex;align-items:center;justify-content:center;line-height:1}
#toggleEye svg, #mpEyeIcon{width:28px!important;height:28px!important;display:block;flex:0 0 28px;}
.eye svg{width:28px;height:28px;stroke:#cfe3ff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

.status{text-align:center;color:#c4ccff;font-size:.95rem;margin:12px 0 6px}
.muted{color:#8aa;font-size:.88rem;word-break:break-all;margin-top:10px}
.muted + .muted{margin-top:4px}
.tiny{color:#9ab;font-size:.86rem;margin:4px 0 10px}

/* Network panel + mascot */
.netPanel{ position:relative; overflow:hidden; }
.net{font-size:.95rem;line-height:1.55;color:#d9e4ff; padding-right:clamp(96px, 40%, 180px);}
.net .ipline{text-align:center;font-weight:800;margin-bottom:6px;font-size:1.05rem;color:#c6dcff}
.net .ports{display:flex;flex-direction:column;gap:4px}
.matrixMascot{
  position:absolute;
  right:8px;
  bottom:-45px;
  width:clamp(96px, 40%, 180px);
  height:auto;
  pointer-events:none;
  user-select:none;
  opacity:.80;
  filter:drop-shadow(0 4px 12px rgba(0,0,0,.45));
}

/* bottom-centered mode bubble */
.footerWrap .modeWrap{display:flex;justify-content:center;margin:8px 0 6px}
.modeBadge{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;font-weight:800;font-size:.96rem;color:#7ad7ff;border:1px solid rgba(0,217,255,.35);background:linear-gradient(180deg, rgba(0,217,255,.16), rgba(0,217,255,.07))}
.modeBadge svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

/* ====== HOLD: Slow red neon pulse (restored) ====== */
@keyframes holdPulseRed{
  0%   { box-shadow:0 0 0 0 rgba(255,50,80,.45), 0 10px 18px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.06) }
  60%  { box-shadow:0 0 36px 18px rgba(255,50,80,.40), 0 10px 18px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.06) }
  100% { box-shadow:0 0 0 0 rgba(255,50,80,0),         0 10px 18px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.06) }
}
.shutdown.holding{ animation:holdPulseRed 1.1s ease-in-out infinite }

.shutdown{ position:relative; isolation:isolate; contain:paint; }
.shutdown.holding::before{
  content:""; position:absolute; inset:-10px; border-radius:inherit; pointer-events:none;
  background:radial-gradient(140% 140% at 50% 50%, rgba(255,60,90,.55) 0%, rgba(255,60,90,.35) 28%, rgba(255,60,90,0) 58%);
  filter:blur(12px); mix-blend-mode:screen; animation:holdPulseRedGlow 900ms ease-in-out infinite; will-change:transform,opacity,filter; z-index:0;
}
@keyframes holdPulseRedGlow{
  0%   { opacity:.60; transform:translateZ(0) scale(.94); }
  50%  { opacity:1.00; transform:translateZ(0) scale(1.06); }
  100% { opacity:.60; transform:translateZ(0) scale(.94); }
}

/* SMOOTHER GPU RED EXPLOSION on release */
@keyframes boomOut{
  0%   { opacity:.95; transform:translateZ(0) scale(.22); }
  45%  { opacity:.92; transform:translateZ(0) scale(1.05); }
  100% { opacity:0;   transform:translateZ(0) scale(1.95); }
}
.screen-blast{
  position:fixed; inset:0; pointer-events:none; z-index:9999;
  background:
    radial-gradient(1800px 1800px at var(--x,50%) var(--y,50%), rgba(255,40,70,.96) 0%, rgba(255,40,70,.60) 22%, rgba(255,40,70,.28) 42%, rgba(255,40,70,.12) 56%, rgba(255,40,70,0) 66%),
    radial-gradient(1200px 1200px at calc(var(--x,50%) - 6%) calc(var(--y,50%) + 8%), rgba(255,120,140,.35) 0%, rgba(255,120,140,0) 40%),
    radial-gradient(1000px 1000px at calc(var(--x,50%) + 8%) calc(var(--y,50%) - 10%), rgba(255,200,220,.28) 0%, rgba(255,200,220,0) 38%);
  filter: blur(14px) saturate(120%);
  mix-blend-mode:screen;
  opacity:0;
  transform:translateZ(0) scale(.22);
  animation:boomOut 740ms cubic-bezier(.22,.61,.36,1) forwards;
  will-change:transform,opacity,filter;
  contain:paint;
}

/* footer alignment refresh: fully centered */
.footerWrap{
  width: calc(100% - 32px);
  max-width: calc(520px - 32px);
  margin:12px auto 22px;
  padding:12px 16px;
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  text-align:center;
  background:linear-gradient(180deg, rgba(255,255,255,.02), transparent), var(--panel);
  border:1px solid #2a2f73;
  border-radius:var(--radius);
  box-shadow:0 8px 18px rgba(0,0,0,.45);
  position:relative;
}

}
.footerWrap .hr{display:none}
.statusLine{
  margin:0 0 8px; color:#cfe3ff; font-weight:800; letter-spacing:.2px;
  display:inline-flex; align-items:center; justify-content:center; gap:8px; padding:6px 10px;
  border-radius:999px; border:1px solid rgba(255,255,255,.12);
  background:linear-gradient(180deg,#171a2a,#0e111d);
  text-align:center; align-self:center;
}
.sigLine{color:#a9b4d7;font-size:.92rem;margin:0; text-align:center}
.sigLine span{color:#d7c9ff}

/* === Glassy depth for links + buttons === */
a.btnlink, button{
  position:relative;
  border:1px solid rgba(255,255,255,.14);
  box-shadow:
    0 14px 26px rgba(0,0,0,.55),
    inset 0 1px 0 rgba(255,255,255,.10),
    inset 0 -2px 0 rgba(0,0,0,.45);
  backdrop-filter:saturate(140%) blur(2px);
}
a.btnlink::before, button::before{
  content:""; position:absolute; inset:0; border-radius:inherit; pointer-events:none;
  background:linear-gradient(180deg,rgba(255,255,255,.18),rgba(255,255,255,0) 55%); opacity:.85;
}
a.btnlink::after{
  content:""; position:absolute; inset:0; border-radius:inherit; pointer-events:none;
  background:radial-gradient(120% 60% at 50% -10%, rgba(255,255,255,.14), transparent 42%);
}
a.btnlink:hover, button:hover{
  transform:translateY(-1px);
  filter:brightness(1.03);
  box-shadow:0 16px 28px rgba(0,0,0,.60), inset 0 1px 0 rgba(255,255,255,.12), inset 0 -2px 0 rgba(0,0,0,.50);
}
a.btnlink:active, button:active{
  transform:translateY(1px);
  box-shadow:0 8px 18px rgba(0,0,0,.45), inset 0 2px 0 rgba(0,0,0,.50);
}
a.btnlink:focus-visible, button:focus-visible{
  outline:none;
  box-shadow:
    0 0 0 3px rgba(0,217,255,.28),
    0 0 0 1px #2a2f73,
    0 14px 26px rgba(0,0,0,.55),
    inset 0 1px 0 rgba(255,255,255,.10),
    inset 0 -2px 0 rgba(0,0,0,.45);
}

/* === GitHub link image in footer (no tap/press indicators) === */
.ghLink{
  position:absolute; left:12px; top:12px;
  display:block; width:44px; height:44px;
  -webkit-tap-highlight-color:transparent; outline:none; border:none;
}
.ghLink:focus, .ghLink:focus-visible{ outline:none; }
.ghLink img{ display:block; width:100%; height:100%; pointer-events:none; user-select:none; }

/* === Buy Me A Coffee link on right (no tap/press indicators) === */
.bmacLink{
  position:absolute; right:12px; top:12px;
  display:block; width:44px; height:44px;
  -webkit-tap-highlight-color:transparent; outline:none; border:none;
}
.bmacLink:focus, .ghLink:focus-visible{ outline:none; }
.bmacLink img{ display:block; width:100%; height:100%; pointer-events:none; user-select:none; }

/* --- Stability for sticky header & vivid green lock text --- */
.header{transform:translateZ(0);-webkit-transform:translateZ(0);backface-visibility:hidden;-webkit-backface-visibility:hidden;will-change:transform;contain:paint}
.statusHot{color:#40f19a;font-weight:800}
/* shutdown status styling */
#headerBar.statusComplete{color:#ff5f6d;font-weight:800}


</style></head><body>
<div class="header" id="headerBar">Loading status…</div>

<h1>
  <span class="brand">
    <span class="mascot"><a href="https://github.com/PastLifeDreamer/Pocket-Comfy" target="_blank" rel="noopener"><img id="mascotImg" src="{{ url_for('static', filename='comfy-mascot.webp') }}{% if request.args.get('switched') %}?v={{ request.args.get('r','') }}{% endif %}" alt="Mascot" fetchpriority="high" loading="eager" decoding="async"></a></span>
    <span class="title">Pocket Comfy</span>
  </span>
</h1>

<div class="container">
  <!-- Panel 1: Comfy Mini (original bracket size) -->
  <div class="panel">
    <a class="btnlink" id="openMiniBtn" href="#">
      <span class="ico">
        <img src="{{ url_for('static', filename='Comfy-Mini.webp') }}" alt="" decoding="async" loading="eager">
      </span>
      <span class="label">Comfy Mini</span>
          <span id="miniDot" class="mode-dot"></span>
    </a>
  </div>

  <!-- Panel 2: ComfyUI (black, logo-only) -->
  <div class="panel">
    <!-- IMPORTANT: link directly to wrapper route so border/top panel render and port 8188 loads inside it -->
    <a class="btnlink" id="openComfyUiBtn" href="/comfyui" aria-label="Open ComfyUI">
      <span class="ico">
        <img src="{{ url_for('static', filename='comfyui-text.webp') }}" alt="Comfy" decoding="async" loading="eager">
      </span>
          <span id="comfyDot" class="mode-dot"></span>
    </a>
  </div>

  <!-- Panel 3: Gallery (same bracket size as ComfyUI) -->
  <div class="panel">
    <button class="gallery" id="openGalleryBtn">
      <span class="ico">
        <img src="{{ url_for('static', filename='ModernMinimalGalleryLogo.webp') }}" alt="" width="32" height="32" decoding="async" loading="eager">
      </span>
      Smart Gallery
      <span id="galleryDot" class="mode-dot"></span>
    </button>
  </div>

  <div class="panel">
    <button class="restart" onclick="post('/restart').then(ok=>{ if(ok){ restartInProgress=true; stoppedOverride=false; toast('Restarting Comfy + Mini…'); }})"><span class="ico"><svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 0115.5-6.36M21 12a9 9 0 01-15.5 6.36"/><path d="M3 5v6h6"/></svg></span>Restart Comfy + Mini</button>
    <button class="stop" onclick="post('/stop').then(ok=>{ if(ok){ stoppedOverride=true; renderHeader({comfy:false,mini:false}, true); toast('Stopped apps (panel still up)'); } })"><span class="ico"><svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg></span>Stop Comfy + Mini</button>
  </div>

  <div class="panel">
    <div class="tiny"><strong>Run Hidden:</strong> Hides the Python console on your PC.<br><strong>Run Visible:</strong> Brings your console back for visibility. <strong>Note:</strong> Both restart Pocket Comfy + ComfyUI + Mini.</div>
    <button class="vis" id="visHideBtn"><span class="hold-fill" id="visHideFill"></span><span class="ico"><svg viewBox="0 0 24 24"><path d="M3 3l18 18"/><path d="M10 10a2 2 0 102.83 2.83"/><path d="M2 12s4-7 10-7 10 7 10 7"/></svg></span>Run Hidden<span id="hiddenDot" class="mode-dot"></span></button>
    <button class="vis" id="visShowBtn"><span class="hold-fill" id="visShowFill"></span><span class="ico"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8"/></svg></span>Run Visible<span id="visibleDot" class="mode-dot"></span></button>
  </div>

  <div class="panel warn">
    <div class="tiny">Press & hold 3 seconds for full script shutdown.       <strong>This stops Pocket Comfy, ComfyUI, Mini and closes the Python console on your PC!!!</strong> Relaunch on the PC to bring Pocket Comfy back.</div>
    <button class="shutdown" id="shutdownBtn"><span class="hold-fill" id="shutdownFill"></span><span class="ico"><svg viewBox="0 0 24 24"><path d="M12 2v10"/><path d="M18.36 6.64a9 9 0 11-12.72 0"/></svg></span>Shutdown All</button>
  </div>

  <div class="panel">
    <label for="pwd">(Password required for Delete & Recreate)</label>
    <div class="pwdwrap">
      <input id="pwd" type="password" placeholder="Password" autocomplete="off"/>
      <button class="eye" id="toggleEye" title="Show/Hide" aria-label="Show/Hide Password">
        <svg id="mpEyeIcon" viewBox="0 0 24 24" width="28" height="28">
          <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>
        </svg>
      </button>
    </div>
    <button class="delete" id="deleteBtn" disabled><span class="hold-fill" id="deleteFill"></span><span class="ico"><svg viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/></svg></span>Delete Output Folder</button>
    <button class="recreate" id="recreateBtn" disabled><span class="hold-fill" id="recreateFill"></span><span class="ico"><svg viewBox="0 0 24 24"><path d="M3 7h6l2 2h10v10H3z"/><path d="M12 12v6"/><path d="M9 15h6"/></svg></span>Recreate Output Folder</button>
    <div class="muted">Only final folder in path is affected:</div>
    <div class="muted">{{ delete_path }}</div>
  </div>

  <!-- Network panel with mascot -->
  <div class="panel netPanel">
    <div class="net" id="netinfo">Loading network info…</div>
    <img class="matrixMascot" src="{{ url_for('static', filename='matrix-mascot.webp') }}" alt="" aria-hidden="true" />
  </div>
</div>

<div class="footerWrap">
  <!-- GitHub link image on the left (no tap indicators) -->
  <a class="ghLink" href="https://github.com/PastLifeDreamer/Pocket-Comfy" target="_blank" rel="noopener">
    <img src="{{ url_for('static', filename='Github-Link.webp') }}" alt="GitHub repository">
  </a>

  <!-- Buy Me A Coffee link image on the right (no tap indicators) -->
  <a class="bmacLink" href="https://buymeacoffee.com/pastlifedreamer" target="_blank" rel="noopener">
    <img src="{{ url_for('static', filename='BMAC.webp') }}" alt="Buy Me a Coffee">
  </a>

  <div class="statusLine" id="statusBox">Ready.</div>
  <div class="modeWrap">
    <div id="modeBox" class="modeBadge" style="display:none">
      <svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8"/></svg>
      <span>Mode: Visible</span>
    </div>
  </div>
  <div class="sigLine">Flask server running • <span>Pocket Comfy</span> by <strong>PastLifeDreamer</strong></div>
</div>

<script>
const CSRF="{{ csrf_token }}";
function toast(m){ document.getElementById('statusBox').textContent=m; }
function disableAllControls(){ document.querySelectorAll('button, a.btnlink').forEach(el=>{ el.disabled=true; el.setAttribute('aria-disabled','true'); el.classList.add('disabled'); }); }
async function post(url, body=""){ try{ const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','X-CSRF-Token':CSRF,'X-Activity':'1'},body}); return (await r.text()).trim()==="success"; }catch(e){ toast("Request failed"); return false; } }
function ping(){ fetch('/activity',{method:'POST',headers:{'X-CSRF-Token':CSRF}}); }
window.addEventListener('focus', ping); document.addEventListener('visibilitychange', ()=>{ if(document.visibilityState==='visible') ping(); }); setInterval(()=>{ if(document.visibilityState==='visible') ping(); }, 30000);

const mpEyeBtn=document.getElementById('toggleEye'); const mpEyeIcon=document.getElementById('mpEyeIcon'); const pwd=document.getElementById('pwd');
function mpSetEye(show){
  if(show){
    pwd.type='text';
    mpEyeIcon.innerHTML='<path d="M3 3L21 21"/><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>';
  } else {
    pwd.type='password';
    mpEyeIcon.innerHTML='<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>';
  }
}
mpEyeBtn.addEventListener('click',e=>{e.preventDefault(); mpSetEye(pwd.type==='password');});

const delBtn=document.getElementById('deleteBtn'), recBtn=document.getElementById('recreateBtn'); let pwTimer=null;
async function checkPw(){ const res=await fetch('/checkpw',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','X-CSRF-Token':CSRF},body:'password='+encodeURIComponent(pwd.value)}); const ok=(await res.text()).trim()==='ok'; delBtn.disabled=!ok; recBtn.disabled=!ok; }
function schedulePwCheck(){ clearTimeout(pwTimer); pwTimer=setTimeout(checkPw,180); }
pwd.addEventListener('input',schedulePwCheck); checkPw();

/* restore + enforce mode dots */
function setModeDots(mode){
  const hd=document.getElementById('hiddenDot'), vd=document.getElementById('visibleDot');
  if(!hd || !vd) return;
  ['hidden','on','off'].forEach(c=>{ hd.classList.remove(c); vd.classList.remove(c); });
  if(mode==='off'){ hd.classList.add('hidden'); vd.classList.add('hidden'); return; }
  const m=(mode==='hidden'||mode==='visible')?mode:'visible';
  if(m==='hidden'){ hd.classList.add('on'); vd.classList.add('off'); }
  else{ vd.classList.add('on'); hd.classList.add('off'); }
}
/* show a default immediately (updated by /status) */
setModeDots('visible');

const header = document.getElementById('headerBar');
// Global status-lock to hold a message during mode switch
window.__statusLockMsg = null;
const checkSVG = '<svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg>';
const xSVG     = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
let stoppedOverride = false;
let restartInProgress = false;
function badge(ok,label){ return `<span class="badge ${ok?'ok':'bad'}">${ok?checkSVG:xSVG}<span>${label} ${ok?'Running':'Stopped'}</span></span>`; }
function renderHeader(s, forceBadges=false){
  // Respect lock message during mode switch
  if (window.__statusLockMsg){ header.style.color='#40f19a'; header.style.fontWeight='800'; header.style.color='#40f19a'; header.style.fontWeight='800'; header.style.color='#40f19a'; header.style.fontWeight='800'; header.textContent = window.__statusLockMsg; return; }
  
  // During shutdown, force red dots and hold message
  if (stoppedOverride){
    header.textContent='Shutting down…';
    setModeDots('off');
    try { ['galleryDot','comfyDot','miniDot'].forEach(id=>{ const d=document.getElementById(id); if(d){ d.classList.remove('on','off','hidden'); d.classList.add('off'); }});} catch(_){/* no-op */}
    return;
  }
if(!s){ header.textContent='Status unavailable'; return; }
  const bothDown = !s.comfy && !s.mini;
  if(bothDown && !(forceBadges || stoppedOverride)){
    header.textContent='Shutdown Complete'; header.classList.add('statusComplete'); header.classList.add('statusComplete');
    setModeDots('off');
  } else { header.classList.remove('statusComplete'); 
    header.innerHTML=`${badge(s.comfy,'ComfyUI')}<span class="sep">•</span>${badge(s.mini,'Mini')}`;
    setModeDots(bothDown ? 'off' : (s.mode_hidden?'hidden':'visible'));
    const gdot=document.getElementById('galleryDot');
    if(gdot){ gdot.classList.remove('on','off','hidden'); gdot.classList.add(s.gallery ? 'on' : 'off'); }
    const cdot=document.getElementById('comfyDot');
    if(cdot){ cdot.classList.remove('on','off','hidden'); cdot.classList.add(s.comfy ? 'on' : 'off'); }
    const mdot=document.getElementById('miniDot');
    if(mdot){ mdot.classList.remove('on','off','hidden'); mdot.classList.add(s.mini ? 'on' : 'off'); }
}
  if (restartInProgress && s.comfy && s.mini){
    toast('Comfy + Mini back online');
    restartInProgress = false;
    stoppedOverride = false;
  }
  renderModeBadge(s);
}
function renderModeBadge(s){
  const box=document.getElementById('modeBox');
  if(!s || (!s.comfy && !s.mini)){ box.style.display='none'; return; }
  box.querySelector('span').textContent='Mode: '+(s.mode_hidden?'Hidden':'Visible');
  box.style.display='inline-flex';
}
async function refreshHeader(){ try{ const s=await (await fetch('/status')).json(); renderHeader(s); }catch{ if(!window.__statusLockMsg){ header.textContent="Status unavailable"; } } }
setInterval(refreshHeader,1000); refreshHeader();

async function refreshNet(){ try{  const n=await (await fetch('/netinfo')).json(); const ip=n.lan_ip||'127.0.0.1', rc=n.flask_port||5000, c=n.comfy_port||8188, m=n.mini_port||3000, g=n.gallery_port||8189; document.getElementById('netinfo').innerHTML=`<div class="ipline"><strong>${ip}</strong></div><div class="ports"><div><strong>Remote Control Port:</strong> ${rc}</div><div><strong>ComfyUI Port:</strong> ${c}</div><div><strong>ComfyUI Mini Port:</strong> ${m}</div><div><strong>Smart Gallery Port:</strong> ${g}</div></div>`;  } catch {  document.getElementById('netinfo').textContent="Network info unavailable.";  } }
setInterval(refreshNet,5000); refreshNet();

/* press-and-hold helper */
function holdFill(btnId, fillId, dur, onComplete, reqEnabled=true, hooks={}){ const btn=document.getElementById(btnId), fill=document.getElementById(fillId); let timer=null, raf=null, start=0, finished=false; function startHold(e){ e.preventDefault(); if (reqEnabled && btn.disabled) return; if (timer) return; start=performance.now(); finished=false; fill.style.width='0%'; timer=setTimeout(async()=>{ finished=true; cancelAnimationFrame(raf); fill.style.width='100%'; hooks.onFinish && hooks.onFinish(btn, e); await onComplete(e); }, dur); hooks.onStart && hooks.onStart(btn, e); animate(); } function animate(){ const pct=Math.min(100, ((performance.now()-start)/dur)*100); fill.style.width=pct+'%'; if (timer) raf=requestAnimationFrame(animate); } function cancelHold(e){ if (e) e.preventDefault(); if (timer){ clearTimeout(timer); timer=null; } cancelAnimationFrame(raf); raf=null; fill.style.width='0%'; if (!finished && hooks.onCancel) hooks.onCancel(btn, e); finished=false; } btn.addEventListener('pointerdown',startHold,{passive:false}); ['pointerup','pointerleave','pointercancel'].forEach(ev=>btn.addEventListener(ev,cancelHold,{passive:false})); }

/* GPU red explosion on release + full disable */
holdFill('shutdownBtn','shutdownFill',3000, async(e)=>{
  const blast=document.createElement('div');
  blast.className='screen-blast';
  const btn=document.getElementById('shutdownBtn');
  const r=btn.getBoundingClientRect();
  const x=(r.left+r.width/2)/window.innerWidth*100;
  const y=(r.top+r.height/2)/window.innerHeight*100;
  blast.style.setProperty('--x',x+'%');
  blast.style.setProperty('--y',y+'%');
  document.body.appendChild(blast);

  toast('Shutting down…');
  disableAllControls();
  stoppedOverride = true;
try { ['galleryDot','comfyDot','miniDot'].forEach(id=>{ const d=document.getElementById(id); if(d){ d.classList.remove('on','off','hidden'); d.classList.add('off'); }});} catch(_){/* no-op */}
await post('/shutdown');
  await waitForStopped(6000);
}, false, { onStart:(b)=>b.classList.add('holding'), onCancel:(b)=>b.classList.remove('holding'), onFinish:(b)=>b.classList.remove('holding') });

holdFill('visHideBtn','visHideFill',3000, async()=>{ disableAllControls(); window.__statusLockMsg = 'Cloaking PC Python Window…'; header.style.color='#40f19a'; header.style.fontWeight='800'; header.style.color='#40f19a'; header.style.fontWeight='800'; header.textContent = window.__statusLockMsg; toast('Cloaking PC Python Window…'); await post('/relaunch_hidden_full'); setTimeout(()=>{ location.replace('/login?switched=1&r='+Date.now()); }, 5600); }, false);
holdFill('visShowBtn','visShowFill',3000, async()=>{ disableAllControls(); window.__statusLockMsg = 'Materializing PC Python Window…'; header.style.color='#40f19a'; header.style.fontWeight='800'; header.style.color='#40f19a'; header.style.fontWeight='800'; header.textContent = window.__statusLockMsg; toast('Materializing PC Python Window…'); await post('/relaunch_visible_full'); setTimeout(()=>{ location.replace('/login?switched=1&r='+Date.now()); }, 5600); }, false);
holdFill('deleteBtn','deleteFill',3000, async()=>{ const ok=await post('/delete','password='+encodeURIComponent(pwd.value)); toast(ok?'Output folder deleted':'Wrong password or error'); });
holdFill('recreateBtn','recreateFill',3000, async()=>{ const ok=await post('/recreate','password='+encodeURIComponent(pwd.value)); toast(ok?'Output folder recreated':'Wrong password or error'); });

/* Open Comfy Mini (ensure + route) */
const openMiniBtn=document.getElementById('openMiniBtn');
function enableMiniBtn(){ openMiniBtn.classList.remove('disabled'); }
window.addEventListener('pageshow', enableMiniBtn);
openMiniBtn.addEventListener('click', async (e)=>{
  e.preventDefault();
  if (openMiniBtn.classList.contains('disabled')) return;
  openMiniBtn.classList.add('disabled');
  toast('Opening Comfy Mini…');
  const ok = await post('/ensure_mini');
  if (ok){ location.href = '/mini'; } else { toast('Could not open Comfy Mini'); openMiniBtn.classList.remove('disabled'); }
},{passive:false});

/* Open ComfyUI — use wrapper route so border/top panel show */
/* No JS needed; anchor's href="/comfyui" handles navigation */

/* === Open Gallery — ensure + open wrapper (/gallery) === */
const openGalleryBtn = document.getElementById('openGalleryBtn');
function urlForPort(port){
  const { protocol, hostname } = window.location;
  const safeHost = hostname.includes(':') ? `[${hostname}]` : hostname;
  return `${protocol}//${safeHost}:${port}/`;
}
if (openGalleryBtn){
  openGalleryBtn.addEventListener('click', async (e)=>{
    e.preventDefault();
    toast('Opening Gallery…');
    try { await fetch('/ensure_gallery', { method:'POST', headers:{ 'X-CSRF-Token': CSRF } }); } catch(_) {}
    window.location.href = '/gallery';
  }, { passive:false });
}
async function waitForStopped(t=5000){
  const st=performance.now();
  while(performance.now()-st<t){
    try{
      const s=await (await fetch('/status')).json();
      if(!s.comfy && !s.mini){ toast('Shutdown Complete'); stoppedOverride=false; renderHeader({comfy:false,mini:false}); return true; }
    }catch{ break; }
    await new Promise(r=>setTimeout(r,300));
  }
  toast('Shutdown Complete'); stoppedOverride=false; renderHeader({comfy:false,mini:false}); return false;
}
/* === Progressive haptic tap: Android (supported browsers) only === */
const CAN_VIBRATE = typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function';
function hapticTap() {
  try { CAN_VIBRATE && navigator.vibrate([8, 5, 8]); } catch (_) {}
}
document.addEventListener('click', (e) => {
  const el = e.target.closest('a.btnlink, button');
  if (!el) return;
  hapticTap();
}, { passive: true });

/* --- Reliable mascot image loader w/ cache-busting after mode switch --- */
function loadMascotReliably(){
  const img = document.getElementById('mascotImg');
  if(!img) return;
  const base = img.getAttribute('src');
  // If src already has a version/r param (set during mode switch), nothing to do.
  if (/[?&](v|r)=/.test(base)) return;

  let tries = 0, max = 5;
  function bustOnce(){
    const u = new URL(base, location.origin);
    u.searchParams.set('v', Date.now().toString());
    img.src = u.toString();
  }
  img.addEventListener('error', () => {
    if(++tries < max) setTimeout(bustOnce, 200 * tries);
  });
  if (location.pathname === '/login' || /[?&]switched=1\b/.test(location.search)){
    bustOnce();
  }
}
document.addEventListener('DOMContentLoaded', loadMascotReliably);
</script>
</body></html>
"""

# ========================= Routes =========================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        p = request.form.get("password","")
        if _safe_eq(p, LOGIN_PASS):
            session["auth_ok"] = True
            session["last_seen"] = time.time()
            session.permanent = True
            return redirect(request.args.get("next") or url_for("ui"))
        else:
            time.sleep(0.4)

    resp = make_response(render_template_string(
        LOGIN_TEMPLATE,
        csrf_token=CSRF_TOKEN,
        hero_logo=url_for('static', filename=HERO_FILE),
        apple_icon=url_for('static', filename='apple-touch-icon.png'),
        favicon32=url_for('static', filename='favicon-32.png'),
        favicon16=url_for('static', filename='favicon-16.png')
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/", methods=["GET"])
@login_required
def ui():
    return render_template_string(
        TEMPLATE,
        delete_path=DELETE_PATH,
        csrf_token=CSRF_TOKEN,
        brand_mascot=url_for('static', filename=BRAND_MASCOT_FILE),
        apple_icon=url_for('static', filename='apple-touch-icon.png'),
        favicon32=url_for('static', filename='favicon-32.png'),
        favicon16=url_for('static', filename='favicon-16.png')
    )

@app.route("/mini", methods=["GET"])
@login_required
def mini_page():
    return render_template_string(
        MINI_TEMPLATE,
        csrf_token=CSRF_TOKEN,
        apple_icon=url_for('static', filename='apple-touch-icon.png'),
        favicon32=url_for('static', filename='favicon-32.png'),
        favicon16=url_for('static', filename='favicon-16.png')
    )
@app.route("/comfyui")
@login_required
def comfyui_page():
    return render_template_string(
        COMFYUI_TEMPLATE,
        apple_icon=url_for("static", filename="apple-touch-icon.png"),
        favicon32=url_for("static", filename="favicon-32.png"),
        favicon16=url_for("static", filename="favicon-16.png"),
        csrf_token=CSRF_TOKEN,
    )

@app.route("/gallery")
@login_required
def gallery_page():
    return render_template_string(
        GALLERY_TEMPLATE,
        apple_icon=url_for("static", filename="apple-touch-icon.png"),
        favicon32=url_for("static", filename="favicon-32.png"),
        favicon16=url_for("static", filename="favicon-16.png"),
        csrf_token=CSRF_TOKEN,
    )


@app.route("/ensure_mini", methods=["POST"])
@login_required
def ensure_mini_route():
    threading.Thread(target=ensure_mini, daemon=True).start()
    return "success"

@app.route("/ensure_comfy", methods=["POST"])
@login_required
def ensure_comfy_route():
    try:
        if not (is_port_in_use(COMFY_PORT_DEFAULT) or comfy_running_by_handle()):
            launch_comfy()
            wait_for_comfy_ready(WAIT_FOR_COMFY_SECS)
        return "success"
    except Exception:
        return ("fail", 500)

@app.route("/ensure_gallery", methods=["POST"])
@login_required
def ensure_gallery_route():
    try:
        if not (is_port_in_use(SMART_GALLERY_PORT_DEFAULT) or gallery_running_by_handle()):
            launch_gallery()
            wait_for_gallery_ready(WAIT_FOR_GALLERY_SECS)
        return "success"
    except Exception:
        return ("fail", 500)


@app.route("/status", methods=["GET"])
@login_required
def status():
    comfy_alive = comfy_running_by_handle()
    mini_alive  = mini_running_by_handle()
    gallery_alive = gallery_running_by_handle()
    if not comfy_alive and is_port_in_use(COMFY_PORT_DEFAULT):
        comfy_alive = True; detected_ports["comfy"] = COMFY_PORT_DEFAULT
    if not mini_alive and is_port_in_use(MINI_PORT_DEFAULT):
        mini_alive = True; detected_ports["mini"] = MINI_PORT_DEFAULT
    if not gallery_alive and is_port_in_use(SMART_GALLERY_PORT_DEFAULT):
        gallery_alive = True; detected_ports["gallery"] = SMART_GALLERY_PORT_DEFAULT
    if comfy_alive:
        port = detect_port_for("comfy", COMFY_PORT_DEFAULT)
        if port: detected_ports["comfy"] = port
    if mini_alive:
        port = detect_port_for("mini", MINI_PORT_DEFAULT)
        if port: detected_ports["mini"] = port
    if gallery_alive:
        port = detect_port_for("gallery", SMART_GALLERY_PORT_DEFAULT)
        if port: detected_ports["gallery"] = port
    return jsonify({"comfy": comfy_alive, "mini": mini_alive, "gallery": gallery_alive, "mode_hidden": is_hidden_mode()})

@app.route("/netinfo", methods=["GET"])
@login_required
def netinfo():
    lan_ip = get_lan_ip()
    comfy_port = detected_ports.get("comfy") or (COMFY_PORT_DEFAULT if is_port_in_use(COMFY_PORT_DEFAULT) else None)
    mini_port  = detected_ports.get("mini")  or (MINI_PORT_DEFAULT  if is_port_in_use(MINI_PORT_DEFAULT)  else None)
    gallery_port = detected_ports.get("gallery") or (SMART_GALLERY_PORT_DEFAULT if is_port_in_use(SMART_GALLERY_PORT_DEFAULT) else None)
    comfy_alive   = is_port_in_use(COMFY_PORT_DEFAULT)   or comfy_running_by_handle()
    mini_alive    = is_port_in_use(MINI_PORT_DEFAULT)    or mini_running_by_handle()
    gallery_alive = is_port_in_use(SMART_GALLERY_PORT_DEFAULT) or gallery_running_by_handle()
    return jsonify({
        "lan_ip": lan_ip, "flask_port": FLASK_PORT,
        "comfy_port": comfy_port, "mini_port": mini_port, "gallery_port": gallery_port,
        "comfy_running": comfy_alive, "mini_running": mini_alive, "gallery_running": gallery_alive,
    })

@app.route("/checkpw", methods=["POST"])
@login_required
def checkpw():
    pw = request.form.get("password", "")
    if not (DELETE_PASSWORD and DELETE_PATH):
        return "no"
    return "ok" if pw == DELETE_PASSWORD else "no"

@app.route("/restart", methods=["POST"])
@login_required
def restart():
    stop_all(); time.sleep(2); launch_all(); return "success"

@app.route("/stop", methods=["POST"])
@login_required
def stop():
    stop_all(); return "success"

@app.route("/shutdown", methods=["POST"])
@login_required
def shutdown():
    def _kill_and_exit():
        stop_all()
        time.sleep(5.0)
        kill_other_controller_instances()
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_kill_and_exit, daemon=True).start()
    return "success"

@app.route("/delete", methods=["POST"])
@login_required
def delete_folder():
    if not (DELETE_PASSWORD and DELETE_PATH): return "error"
    if request.form.get("password") != DELETE_PASSWORD: return "error"
    try:
        if os.path.exists(DELETE_PATH): shutil.rmtree(DELETE_PATH, ignore_errors=True)
        return "success"
    except Exception: return "error"

@app.route("/recreate", methods=["POST"])
@login_required
def recreate_folder():
    if not (DELETE_PASSWORD and DELETE_PATH): return "error"
    if request.form.get("password") != DELETE_PASSWORD: return "error"
    try:
        os.makedirs(DELETE_PATH, exist_ok=True); return "success"
    except Exception: return "error"

@app.route("/relaunch_hidden_full", methods=["POST"])
@login_required
def route_relaunch_hidden_full():
    def worker(): stop_all(); time.sleep(0.8); relaunch_hidden_core()
    threading.Thread(target=worker, daemon=True).start()
    return "success"

@app.route("/relaunch_visible_full", methods=["POST"])
@login_required
def route_relaunch_visible_full():
    def worker(): stop_all(); time.sleep(0.8); relaunch_visible_core_autostart()
    threading.Thread(target=worker, daemon=True).start()
    return "success"

# =================== Server bootstrap =====================
def run_flask():
    import logging
    from werkzeug.serving import WSGIRequestHandler
    class SilentRequestHandler(WSGIRequestHandler):
        def log(self, *args, **kwargs): pass
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("werkzeug").disabled = True
    app.logger.disabled = True
    if START_DELAY: time.sleep(START_DELAY)
    lan = get_lan_ip()
    print(f"[INFO] Flask at http://0.0.0.0:{FLASK_PORT}  (LAN: http://{lan}:{FLASK_PORT})")
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False,
            request_handler=SilentRequestHandler)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    if not SKIP_LAUNCH:
        launch_all()
    while True: time.sleep(1)

if __name__ == "__main__":
    print("Starting Pocket Comfy")
    main()


# === Pocket Comfy Gallery integration (auto) ===
try:
    from gallery_routes import register_gallery
    register_gallery(app)
except Exception as e:
    print('[GALLERY] integration error:', e)
    
    