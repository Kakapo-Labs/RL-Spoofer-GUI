# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from PyInstaller.utils.hooks import collect_data_files

# --- Configuration ---
# The directory where your main.py, splash.mov, and config.json are located.
# os.getcwd() assumes you run pyinstaller from this directory.
spec_dir = os.getcwd()
app_name = 'Rocket League Name Spoofer'

# --- Collect Data Files ---
# This list includes your video, config file, icon, and automatically collects
# all necessary assets (like themes and fonts) from the customtkinter library.
datas = [
    ('splash.mov', '.'),
    ('config.json', '.'),
    ('icon.ico', '.'),
    ('discord.png', '.') # <-- ADD THIS LINE
]
datas += collect_data_files('customtkinter')


# --- Hidden Imports ---
# A comprehensive list of modules that PyInstaller might not find automatically.
# This helps prevent "ModuleNotFound" errors in the final executable.
hiddenimports = [
    # Core & Standard Libs
    'asyncio',
    'json',
    'threading',
    'webbrowser',
    'winreg',
    'platform',
    'socket',
    're',
    'subprocess',
    'time',
    'ctypes',

    # GUI Libraries & Dependencies
    'tkinter',
    'customtkinter',
    'pystray',
    'PIL',
    'PIL.Image',
    'PIL.ImageTk',
    'PIL.ImageDraw',  # Explicitly include for creating the tray icon
    'cv2',           # For OpenCV video playback

    # Mitmproxy and its dependencies
    'mitmproxy.tools.dump',
    'mitmproxy.tools.web',
    'mitmproxy.addons',
    'cryptography',
    'brotli',
    'zstandard',
    'public_suffix_list',
    'pkg_resources.py2_warn',  # Common hidden import needed by some libraries
]


a = Analysis(
    ['main.py'],
    pathex=[spec_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    [],  # a.zipfiles
    a.datas,
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Set to False for a GUI application without a command window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True, # Request admin privileges for managing proxy settings
    icon='icon.ico' # <-- ADDED: Embeds the icon into the .exe file
)
