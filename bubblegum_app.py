"""
BubbleGum — Form Extractor + Form Filler (Business Logic)
This file is auto-updated from GitHub. exec()'d by bubblegum_launcher.py.
"""

import base64
import csv
import hashlib
import hmac
import io
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
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

PORT = 8000
APP_DIR = Path(os.environ.get("APPDATA", "")) / "BubbleGum"


# ── WAF / JS-Form Detection ─────────────────────────────────────────────────

WAF_PATTERNS = [
    (r'\.well-known/sgcaptcha/', 'sgcaptcha'),
    (r'cf-browser-verification|__cf_chl_managed_tk|challenges\.cloudflare\.com', 'cloudflare'),
    (r'sucuri\.net|cloudproxy', 'sucuri'),
    (r'wordfence|wfvt_\d+', 'wordfence'),
    (r'imunify360|i360', 'imunify360'),
    (r'shield-security|icwp', 'shield'),
]

JS_FORM_SCRIPTS = [
    'ninja-forms', 'wpforms', 'gravityforms', 'formidable',
    'elementor', 'forminator', 'fluent-form', 'caldera',
    'happyforms', 'ws-form', 'everest-forms', 'fluentform',
]


def detect_waf(html_text):
    for pattern, name in WAF_PATTERNS:
        if re.search(pattern, html_text, re.I):
            return name
    return None


def detect_js_forms_no_html_forms(html_text):
    """Detect JS-rendered forms: form plugin scripts present but no real rendered inputs.
    Ninja Forms embeds <input> inside <script type="text/template"> blocks — these
    are templates, not real DOM elements. Strip all <script> content first."""
    lower = html_text.lower()
    has_js_form = any(sig in lower for sig in JS_FORM_SCRIPTS)
    if not has_js_form:
        return False

    # Strip <script> blocks (templates like Ninja Forms live inside these)
    stripped = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html_text, flags=re.I)

    # Count real form inputs in the actual DOM (not templates)
    input_count = len(re.findall(r'<input[\s>]', stripped, re.I))
    select_count = len(re.findall(r'<select[\s>]', stripped, re.I))
    textarea_count = len(re.findall(r'<textarea[\s>]', stripped, re.I))
    # Subtract search inputs (common false positives)
    search_count = len(re.findall(r'type=["\']search["\']', stripped, re.I))
    real_inputs = input_count + select_count + textarea_count - search_count

    # If JS form plugin is present but very few real DOM inputs, forms are JS-rendered
    if real_inputs <= 3:
        return True
    return False


# ── Headless Edge Browser (Selenium) ─────────────────────────────────────────

_edge_driver = None
_edge_lock = threading.Lock()


def _get_edge_driver():
    global _edge_driver
    if _edge_driver is not None:
        try:
            _edge_driver.title  # test if alive
            return _edge_driver
        except Exception:
            _edge_driver = None

    from selenium.webdriver import Edge, EdgeOptions

    opts = EdgeOptions()
    opts.add_argument('--headless=new')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    # Anti-detection stealth
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    opts.add_experimental_option('useAutomationExtension', False)
    opts.add_argument('--disable-infobars')
    opts.add_argument('--disable-extensions')
    opts.add_argument('--window-size=1920,1080')
    opts.add_argument('--lang=en-US')
    opts.add_argument(
        'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0'
    )

    _edge_driver = Edge(options=opts)
    _edge_driver.set_page_load_timeout(30)

    # CDP stealth: hide webdriver flag, fake plugins/languages
    try:
        _edge_driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': '''
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = {runtime: {}};
        '''})
    except Exception:
        pass

    return _edge_driver


def fetch_with_edge(url):
    with _edge_lock:
        driver = _get_edge_driver()
        driver.get(url)
        time.sleep(3)
        html = driver.page_source
        return html


# ── Form Filler (visible browser) ───────────────────────────────────────────

_filler_driver = None
_filler_lock = threading.Lock()
_filler_data = None       # list of dicts (rows from CSV)
_filler_index = 0
_filler_url = ""           # current URL being filled
_filler_url_col = None     # name of the URL column in CSV
_filler_results = []       # list of {"url", "row", "status", "filled", "errors", "timestamp"}
_filler_ai_key = ""
_filler_oai_key = ""
_filler_ai_model = ""
_filler_field_maps = {}    # {url: {csv_col: element_id}} — AI mapping cache
_captcha_api_key = "CAP-ED5F5BC298ACD06F27102102B73F5F4F4D5ED5187C2A57D42A43FA4100C85B0F"


def _get_filler_driver():
    global _filler_driver
    if _filler_driver is not None:
        try:
            _filler_driver.title
            return _filler_driver
        except Exception:
            _filler_driver = None

    from selenium.webdriver import Edge, EdgeOptions

    opts = EdgeOptions()
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    opts.add_experimental_option('useAutomationExtension', False)
    opts.add_argument('--disable-infobars')
    opts.add_argument('--lang=en-US')
    opts.add_argument(
        'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0'
    )

    _filler_driver = Edge(options=opts)
    _filler_driver.set_page_load_timeout(30)
    _filler_driver.maximize_window()

    # CDP stealth for visible browser
    try:
        _filler_driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': '''
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = {runtime: {}};
        '''})
    except Exception:
        pass

    return _filler_driver


def _close_filler_driver():
    global _filler_driver
    if _filler_driver is not None:
        try:
            _filler_driver.quit()
        except Exception:
            pass
        _filler_driver = None


def _call_ai_api(prompt, max_tokens=4096):
    """Call Anthropic or OpenAI API. Returns response text or ''."""
    try:
        is_openai = _filler_ai_model.startswith(('gpt-', 'o1', 'o3', 'o4'))
        if is_openai:
            if not _filler_oai_key:
                return ''
            url = 'https://api.openai.com/v1/chat/completions'
            payload = json.dumps({
                'model': _filler_ai_model,
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': max_tokens, 'temperature': 0
            }).encode()
            headers = {'Content-Type': 'application/json',
                       'Authorization': f'Bearer {_filler_oai_key}'}
        else:
            if not _filler_ai_key:
                return ''
            url = 'https://api.anthropic.com/v1/messages'
            payload = json.dumps({
                'model': _filler_ai_model or 'claude-sonnet-4-20250514',
                'max_tokens': max_tokens,
                'messages': [{'role': 'user', 'content': prompt}]
            }).encode()
            headers = {'Content-Type': 'application/json',
                       'x-api-key': _filler_ai_key,
                       'anthropic-version': '2023-06-01'}

        req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
        ctx = ssl.create_default_context()
        resp = urllib.request.urlopen(req, timeout=60, context=ctx)
        data = json.loads(resp.read().decode())

        if is_openai:
            return data.get('choices', [{}])[0].get('message', {}).get('content', '')
        else:
            blocks = data.get('content', [])
            return blocks[0].get('text', '') if blocks else ''
    except Exception as e:
        print(f"[filler-ai] API call failed: {e}", flush=True)
        return ''


def _build_field_map(driver, csv_columns, page_url):
    """Use AI to map CSV column names to form field IDs on the page.
    Returns {csv_col: element_id_or_selector} dict."""
    global _filler_field_maps

    # Check cache
    if page_url in _filler_field_maps:
        return _filler_field_maps[page_url]

    # Extract all visible form fields from the page via JS
    fields_json = driver.execute_script("""
        const fields = [];
        const els = document.querySelectorAll('input, select, textarea');
        for (const el of els) {
            const tag = el.tagName.toLowerCase();
            let type = tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : (el.type || 'text');
            if (['submit','button','image','reset'].includes(type)) continue;
            if (el.offsetParent === null && type !== 'hidden') continue;  // skip invisible

            const id = el.id || '';
            const name = el.name || '';

            // Find label
            let label = '';
            if (id) {
                const lbl = document.querySelector('label[for="' + CSS.escape(id) + '"]');
                if (lbl) label = lbl.textContent.trim();
            }
            if (!label) {
                const parent = el.closest('label');
                if (parent) label = parent.textContent.trim();
            }
            if (!label) label = el.getAttribute('aria-label') || '';
            if (!label) label = el.placeholder || '';
            if (!label) label = name;

            // Clean label
            label = label.replace(/\\s+/g, ' ').trim().substring(0, 150);

            fields.push({id, name, label, tag, type});
        }
        return JSON.stringify(fields);
    """)

    try:
        form_fields = json.loads(fields_json)
    except Exception:
        form_fields = []

    if not form_fields:
        print(f"[filler-ai] No form fields found on page", flush=True)
        _filler_field_maps[page_url] = {}
        return {}

    # Build the AI prompt
    fields_desc = json.dumps(form_fields, indent=2)
    cols_desc = json.dumps([c for c in csv_columns if c != _filler_url_col])

    prompt = f"""You are mapping CSV data columns to HTML form fields on a web page.

CSV COLUMNS (these are the data column headers):
{cols_desc}

FORM FIELDS on the page (id, name, label, type):
{fields_desc}

TASK: For each CSV column, find the matching form field. Return a JSON object mapping each CSV column to the form field's "id" (preferred) or "name" attribute.

RULES:
- Match by semantic meaning, not exact text. E.g. "high_school_location" maps to a field labeled "Location (City, State)" in the education section.
- "percent_borrowed" maps to a field labeled "% Borrowed"
- Skip columns that have no matching field (like page_url, honeypot, captcha, recaptcha, hidden tokens)
- If a column could match multiple fields (e.g. two "GPA" fields), use context from the column name to pick the right one (e.g. "high_school_gpa" → the GPA under High School, "college_gpa" → the GPA under College)
- Use the field's "id" if available, otherwise "name"
- Return ONLY valid JSON: {{"csv_column": "field_id_or_name", ...}}
- Do NOT include unmatchable columns in the output"""

    print(f"[filler-ai] Requesting field mapping for {page_url} ({len(csv_columns)} cols, {len(form_fields)} fields)", flush=True)
    response = _call_ai_api(prompt, max_tokens=4096)

    if not response:
        print(f"[filler-ai] No AI response, falling back to empty mapping", flush=True)
        _filler_field_maps[page_url] = {}
        return {}

    # Parse JSON from response
    try:
        # Extract JSON object
        start = response.index('{')
        end = response.rindex('}') + 1
        mapping = json.loads(response[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        print(f"[filler-ai] Failed to parse AI response: {e}", flush=True)
        _filler_field_maps[page_url] = {}
        return {}

    print(f"[filler-ai] Mapped {len(mapping)} columns", flush=True)
    _filler_field_maps[page_url] = mapping
    return mapping


def _detect_captcha(driver):
    """Detect captcha on page. Returns (type, sitekey) or (None, None)."""
    try:
        result = driver.execute_script("""
            // reCAPTCHA v2
            let el = document.querySelector('.g-recaptcha[data-sitekey]');
            if (el) return {type: 'recaptcha_v2', sitekey: el.getAttribute('data-sitekey')};
            // reCAPTCHA v2 invisible
            el = document.querySelector('[data-sitekey][data-size="invisible"]');
            if (el) return {type: 'recaptcha_v2', sitekey: el.getAttribute('data-sitekey')};
            // reCAPTCHA via iframe
            let iframe = document.querySelector('iframe[src*="recaptcha/api2"]');
            if (iframe) {
                let m = iframe.src.match(/[?&]k=([^&]+)/);
                if (m) return {type: 'recaptcha_v2', sitekey: m[1]};
            }
            // hCaptcha
            el = document.querySelector('.h-captcha[data-sitekey]');
            if (el) return {type: 'hcaptcha', sitekey: el.getAttribute('data-sitekey')};
            // Cloudflare Turnstile
            el = document.querySelector('.cf-turnstile[data-sitekey]');
            if (el) return {type: 'turnstile', sitekey: el.getAttribute('data-sitekey')};
            return null;
        """)
        if result:
            return result.get('type'), result.get('sitekey')
    except Exception:
        pass
    return None, None


def _solve_captcha(captcha_type, sitekey, page_url):
    """Send captcha to CapSolver and poll for solution. Returns token or None."""
    if not _captcha_api_key or not sitekey:
        return None

    task_types = {
        'recaptcha_v2': 'ReCaptchaV2TaskProxyLess',
        'hcaptcha': 'HCaptchaTaskProxyless',
        'turnstile': 'AntiTurnstileTaskProxyLess',
    }
    task_type = task_types.get(captcha_type)
    if not task_type:
        return None

    ctx = ssl.create_default_context()

    # Create task
    task_data = {
        "clientKey": _captcha_api_key,
        "task": {
            "type": task_type,
            "websiteURL": page_url,
            "websiteKey": sitekey
        }
    }
    try:
        req = urllib.request.Request(
            "https://api.capsolver.com/createTask",
            data=json.dumps(task_data).encode(),
            headers={"Content-Type": "application/json"},
            method='POST'
        )
        resp = urllib.request.urlopen(req, timeout=15, context=ctx)
        result = json.loads(resp.read().decode())
        task_id = result.get("taskId")
        if not task_id:
            print(f"[captcha] Create task failed: {result}", flush=True)
            return None
    except Exception as e:
        print(f"[captcha] Create task error: {e}", flush=True)
        return None

    # Poll for result (max 120 seconds)
    poll_data = {"clientKey": _captcha_api_key, "taskId": task_id}
    for _ in range(60):
        time.sleep(2)
        try:
            req = urllib.request.Request(
                "https://api.capsolver.com/getTaskResult",
                data=json.dumps(poll_data).encode(),
                headers={"Content-Type": "application/json"},
                method='POST'
            )
            resp = urllib.request.urlopen(req, timeout=15, context=ctx)
            result = json.loads(resp.read().decode())
            status = result.get("status")
            if status == "ready":
                token = result.get("solution", {}).get("gRecaptchaResponse") or \
                        result.get("solution", {}).get("token")
                print(f"[captcha] Solved ({captcha_type})", flush=True)
                return token
            if status not in ("idle", "processing"):
                print(f"[captcha] Unexpected status: {result}", flush=True)
                return None
        except Exception as e:
            print(f"[captcha] Poll error: {e}", flush=True)
            return None

    print("[captcha] Timed out", flush=True)
    return None


def _inject_captcha_token(driver, captcha_type, token):
    """Inject solved captcha token into the page."""
    try:
        if captcha_type == 'recaptcha_v2':
            driver.execute_script("""
                var token = arguments[0];
                // Inject token into all g-recaptcha-response textareas
                document.querySelectorAll('#g-recaptcha-response, [name="g-recaptcha-response"]').forEach(function(el) {
                    el.style.display = 'block'; el.value = token; el.style.display = 'none';
                });
                // Trigger callback — try multiple methods
                var called = false;
                // Method 1: data-callback attribute
                var widget = document.querySelector('.g-recaptcha');
                if (widget) {
                    var cb = widget.getAttribute('data-callback');
                    if (cb && typeof window[cb] === 'function') { window[cb](token); called = true; }
                }
                // Method 2: ___grecaptcha_cfg.clients callback (handles anonymous callbacks)
                if (!called) {
                    try {
                        var clients = ___grecaptcha_cfg.clients;
                        for (var i in clients) {
                            var c = clients[i];
                            // Walk the client object looking for callback functions
                            for (var k1 in c) {
                                if (typeof c[k1] === 'object' && c[k1] !== null) {
                                    for (var k2 in c[k1]) {
                                        if (typeof c[k1][k2] === 'object' && c[k1][k2] !== null) {
                                            for (var k3 in c[k1][k2]) {
                                                if (typeof c[k1][k2][k3] === 'function') {
                                                    try { c[k1][k2][k3](token); called = true; } catch(e) {}
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    } catch(e) {}
                }
                // Dismiss challenge overlay if visible
                try {
                    var overlay = document.querySelector('iframe[src*="recaptcha/api2/bframe"]');
                    if (overlay) overlay.parentElement.parentElement.style.display = 'none';
                    // Also hide the backdrop
                    var backdrops = document.querySelectorAll('div[style*="visibility: visible"][style*="position: fixed"]');
                    backdrops.forEach(function(el) { if (el.querySelector('iframe[src*="recaptcha"]')) el.style.display = 'none'; });
                } catch(e) {}
            """, token)
        elif captcha_type == 'hcaptcha':
            driver.execute_script("""
                var ta = document.querySelector('[name="h-captcha-response"]');
                if (ta) ta.value = arguments[0];
                document.querySelectorAll('[name="g-recaptcha-response"]').forEach(function(el) {
                    el.value = arguments[0];
                });
                var widget = document.querySelector('.h-captcha');
                if (widget) {
                    var cb = widget.getAttribute('data-callback');
                    if (cb && typeof window[cb] === 'function') window[cb](arguments[0]);
                }
            """, token)
        elif captcha_type == 'turnstile':
            driver.execute_script("""
                var ta = document.querySelector('[name="cf-turnstile-response"]');
                if (ta) ta.value = arguments[0];
                document.querySelectorAll('input[name="cf-turnstile-response"]').forEach(function(el) {
                    el.value = arguments[0];
                });
            """, token)
        return True
    except Exception as e:
        print(f"[captcha] Inject error: {e}", flush=True)
        return False


def _fill_current_row():
    """Fill the current row's data into the form. Returns status dict."""
    global _filler_data, _filler_index, _filler_driver

    if _filler_data is None or _filler_index >= len(_filler_data):
        return {"ok": False, "error": "No data or index out of range"}

    driver = _filler_driver
    if driver is None:
        return {"ok": False, "error": "Browser not open"}

    row = _filler_data[_filler_index]
    filled = []
    errors = []

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select, WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    # Determine which column is the URL column (skip it during fill)
    url_col = _filler_url_col

    # Wait for page to have at least one input/select/textarea
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input, select, textarea"))
        )
    except Exception:
        pass

    time.sleep(1)  # extra settle time for JS-rendered forms

    # Get AI field mapping (cached per URL)
    current_url = driver.current_url
    csv_columns = list(row.keys())
    ai_map = _build_field_map(driver, csv_columns, current_url)

    for col_name, value in row.items():
        # Skip the URL column
        if url_col and col_name == url_col:
            continue
        if not value or str(value).strip() == '':
            continue
        value = str(value).strip()

        elements = []

        # Strategy 0: AI mapping (primary)
        if col_name in ai_map:
            field_ref = ai_map[col_name]
            try:
                elements = driver.find_elements(By.ID, field_ref)
            except Exception:
                pass
            if not elements:
                try:
                    elements = driver.find_elements(By.NAME, field_ref)
                except Exception:
                    pass

        # Fallback 1: exact name attribute match
        if not elements:
            elements = driver.find_elements(By.NAME, col_name)

        # Fallback 2: exact id match
        if not elements:
            elements = driver.find_elements(By.ID, col_name)

        if not elements:
            errors.append(f"Field not found: {col_name}")
            continue

        el = elements[0]
        tag = el.tag_name.lower()
        input_type = (el.get_attribute('type') or '').lower()

        try:
            if tag == 'select':
                sel = Select(el)
                try:
                    sel.select_by_value(value)
                except Exception:
                    try:
                        sel.select_by_visible_text(value)
                    except Exception:
                        # Try partial match
                        matched = False
                        for opt in sel.options:
                            if value.lower() in opt.text.lower():
                                sel.select_by_visible_text(opt.text)
                                matched = True
                                break
                        if not matched:
                            errors.append(f"No option matching '{value}' for {col_name}")
                            continue
                filled.append(col_name)

            elif input_type in ('checkbox', 'radio'):
                should_check = value.lower() in ('1', 'true', 'yes', 'y', 'checked', 'on')
                is_checked = el.is_selected()
                if should_check != is_checked:
                    el.click()
                filled.append(col_name)

            elif input_type == 'file':
                # For file inputs, value should be an absolute file path
                if os.path.isfile(value):
                    el.send_keys(os.path.abspath(value))
                    filled.append(col_name)
                else:
                    errors.append(f"File not found: {value}")

            elif input_type in ('date', 'datetime-local', 'month', 'week', 'time'):
                # Date/time inputs: use JS to bypass picker widgets
                try:
                    driver.execute_script(
                        "arguments[0].value = arguments[1];"
                        "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                        "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));"
                        "arguments[0].blur();",
                        el, value
                    )
                except Exception:
                    try:
                        el.clear()
                        el.send_keys(value)
                    except Exception as e2:
                        errors.append(f"Error filling date {col_name}: {str(e2)[:100]}")
                        continue
                filled.append(col_name)

            else:
                # text, email, tel, url, number, textarea, etc.
                # Detect date/calendar fields — Ninja Forms pikaday, flatpickr, etc.
                try:
                    has_picker = driver.execute_script("""
                        var el = arguments[0];
                        var cls = (el.className || '') + ' ' + (el.getAttribute('data-type') || '');
                        if (/date|pikaday|calendar|datepick|flatpickr/i.test(cls)) return true;
                        if (el.getAttribute('type') === 'date') return true;
                        if (el.hasAttribute('data-pikaday')) return true;
                        // Ninja Forms: check parent container for pika-single
                        var container = el.closest('.nf-field-container, .field-wrap, .form-group');
                        if (container && container.querySelector('.pika-single, .flatpickr-calendar')) return true;
                        if (el.parentElement && el.parentElement.querySelector('.pika-single, .flatpickr-calendar')) return true;
                        // Check if element has a _flatpickr or pikaday instance
                        if (el._flatpickr || el._pikaday) return true;
                        return false;
                    """, el)
                except Exception:
                    has_picker = False

                if has_picker:
                    try:
                        driver.execute_script(
                            "arguments[0].value = arguments[1];"
                            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));"
                            "arguments[0].blur();",
                            el, value
                        )
                        filled.append(col_name)
                        continue
                    except Exception:
                        pass

                try:
                    el.clear()
                    el.send_keys(value)
                except Exception:
                    try:
                        driver.execute_script(
                            "arguments[0].value = arguments[1];"
                            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                            el, value
                        )
                    except Exception as js_e:
                        errors.append(f"Error filling {col_name}: {str(js_e)[:100]}")
                        continue
                filled.append(col_name)

        except Exception as e:
            errors.append(f"Error filling {col_name}: {str(e)[:100]}")

    # Get the URL for this row
    row_url = _filler_url
    if url_col and url_col in row:
        row_url = row[url_col]

    return {
        "ok": True,
        "row": _filler_index + 1,
        "total": len(_filler_data),
        "filled": filled,
        "errors": errors,
        "remaining": len(_filler_data) - _filler_index - 1,
        "url": row_url
    }


# ── HTTP Server ──────────────────────────────────────────────────────────────

def get_serve_dir():
    """Serve from APPDATA if updated HTML exists there, else from bundled."""
    appdata_html = APP_DIR / "bubblegum.html"
    if appdata_html.exists():
        return str(APP_DIR)
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


class QuietHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=get_serve_dir(), **kwargs)

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/proxy':
            self._handle_proxy(parsed.query)
        elif parsed.path == '/quit':
            self._send_json(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(2), os._exit(0)), daemon=True).start()
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/extract/multistep':
            self._handle_multistep_extract()
        elif parsed.path == '/filler/start':
            self._handle_filler_start()
        elif parsed.path == '/filler/fill':
            self._handle_filler_fill()
        elif parsed.path == '/filler/next':
            self._handle_filler_next()
        elif parsed.path == '/filler/stop':
            self._handle_filler_stop()
        elif parsed.path == '/filler/solve-captcha':
            self._handle_solve_captcha()
        else:
            self.send_error(404)

    def do_OPTIONS(self):
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

    # ── Multi-Step Form Extraction ─────────────────────────────

    def _handle_multistep_extract(self):
        """Use Selenium to open a form, click through all steps, extract all fields."""
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

        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            with _edge_lock:
                driver = _get_edge_driver()
                driver.get(url)
                time.sleep(4)  # wait for JS forms to render

            all_fields = []
            step = 1
            max_steps = 20

            while step <= max_steps:
                # Extract fields from current step via JS
                fields_json = driver.execute_script("""
                    const fields = [];
                    const seen = new Set();
                    const els = document.querySelectorAll('input, select, textarea');
                    for (const el of els) {
                        const tag = el.tagName.toLowerCase();
                        let type = tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : (el.type || 'text');
                        if (['submit','button','image','reset'].includes(type)) continue;

                        // Skip invisible elements (but keep hidden fields)
                        if (type !== 'hidden' && el.offsetParent === null) continue;

                        const id = el.id || '';
                        const name = el.name || '';
                        const key = id || name || '';
                        if (!key || seen.has(key)) continue;
                        seen.add(key);

                        let label = '';
                        if (id) {
                            try {
                                const lbl = document.querySelector('label[for="' + CSS.escape(id) + '"]');
                                if (lbl) label = lbl.textContent.trim();
                            } catch(e) {}
                        }
                        if (!label) {
                            const parent = el.closest('label');
                            if (parent) label = parent.textContent.trim();
                        }
                        if (!label) label = el.getAttribute('aria-label') || '';
                        if (!label) label = el.placeholder || '';
                        if (!label && name) label = name.replace(/[\\[\\]_\\-\\.]+/g, ' ').trim();
                        if (!label) label = type;
                        label = label.replace(/\\s+/g, ' ').trim().substring(0, 150);

                        const required = el.required || el.classList.contains('required') || el.getAttribute('aria-required') === 'true';
                        const placeholder = el.placeholder || '';

                        let options = null;
                        if (tag === 'select') {
                            options = Array.from(el.options).map(o => o.textContent.trim()).filter(t => t && !t.match(/^-+$/) && !t.match(/^select/i));
                        }

                        fields.push({label, name, id, type, tag, required, placeholder, options: options});
                    }
                    return JSON.stringify(fields);
                """)

                try:
                    step_fields = json.loads(fields_json)
                except Exception:
                    step_fields = []

                # Add step number to each field
                for f in step_fields:
                    f['step'] = step
                    # Deduplicate against already collected fields
                    key = f.get('id') or f.get('name') or f.get('label')
                    if not any((af.get('id') or af.get('name') or af.get('label')) == key for af in all_fields):
                        all_fields.append(f)

                print(f"[multistep] Step {step}: {len(step_fields)} fields (total: {len(all_fields)})", flush=True)

                # Try to find and click a Next/Continue button
                next_clicked = False
                next_selectors = [
                    'input.nf-next', 'button.nf-next',                     # Ninja Forms
                    '.gform_next_button', '.gform_button_next',            # Gravity Forms
                    '.wpforms-page-next', '.wpforms-page-button.next',     # WPForms
                    '.forminator-button-next',                              # Forminator
                    'button.frm_next_page', '.frm_button_next',            # Formidable
                    '[data-action="next"]', '[data-nav="next"]',
                    'button[type="button"]',                                # Generic
                ]

                for sel in next_selectors:
                    try:
                        btns = driver.find_elements(By.CSS_SELECTOR, sel)
                        for btn in btns:
                            text = (btn.text or btn.get_attribute('value') or '').lower()
                            if any(w in text for w in ['next', 'continue', 'forward', '>']):
                                if btn.is_displayed() and btn.is_enabled():
                                    btn.click()
                                    time.sleep(2)
                                    next_clicked = True
                                    break
                        if next_clicked:
                            break
                    except Exception:
                        continue

                if not next_clicked:
                    break
                step += 1

            # Detect form plugin from page source
            page_src = driver.page_source[:80000].lower()
            plugin = 'HTML'
            plugin_sigs = [
                ('wpcf7', 'CF7'), ('wpforms', 'WPForms'), ('gform', 'Gravity Forms'),
                ('ninja-forms', 'Ninja Forms'), ('nf-form', 'Ninja Forms'),
                ('formidable', 'Formidable'), ('elementor', 'Elementor'),
                ('forminator', 'Forminator'), ('fluent-form', 'Fluent Forms'),
                ('wp-event-manager', 'WP Event Manager'),
            ]
            for sig, name in plugin_sigs:
                if sig in page_src:
                    plugin = name
                    break

            # Build response in same format as frontend DOM extraction
            forms = [{
                "plugin": plugin,
                "is_multi_step": step > 1,
                "total_steps": step,
                "fields": [{
                    "label": f.get("label", ""),
                    "name": f.get("name", ""),
                    "type": f.get("type", ""),
                    "required": f.get("required", False),
                    "placeholder": f.get("placeholder", ""),
                    "options": f.get("options"),
                    "step": f.get("step", 1)
                } for f in all_fields]
            }]

            self._send_json(200, {
                "url": url,
                "forms": forms,
                "steps_found": step,
                "total_fields": len(all_fields)
            })

        except Exception as e:
            import traceback
            print(f"[multistep] Error: {traceback.format_exc()}", flush=True)
            self._send_json(500, {"error": str(e)})

    # ── Proxy ────────────────────────────────────────────────────

    def _handle_proxy(self, query):
        """Always use headless Edge browser — bypasses WAF, bot detection, JS rendering."""
        params = urllib.parse.parse_qs(query)
        url = params.get('url', [''])[0]
        if not url:
            self.send_error(400, 'Missing url parameter')
            return
        if not url.startswith(('http://', 'https://')):
            self.send_error(400, 'URL must start with http:// or https://')
            return

        try:
            print(f"[proxy] Edge fetching {url}", flush=True)
            html = fetch_with_edge(url)
            data = html.encode('utf-8')
            print(f"[proxy] Edge returned {len(data)} bytes", flush=True)

            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('X-Proxy-Status', '200')
            self.send_header('X-Proxy-Url', url)
            self.send_header('X-Proxy-Method', 'edge')
            self.send_header('Access-Control-Expose-Headers',
                             'X-Proxy-Status, X-Proxy-Url, X-Proxy-Method')
            self.end_headers()
            self.wfile.write(data)

        except Exception as e:
            print(f"[proxy] Edge failed for {url}: {e}", flush=True)
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('X-Proxy-Status', '502')
            self.send_header('X-Proxy-Url', url)
            self.send_header('X-Proxy-Method', 'edge-failed')
            self.send_header('Access-Control-Expose-Headers',
                             'X-Proxy-Status, X-Proxy-Url, X-Proxy-Method')
            self.end_headers()
            self.wfile.write(str(e).encode())

    # ── Form Filler Endpoints ────────────────────────────────────

    def _handle_filler_start(self):
        """Start a form filling session. Expects JSON with csv_data (string).
        CSV first column can be a URL column — auto-detected if values look like URLs.
        If no URL column, a 'url' field in the JSON is used as fallback."""
        global _filler_data, _filler_index, _filler_url, _filler_url_col, _filler_results
        global _filler_ai_key, _filler_oai_key, _filler_ai_model, _filler_field_maps
        global _captcha_api_key

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        csv_text = data.get('csv_data', '').strip()
        fallback_url = data.get('url', '').strip()

        # AI keys
        if data.get('ai_key'):
            _filler_ai_key = data['ai_key']
        if data.get('oai_key'):
            _filler_oai_key = data['oai_key']
        if data.get('ai_model'):
            _filler_ai_model = data['ai_model']
        if data.get('captcha_key'):
            _captcha_api_key = data['captcha_key']

        if not csv_text:
            self._send_json(400, {"error": "Missing csv_data"})
            return

        # Parse CSV
        try:
            reader = csv.DictReader(io.StringIO(csv_text))
            rows = list(reader)
        except Exception as e:
            self._send_json(400, {"error": f"CSV parse error: {e}"})
            return

        if not rows:
            self._send_json(400, {"error": "CSV has no data rows"})
            return

        # Auto-detect URL column: first column whose first value looks like a URL
        headers = list(rows[0].keys())
        url_col = None
        for h in headers:
            val = (rows[0].get(h) or '').strip()
            if val.startswith(('http://', 'https://')):
                url_col = h
                break

        # Determine the URL for the first row
        if url_col:
            first_url = rows[0][url_col].strip()
        elif fallback_url:
            first_url = fallback_url
        else:
            self._send_json(400, {"error": "No URL column found in CSV and no fallback URL provided"})
            return

        _filler_url_col = url_col
        _filler_url = first_url
        _filler_data = rows
        _filler_index = 0
        _filler_results = []

        # Open visible browser and navigate
        with _filler_lock:
            try:
                driver = _get_filler_driver()
                driver.get(first_url)
                time.sleep(2)
            except Exception as e:
                self._send_json(500, {"error": f"Browser launch failed: {e}"})
                return

        # Fill first row
        with _filler_lock:
            result = _fill_current_row()

        result['columns'] = headers
        result['total'] = len(rows)
        result['url_column'] = url_col
        self._send_json(200, result)

    def _handle_filler_fill(self):
        """Re-fill the current row (e.g., after page reload)."""
        with _filler_lock:
            result = _fill_current_row()
        self._send_json(200, result)

    def _handle_filler_next(self):
        """Record current row result, move to next row. Navigate if URL changes."""
        global _filler_index, _filler_data, _filler_url, _filler_results

        if _filler_data is None:
            self._send_json(400, {"error": "No active session"})
            return

        # Record result for current row
        current_row = _filler_data[_filler_index]
        row_url = _filler_url
        if _filler_url_col and _filler_url_col in current_row:
            row_url = current_row[_filler_url_col].strip()
        _filler_results.append({
            "url": row_url,
            "row": _filler_index + 1,
            "status": "submitted",
            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S')
        })

        _filler_index += 1
        if _filler_index >= len(_filler_data):
            self._send_json(200, {
                "ok": True, "done": True,
                "message": f"All {len(_filler_data)} rows completed",
                "results": _filler_results
            })
            return

        # Determine URL for next row
        next_row = _filler_data[_filler_index]
        if _filler_url_col and _filler_url_col in next_row:
            next_url = next_row[_filler_url_col].strip()
        else:
            next_url = _filler_url

        url_changed = next_url != row_url

        with _filler_lock:
            try:
                driver = _get_filler_driver()
                driver.get(next_url)
                _filler_url = next_url
                time.sleep(2)
            except Exception as e:
                self._send_json(500, {"error": f"Navigation failed: {e}"})
                return

            result = _fill_current_row()

        result['url_changed'] = url_changed
        self._send_json(200, result)

    def _handle_solve_captcha(self):
        """Detect and solve captcha on current page using CapSolver."""
        if not _captcha_api_key:
            self._send_json(400, {"error": "No CapSolver key set"})
            return
        if not _filler_driver:
            self._send_json(400, {"error": "No browser open"})
            return

        with _filler_lock:
            ctype, skey = _detect_captcha(_filler_driver)

        if not ctype:
            self._send_json(200, {"ok": True, "captcha_found": False})
            return

        t0 = time.time()
        token = _solve_captcha(ctype, skey, _filler_driver.current_url)
        elapsed = round(time.time() - t0, 1)

        if not token:
            self._send_json(200, {"ok": False, "error": f"Solve failed ({elapsed}s) \u2014 click again to retry or solve manually",
                                   "captcha_type": ctype, "time": elapsed})
            return

        with _filler_lock:
            _inject_captcha_token(_filler_driver, ctype, token)

        self._send_json(200, {"ok": True, "captcha_found": True, "captcha_type": ctype, "time": elapsed})

    def _handle_filler_stop(self):
        """Stop the filling session and close browser."""
        global _filler_data, _filler_index, _filler_results
        results = list(_filler_results)
        _filler_data = None
        _filler_index = 0
        _filler_results = []
        with _filler_lock:
            _close_filler_driver()
        self._send_json(200, {"ok": True, "message": "Session stopped", "results": results})


# ── Server Lifecycle ─────────────────────────────────────────────────────────

def kill_port(port):
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
    if not port_available(PORT):
        kill_port(PORT)
        time.sleep(0.5)
    server = HTTPServer(("127.0.0.1", PORT), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ── Main ─────────────────────────────────────────────────────────────────────

# Note: main() and activation are in bubblegum_launcher.py (the exe).
# This file is exec()'d by the launcher. start_server() is called by the launcher.
