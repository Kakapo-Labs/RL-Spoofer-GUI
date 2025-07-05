import os
import subprocess
import time
import sys
from threading import Thread, Event
import asyncio
import tkinter as tk
from tkinter import messagebox
import json
import re
import winreg
import platform
import socket
import webbrowser

import customtkinter as ctk
try:
    import pystray
    from PIL import Image, ImageTk
    PYSTRAY_AVAILABLE = True
except ImportError:
    print("Warning: 'Pillow' or 'pystray' not installed. System tray icon and GIF animations will not be available.", file=sys.stderr, flush=True)
    print("Install with: pip install Pillow pystray", file=sys.stderr, flush=True)
    PYSTRAY_AVAILABLE = False

# Add dependency for video playback
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    print("Warning: 'opencv-python' not installed. Video splash screen will not be available.", file=sys.stderr, flush=True)
    print("Install with: pip install opencv-python", file=sys.stderr, flush=True)
    CV2_AVAILABLE = False


# --- Global Variables for Logging ---
# Log file path will now be determined dynamically based on APP_DIR
original_stdout = sys.stdout
original_stderr = sys.stderr
log_stdout_file = None
log_stderr_file = None

# --- Path Helper for PyInstaller ---
def get_asset_path(relative_path):
    """ Get absolute path to an asset, works for dev and for PyInstaller. """
    if getattr(sys, 'frozen', False):
        # If the application is run as a bundle, the PyInstaller bootloader
        # creates a temporary folder and stores its path in _MEIPASS.
        base_path = sys._MEIPASS
    else:
        # In development, the assets are relative to the script's directory.
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


# Custom exception handler for unhandled exceptions
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    print("\n--- Unhandled Exception Caught ---", file=sys.stderr, flush=True)
    import traceback
    traceback.print_exception(exc_type, exc_value, exc_traceback, file=sys.stderr)
    print("--- End of Unhandled Exception ---", file=sys.stderr, flush=True)
    error_detail = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    messagebox.showerror("Application Error",
                         f"An unexpected error occurred:\n\n{exc_value}\n\n"
                         f"Details have been written to the log file.\n" # Updated message
                         f"Please restart the application. If the problem persists, share the log file.")

sys.excepthook = handle_exception

# Tkinter specific exception handler for events/callbacks
def tk_exception_handler(self, exc, val, tb):
    handle_exception(exc, val, tb)

# Import mitmproxy components after logging is set up
try:
    from mitmproxy import http
    from mitmproxy.tools.dump import DumpMaster
    from mitmproxy.options import Options
    print("DEBUG: mitmproxy modules imported successfully.", file=sys.stdout, flush=True)
except ImportError as e:
    print(f"CRITICAL ERROR: Failed to import mitmproxy modules: {e}", file=sys.stderr, flush=True)
    messagebox.showerror("Import Error", f"Failed to import mitmproxy components. Please ensure mitmproxy is installed correctly.\nError: {e}")
    sys.exit(1)
except Exception as e:
    print(f"CRITICAL ERROR: An unexpected error occurred during mitmproxy import: {e}", file=sys.stderr, flush=True)
    messagebox.showerror("Startup Error", f"An unexpected error occurred during startup: {e}")
    sys.exit(1)


# --- Configuration ---
MITMPROXY_LISTEN_HOST = '127.0.0.1'
MITMPROXY_LISTEN_PORT = 8080
ROCKET_LEAGUE_PROCESS_NAME = "RocketLeague.exe"
SCAN_INTERVAL_SECONDS = 0.5
MAX_NAME_LENGTH = 32

mitmproxy_master = None
mitmproxy_fully_running_event = asyncio.Event()
mitmproxy_addon_instance = None
mitmproxy_thread_ref = None

# APP_DIR is for user-writable files like configs, logs, and first-run flags.
# It should point to a user-specific data directory, not the executable's directory.
APP_NAME = "RLNameSpoofer"
APP_VERSION = "1.0.0" # New version number
if sys.platform == "win32":
    APP_DIR = os.path.join(os.getenv('APPDATA'), APP_NAME)
elif sys.platform == "darwin":
    APP_DIR = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', APP_NAME)
else:
    # Fallback for Linux/other, typically ~/.config or ~/.local/share
    APP_DIR = os.path.join(os.path.expanduser('~'), '.config', APP_NAME)

# Ensure the application directory exists
os.makedirs(APP_DIR, exist_ok=True)

# Define paths for user-specific files within the APP_DIR
CONFIG_FILE_NAME = "config.json"
CONFIG_FILE_PATH = os.path.join(APP_DIR, CONFIG_FILE_NAME)
FIRST_RUN_FLAG_FILE = os.path.join(APP_DIR, "first_run_flag.txt")
log_file_path = os.path.join(APP_DIR, "mitmproxy_app_log.txt")


DEFAULT_CONFIG = {
    "last_spoof_name": "Kakpo Is Cool B)",
    "auto_scan_on_startup": False,
    "start_minimized": False
}

# --- Redirect stdout and stderr to a log file for debugging ---
# This block needs to be after APP_DIR and log_file_path are defined
try:
    log_stdout_file = open(log_file_path, 'a', encoding='utf-8')
    sys.stdout = log_stdout_file
    print(f"\n--- Application started at {time.ctime()} --- (stdout redirected)", file=sys.stdout, flush=True)
    log_stderr_file = log_stdout_file
    sys.stderr = log_stderr_file
    print(f"--- Application started at {time.ctime()} --- (stderr redirected)", file=sys.stdout, flush=True)
except Exception as e:
    sys.stdout = original_stdout
    sys.stderr = original_stderr
    print(f"CRITICAL ERROR: Failed to redirect stdout/stderr to log file: {e}", file=sys.stderr, flush=True)
    messagebox.showerror("Logging Setup Error", f"Failed to set up application logging: {e}\nApplication might not log further errors.")


def load_config():
    """Loads configuration from config.json. Creates it if missing or corrupted."""
    print("DEBUG: Attempting to load config.", file=sys.stdout, flush=True)
    # Ensure the directory exists before attempting to read/write
    os.makedirs(APP_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE_PATH):
        try:
            with open(CONFIG_FILE_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)
                print("DEBUG: Config loaded successfully.", file=sys.stdout, flush=True)
                # Merge with default config to ensure all keys are present
                return {**DEFAULT_CONFIG, **config}
        except json.JSONDecodeError as e:
            print(f"ERROR: Error loading config.json: {e}. File might be corrupted. Using default config.", file=sys.stderr, flush=True)
            # If corrupted, return default. The save_config will overwrite it later.
            return DEFAULT_CONFIG
        except Exception as e:
            print(f"ERROR: Unexpected error reading config.json: {e}. Using default config.", file=sys.stderr, flush=True)
            return DEFAULT_CONFIG
    print("DEBUG: Config file not found. Using default config.", file=sys.stdout, flush=True)
    return DEFAULT_CONFIG

def save_config(config):
    """Saves configuration to config.json."""
    print("DEBUG: Attempting to save config.", file=sys.stdout, flush=True)
    try:
        # Ensure the directory exists before attempting to write
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        print("DEBUG: Config saved successfully.", file=sys.stdout, flush=True)
    except IOError as e:
        print(f"ERROR: Error saving config.json: {e}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"ERROR: Unexpected error writing config.json: {e}", file=sys.stderr, flush=True)

def is_port_in_use(host, port):
    print(f"DEBUG: Checking if port {port} is in use.", file=sys.stdout, flush=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            print(f"DEBUG: Port {port} is free.", file=sys.stdout, flush=True)
            return False
        except OSError:
            print(f"DEBUG: Port {port} is in use.", file=sys.stdout, flush=True)
            return True
        except Exception as e:
            print(f"ERROR: Error checking port availability: {e}", file=sys.stderr, flush=True)
            return True # Assume in use to be safe

def set_system_proxy(host, port, gui_root):
    print(f"DEBUG: Attempting to set system proxy to {host}:{port}.", file=sys.stdout, flush=True)
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                             0, winreg.KEY_WRITE)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"{host}:{port}")
        winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ,
                          "<local>;*.epicgames.com;*.psyonix.com;*.live.psynet.gg")
        winreg.CloseKey(key)
        print(f"DEBUG: System proxy set to {host}:{port} successfully.", file=sys.stdout, flush=True)
        return True
    except PermissionError:
        print("ERROR: Permission denied when setting system proxy. Run as administrator!", file=sys.stderr, flush=True)
        return False
    except Exception as e:
        error_msg = f"An error occurred while setting system proxy: {e}"
        print(f"ERROR: Failed to set system proxy: {error_msg}", file=sys.stderr, flush=True)
        gui_root.after(0, lambda msg=error_msg: messagebox.showerror("Proxy Error", msg))
        return False

def disable_system_proxy():
    print("DEBUG: Attempting to disable system proxy.", file=sys.stdout, flush=True)
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                             0, winreg.KEY_WRITE)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        for val_name in ["ProxyServer", "ProxyOverride"]:
            try:
                winreg.DeleteValue(key, val_name)
            except FileNotFoundError:
                pass # Value might not exist, which is fine
        winreg.CloseKey(key)
        print("DEBUG: System proxy disabled successfully.", file=sys.stdout, flush=True)
        return True
    except Exception as e:
        print(f"ERROR: Failed to disable system proxy: {e}", file=sys.stderr, flush=True)
        return False

TASK_NAME = "RLNameSpoofer"

def get_app_executable_path_with_args():
    print("DEBUG: Getting app executable path with args.", file=sys.stdout, flush=True)
    app_path = ""
    if getattr(sys, 'frozen', False):
        app_path = f'"{sys.argv[0]}"'
        print(f"DEBUG: Frozen app detected. Executable path for Task Scheduler: {app_path}", file=sys.stdout, flush=True)
    else:
        python_exe = sys.executable
        pythonw_exe = python_exe.replace('python.exe', 'pythonw.exe')

        python_executable_to_use = python_exe
        if os.path.exists(pythonw_exe):
            python_executable_to_use = pythonw_exe
            print(f"DEBUG: Using pythonw.exe for Task Scheduler: {python_executable_to_use}", file=sys.stdout, flush=True)
        else:
            print(f"DEBUG: pythonw.exe not found. Falling back to python.exe for Task Scheduler: {python_executable_to_use}", file=sys.stdout, flush=True)

        app_path = f'"{python_executable_to_use}" "{os.path.abspath(sys.argv[0])}"'

    app_args = "--startup-auto-scan --start-minimized"
    full_command = f'{app_path} {app_args}'
    print(f"DEBUG: Generated full command for Task Scheduler: {full_command}", file=sys.stdout, flush=True)
    return full_command, app_path, app_args

def _add_task_to_scheduler_sync():
    print("DEBUG: Attempting to add task to scheduler (sync).", file=sys.stdout, flush=True)
    if platform.system() != "Windows":
        print("WARNING: Task Scheduler integration is only supported on Windows.", file=sys.stderr, flush=True)
        return False

    full_command, app_path, app_args = get_app_executable_path_with_args()

    try:
        command = [
            'schtasks', '/create',
            '/tn', TASK_NAME,
            '/tr', full_command,
            '/sc', 'onlogon',
            '/rl', 'highest',
            '/f'
        ]
        print(f"DEBUG: Executing schtasks command: {' '.join(command)}", file=sys.stdout, flush=True)
        result = subprocess.run(command, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)

        if result.returncode == 0:
            print(f"DEBUG: Successfully added '{TASK_NAME}' to Task Scheduler. stdout: {result.stdout}", file=sys.stdout, flush=True)
            return True
        else:
            error_msg = f"Failed to add '{TASK_NAME}' to Task Scheduler. Error: {result.stderr}"
            print(f"ERROR: {error_msg}", file=sys.stderr, flush=True)
            return False
    except Exception as e:
        print(f"ERROR: An unexpected error occurred while adding to Task Scheduler: {e}", file=sys.stderr, flush=True)
        return False

def _remove_task_from_scheduler_sync():
    print("DEBUG: Attempting to remove task from scheduler (sync).", file=sys.stdout, flush=True)
    if platform.system() != "Windows":
        return False

    try:
        command = ['schtasks', '/delete', '/tn', TASK_NAME, '/f']
        print(f"DEBUG: Executing schtasks command: {' '.join(command)}", file=sys.stdout, flush=True)
        result = subprocess.run(command, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)

        if result.returncode == 0:
            print(f"DEBUG: Successfully removed '{TASK_NAME}' from Task Scheduler. stdout: {result.stdout}", file=sys.stdout, flush=True)
            return True
        else:
            if "ERROR: The system cannot find the file specified." in result.stderr or \
               "ERROR: The specified task name does not exist." in result.stderr:
                print(f"DEBUG: '{TASK_NAME}' not found in Task Scheduler, no action needed.", file=sys.stdout, flush=True)
                return True
            else:
                error_msg = f"Failed to remove '{TASK_NAME}' from Task Scheduler. Error: {result.stderr}"
                print(f"ERROR: {error_msg}", file=sys.stderr, flush=True)
                return False
    except Exception as e:
        print(f"ERROR: An unexpected error occurred while removing from Task Scheduler: {e}", file=sys.stderr, flush=True)
        return False

def is_task_scheduled():
    print(f"DEBUG: Querying task scheduler for task: {TASK_NAME}.", file=sys.stdout, flush=True)
    if platform.system() != "Windows":
        return False

    try:
        command = ['schtasks', '/query', '/tn', TASK_NAME]
        result = subprocess.run(command, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)

        if result.returncode == 0:
            print(f"DEBUG: Task '{TASK_NAME}' found in Task Scheduler.", file=sys.stdout, flush=True)
            return True
        elif result.returncode == 1:
            print(f"DEBUG: Task '{TASK_NAME}' not found in Task Scheduler.", file=sys.stdout, flush=True)
            return False
        else:
            print(f"ERROR: Error querying Task Scheduler: {result.stderr}", file=sys.stderr, flush=True)
            return False
    except Exception as e:
        print(f"ERROR: An unexpected error occurred while checking Task Scheduler status: {e}", file=sys.stderr, flush=True)
        return False

def is_process_running(process_name):
    print(f"DEBUG: Checking if process '{process_name}' is running.", file=sys.stdout, flush=True)
    if platform.system() != "Windows":
        print(f"WARNING: Process detection for '{process_name}' is only implemented for Windows.", file=sys.stderr, flush=True)
        return False
    try:
        cmd = ['tasklist', '/FO', 'CSV', '/NH', '/FI', f'IMAGENAME eq {process_name}']
        output = subprocess.check_output(cmd, creationflags=subprocess.CREATE_NO_WINDOW).decode('utf-8')
        return re.search(rf'"{re.escape(process_name)}"', output, re.IGNORECASE) is not None
    except Exception as e:
        print(f"ERROR: Error checking for process '{process_name}': {e}", file=sys.stderr, flush=True)
        return False

def is_mitmproxy_cert_installed():
    """
    Checks if the mitmproxy certificate is installed in the Windows Trusted Root store
    for either the Current User or Local Machine using PowerShell. This is a more
    reliable method than using 'certutil'.
    Returns True if found, False otherwise.
    """
    if platform.system() != "Windows":
        print("WARNING: Certificate check is only supported on Windows.", file=sys.stderr, flush=True)
        return True  # Assume installed on other platforms to not block functionality

    try:
        # PowerShell command to check both CurrentUser and LocalMachine Root stores.
        # It will output the first certificate found that has 'mitmproxy' in its subject.
        # Using -ExecutionPolicy Bypass to avoid issues on systems with restricted policies.
        ps_command = (
            "$userCert = Get-ChildItem -Path Cert:\\CurrentUser\\Root | Where-Object { $_.Subject -like '*mitmproxy*' } | Select-Object -First 1; "
            "$machineCert = Get-ChildItem -Path Cert:\\LocalMachine\\Root | Where-Object { $_.Subject -like '*mitmproxy*' } | Select-Object -First 1; "
            "if ($userCert -or $machineCert) { Write-Output 'Found' }"
        )

        command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_command]

        # Use CREATE_NO_WINDOW to prevent a console window from flashing.
        # Specify encoding to handle PowerShell output correctly.
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW
        )

        # If the command succeeded and the output contains "Found", the certificate exists.
        if result.returncode == 0 and 'Found' in result.stdout:
            print("DEBUG: mitmproxy certificate found in a Trusted Root store (checked CurrentUser and LocalMachine via PowerShell).", file=sys.stdout, flush=True)
            return True
        else:
            # Log details if the certificate is not found or if an error occurred.
            print(f"DEBUG: mitmproxy certificate not found via PowerShell. Exit code: {result.returncode}", file=sys.stdout, flush=True)
            if result.stderr:
                print(f"DEBUG: PowerShell stderr: {result.stderr.strip()}", file=sys.stderr, flush=True)
            return False

    except FileNotFoundError:
        # This handles the case where PowerShell is not installed or not in the system's PATH.
        print("ERROR: 'powershell.exe' not found. Cannot check for certificate. Assuming it is installed to not block functionality.", file=sys.stderr, flush=True)
        return True  # Fallback to not block the user
    except Exception as e:
        print(f"ERROR: An unexpected error occurred while checking for certificate with PowerShell: {e}", file=sys.stderr, flush=True)
        return False  # Fail safely, assume not installed

class NameSpoofAddon:
    def __init__(self, new_name):
        self.new_name = new_name
        print(f"DEBUG: Addon initialized: Preparing to spoof display names to '{self.new_name}'.", file=sys.stdout, flush=True)

    def update_name(self, new_name):
        old_name = self.new_name
        self.new_name = new_name
        print(f"DEBUG: NameSpoofAddon: Updated spoof name from '{old_name}' to '{self.new_name}'.", file=sys.stdout, flush=True)

    def response(self, flow: http.HTTPFlow):
        target_domains = ["epicgames.dev", "epicgames.com", "psyonix.com", "live.psynet.gg"]
        if any(domain in flow.request.pretty_host for domain in target_domains) and \
                "application/json" in flow.response.headers.get("Content-Type", ""):
            self._process_json_body(flow)

    def _process_json_body(self, flow: http.HTTPFlow):
        message = flow.response
        try:
            body_data = message.json()
        except json.JSONDecodeError:
            return

        spoofed = False
        if isinstance(body_data, list) and len(body_data) == 1 and isinstance(body_data[0], dict):
            user_data = body_data[0]
            required_keys = ["accountId", "displayName", "preferredLanguage", "linkedAccounts", "cabinedMode"]
            if all(key in user_data for key in required_keys):
                if isinstance(user_data.get("linkedAccounts"), list) and isinstance(user_data.get("cabinedMode"), bool):
                    if user_data["displayName"] != self.new_name:
                        print(f"DEBUG: Identified specific user data response for {user_data.get('accountId')}.", file=sys.stdout, flush=True)
                        print(f"DEBUG: Spoofing displayName from '{user_data['displayName']}' to '{self.new_name}'.", file=sys.stdout, flush=True)
                        user_data["displayName"] = self.new_name
                        spoofed = True
                else:
                    print(
                        "DEBUG: JSON structure partially matched, but 'linkedAccounts' or 'cabinedMode' types are incorrect. Skipping.",
                        file=sys.stderr, flush=True)

        if spoofed:
            message.content = json.dumps(body_data, ensure_ascii=False).encode('utf-8')
            if "Content-Length" in message.headers:
                message.headers["Content-Length"] = str(len(message.content))
            print(f"DEBUG: *** Name spoofing applied to {flow.request.url} ***", file=sys.stdout, flush=True)

def run_mitmproxy_thread_target(new_name, gui_root):
    global mitmproxy_master, mitmproxy_fully_running_event, mitmproxy_addon_instance, mitmproxy_thread_ref
    print("DEBUG: Entering run_mitmproxy_thread_target.", file=sys.stdout, flush=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run_proxy_async():
        global mitmproxy_master, mitmproxy_addon_instance
        print("DEBUG: Entering run_proxy_async.", file=sys.stdout, flush=True)

        options = Options(
            listen_host=MITMPROXY_LISTEN_HOST,
            listen_port=MITMPROXY_LISTEN_PORT,
            mode=["regular"],
        )

        mitmproxy_master = DumpMaster(options, with_termlog=False)
        addon = NameSpoofAddon(new_name)
        mitmproxy_master.addons.add(addon)
        mitmproxy_addon_instance = addon

        mitmproxy_master.options.block_global = False
        print(f"DEBUG: Mitmproxy: Set block_global to {mitmproxy_master.options.block_global}", file=sys.stdout, flush=True)

        print(f"DEBUG: Mitmproxy thread: Attempting to run proxy on {MITMPROXY_LISTEN_HOST}:{MITMPROXY_LISTEN_PORT}...", file=sys.stdout, flush=True)

        try:
            mitmproxy_fully_running_event.set()
            print("DEBUG: Mitmproxy thread: Setup complete, now running master loop.", file=sys.stdout, flush=True)
            await mitmproxy_master.run()
            print("DEBUG: Mitmproxy thread: Master run task completed (normal shutdown).", file=sys.stdout, flush=True)
        except asyncio.CancelledError:
            print("DEBUG: Mitmproxy thread: Master run task cancelled (normal shutdown).", file=sys.stdout, flush=True)
        except Exception as e:
            error_msg = f"An error occurred in mitmproxy: {e}\nCheck log file."
            print(f"ERROR: Mitmproxy thread error during run: {error_msg}", file=sys.stderr, flush=True)
            gui_root.after(0, lambda msg=error_msg: messagebox.showerror("Mitmproxy Runtime Error", msg))
        finally:
            print("DEBUG: Mitmproxy thread: Performing final cleanup in thread.", file=sys.stdout, flush=True)
            if mitmproxy_master:
                mitmproxy_master.shutdown()
                mitmproxy_master = None
            mitmproxy_fully_running_event.clear()
            mitmproxy_addon_instance = None
            print("DEBUG: Mitmproxy thread: Event cleared, master and addon references nulled.", file=sys.stdout, flush=True)
        print("DEBUG: Exiting run_proxy_async.", file=sys.stdout, flush=True)

    try:
        asyncio.run(run_proxy_async())
    except RuntimeError as e:
        error_msg = f"Failed to initialize mitmproxy event loop: {e}\nCheck log file."
        print(f"ERROR: Mitmproxy thread: Runtime Error during asyncio.run(): {error_msg}", file=sys.stderr, flush=True)
        gui_root.after(0, lambda msg=error_msg: messagebox.showerror("Mitmproxy Setup Error", msg))
    except Exception as e:
        error_msg = f"An unexpected error occurred during proxy startup: {e}\nCheck log file."
        print(f"ERROR: Mitmproxy thread: Unexpected error outside async run: {error_msg}", file=sys.stderr, flush=True)
        gui_root.after(0, lambda msg=error_msg: messagebox.showerror("Mitmproxy General Error", msg))
    finally:
        print("DEBUG: Exiting run_mitmproxy_thread_target.", file=sys.stdout, flush=True)
        pass

def stop_mitmproxy_gracefully():
    global mitmproxy_master, mitmproxy_addon_instance, mitmproxy_fully_running_event
    print("DEBUG: Attempting to stop mitmproxy gracefully.", file=sys.stdout, flush=True)
    if mitmproxy_master:
        print("DEBUG: Mitmproxy: Signaling master to shut down from main thread...", file=sys.stdout, flush=True)
        mitmproxy_master.shutdown()
    else:
        print("DEBUG: Mitmproxy master not running, nothing to shut down.", file=sys.stdout, flush=True)
    mitmproxy_addon_instance = None
    mitmproxy_fully_running_event.clear()
    print("DEBUG: stop_mitmproxy_gracefully completed.", file=sys.stdout, flush=True)

class SpooferGUI:
    def __init__(self, master):
        print("DEBUG: Entering SpooferGUI __init__.", file=sys.stdout, flush=True)
        self.master = master
        self.proxy_thread = None
        self._after_id = None
        self.shutdown_complete_event = Event()
        self.is_proxy_running = False
        self.is_app_closing = False
        self.last_video_frame = None

        # --- NEW: Stores the name at the moment the proxy is activated ---
        self.name_at_proxy_start = None

        self.auto_scan_var = ctk.BooleanVar(value=False)
        self.auto_scan_thread = None
        self.auto_scan_stop_event = Event()
        self.rl_process_active = False

        self.tray_icon = None
        self.tray_thread = None
        self.tooltip_window = None

        self.main_ui_loaded_event = Event()
        self.ui_initialized = False

        self.is_syncing_ui = False
        self.previous_frame = None # To store the last active frame before showing FAQ

        # --- NEW: State variables for FAQ toggle and animation ---
        self.faq_visible = False
        self.is_animating = False

        # For borderless window dragging
        self._offset_x = 0
        self._offset_y = 0

        self._setup_theme()
        self._setup_window()
        self._create_fonts()

        # Load config and set initial variable state from it
        self.app_config = load_config()
        self.new_name_var = ctk.StringVar(value=self.app_config["last_spoof_name"])
        self.auto_scan_var.set(self.app_config.get("auto_scan_on_startup", False))

        # --- Startup Logic ---
        print(f"DEBUG: SpooferGUI.__init__: sys.argv = {sys.argv}", file=sys.stdout, flush=True)
        self.launched_minimized_by_startup = "--start-minimized" in sys.argv and PYSTRAY_AVAILABLE
        # --- MODIFICATION: Check for startup argument to force auto-scan on. ---
        startup_auto_scan_requested = "--startup-auto-scan" in sys.argv

        # If started by scheduler, force the auto-scan var to True for this session.
        if startup_auto_scan_requested:
            print("DEBUG: Startup auto-scan requested via command-line arg. Forcing auto-scan ON for this session.", file=sys.stdout, flush=True)
            self.auto_scan_var.set(True)

        # If auto-scan is enabled (either by config or startup arg), start the background process.
        if self.auto_scan_var.get():
            print("DEBUG: Auto-scan is enabled. Starting background scanner.", file=sys.stdout, flush=True)
            # We call the toggle function which will handle starting the thread
            self.on_auto_scan_toggle()

        if self.launched_minimized_by_startup:
            print("DEBUG: Minimized startup. Deferring UI creation.", file=sys.stdout, flush=True)
            self.master.withdraw()
            self.create_tray_icon()
        else:
            # Normal startup: Build the UI immediately and then show splash
            self.build_full_ui()

            # Not starting minimized. Show window, then play splash immediately to cover loading.
            self.master.attributes("-alpha", 1.0) # Make window visible

            # --- Bring window to the front on startup ---
            self.master.lift()
            self.master.attributes('-topmost', True)
            self.master.focus_force()
            self.master.after(100, lambda: self.master.attributes('-topmost', False))
            # --- End bring window to the front ---

            # Place splash frame on top
            self.splash_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.gif_label.pack(fill="both", expand=True)
            self.splash_frame.tkraise()
            # Play video
            self.master.after(50, self._play_video) # Use a small delay to ensure the window is drawn before playback starts

        # Final setup
        self.master.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.master.bind("<Unmap>", self.on_minimize)
        print("DEBUG: Exiting SpooferGUI __init__.", file=sys.stdout, flush=True)

    def build_full_ui(self):
        """Creates all the complex UI widgets on demand."""
        if self.ui_initialized:
            return # Don't build it twice
        print("DEBUG: Building the full UI now.", file=sys.stdout, flush=True)

        # This frame will have the rounded corners and the actual background color
        self.main_container = ctk.CTkFrame(self.master, corner_radius=30, fg_color=self.COLORS["bg"])
        self.main_container.pack(fill="both", expand=True)

        # This frame holds the actual content, allowing the title bar to be separate
        # from the main content area if needed, and is transparent itself.
        self.content_container = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.content_container.pack(fill="both", expand=True)

        # Create custom title bar
        self._create_title_bar()

        # --- Frames Setup ---
        # Splash frame for video - NOW A CHILD OF main_container FOR ROUNDED CORNERS
        self.splash_frame = ctk.CTkFrame(self.main_container, fg_color="transparent", corner_radius=0)
        self.gif_label = ctk.CTkLabel(self.splash_frame, text="", fg_color="transparent")

        # Main content frames (remain as children of content_container)
        self.main_frame = ctk.CTkFrame(self.content_container, fg_color="transparent")
        self.setup_frame = ctk.CTkFrame(self.content_container, fg_color="transparent")
        self.setup_frame_step2 = ctk.CTkFrame(self.content_container, fg_color="transparent")
        self.faq_frame = ctk.CTkFrame(self.content_container, fg_color="transparent") # New FAQ frame

        # Create all the widgets and bind events
        self._create_all_widgets()

        # --- MODIFICATION: Set ui_initialized flag before calling post-load actions ---
        # This ensures that subsequent functions like sync_startup_ui can run correctly.
        self.ui_initialized = True

        self._post_main_ui_load_actions()

        print("DEBUG: UI build complete.", file=sys.stdout, flush=True)

    def _setup_theme(self):
        """Define the new sleek, premium color palette."""
        print("DEBUG: Entering _setup_theme.", file=sys.stdout, flush=True)
        ctk.set_appearance_mode("Dark")

        self.COLORS = {
            "bg": "#101010",
            "frame_bg": "#151515",
            "title_bar_bg": "#151515",
            "primary_green": "#22f565",
            "primary_green_hover": "#1fdd5c",
            "stop_red": "#F47174",
            "stop_red_hover": "#D85B5E",
            "text_primary": "#F2F2F7",
            "text_secondary": "#8E8E93",
            "border": "#3A3A3C",
            "status_idle": "#636366",
            "link_blue": "#64B5F6", # Added for hyperlinks
            "warning_orange": "#FF9800" # New orange for warnings
        }
        print("DEBUG: Exiting _setup_theme.", file=sys.stdout, flush=True)

    def _setup_window(self):
        """Configure the main window's properties for a borderless design."""
        print("DEBUG: Entering _setup_window.", file=sys.stdout, flush=True)

        # Set initial size
        window_width, window_height = 480, 600

        # --- Center Window on Screen ---
        self.master.update_idletasks()
        screen_width = self.master.winfo_screenwidth()
        screen_height = self.master.winfo_screenheight()
        x = (screen_width // 2) - (window_width // 2)
        y = (screen_height // 2) - (window_height // 2)
        self.master.geometry(f'{window_width}x{window_height}+{x}+{y}')

        # --- LOCK WINDOW SIZE ---
        self.master.resizable(False, False)
        self.master.minsize(window_width, window_height)
        self.master.maxsize(window_width, window_height)

        # --- Set Window Title ---
        self.master.title("Rocket League Name Spoofer")

        # --- ADDED: Set the application icon for the window and taskbar ---
        try:
            self.master.iconbitmap(get_asset_path("icon.ico"))
        except Exception as e:
            print(f"ERROR: Failed to set application icon: {e}", file=sys.stderr, flush=True)

        # --- BORDERLESS WINDOW SETUP ---
        self.master.overrideredirect(True) # Remove title bar and borders

        TRANSPARENT_COLOR = '#010101'
        self.master.attributes("-transparentcolor", TRANSPARENT_COLOR)
        self.master.configure(fg_color=TRANSPARENT_COLOR)

        self.master.attributes("-alpha", 0.0) # Start fully transparent, will be made visible before splash
        print("DEBUG: Exiting _setup_window.", file=sys.stdout, flush=True)

    def _create_fonts(self):
        """Define the fonts used throughout the application."""
        print("DEBUG: Entering _create_fonts.", file=sys.stdout, flush=True)
        self.font_main_title = ctk.CTkFont(family="Rubik", size=13, weight="bold")
        self.font_entry = ctk.CTkFont(family="Rubik", size=18)
        self.font_action_button = ctk.CTkFont(family="Rubik", size=16, weight="bold")
        self.font_startup_button = ctk.CTkFont(family="Rubik", size=13)
        self.font_auto_manage = ctk.CTkFont(family="Rubik", size=14)
        self.font_status = ctk.CTkFont(family="Rubik", size=12, weight="normal")
        self.font_credit = ctk.CTkFont(family="Rubik", size=11, slant="italic")
        self.font_setup_title = ctk.CTkFont(family="Rubik", size=26, weight="bold")
        self.font_setup_text = ctk.CTkFont(family="Rubik", size=15)
        self.font_char_counter = ctk.CTkFont(family="Rubik", size=12)
        self.font_title_bar = ctk.CTkFont(family="Rubik", size=12, weight="bold")
        self.font_faq_question = ctk.CTkFont(family="Rubik", size=16, weight="bold")
        self.font_faq_answer = ctk.CTkFont(family="Rubik", size=14)
        print("DEBUG: Exiting _create_fonts.", file=sys.stdout, flush=True)

    def _create_title_bar(self):
        """Creates the custom title bar with drag support and window controls."""
        self.title_bar = ctk.CTkFrame(self.content_container, height=40, fg_color=self.COLORS["title_bar_bg"], corner_radius=0)
        self.title_bar.pack(fill="x", side="top")

        self.title_bar.bind("<ButtonPress-1>", self._start_move)
        self.title_bar.bind("<ButtonRelease-1>", self._stop_move)
        self.title_bar.bind("<B1-Motion>", self._do_move)

        title_label = ctk.CTkLabel(self.title_bar, text="ROCKET LEAGUE NAME SPOOFER", font=self.font_title_bar, text_color=self.COLORS["text_secondary"])
        title_label.place(relx=0.5, rely=0.5, anchor="center")

        title_label.bind("<ButtonPress-1>", self._start_move)
        title_label.bind("<ButtonRelease-1>", self._stop_move)
        title_label.bind("<B1-Motion>", self._do_move)

        # --- FAQ Button (UPDATED) ---
        faq_button = ctk.CTkButton(
            self.title_bar, text="?", command=self.toggle_faq_screen, width=40, height=40,
            font=ctk.CTkFont(size=16, weight="bold"), fg_color="transparent", hover_color=self.COLORS["border"], text_color=self.COLORS["text_secondary"]
        )
        faq_button.pack(side="left", fill="y")

        close_button = ctk.CTkButton(
            self.title_bar, text="✕", command=self.on_closing, width=40, height=40,
            font=ctk.CTkFont(size=16), fg_color="transparent", hover_color=self.COLORS["stop_red"], text_color=self.COLORS["text_secondary"]
        )
        close_button.pack(side="right", fill="y")

        minimize_button = ctk.CTkButton(
            self.title_bar, text="—", command=self.hide_window, width=40, height=40,
            font=ctk.CTkFont(size=16, weight="bold"), fg_color="transparent", hover_color=self.COLORS["border"], text_color=self.COLORS["text_secondary"]
        )
        minimize_button.pack(side="right", fill="y")

    def _start_move(self, event):
        self._offset_x = event.x_root - self.master.winfo_rootx()
        self._offset_y = event.y_root - self.master.winfo_rooty()

    def _stop_move(self, event):
        self._offset_x = None
        self._offset_y = None

    def _do_move(self, event):
        new_x = self.master.winfo_pointerx() - self._offset_x
        new_y = self.master.winfo_pointery() - self._offset_y
        self.master.geometry(f"+{new_x}+{new_y}")

    def _create_all_widgets(self):
        """
        Creates and lays out all main GUI and setup widgets.
        This can be called from a background thread or directly.
        """
        print("DEBUG: Creating all UI widgets...", file=sys.stdout, flush=True)
        # --- Main Frame Widgets ---
        self.main_frame_content = ctk.CTkFrame(self.main_frame, fg_color=self.COLORS["frame_bg"], corner_radius=12)
        self.main_frame_content.pack(pady=20, padx=20, fill="both", expand=True)
        self.main_frame_content.grid_columnconfigure(0, weight=1)
        self.title_label = ctk.CTkLabel(self.main_frame_content, text="SPOOFED DISPLAY NAME", font=self.font_main_title, text_color=self.COLORS["text_secondary"])
        self.new_name_entry = ctk.CTkEntry(self.main_frame_content, textvariable=self.new_name_var, font=self.font_entry, corner_radius=10, border_width=2, border_color=self.COLORS["border"], fg_color=self.COLORS["bg"], text_color=self.COLORS["text_primary"], justify="center", height=55)

        # --- NEW: Warning label for name changes while proxy is active ---
        self.restart_warning_label = ctk.CTkLabel(self.main_frame_content, text="Restart Rocket League for name change to take effect", font=self.font_status, text_color=self.COLORS["warning_orange"])

        self.char_counter_label = ctk.CTkLabel(self.main_frame_content, text="0/32", font=self.font_char_counter, text_color=self.COLORS["status_idle"])
        self.toggle_button = ctk.CTkButton(self.main_frame_content, text="ACTIVATE", command=self.toggle_proxy_clicked, height=55, font=self.font_action_button, corner_radius=12, fg_color="transparent", border_width=2, border_color=self.COLORS["primary_green"], text_color=self.COLORS["primary_green"], hover_color=self.COLORS["frame_bg"])
        self.auto_scan_checkbox = ctk.CTkCheckBox(self.main_frame_content, text="Automatically attach to Rocket League", variable=self.auto_scan_var, font=self.font_auto_manage, text_color=self.COLORS["text_secondary"], checkbox_width=24, checkbox_height=24, corner_radius=7, fg_color=self.COLORS["primary_green"], hover_color=self.COLORS["primary_green_hover"], border_color=self.COLORS["border"], border_width=2)
        self.status_frame = ctk.CTkFrame(self.main_frame_content, fg_color="transparent")
        self.status_frame.grid_columnconfigure(1, weight=1)
        self.proxy_status_indicator = ctk.CTkLabel(self.status_frame, text="●", font=ctk.CTkFont(size=20), text_color=self.COLORS["status_idle"])
        self.status_label = ctk.CTkLabel(self.status_frame, text="Proxy: Inactive", text_color=self.COLORS["text_secondary"], font=self.font_status)
        self.rl_status_indicator = ctk.CTkLabel(self.status_frame, text="●", font=ctk.CTkFont(size=20), text_color=self.COLORS["status_idle"])
        self.rl_status_label = ctk.CTkLabel(self.status_frame, text="Game: Not Running", text_color=self.COLORS["text_secondary"], font=self.font_status)
        self.task_scheduler_button = ctk.CTkButton(self.main_frame_content, text="Add/Remove from Startup", command=self.toggle_task_scheduler_clicked, height=45, font=self.font_startup_button, corner_radius=10, fg_color="transparent", hover_color=self.COLORS["frame_bg"], text_color=self.COLORS["text_secondary"], border_width=2, border_color=self.COLORS["border"])

        # --- Setup Frame Widgets ---
        self.setup_title_label = ctk.CTkLabel(self.setup_frame, text="First-Time Setup", font=self.font_setup_title, text_color=self.COLORS["text_primary"])
        self.setup_description_label = ctk.CTkLabel(self.setup_frame, text="Welcome!\nTo intercept and edit Rocket League's HTTPS traffic, a local certificate must be installed. This is UNIQUE to your device and is a one-time process.", font=self.font_setup_text, text_color=self.COLORS["text_secondary"], wraplength=400, justify="center")
        self.begin_setup_button = ctk.CTkButton(self.setup_frame, text="BEGIN SETUP", command=self._start_setup_process, height=60, font=self.font_action_button, corner_radius=12, fg_color="transparent", border_width=2, border_color=self.COLORS["primary_green"], text_color=self.COLORS["primary_green"], hover_color=self.COLORS["frame_bg"])

        # --- Setup Step 2 Frame Widgets ---
        self.setup_step2_title_label = ctk.CTkLabel(self.setup_frame_step2, text="Install Certificate", font=self.font_setup_title, text_color=self.COLORS["text_primary"])
        self.setup_instructions_label = ctk.CTkLabel(self.setup_frame_step2, text="1. A browser page to http://mitm.it has opened.\n2. Download and run the certificate for your OS.\n3. Install it into the 'Trusted Root Certification Authorities' store.\n4. When finished, click the button below.", font=self.font_setup_text, text_color=self.COLORS["text_secondary"], wraplength=400, justify="left")
        self.open_browser_button = ctk.CTkButton(self.setup_frame_step2, text="Re-open mitm.it", command=lambda: webbrowser.open_new_tab("http://mitm.it"), height=45, font=self.font_startup_button, corner_radius=10, fg_color="transparent", hover_color=self.COLORS["frame_bg"], text_color=self.COLORS["text_secondary"], border_width=2, border_color=self.COLORS["border"])
        self.finish_setup_button = ctk.CTkButton(self.setup_frame_step2, text="FINISH SETUP", command=self._finish_setup_process, height=60, font=self.font_action_button, corner_radius=12, fg_color="transparent", border_width=2, border_color=self.COLORS["primary_green"], text_color=self.COLORS["primary_green"], hover_color=self.COLORS["frame_bg"])
        self.setup_status_label = ctk.CTkLabel(self.setup_frame_step2, text="Status: Proxy running for certificate download...", fg_color="transparent", text_color=self.COLORS["primary_green"], font=self.font_status)

        # --- FAQ Frame Widgets (MODIFIED) ---
        self.faq_content_frame = ctk.CTkFrame(self.faq_frame, fg_color=self.COLORS["frame_bg"], corner_radius=12)
        self.faq_content_frame.pack(pady=20, padx=20, fill="both", expand=True)
        self.faq_content_frame.grid_columnconfigure(0, weight=1)

        faq_title = ctk.CTkLabel(self.faq_content_frame, text="Frequently Asked Questions", font=self.font_setup_title, text_color=self.COLORS["text_primary"])
        faq_title.grid(row=0, column=0, pady=(15, 20), padx=20, sticky="ew")

        faq_scrollable_frame = ctk.CTkScrollableFrame(self.faq_content_frame, fg_color="transparent")
        faq_scrollable_frame.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        self.faq_content_frame.grid_rowconfigure(1, weight=1)
        faq_scrollable_frame.grid_columnconfigure(0, weight=1)

        faq_data = {
            "Q1: How do I use this?": "A: Enter a new name, press 'Activate', THEN open Rocket League and enjoy your new name!\n\nIf you'd like to make it even easier, check the box to automatically detect whenever Rocket League is opened on your behalf and press 'Run automatically on Startup' to launch this program in the background everytime you turn on your PC for 'Set-and-forget' name spoofing.",
            "Q2: It's not working!": "A: Ensure you've PROPERLY completed the first-time setup to install the mitmproxy certificate. If it still fails, make sure no other proxy or VPN is running. Restarting the app and your game can also solve issues.\n\nFor persistent problems, check the log file in %APPDATA%/RLNameSpoofer. If all else fails, try using an LLM (ChatGPT, Gemini, etc.) to help solve the problem, or join the [Kakapo Labs Discord server](https://discord.gg/hXAVPfYHUN).",
            "Q3: Will Psyonix ban me?": "A: It is highly unlikely. This spoofer does not give any (real) competitive advantage. However, like with all third-party tools, use it at your own discretion, especially ones that affect the experience of other players like this one.\nPsyonix has a history of being INCREDIBLY relaxed in how they enforce their modding rules, but nobody knows what the future may hold. Anyone who says otherwise is likely lying and trying to sell you a product; there is ALWAYS some risk involved when modding online games.",
            "Q4: Why does this program need admin privileges?": "A: Administrator privileges are required to modify Windows proxy settings, which forces Rocket League's internet traffic to run through this program. It's also needed to add the app to the Windows Task Scheduler for automatic startup.",
            "Q5: I paid money for this!": "A: Sorry, amigo, but if you spent money on this, you've been swindled (it happens to the best of us, don't feel too bad). All of my creations under Kakapo Labs have been and will always be free and available from my GitHub. I want to build resources to help people have fun, not to scam kids out of their pocket money. If you have paid money for this, I'd recommend requesting a refund from your bank IMMEDIATELY. As for how you'll get your revenge, I'll leave that up to you.",
            "Q6: Is this safe?": "A: Yes. The program is open-source on [GitHub](https://github.com/Kakapo-Labs/RL-Spoofer-GUI) and only modifies traffic locally on your computer. It does not inject code into the game or alter game files. Don't just take my word for it; go check the source code for yourself. Never trust anyone handing out free, closed-source .exe files, no matter how tempting."
        }

        q_labels = []
        for i, (question, answer) in enumerate(faq_data.items()):
            # Create the question label
            q_label = ctk.CTkLabel(faq_scrollable_frame, text=question, font=self.font_faq_question, text_color=self.COLORS["primary_green"], justify="left", anchor="w")
            q_label.grid(row=i*2, column=0, sticky="ew", padx=15, pady=(20, 5))
            q_labels.append(q_label) # Add to list for dynamic wrapping

            # Create a Textbox for the answer to handle wrapping and hyperlinks
            a_textbox = ctk.CTkTextbox(faq_scrollable_frame,
                                       font=self.font_faq_answer,
                                       text_color=self.COLORS["text_secondary"],
                                       wrap="word",
                                       fg_color="transparent",
                                       border_width=0,
                                       activate_scrollbars=False)
            a_textbox.grid(row=i*2 + 1, column=0, sticky="ew", padx=15, pady=(0, 15))
            a_textbox.configure(state="normal") # Enable to insert text

            link_map = {}
            link_pattern = re.compile(r'\[([^\]]+)\]\((https?://[^\s\)]+)\)')
            last_end = 0

            # Find all links and insert text and tags
            for match_num, match in enumerate(link_pattern.finditer(answer)):
                start, end = match.span()
                display_text = match.group(1)
                link_url = match.group(2)
                tag_name = f"link_{i}_{match_num}"

                a_textbox.insert("end", answer[last_end:start])
                a_textbox.insert("end", display_text, tag_name)
                link_map[tag_name] = link_url
                last_end = end

            a_textbox.insert("end", answer[last_end:])

            # Configure tags for hyperlink appearance and behavior
            for tag_name, url in link_map.items():
                a_textbox.tag_config(tag_name, foreground=self.COLORS["link_blue"], underline=True)

                # Create a closure to capture the correct URL for the handler
                def make_link_handler(url_to_open):
                    return lambda event: webbrowser.open_new(url_to_open)

                a_textbox.tag_bind(tag_name, "<Button-1>", make_link_handler(url))
                a_textbox.tag_bind(tag_name, "<Enter>", lambda e, tb=a_textbox: tb.configure(cursor="hand2"))
                a_textbox.tag_bind(tag_name, "<Leave>", lambda e, tb=a_textbox: tb.configure(cursor=""))

            a_textbox.configure(state="disabled") # Make it read-only

            # --- REVISED CODE TO DYNAMICALLY SET TEXTBOX HEIGHT ---
            a_textbox.update_idletasks() # Ensure text is processed and wrapped

            try:
                # Get the number of display lines
                num_lines = int(a_textbox.index("end-1c").split('.')[0])

                # Get font and padding details from the widget
                font = a_textbox.cget("font")
                line_height = font.metrics("linespace")
                border_spacing = a_textbox.cget("border_spacing")
                # Get vertical padding/spacing inside the textbox
                spacing1 = a_textbox.cget("spacing1")
                spacing3 = a_textbox.cget("spacing3")

                # Calculate the required height
                # Height = (lines * space_per_line) + internal_text_padding + external_border_padding
                required_height = (num_lines * line_height) + spacing1 + spacing3 + (2 * border_spacing)

                # Set the calculated height
                a_textbox.configure(height=required_height)

            except (tk.TclError, ValueError) as e:
                # Handle cases where the textbox is empty or index is invalid
                print(f"DEBUG: Could not calculate height for a FAQ answer, using default. Error: {e}", file=sys.stdout, flush=True)
            # --- END OF REVISED CODE ---

        # Function to dynamically update wraplength of question labels
        def update_wraplength(event):
            new_wraplength = event.width - 40 # Subtract padding
            for label in q_labels:
                label.configure(wraplength=new_wraplength)

        faq_scrollable_frame.bind('<Configure>', update_wraplength)

        # The back button now also calls toggle_faq_screen
        back_button = ctk.CTkButton(self.faq_content_frame, text="← Back", command=self.toggle_faq_screen, height=45, font=self.font_startup_button, corner_radius=10, fg_color="transparent", hover_color=self.COLORS["frame_bg"], text_color=self.COLORS["text_secondary"], border_width=2, border_color=self.COLORS["border"])
        back_button.grid(row=2, column=0, pady=(10, 15), padx=20, sticky="sew")


        # --- Layout Grids ---
        self.title_label.grid(row=0, column=0, pady=(15, 5), padx=20, sticky="ew")
        self.new_name_entry.grid(row=1, column=0, pady=0, padx=20, sticky="ew")

        # --- NEW/MODIFIED LAYOUT for warning and char counter ---
        self.restart_warning_label.grid(row=2, column=0, pady=(4, 0), padx=25, sticky="w")
        self.restart_warning_label.grid_remove() # Hide initially
        self.char_counter_label.grid(row=2, column=0, pady=(4, 15), padx=25, sticky="e")

        self.toggle_button.grid(row=3, column=0, pady=5, padx=20, sticky="ew")
        self.auto_scan_checkbox.grid(row=4, column=0, pady=15, padx=20, sticky="w")
        self.status_frame.grid(row=5, column=0, pady=10, padx=20, sticky="ew")
        self.main_frame_content.grid_rowconfigure(6, weight=1) # Spacer row
        self.task_scheduler_button.grid(row=7, column=0, pady=(10, 5), padx=20, sticky="sew")

        # --- NEW: Footer section ---
        # This frame holds the buttons
        buttons_footer_frame = ctk.CTkFrame(self.main_frame_content, fg_color="transparent")
        buttons_footer_frame.grid(row=8, column=0, pady=(5, 0), padx=20, sticky="ew")
        buttons_footer_frame.grid_columnconfigure(0, weight=1) # Left button
        buttons_footer_frame.grid_columnconfigure(1, weight=1) # Right button

        self.reinstall_menu_button = ctk.CTkButton(
            buttons_footer_frame,
            text="Re-run Setup",
            command=self.reinstall_certificate,
            font=ctk.CTkFont(family="Rubik", size=10),
            fg_color="transparent",
            text_color=self.COLORS["text_secondary"],
            hover_color=self.COLORS["border"],
            width=80,
            height=24,
            border_width=1,
            border_color=self.COLORS["border"]
        )
        self.reinstall_menu_button.grid(row=0, column=0, sticky="w")

        # Load discord icon
        discord_icon = None
        if PYSTRAY_AVAILABLE:
            try:
                discord_icon_path = get_asset_path("discord.png")
                discord_icon = ctk.CTkImage(Image.open(discord_icon_path), size=(24, 24))
            except Exception as e:
                print(f"WARNING: Could not load discord.png: {e}. Using text icon instead.", file=sys.stderr, flush=True)
                discord_icon = None

        self.discord_button = ctk.CTkButton(
            buttons_footer_frame,
            text="" if discord_icon else "D",
            image=discord_icon,
            command=lambda: webbrowser.open_new_tab("https://discord.gg/hXAVPfYHUN"),
            width=24,
            height=24,
            border_width=0,
            fg_color="transparent",
            text_color=self.COLORS["text_secondary"],
            hover_color=self.COLORS["border"],
        )
        self.discord_button.grid(row=0, column=1, sticky="e")

        # This frame holds the centered credits below the buttons
        credits_footer_frame = ctk.CTkFrame(self.main_frame_content, fg_color="transparent")
        credits_footer_frame.grid(row=9, column=0, pady=(2, 10), padx=20, sticky="ew")

        self.credit_label = ctk.CTkLabel(credits_footer_frame, text="GUI and Reverse Engineering by Kakpo", font=self.font_credit, text_color=self.COLORS["status_idle"])
        self.credit_label.pack()

        self.version_label = ctk.CTkLabel(credits_footer_frame, text=f"Version {APP_VERSION}", font=self.font_credit, text_color=self.COLORS["status_idle"])
        self.version_label.pack()
        # --- END NEW WIDGETS ---

        self.proxy_status_indicator.grid(row=0, column=0, padx=(15, 5), pady=5)
        self.status_label.grid(row=0, column=1, padx=(0, 15), pady=5, sticky="w")
        self.rl_status_indicator.grid(row=1, column=0, padx=(15, 5), pady=5)
        self.rl_status_label.grid(row=1, column=1, padx=(0, 15), pady=5, sticky="w")
        self.setup_frame.grid_columnconfigure(0, weight=1)
        self.setup_title_label.grid(row=0, column=0, pady=(80, 20), padx=25)
        self.setup_description_label.grid(row=1, column=0, pady=(0, 40), padx=25)
        self.begin_setup_button.grid(row=2, column=0, pady=(20, 60), padx=40, sticky="ew")
        self.setup_frame_step2.grid_columnconfigure(0, weight=1)
        self.setup_step2_title_label.grid(row=0, column=0, pady=(60, 20), padx=25)
        self.setup_instructions_label.grid(row=1, column=0, pady=0, padx=30)
        self.open_browser_button.grid(row=2, column=0, pady=20, padx=40, sticky="ew")
        self.setup_status_label.grid(row=3, column=0, pady=10, padx=25)
        self.finish_setup_button.grid(row=4, column=0, pady=(20, 60), padx=40, sticky="ew")
        print("DEBUG: Finished creating UI widgets.", file=sys.stdout, flush=True)

    def _post_main_ui_load_actions(self):
        """
        Actions to perform on the main thread after the main UI widgets have been loaded.
        """
        print("DEBUG: Main thread: Performing post-main-UI-load actions.", file=sys.stdout, flush=True)
        self.char_counter_label.bind("<Enter>", self._show_tooltip)
        self.char_counter_label.bind("<Leave>", self._hide_tooltip)
        self.auto_scan_checkbox.configure(command=self.on_auto_scan_toggle)
        self.new_name_var.trace_add("write", self.on_new_name_change)
        self.new_name_var.trace_add("write", self._update_char_counter)
        self._update_char_counter()

        self._check_and_show_initial_screen()
        self.main_ui_loaded_event.set()
        print("DEBUG: Main thread: Post-main-UI-load actions complete.", file=sys.stdout, flush=True)

    def _play_video(self):
        """Starts the video playback."""
        print("DEBUG: Entering _play_video.", file=sys.stdout, flush=True)

        if not CV2_AVAILABLE:
            print("ERROR: OpenCV (cv2) is not installed. Cannot play video. Skipping splash.", file=sys.stderr, flush=True)
            self.splash_frame.place_forget()
            return

        # Use the get_asset_path helper function to find the video
        video_path = get_asset_path("splash.mov")

        if not os.path.exists(video_path):
            print(f"ERROR: splash.mov not found at path: {video_path}. Skipping splash.", file=sys.stderr, flush=True)
            self.splash_frame.place_forget()
            return

        try:
            self.video_capture = cv2.VideoCapture(video_path)
            if not self.video_capture.isOpened():
                print(f"ERROR: Could not open video file: {video_path}", file=sys.stderr, flush=True)
                self.splash_frame.place_forget()
                return

            fps = self.video_capture.get(cv2.CAP_PROP_FPS)
            self.video_delay = int(1000 / fps) if fps > 0 else 33
            self._update_video_frame()

        except Exception as e:
            print(f"ERROR: Failed to start video playback: {e}", file=sys.stderr, flush=True)
            if hasattr(self, 'video_capture'):
                self.video_capture.release()
            self.splash_frame.place_forget()

    def _update_video_frame(self):
        """Reads, resizes, and displays the next frame of the video."""
        if not hasattr(self, 'video_capture') or not self.video_capture.isOpened():
            return

        ret, frame = self.video_capture.read()

        if ret:
            window_width = self.master.winfo_width()
            window_height = self.master.winfo_height()

            if window_height <= 0 or window_width <= 0:
                self.master.after(20, self._update_video_frame)
                return

            resized_frame = cv2.resize(frame, (window_width, window_height), interpolation=cv2.INTER_AREA)
            frame_rgba = cv2.cvtColor(resized_frame, cv2.COLOR_BGR2RGBA)
            self.last_video_frame = Image.fromarray(frame_rgba)
            imgtk = ImageTk.PhotoImage(image=self.last_video_frame)

            self.gif_label.configure(image=imgtk)
            self.gif_label.image = imgtk

            self.master.after(self.video_delay, self._update_video_frame)
        else:
            print("DEBUG: Video playback finished. Starting transition.", file=sys.stdout, flush=True)
            self.video_capture.release()
            self.master.after(100, self._transition_from_splash)

    def _transition_from_splash(self, alpha=255):
        """
        Fades out the last frame of the splash video to reveal the UI underneath.
        """
        if not self.main_ui_loaded_event.is_set():
            self.master.after(100, self._transition_from_splash, alpha)
            return

        if alpha > 0:
            if self.last_video_frame:
                img_copy = self.last_video_frame.copy()
                img_copy.putalpha(alpha)
                imgtk = ImageTk.PhotoImage(image=img_copy)
                self.gif_label.image = imgtk
                self.gif_label.configure(image=imgtk)
            new_alpha = max(alpha - 20, 0)
            self.master.after(20, self._transition_from_splash, new_alpha)
        else:
            print("DEBUG: Splash fade complete.", file=sys.stdout, flush=True)
            self.splash_frame.place_forget()

    def _check_and_show_initial_screen(self):
        """Determines whether to show the main GUI or the first-time setup screen."""
        print("DEBUG: Entering _check_and_show_initial_screen.", file=sys.stdout, flush=True)
        # Use the updated FIRST_RUN_FLAG_FILE path
        has_completed_setup = os.path.exists(FIRST_RUN_FLAG_FILE)
        print(f"DEBUG: First run flag file '{FIRST_RUN_FLAG_FILE}' exists: {has_completed_setup}", file=sys.stdout, flush=True)

        self.update_status_after_stop()

        self.sync_startup_ui()

        if has_completed_setup or self.launched_minimized_by_startup:
            print("DEBUG: Showing main GUI.", file=sys.stdout, flush=True)
            self._show_main_gui()
        else:
            print("DEBUG: Showing first-time setup screen.", file=sys.stdout, flush=True)
            self._show_first_time_setup_screen()
        print("DEBUG: Exiting _check_and_show_initial_screen.", file=sys.stdout, flush=True)

    def _show_first_time_setup_screen(self):
        self.main_frame.pack_forget()
        self.setup_frame_step2.pack_forget()
        self.faq_frame.place_forget() # Use place_forget for animated frame
        self.setup_frame.pack(pady=0, padx=0, fill="both", expand=True)
        self.previous_frame = self.setup_frame

    def _show_setup_step2_screen(self):
        self.setup_frame.pack_forget()
        self.main_frame.pack_forget()
        self.faq_frame.place_forget() # Use place_forget for animated frame
        self.setup_frame_step2.pack(pady=0, padx=0, fill="both", expand=True)
        self.previous_frame = self.setup_frame_step2

    def _show_main_gui(self):
        self.setup_frame.pack_forget()
        self.setup_frame_step2.pack_forget()
        self.faq_frame.place_forget() # Use place_forget for animated frame
        self.main_frame.pack(pady=0, padx=0, fill="both", expand=True)
        self.previous_frame = self.main_frame

    # --- NEW: Method to toggle the FAQ screen with animation ---
    def toggle_faq_screen(self):
        """Toggles the visibility of the FAQ screen with a slide animation."""
        if self.is_animating:
            return  # Prevent clicks during animation

        self.is_animating = True
        if not self.faq_visible:
            # --- SHOW FAQ ---
            # Determine which frame is currently visible to set as previous_frame
            if self.main_frame.winfo_ismapped():
                self.previous_frame = self.main_frame
            elif self.setup_frame.winfo_ismapped():
                self.previous_frame = self.setup_frame
            elif self.setup_frame_step2.winfo_ismapped():
                self.previous_frame = self.setup_frame_step2
            else:
                self.previous_frame = self.main_frame # Fallback

            # Hide the current frame before starting animation
            if self.previous_frame:
                self.previous_frame.pack_forget()

            self.faq_visible = True
            self._animate_faq_in()
        else:
            # --- HIDE FAQ ---
            self.faq_visible = False
            self._animate_faq_out()

    # --- NEW: Animation for sliding the FAQ screen in ---
    def _animate_faq_in(self, step=0):
        """Animates the FAQ frame sliding in from the top."""
        total_steps = 60  # Increased for smoother animation
        animation_delay = 5  # Decreased for more frames

        if step == 0:
            # Initial placement above the window, ready to slide in
            self.faq_frame.place(relx=0, rely=-1, relwidth=1, relheight=1)

        if step <= total_steps:
            # Use a more pronounced ease-out function for a smoother stop
            ratio = step / total_steps
            eased_ratio = 1 - (1 - ratio) ** 5  # Quintic ease-out
            new_y = -1 + eased_ratio

            self.faq_frame.place_configure(rely=new_y)
            self.master.after(animation_delay, self._animate_faq_in, step + 1)
        else:
            # Animation finished, lock in final position
            self.faq_frame.place_configure(rely=0)
            self.is_animating = False

    # --- NEW: Animation for sliding the FAQ screen out ---
    def _animate_faq_out(self, step=0):
        """Animates the FAQ frame sliding out to the top."""
        total_steps = 60  # Increased for smoother animation
        animation_delay = 5  # Decreased for more frames

        if step <= total_steps:
            # Use a more pronounced ease-out function for a smoother start
            ratio = step / total_steps
            eased_ratio = 1 - (1 - ratio) ** 5  # Quintic ease-out
            new_y = 0 - eased_ratio

            self.faq_frame.place_configure(rely=new_y)
            self.master.after(animation_delay, self._animate_faq_out, step + 1)
        else:
            # Animation finished, hide the frame and show the previous one
            self.faq_frame.place_forget()
            if self.previous_frame:
                self.previous_frame.pack(pady=0, padx=0, fill="both", expand=True)
            else:
                # Fallback to main GUI if something goes wrong
                self._show_main_gui()
            self.is_animating = False

    def reinstall_certificate(self):
        """
        Allows the user to re-run the first-time setup by deleting the setup completion flag
        and resetting associated settings for a clean re-installation.
        """
        print("DEBUG: User initiated certificate re-installation.", file=sys.stdout, flush=True)
        confirmed = messagebox.askyesno(
            "Re-run Setup?",
            "This will take you back to the first-time setup screen to reinstall the certificate.\n\n"
            "This will also turn OFF 'auto-attach' and 'run on startup' settings.\n\n"
            "Do you want to continue?",
            icon='warning'
        )

        if not confirmed:
            print("DEBUG: User cancelled certificate re-installation.", file=sys.stdout, flush=True)
            return

        # Disable the button to prevent double-clicks
        self.reinstall_menu_button.configure(state=tk.DISABLED, text="Resetting...")

        def reset_flow():
            # Step 1: Stop the proxy if it's running
            if self.is_proxy_running:
                print("DEBUG: Proxy is running, stopping it before resetting setup.", file=sys.stdout, flush=True)
                self._stop_proxy_operations(is_app_closing=False)
                # Now we need to wait for it to actually stop.
                self.master.after(100, wait_for_proxy_to_stop)
            else:
                # If proxy is not running, just proceed to the next step
                perform_full_reset()

        def wait_for_proxy_to_stop():
            if not self.is_proxy_running:
                # Proxy has stopped, now we can proceed
                perform_full_reset()
            else:
                # Still waiting...
                self.master.after(100, wait_for_proxy_to_stop)

        def perform_full_reset():
            print("DEBUG: Performing full reset for re-installation.", file=sys.stdout, flush=True)

            # --- Resetting settings for a clean reinstall ---
            print("DEBUG: Resetting auto-scan and startup task settings.", file=sys.stdout, flush=True)

            # 1. Functionally disable auto-scan.
            # This sets the variable, stops the scanning thread, and saves the config via the toggle function.
            self.auto_scan_var.set(False)
            self.on_auto_scan_toggle()
            print("DEBUG: Auto-scan functionally disabled.", file=sys.stdout, flush=True)

            # 2. Remove the task from Windows Task Scheduler if it exists
            if _remove_task_from_scheduler_sync():
                print("DEBUG: Successfully removed task from scheduler (if it existed).", file=sys.stdout, flush=True)
            else:
                print("WARNING: Failed to remove task from scheduler. May require admin rights.", file=sys.stderr, flush=True)

            # 3. Remove the flag file
            try:
                if os.path.exists(FIRST_RUN_FLAG_FILE):
                    os.remove(FIRST_RUN_FLAG_FILE)
                    print(f"DEBUG: Removed first run flag file: {FIRST_RUN_FLAG_FILE}", file=sys.stdout, flush=True)
            except OSError as e:
                print(f"ERROR: Failed to remove first run flag file: {e}", file=sys.stderr, flush=True)
                messagebox.showerror("Error", f"Could not remove the setup flag file.\nError: {e}")
                # Re-enable the button on failure
                self.reinstall_menu_button.configure(state=tk.NORMAL, text="Re-run Setup")
                return

            # 4. Transition to the setup screen
            self._show_first_time_setup_screen()

            # 5. Sync the startup button UI to reflect the change
            self.sync_startup_ui()

            # 6. Re-enable the button for future use (though it won't be visible on the setup screen)
            self.reinstall_menu_button.configure(state=tk.NORMAL, text="Re-run Setup")

        # Start the process
        self.master.after(50, reset_flow)

    def _start_setup_process(self):
        print("DEBUG: Beginning setup process: Starting proxy.", file=sys.stdout, flush=True)
        self.begin_setup_button.configure(state=tk.DISABLED, text="STARTING...")
        self._start_proxy_operations(is_setup_process=True)

        def wait_for_proxy_and_open_browser():
            if mitmproxy_fully_running_event.is_set():
                self._show_setup_step2_screen()
                self.begin_setup_button.configure(state=tk.NORMAL, text="BEGIN SETUP")
                try:
                    webbrowser.open_new_tab("http://mitm.it")
                except Exception as e:
                    messagebox.showerror("Browser Error", f"Could not open browser to http://mitm.it. Please open it manually.\nError: {e}")
                self.setup_status_label.configure(text="Status: Proxy active. Install cert from your browser.", text_color=self.COLORS["primary_green"])
            elif self.is_proxy_running:
                self.master.after(500, wait_for_proxy_and_open_browser)
            else:
                self.setup_status_label.configure(text="Status: Failed to start proxy.", text_color=self.COLORS["stop_red"])
                self.begin_setup_button.configure(state=tk.NORMAL, text="BEGIN SETUP")
                messagebox.showerror("Setup Error", "Failed to start proxy. Please ensure no other proxy is running and try again.")
                self._show_first_time_setup_screen()
        self.master.after(100, wait_for_proxy_and_open_browser)

    def _finish_setup_process(self):
        self.finish_setup_button.configure(state=tk.DISABLED, text="FINISHING...")
        self.setup_status_label.configure(text="Status: Shutting down proxy...", text_color=self.COLORS["text_secondary"])
        self._stop_proxy_operations(is_app_closing=False)

        def wait_for_proxy_shutdown_and_transition():
            if not self.is_proxy_running:
                try:
                    # Use the updated FIRST_RUN_FLAG_FILE path
                    with open(FIRST_RUN_FLAG_FILE, 'w') as f: f.write("setup_complete")
                except Exception as e:
                    messagebox.showwarning("File Write Error", "Could not save completion status. You might see this setup screen again.")
                self._show_main_gui()
            else:
                self.master.after(100, wait_for_proxy_shutdown_and_transition)
        self.master.after(100, wait_for_proxy_shutdown_and_transition)

    def toggle_proxy_clicked(self):
        if self.auto_scan_var.get():
            messagebox.showinfo("Auto-Management Active", "Please uncheck 'Auto-manage' to manually control the proxy.")
            return
        if self.is_proxy_running: self._stop_proxy_operations(is_app_closing=False)
        else: self._start_proxy_operations()

    def _start_proxy_operations(self, is_setup_process=False):
        # --- NEW CERTIFICATE CHECK ---
        if not is_setup_process and not is_mitmproxy_cert_installed():
            msg = ("Mitmproxy certificate not found or not installed correctly in the Trusted Root store.\n\n"
                   "Please run the first-time setup again from the main window to install it. If you have already done this, try restarting the application.")
            self.master.after(0, lambda: messagebox.showerror("Certificate Missing", msg))
            if hasattr(self, 'status_label') and self.main_frame.winfo_ismapped():
                self.status_label.configure(text="Proxy: Cert Missing", text_color=self.COLORS["stop_red"])
                self.proxy_status_indicator.configure(text_color=self.COLORS["stop_red"])
                # Re-enable controls so the user is not stuck
                self.toggle_button.configure(state=tk.NORMAL)
                self.auto_scan_checkbox.configure(state=tk.NORMAL)
            self.is_proxy_running = False
            if not self.is_app_closing: self.shutdown_complete_event.set()
            return
        # --- END OF NEW CHECK ---

        global mitmproxy_thread_ref
        if self.is_proxy_running: return
        if is_port_in_use(MITMPROXY_LISTEN_HOST, MITMPROXY_LISTEN_PORT):
            msg = f"Port {MITMPROXY_LISTEN_PORT} is already in use. Cannot start proxy."
            self.master.after(0, lambda msg=msg: messagebox.showerror("Port in Use", msg))
            if hasattr(self, 'status_label') and self.main_frame.winfo_ismapped():
                self.status_label.configure(text="Proxy: Port in use", text_color=self.COLORS["stop_red"])
                self.proxy_status_indicator.configure(text_color=self.COLORS["stop_red"])
                self.toggle_button.configure(state=tk.NORMAL)
                self.auto_scan_checkbox.configure(state=tk.NORMAL)
            self.shutdown_complete_event.set()
            return

        new_name = self.new_name_var.get() or DEFAULT_CONFIG["last_spoof_name"]
        if hasattr(self, 'status_label') and self.main_frame.winfo_ismapped():
            self.status_label.configure(text="Proxy: Starting...", text_color=self.COLORS["text_secondary"])
            self.proxy_status_indicator.configure(text_color=self.COLORS["text_secondary"])
            self.toggle_button.configure(state=tk.DISABLED)
            self.auto_scan_checkbox.configure(state=tk.DISABLED)
        if not self.is_app_closing: self.shutdown_complete_event.clear()
        if not set_system_proxy(MITMPROXY_LISTEN_HOST, MITMPROXY_LISTEN_PORT, self.master):
            if hasattr(self, 'status_label') and self.main_frame.winfo_ismapped():
                self.status_label.configure(text="Proxy: Failed", text_color=self.COLORS["stop_red"])
                self.proxy_status_indicator.configure(text_color=self.COLORS["stop_red"])
                self.toggle_button.configure(state=tk.NORMAL)
                self.auto_scan_checkbox.configure(state=tk.NORMAL)
            self.is_proxy_running = False
            if not self.is_app_closing: self.shutdown_complete_event.set()
            return

        mitmproxy_fully_running_event.clear()
        mitmproxy_thread_ref = Thread(target=run_mitmproxy_thread_target, args=(new_name, self.master), daemon=True)
        mitmproxy_thread_ref.start()
        self.master.after(100, self.wait_for_proxy_startup)
        self.is_proxy_running = True

    def _stop_proxy_operations(self, is_app_closing=False):
        if not self.is_proxy_running:
            if is_app_closing: self.shutdown_complete_event.set()
            self.master.after(0, self.update_status_after_stop)
            return
        if hasattr(self, 'status_label') and self.main_frame.winfo_ismapped():
            self.status_label.configure(text="Proxy: Stopping...", text_color=self.COLORS["text_secondary"])
            self.proxy_status_indicator.configure(text_color=self.COLORS["text_secondary"])
            self.toggle_button.configure(state=tk.DISABLED)
        if is_app_closing: self.shutdown_complete_event.clear()

        def perform_stop_operations_thread():
            global mitmproxy_thread_ref
            try:
                stop_mitmproxy_gracefully()
                if mitmproxy_thread_ref and mitmproxy_thread_ref.is_alive():
                    mitmproxy_thread_ref.join(timeout=10)
                    if mitmproxy_thread_ref.is_alive(): print("WARNING: Mitmproxy thread did not terminate gracefully.", file=sys.stderr, flush=True)
                    mitmproxy_thread_ref = None
                if not disable_system_proxy(): print("WARNING: Failed to disable system proxy.", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"ERROR: Exception during background stop operations: {e}", file=sys.stderr, flush=True)
            finally:
                self.is_proxy_running = False
                if is_app_closing: self.shutdown_complete_event.set()
                self.master.after(100, self.update_status_after_stop)
        Thread(target=perform_stop_operations_thread, daemon=True).start()

    def wait_for_proxy_startup(self):
        global mitmproxy_thread_ref
        if mitmproxy_fully_running_event.is_set() and mitmproxy_thread_ref and mitmproxy_thread_ref.is_alive():
            if hasattr(self, 'status_label') and self.main_frame.winfo_ismapped():
                self.status_label.configure(text="Proxy: Active", text_color=self.COLORS["primary_green"])
                self.proxy_status_indicator.configure(text_color=self.COLORS["primary_green"])
                self.toggle_button.configure(text="DEACTIVATE", fg_color="transparent", border_color=self.COLORS["stop_red"], text_color=self.COLORS["stop_red"], state=tk.DISABLED if self.auto_scan_var.get() else tk.NORMAL)
                self.auto_scan_checkbox.configure(state=tk.NORMAL)

            # --- NEW: Store the current name when the proxy becomes active ---
            self.name_at_proxy_start = self.new_name_var.get()

            self.on_new_name_change() # Call to ensure addon name is synced
            if not self.is_app_closing: self.shutdown_complete_event.set()
        elif mitmproxy_thread_ref and mitmproxy_thread_ref.is_alive():
            self.master.after(500, self.wait_for_proxy_startup)
        else:
            if hasattr(self, 'status_label') and self.main_frame.winfo_ismapped():
                self.status_label.configure(text="Proxy: Failed", text_color=self.COLORS["stop_red"])
                self.proxy_status_indicator.configure(text_color=self.COLORS["stop_red"])
                self.toggle_button.configure(text="ACTIVATE", fg_color="transparent", border_color=self.COLORS["primary_green"], text_color=self.COLORS["primary_green"], state=tk.NORMAL if not self.auto_scan_var.get() else tk.DISABLED)
                self.auto_scan_checkbox.configure(state=tk.NORMAL)
            if self.master.winfo_exists(): messagebox.showerror("Mitmproxy Startup Error", "Mitmproxy failed to start. See log for details.")
            disable_system_proxy()
            self.is_proxy_running = False
            if not self.is_app_closing: self.shutdown_complete_event.set()

    def on_new_name_change(self, *args):
        if self._after_id: self.master.after_cancel(self._after_id)
        self._update_char_counter()

        # --- REFINED LOGIC: Only show warning if name changes AFTER proxy is active ---
        if self.is_proxy_running:
            if self.new_name_var.get() != self.name_at_proxy_start:
                self.restart_warning_label.grid()
            else:
                self.restart_warning_label.grid_remove()

        self._after_id = self.master.after(300, self._perform_name_update_and_save)

    def _perform_name_update_and_save(self):
        new_name = self.new_name_var.get()
        if mitmproxy_addon_instance and new_name:
            mitmproxy_addon_instance.update_name(new_name)
        self.app_config["last_spoof_name"] = new_name
        save_config(self.app_config)

    def _update_char_counter(self, *args):
        if hasattr(self, 'char_counter_label') and self.char_counter_label.winfo_exists():
            char_count = len(self.new_name_var.get())
            self.char_counter_label.configure(text=f"{char_count}/{MAX_NAME_LENGTH}")
            if char_count > MAX_NAME_LENGTH: self.char_counter_label.configure(text_color=self.COLORS["stop_red"])
            else: self.char_counter_label.configure(text_color=self.COLORS["text_secondary"])

    def _show_tooltip(self, event):
        if self.tooltip_window or not self.char_counter_label.winfo_exists(): return
        x = self.char_counter_label.winfo_rootx() + self.char_counter_label.winfo_width()
        y = self.char_counter_label.winfo_rooty() + self.char_counter_label.winfo_height()
        self.tooltip_window = tk.Toplevel(self.master)
        self.tooltip_window.wm_overrideredirect(True)
        self.tooltip_window.wm_geometry(f"+{x}+{y}")
        self.tooltip_window.attributes("-topmost", True)
        label = ctk.CTkLabel(self.tooltip_window, text=f"Names are truncated past {MAX_NAME_LENGTH} characters.", font=self.font_char_counter, text_color=self.COLORS["text_primary"], fg_color=self.COLORS["frame_bg"], corner_radius=5, padx=8, pady=5)
        label.pack()

    def _hide_tooltip(self, event):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None

    def update_status_after_stop(self):
        if self.ui_initialized and hasattr(self, 'status_label') and self.main_frame.winfo_ismapped():
            # --- NEW: Hide the warning label and reset the stored name ---
            self.restart_warning_label.grid_remove()
            self.name_at_proxy_start = None

            self.status_label.configure(text="Proxy: Inactive", text_color=self.COLORS["text_secondary"])
            self.proxy_status_indicator.configure(text_color=self.COLORS["status_idle"])
            self.toggle_button.configure(text="ACTIVATE", fg_color="transparent", border_color=self.COLORS["primary_green"], text_color=self.COLORS["primary_green"], state=tk.NORMAL if not self.auto_scan_var.get() else tk.DISABLED)
            self.auto_scan_checkbox.configure(state=tk.NORMAL)

    # --- MODIFICATION: This function now ONLY updates the button's appearance. ---
    def sync_startup_ui(self):
        """
        Checks the actual Task Scheduler state and updates the UI button to match
        reality. This is the single source of truth for the button's state.
        It does NOT affect the auto-scan checkbox or config settings.
        """
        if not self.ui_initialized or self.is_syncing_ui:
            return
        self.is_syncing_ui = True
        print("DEBUG: Syncing startup UI state with Task Scheduler.", file=sys.stdout, flush=True)

        self.task_scheduler_button.configure(state=tk.DISABLED, text="...")
        self.auto_scan_checkbox.configure(state=tk.DISABLED)

        def _task():
            try:
                is_enabled = is_task_scheduled()
                print(f"DEBUG: Task is_scheduled result: {is_enabled}", file=sys.stdout, flush=True)

                def _update_ui():
                    # Update button's appearance based on scheduler state
                    if is_enabled:
                        self.task_scheduler_button.configure(text="Remove from Startup", border_color=self.COLORS["stop_red"], text_color=self.COLORS["stop_red"])
                    else:
                        self.task_scheduler_button.configure(text="Run automatically on Startup", border_color=self.COLORS["border"], text_color=self.COLORS["text_secondary"])

                    # Re-enable controls
                    self.task_scheduler_button.configure(state=tk.NORMAL)
                    self.auto_scan_checkbox.configure(state=tk.NORMAL)
                    self.is_syncing_ui = False

                self.master.after(0, _update_ui)
            except Exception as e:
                print(f"ERROR: Failed during sync_startup_ui thread: {e}", file=sys.stderr, flush=True)
                if self.ui_initialized:
                    self.master.after(0, lambda: self.task_scheduler_button.configure(state=tk.NORMAL))
                    self.master.after(0, lambda: self.auto_scan_checkbox.configure(state=tk.NORMAL))
                self.is_syncing_ui = False

        Thread(target=_task, daemon=True).start()

    # --- MODIFICATION: This function no longer changes any config files. ---
    def toggle_task_scheduler_clicked(self):
        """
        Adds or removes the task from Task Scheduler without affecting any
        other application settings.
        """
        if self.is_syncing_ui:
            return

        self.task_scheduler_button.configure(state=tk.DISABLED)
        self.auto_scan_checkbox.configure(state=tk.DISABLED)

        def perform_task_scheduler_action():
            is_currently_enabled = is_task_scheduled()

            if is_currently_enabled:
                if _remove_task_from_scheduler_sync():
                    self.master.after(0, lambda: messagebox.showinfo("Task Scheduler", "Removed from Windows startup."))
                else:
                    self.master.after(0, lambda: messagebox.showerror("Task Scheduler Error", "Failed to remove from startup."))
            else:
                if _add_task_to_scheduler_sync():
                    self.master.after(0, lambda: messagebox.showinfo("Task Scheduler", "Added to Windows startup."))
                else:
                    self.master.after(0, lambda: messagebox.showerror("Task Scheduler Error", "Failed to add to startup. Please run as administrator."))

            # Always re-sync the UI from the definitive source (Task Scheduler)
            # to ensure the button is correct, regardless of action success.
            self.master.after(0, self.sync_startup_ui)

        Thread(target=perform_task_scheduler_action, daemon=True).start()

    def on_auto_scan_toggle(self, *args):
        """
        This function now ONLY starts or stops the background scanning thread
        for the CURRENT SESSION and saves the checkbox state for future manual launches.
        It does not affect the startup task.
        """
        if self.is_syncing_ui:
            # Prevent this from running while the UI is syncing to avoid race conditions
            return

        # Check for certificate before allowing auto-scan to be enabled
        if self.auto_scan_var.get() and not is_mitmproxy_cert_installed():
            messagebox.showerror("Certificate Missing",
                                 "The mitmproxy certificate is not installed. "
                                 "Auto-attaching requires the certificate to be installed first.\n\n"
                                 "Please run the first-time setup.")
            self.auto_scan_var.set(False)  # Force the checkbox off
            return

        # Save the checkbox state to the config for persistence across manual sessions
        self.app_config["auto_scan_on_startup"] = self.auto_scan_var.get()
        save_config(self.app_config)

        if self.auto_scan_var.get():
            if self.ui_initialized and hasattr(self, 'toggle_button'):
                self.toggle_button.configure(state=tk.DISABLED)
                self.rl_status_label.configure(text="Game: Scanning...", text_color=self.COLORS["text_secondary"])
                self.rl_status_indicator.configure(text_color=self.COLORS["text_secondary"])

            self.auto_scan_stop_event.clear()
            if not self.auto_scan_thread or not self.auto_scan_thread.is_alive():
                self.auto_scan_thread = Thread(target=self._auto_scan_rocket_league, daemon=True)
                self.auto_scan_thread.start()
        else:
            self.auto_scan_stop_event.set()
            if self.is_proxy_running: self._stop_proxy_operations(is_app_closing=False)
            else: self.update_status_after_stop()
            if self.ui_initialized and hasattr(self, 'rl_status_label'):
                self.rl_status_label.configure(text="Game: Auto-scan off", text_color=self.COLORS["text_secondary"])
                self.rl_status_indicator.configure(text_color=self.COLORS["status_idle"])
                self.toggle_button.configure(state=tk.NORMAL)

    def _auto_scan_rocket_league(self):
        time.sleep(SCAN_INTERVAL_SECONDS * 2)
        while not self.auto_scan_stop_event.is_set():
            current_rl_status = is_process_running(ROCKET_LEAGUE_PROCESS_NAME)
            if self.ui_initialized and hasattr(self, 'rl_status_label'):
                if current_rl_status != self.rl_process_active:
                    self.rl_process_active = current_rl_status
                    if current_rl_status:
                        self.master.after(0, lambda: self.rl_status_label.configure(text="Game: Running", text_color=self.COLORS["primary_green"]))
                        self.master.after(0, lambda: self.rl_status_indicator.configure(text_color=self.COLORS["primary_green"]))
                    else:
                        self.master.after(0, lambda: self.rl_status_label.configure(text="Game: Not Running", text_color=self.COLORS["text_secondary"]))
                        self.master.after(0, lambda: self.rl_status_indicator.configure(text_color=self.COLORS["status_idle"]))

            if current_rl_status and not self.is_proxy_running: self.master.after(0, self._start_proxy_operations, False)
            elif not current_rl_status and self.is_proxy_running: self.master.after(0, self._stop_proxy_operations, False)
            time.sleep(SCAN_INTERVAL_SECONDS)

    def create_tray_icon(self):
        if not PYSTRAY_AVAILABLE: return
        width, height = 64, 64
        try:
            image_color = tuple(int(self.COLORS["primary_green"].lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
            image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
            from PIL import ImageDraw
            draw = ImageDraw.Draw(image)
            draw.ellipse((4, 4, width-4, height-4), fill=image_color)
        except Exception as e:
            print(f"ERROR: Failed to create tray icon image: {e}", file=sys.stderr, flush=True)
            return
        menu = (pystray.MenuItem('Show', self.show_window, default=True), pystray.MenuItem('Quit', self.quit_application))
        self.tray_icon = pystray.Icon("name_spoofer", image, "Rocket League Name Spoofer", menu)
        self.tray_thread = Thread(target=self.tray_icon.run, daemon=True)
        self.tray_thread.start()

    def show_window(self, icon=None, item=None):
        self.master.after(0, self._actual_show_window)

    def _actual_show_window(self):
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
            self.tray_thread = None

        # Build the UI if this is the first time the window is being shown
        if not self.ui_initialized:
            self.master.after(0, self.build_full_ui)
            # After building the UI, we must also run the logic that decides
            # whether to show the setup screen or the main screen.
            self.master.after(100, self._check_and_show_initial_screen)

        # Make the window visible with a more robust sequence
        self.master.after(200, self._force_show)

    def _force_show(self):
        """A more robust sequence to force the window to appear correctly."""
        print("DEBUG: Force showing window.", file=sys.stdout, flush=True)
        self.master.deiconify()
        self.master.lift()
        self.master.state('normal')
        self.master.attributes("-alpha", 1.0)

        # --- LOCK SIZE AND RE-CENTER ---
        # Explicitly set the size to 480x600 and re-center on restore
        window_width, window_height = 480, 600
        self.master.update_idletasks()
        screen_width = self.master.winfo_screenwidth()
        screen_height = self.master.winfo_screenheight()
        x = (screen_width // 2) - (window_width // 2)
        y = (screen_height // 2) - (window_height // 2)
        self.master.geometry(f'{window_width}x{window_height}+{x}+{y}')

        self.master.attributes('-topmost', 1)
        self.master.focus_force()
        self.master.after(100, lambda: self.master.attributes('-topmost', 0))

    def hide_window(self):
        if PYSTRAY_AVAILABLE:
            self.master.withdraw()
            self.create_tray_icon()
        else:
            self.master.iconify()

    def quit_application(self, icon=None, item=None):
        if self.tray_icon: self.tray_icon.stop()
        self.master.after(0, self.on_closing)

    def on_minimize(self, event):
        if self.master.state() == 'iconic':
            self.hide_window()

    def on_closing(self):
        self.is_app_closing = True
        if self.auto_scan_thread and self.auto_scan_thread.is_alive():
            self.auto_scan_stop_event.set()
            self.auto_scan_thread.join(timeout=SCAN_INTERVAL_SECONDS + 2)
            if self.auto_scan_thread.is_alive(): print("WARNING: Auto-scan thread did not terminate gracefully.", file=sys.stderr, flush=True)
            self.auto_scan_thread = None
        self._stop_proxy_operations(is_app_closing=True)
        def wait_for_shutdown_and_destroy():
            if self.shutdown_complete_event.is_set():
                self.clean_up_logging()
                self.master.destroy()
            else:
                self.master.after(100, wait_for_shutdown_and_destroy)
        self.master.after(100, wait_for_shutdown_and_destroy)

    def clean_up_logging(self):
        global sys, original_stdout, original_stderr, log_stdout_file, log_stderr_file
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        if log_stdout_file and not log_stdout_file.closed:
            log_stdout_file.close()
            log_stdout_file = None
        log_stderr_file = None

if __name__ == "__main__":
    print("DEBUG: Application starting from __main__ block...", file=sys.stdout, flush=True)
    tk.Tk.report_callback_exception = tk_exception_handler
    if sys.platform == "win32":
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin(): # Corrected function name
                messagebox.showwarning("Administrator Privileges Required", "Please run as administrator to manage system proxy settings.")
                # Ensure logs are closed and streams reset before exiting
                if log_stdout_file and not log_stdout_file.closed: log_stdout_file.close()
                sys.stdout = original_stdout
                sys.stderr = original_stderr
                sys.exit(1)
        except (ImportError, Exception) as e:
            print(f"ERROR: Could not check for admin privileges: {e}", file=sys.stderr, flush=True)
    root = ctk.CTk()
    app = SpooferGUI(root)
    root.mainloop()
    print("DEBUG: Application main loop exited.", file=original_stdout,  flush=True)
