"""
BubbleGum Launcher — Thin exe shell.
Handles activation + auto-update. Downloads latest bubblegum_app.py + bubblegum.html
from GitHub, exec()'s the logic, starts the server.
"""

import hashlib
import hmac
import json
import os
import ssl
import subprocess
import sys
import time
import uuid
import urllib.request
import webbrowser
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────

PORT = 8000
APP_DIR = Path(os.environ.get("APPDATA", "")) / "BubbleGum"
LICENSE_FILE = APP_DIR / "license.dat"
GITHUB_REPO = "sandanglokta1987-dev/bubblegum"
GITHUB_BRANCH = "rebuild"
UPDATE_FILES = ["bubblegum_app.py", "bubblegum.html"]

_S = [0x42,0x75,0x62,0x62,0x6C,0x65,0x47,0x75,0x6D,0x2D,
      0x53,0x33,0x63,0x72,0x33,0x74,0x2D,0x4B,0x33,0x79,
      0x2D,0x58,0x39,0x71,0x37,0x5A,0x6D,0x50,0x32,0x76,
      0x4C,0x38]
SECRET = bytes(_S).decode()


# ── Machine Fingerprint + Activation ─────────────────────────────────────────

def _wmic(cls, prop):
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
    mac = format(uuid.getnode(), "012X")
    cpu = _wmic("cpu", "ProcessorId")
    disk = _wmic("diskdrive", "SerialNumber")
    raw = f"{mac}|{cpu}|{disk}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16].upper()
    return f"{digest[:4]}-{digest[4:8]}-{digest[8:12]}-{digest[12:16]}"


def compute_key(machine_id):
    h = hmac.new(SECRET.encode(), machine_id.encode(), hashlib.sha256).hexdigest()[:20].upper()
    return f"{h[:5]}-{h[5:10]}-{h[10:15]}-{h[15:20]}"


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
    return stored == compute_key(get_machine_id())


def show_activation_dialog():
    import tkinter as tk
    from tkinter import messagebox

    machine_id = get_machine_id()
    result = {"activated": False}

    root = tk.Tk()
    root.title("BubbleGum — Activation")
    root.geometry("480x320")
    root.resizable(False, False)
    root.configure(bg="#1a1a2e")

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

    tk.Label(root, text="Activation Key:", font=("Consolas", 10),
             fg="#aaa", bg="#1a1a2e").pack(pady=(20, 5))
    key_entry = tk.Entry(root, font=("Consolas", 12), width=28,
                         fg="#fff", bg="#2a2a3e", relief="flat",
                         insertbackground="#fff", justify="center")
    key_entry.pack()
    key_entry.focus_set()

    def activate():
        entered = key_entry.get().strip().upper()
        if entered == compute_key(machine_id):
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
    root.bind("<Return>", lambda e: activate())
    root.protocol("WM_DELETE_WINDOW", lambda: (root.destroy(),))
    root.mainloop()
    return result["activated"]


# ── Auto-Updater ─────────────────────────────────────────────────────────────

def _update_files():
    """Download latest bubblegum_app.py + bubblegum.html from GitHub."""
    try:
        sha_file = APP_DIR / ".update_sha"

        # Always check: if cached HTML is old V3, wipe everything and force re-download
        cached_html = APP_DIR / "bubblegum.html"
        if cached_html.exists():
            try:
                snippet = cached_html.read_text(encoding='utf-8', errors='replace')[:3000]
                if any(marker in snippet for marker in ['Firecrawl', 'Scanner', 'V3', 'Spammer URL', 'firecrawl']):
                    for f in [cached_html, APP_DIR / "bubblegum_app.py",
                              sha_file, APP_DIR / ".html_sha", APP_DIR / ".version"]:
                        if f.exists():
                            f.unlink()
                    print("[updater] Old V3 cache detected and removed", flush=True)
            except Exception:
                pass

        local_sha = sha_file.read_text().strip() if sha_file.exists() else ""

        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'BubbleGum/2.0',
            'Accept': 'application/vnd.github.v3+json',
        })
        ctx = ssl.create_default_context()
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        remote_sha = json.loads(resp.read().decode())["sha"]

        if remote_sha == local_sha:
            print("[updater] Up to date.", flush=True)
            return

        print(f"[updater] Update available: {remote_sha[:8]}", flush=True)
        APP_DIR.mkdir(parents=True, exist_ok=True)

        for filename in UPDATE_FILES:
            raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{filename}"
            req = urllib.request.Request(raw_url, headers={'User-Agent': 'BubbleGum/2.0'})
            content = urllib.request.urlopen(req, timeout=30, context=ctx).read()
            (APP_DIR / filename).write_bytes(content)
            print(f"[updater] {filename}: {len(content)} bytes", flush=True)

        sha_file.write_text(remote_sha)
        print(f"[updater] Done: {remote_sha[:8]}", flush=True)

    except Exception as e:
        print(f"[updater] Failed (offline?): {e}", flush=True)


# ── Load + Run ───────────────────────────────────────────────────────────────

def _get_logic_path():
    """Return path to bubblegum_app.py — APPDATA (updated) or bundled (fallback)."""
    appdata = APP_DIR / "bubblegum_app.py"
    if appdata.exists():
        return appdata
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "bubblegum_app.py"
    return Path(os.path.dirname(os.path.abspath(__file__))) / "bubblegum_app.py"


def _show_splash_and_launch():
    """Show a branded splash window during startup, then open browser."""
    import tkinter as tk
    import threading

    W, H = 480, 260
    root = tk.Tk()
    root.title("BubbleGum")
    root.geometry(f"{W}x{H}")
    root.resizable(False, False)
    root.overrideredirect(True)  # no title bar
    root.attributes('-topmost', True)

    # Center on screen
    root.update_idletasks()
    x = (root.winfo_screenwidth() // 2) - (W // 2)
    y = (root.winfo_screenheight() // 2) - (H // 2)
    root.geometry(f"{W}x{H}+{x}+{y}")

    try:
        if getattr(sys, "frozen", False):
            icon_path = os.path.join(sys._MEIPASS, "bubblegum.ico")
        else:
            icon_path = os.path.join(os.path.dirname(__file__), "bubblegum.ico")
        if os.path.exists(icon_path):
            root.iconbitmap(icon_path)
    except Exception:
        pass

    # Dark gradient background using canvas
    canvas = tk.Canvas(root, width=W, height=H, highlightthickness=0, bd=0)
    canvas.pack(fill="both", expand=True)

    # Draw gradient background (dark purple to darker)
    for i in range(H):
        r = int(26 - (i / H) * 10)
        g = int(10 - (i / H) * 6)
        b = int(30 + (i / H) * 10)
        color = f'#{max(r,0):02x}{max(g,0):02x}{min(b,40):02x}'
        canvas.create_line(0, i, W, i, fill=color)

    # Subtle glow circle
    canvas.create_oval(W//2 - 120, 20, W//2 + 120, 160,
                       fill='', outline='', width=0)

    # Logo bubble
    canvas.create_oval(W//2 - 90, 30, W//2 - 50, 70,
                       fill='#ff69b4', outline='#ff8cc8', width=2)
    canvas.create_text(W//2 - 70, 50, text='\U0001F9CB', font=("Segoe UI", 16))

    # Title
    canvas.create_text(W//2 + 10, 50, text="BubbleGum",
                       font=("Segoe UI", 32, "bold"), fill="#ff69b4", anchor="w")

    # Tagline
    canvas.create_text(W//2, 95, text="Intelligent Form Automation",
                       font=("Segoe UI", 12), fill="#a070a0")

    # Version
    canvas.create_text(W - 20, H - 15, text="v4.0",
                       font=("Segoe UI", 9), fill="#4a2250", anchor="e")

    # Status text
    status_var = tk.StringVar(value="Starting...")
    status_label = tk.Label(root, textvariable=status_var,
                            font=("Segoe UI", 10), fg="#d4a0d0",
                            bg="#1a1a2e", bd=0)
    status_label.place(x=W//2, y=145, anchor="center")

    # Progress bar background
    bar_x, bar_y, bar_w, bar_h = 60, 175, W - 120, 5
    canvas.create_rectangle(bar_x, bar_y, bar_x + bar_w, bar_y + bar_h,
                            fill="#2a1230", outline="#3a1a40", width=1)
    # Progress bar fill (will be updated)
    bar_fill_id = canvas.create_rectangle(bar_x, bar_y, bar_x, bar_y + bar_h,
                                          fill="#ff69b4", outline="", width=0)

    # Powered by line
    canvas.create_text(W//2, H - 15, text="powered by Selenium + AI",
                       font=("Segoe UI", 8), fill="#3a1a40")

    def set_status(msg, pct=0):
        try:
            status_var.set(msg)
            fill_w = int((bar_w * pct) / 100)
            canvas.coords(bar_fill_id, bar_x, bar_y, bar_x + fill_w, bar_y + bar_h)
            root.update_idletasks()
        except Exception:
            pass

    def startup_thread():
        try:
            set_status("Checking for updates...", 10)
            _update_files()

            set_status("Loading...", 50)
            logic_path = _get_logic_path()
            code = logic_path.read_text(encoding='utf-8')
            exec(compile(code, str(logic_path), 'exec'), globals())

            set_status("Starting server...", 75)
            server = start_server()  # noqa: F821

            set_status("Opening browser...", 90)
            url = f"http://127.0.0.1:{PORT}/bubblegum.html"
            time.sleep(0.3)
            webbrowser.open(url)

            set_status("Ready!", 100)
            time.sleep(0.5)

            # Close splash
            try:
                root.destroy()
            except Exception:
                pass

            # Keep server alive
            server.serve_forever()

        except Exception as e:
            set_status(f"Error: {e}", 0)
            time.sleep(5)
            try:
                root.destroy()
            except Exception:
                pass
            os._exit(1)

    t = threading.Thread(target=startup_thread, daemon=True)
    t.start()
    root.mainloop()

    # If splash was closed manually (X button), keep running until server dies
    t.join()


def main():
    if not is_activated():
        if not show_activation_dialog():
            sys.exit(0)

    _show_splash_and_launch()


if __name__ == "__main__":
    main()
