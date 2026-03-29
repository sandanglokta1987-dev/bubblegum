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
_CAPSOLVER_KEY = "CAP-ED5F5BC298ACD06F27102102B73F5F4F4D5ED5187C2A57D42A43FA4100C85B0F"
_CAPSOLVER_EXT_URL = "https://github.com/capsolver/capsolver-browser-extension/releases/download/v.1.17.0/CapSolver.Browser.Extension-chrome-v1.17.0.zip"


def _get_capsolver_extension_path():
    """Download + configure CapSolver browser extension. Returns path to unpacked dir or None."""
    ext_dir = APP_DIR / "capsolver_ext"
    config_file = ext_dir / "assets" / "config.js"

    # Check if already configured with LF line endings
    if config_file.exists():
        try:
            raw = config_file.read_bytes()
            if _CAPSOLVER_KEY.encode() in raw and b'\r\n' not in raw:
                return str(ext_dir)
            # CRLF detected or key missing — re-extract
            import shutil
            shutil.rmtree(str(ext_dir), ignore_errors=True)
        except Exception:
            pass

    # Download and extract
    try:
        import zipfile
        print("[capsolver] Downloading extension...", flush=True)
        ctx = ssl.create_default_context()
        req = urllib.request.Request(_CAPSOLVER_EXT_URL, headers={'User-Agent': 'BubbleGum/4.0'})
        resp = urllib.request.urlopen(req, timeout=30, context=ctx)
        zip_data = resp.read()

        ext_dir.mkdir(parents=True, exist_ok=True)
        zip_path = APP_DIR / "capsolver_ext.zip"
        zip_path.write_bytes(zip_data)

        with zipfile.ZipFile(str(zip_path), 'r') as zf:
            zf.extractall(str(ext_dir))
        zip_path.unlink()
        print(f"[capsolver] Extracted to {ext_dir}", flush=True)
    except Exception as e:
        print(f"[capsolver] Download failed: {e}", flush=True)
        return None

    # Configure with API key
    try:
        config_content = f"""export const defaultConfig = {{
  apiKey: '{_CAPSOLVER_KEY}',
  appId: '',
  useCapsolver: true,
  manualSolving: false,
  solvedCallback: 'captchaSolvedCallback',
  useProxy: false,
  enabledForRecaptcha: true,
  enabledForRecaptchaV3: true,
  enabledForImageToText: true,
  enabledForAwsCaptcha: true,
  enabledForCloudflare: true,
  reCaptchaMode: 'click',
  hCaptchaMode: 'click',
  reCaptchaDelayTime: 0,
  hCaptchaDelayTime: 0,
  reCaptchaRepeatTimes: 10,
  hCaptchaRepeatTimes: 10,
  reCaptcha3RepeatTimes: 10,
  reCaptcha3TaskType: 'ReCaptchaV3TaskProxyLess',
  showSolveButton: true,
}};
"""
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_bytes(config_content.encode('utf-8'))  # LF line endings, not CRLF
        print("[capsolver] Extension configured (LF line endings)", flush=True)
    except Exception as e:
        print(f"[capsolver] Config write failed: {e}", flush=True)
        return None

    return str(ext_dir)


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

    # Load CapSolver browser extension for auto captcha solving
    ext_path = _get_capsolver_extension_path()
    if ext_path:
        opts.add_argument(f'--load-extension={ext_path}')

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

        # Scroll element into view before interacting
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.1)
        except Exception:
            pass

        try:
            if tag == 'select':
                sel = Select(el)
                try:
                    sel.select_by_value(value)
                except Exception:
                    try:
                        sel.select_by_visible_text(value)
                    except Exception:
                        # Try partial match + stripped comparison
                        matched = False
                        val_clean = re.sub(r'[,\s\$]', '', value.lower())
                        for opt in sel.options:
                            opt_text = opt.text.strip()
                            opt_clean = re.sub(r'[,\s\$]', '', opt_text.lower())
                            if value.lower() in opt_text.lower() or val_clean == opt_clean:
                                sel.select_by_visible_text(opt_text)
                                matched = True
                                break
                        if not matched:
                            errors.append(f"No option matching '{value}' for {col_name}")
                            continue
                filled.append(col_name)

            elif input_type == 'radio':
                # Radio GROUP: find the right option by value, label, or partial text match
                field_name = el.get_attribute('name') or col_name
                all_radios = driver.find_elements(By.NAME, field_name)
                clicked = False

                def click_radio(r):
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", r)
                        time.sleep(0.1)
                        r.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", r)
                    return True

                # Simple boolean (yes/true/1) — click first if truthy
                if value.lower() in ('1', 'true', 'yes', 'y', 'checked', 'on'):
                    if not el.is_selected():
                        click_radio(el)
                    clicked = True
                elif value.lower() in ('0', 'false', 'no', 'n', 'unchecked', 'off'):
                    for r in all_radios:
                        r_val = (r.get_attribute('value') or '').strip()
                        if r_val.lower() == 'no':
                            clicked = click_radio(r)
                            break
                else:
                    # Match by value attribute (exact)
                    for r in all_radios:
                        r_val = (r.get_attribute('value') or '').strip()
                        if r_val.lower() == value.lower():
                            clicked = click_radio(r)
                            break

                    # Match by associated label text (partial — first 40 chars)
                    if not clicked:
                        for r in all_radios:
                            r_id = r.get_attribute('id') or ''
                            label_text = ''
                            if r_id:
                                try:
                                    lbl = driver.find_element(By.CSS_SELECTOR, f'label[for="{r_id}"]')
                                    label_text = lbl.text.strip()
                                except Exception:
                                    pass
                            if not label_text:
                                try:
                                    parent = r.find_element(By.XPATH, '..')
                                    label_text = parent.text.strip()
                                except Exception:
                                    pass
                            if label_text and value.lower()[:40] in label_text.lower():
                                clicked = click_radio(r)
                                break

                    # Partial match on value attribute
                    if not clicked:
                        for r in all_radios:
                            r_val = (r.get_attribute('value') or '').strip()
                            if r_val and value.lower()[:30] in r_val.lower():
                                clicked = click_radio(r)
                                break

                    # Last resort: match value anywhere in label text
                    if not clicked:
                        for r in all_radios:
                            r_id = r.get_attribute('id') or ''
                            label_text = ''
                            if r_id:
                                try:
                                    lbl = driver.find_element(By.CSS_SELECTOR, f'label[for="{r_id}"]')
                                    label_text = lbl.text.strip()
                                except Exception:
                                    pass
                            r_val = (r.get_attribute('value') or '').strip()
                            combined = (label_text + ' ' + r_val).lower()
                            if value.lower()[:20] in combined:
                                clicked = click_radio(r)
                                break

                if clicked:
                    filled.append(col_name)
                else:
                    errors.append(f"No radio option matching '{value[:50]}' for {col_name}")

            elif input_type == 'checkbox':
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


# ── Second Pass: Fill Empty Required Fields ──────────────────────────────────

_SCAN_ALL_EMPTY_JS = r"""
(function() {
    var results = [];
    var seen = new Set();

    function isVisible(el) {
        return el.offsetParent !== null && getComputedStyle(el).display !== 'none'
            && getComputedStyle(el).visibility !== 'hidden';
    }

    function getLabel(el) {
        var label = '';
        if (el.id) {
            var lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (lbl) label = lbl.textContent.trim();
        }
        if (!label) {
            var parent = el.closest('label, .gfield, .nf-field, .wpforms-field, .frm_form_field');
            if (parent) {
                var lbl = parent.querySelector('label, .gfield_label, .nf-label, .wpforms-field-label');
                if (lbl) label = lbl.textContent.trim();
            }
        }
        if (!label) label = el.getAttribute('aria-label') || el.placeholder || el.name || '';
        return label.replace(/\s+/g, ' ').substring(0, 200);
    }

    function getOptions(el) {
        var name = el.name;
        if (el.type === 'radio') {
            var radios = document.querySelectorAll('input[name="' + CSS.escape(name) + '"]');
            var opts = [];
            radios.forEach(function(r) {
                var rLabel = '';
                if (r.id) {
                    var lbl = document.querySelector('label[for="' + CSS.escape(r.id) + '"]');
                    if (lbl) rLabel = lbl.textContent.trim();
                }
                if (!rLabel) rLabel = r.value;
                opts.push(rLabel);
            });
            return opts;
        }
        if (el.tagName === 'SELECT') {
            var opts = [];
            for (var i = 0; i < el.options.length; i++) {
                var t = el.options[i].text.trim();
                if (t && el.options[i].value) opts.push(t);
            }
            return opts;
        }
        return [];
    }

    document.querySelectorAll('input, select, textarea').forEach(function(el) {
        if (!isVisible(el)) return;
        var type = (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase();
        if (['hidden', 'submit', 'button', 'file', 'image', 'reset'].includes(type)) return;
        if (el.name && (el.name.includes('captcha') || el.name.includes('honeypot'))) return;
        if (el.className && /g-recaptcha|h-captcha/i.test(el.className)) return;

        var isEmpty = false;
        if (type === 'radio') {
            var name = el.name;
            if (seen.has('radio:' + name)) return;
            seen.add('radio:' + name);
            var checked = document.querySelector('input[name="' + CSS.escape(name) + '"]:checked');
            isEmpty = !checked;
        } else if (type === 'checkbox') {
            isEmpty = !el.checked;
        } else if (el.tagName === 'SELECT') {
            isEmpty = el.selectedIndex <= 0 || !el.value;
        } else {
            isEmpty = !el.value.trim();
        }

        if (!isEmpty) return;

        results.push({
            id: el.id || '',
            name: el.name || '',
            tag: el.tagName.toLowerCase(),
            type: type,
            label: getLabel(el),
            placeholder: el.placeholder || '',
            options: getOptions(el)
        });
    });

    return results;
})()
"""


def _fill_all_empty(driver):
    """Find ALL empty visible fields on the page and fill them using AI.
    Fills everything except file uploads and captchas.
    Returns {found, filled, errors} or None if nothing to do."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select

    try:
        empty_fields = driver.execute_script(_SCAN_ALL_EMPTY_JS)
    except Exception as e:
        print(f"[second-pass] JS scan failed: {e}", flush=True)
        return None

    if not empty_fields:
        print("[second-pass] No empty fields found — form is 100% filled.", flush=True)
        return None

    print(f"[second-pass] Found {len(empty_fields)} empty field(s) to fill.", flush=True)

    # Build prompt for AI
    lines = []
    for i, f in enumerate(empty_fields, 1):
        desc = f'"{f["label"] or f["name"] or f["id"]}" ({f["type"]})'
        if f.get("placeholder"):
            desc += f' — placeholder: "{f["placeholder"]}"'
        if f.get("options"):
            desc += f' — options: {" | ".join(f["options"])}'
        lines.append(f"{i}. {desc}")

    prompt = (
        "You MUST fill ALL of these empty form fields. Do NOT skip any.\n"
        "Generate realistic data for an average US adult.\n\n"
        "Rules:\n"
        "- For checkboxes (agreements, terms, consent, newsletters): ALWAYS set to \"true\"\n"
        "- For text areas / descriptions / comments: write 2-3 realistic sentences\n"
        "- For emails: use realistic format like firstname.lastname@gmail.com\n"
        "- For phone numbers: use US format (xxx) xxx-xxxx\n"
        "- For date fields: use a recent date in YYYY-MM-DD format\n"
        "- For selects/radios: pick the FIRST reasonable option from the list\n"
        "- For name fields: use realistic American names\n"
        "- For address fields: use a realistic US address\n"
        "- For URLs/websites: use a realistic .com URL\n"
        "- For number fields: use a reasonable number\n"
        "- EVERY field must have a value. No blanks. No nulls.\n\n"
        "FIELDS:\n" + "\n".join(lines) + "\n\n"
        "Return ONLY a JSON object mapping field numbers (as strings) to values.\n"
        'Example: {"1": "John", "2": "true", "3": "Option A", "4": "A short paragraph..."}\n'
        "No markdown fences, no explanation — just the JSON object."
    )

    raw = _call_ai_api(prompt, max_tokens=2048)
    if not raw:
        print("[second-pass] AI returned empty response.", flush=True)
        return {"found": len(empty_fields), "filled": 0, "errors": ["AI returned empty response"]}

    # Parse JSON — strip markdown fences if present
    cleaned = re.sub(r'^```(?:json)?\s*', '', raw.strip())
    cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        values = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"[second-pass] JSON parse failed: {e}\nRaw: {raw[:500]}", flush=True)
        return {"found": len(empty_fields), "filled": 0, "errors": [f"JSON parse failed: {e}"]}

    filled_count = 0
    fill_errors = []

    for i, field in enumerate(empty_fields, 1):
        ftype = field["type"]

        # Checkboxes: always check them regardless of AI response
        if ftype == 'checkbox':
            val = "true"
        else:
            val = values.get(str(i))
            if val is None:
                fill_errors.append(f"AI skipped field #{i} ({field.get('label', '')})")
                continue
            val = str(val).strip()
            if not val:
                fill_errors.append(f"AI returned blank for #{i} ({field.get('label', '')})")
                continue
        fid = field["id"]
        fname = field["name"]

        try:
            # Locate the element
            el = None
            if fid:
                els = driver.find_elements(By.ID, fid)
                if els:
                    el = els[0]
            if not el and fname:
                els = driver.find_elements(By.NAME, fname)
                if els:
                    # For radio, find all with same name
                    if ftype == 'radio':
                        el = els[0]  # we'll handle the group below
                    else:
                        el = els[0]
            if not el:
                fill_errors.append(f"Could not locate field #{i} ({field.get('label', '')})")
                continue

            # Scroll into view
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.1)

            if ftype == 'radio':
                # Find all radios in the group and match by label/value
                group_name = el.get_attribute('name')
                all_radios = driver.find_elements(By.NAME, group_name) if group_name else [el]
                clicked = False
                val_lower = val.lower()

                for r in all_radios:
                    # Check value attribute
                    r_val = (r.get_attribute('value') or '').strip()
                    if r_val.lower() == val_lower:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", r)
                        try:
                            r.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", r)
                        clicked = True
                        break

                if not clicked:
                    # Match by label text
                    for r in all_radios:
                        r_id = r.get_attribute('id') or ''
                        label_text = ''
                        if r_id:
                            try:
                                lbl = driver.find_element(By.CSS_SELECTOR, f'label[for="{r_id}"]')
                                label_text = lbl.text.strip()
                            except Exception:
                                pass
                        if not label_text:
                            try:
                                parent = r.find_element(By.XPATH, '..')
                                label_text = parent.text.strip()
                            except Exception:
                                pass
                        if label_text and val_lower in label_text.lower():
                            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", r)
                            try:
                                r.click()
                            except Exception:
                                driver.execute_script("arguments[0].click();", r)
                            clicked = True
                            break

                if clicked:
                    filled_count += 1
                else:
                    fill_errors.append(f"No radio match for #{i}: '{val[:50]}'")

            elif ftype == 'checkbox':
                if not el.is_selected():
                    try:
                        el.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", el)
                filled_count += 1

            elif field["tag"] == 'select':
                sel = Select(el)
                selected = False
                try:
                    sel.select_by_visible_text(val)
                    selected = True
                except Exception:
                    pass
                if not selected:
                    try:
                        sel.select_by_value(val)
                        selected = True
                    except Exception:
                        pass
                if not selected:
                    # Partial match
                    val_lower = val.lower()
                    for opt in sel.options:
                        if val_lower in opt.text.strip().lower():
                            sel.select_by_visible_text(opt.text.strip())
                            selected = True
                            break
                if selected:
                    filled_count += 1
                else:
                    fill_errors.append(f"No select match for #{i}: '{val[:50]}'")

            else:
                # text, email, tel, url, number, textarea, date
                if ftype in ('date', 'datetime-local', 'month', 'week', 'time'):
                    driver.execute_script(
                        "arguments[0].value = arguments[1];"
                        "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                        "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                        el, val
                    )
                else:
                    try:
                        el.clear()
                        el.send_keys(val)
                    except Exception:
                        driver.execute_script(
                            "arguments[0].value = arguments[1];"
                            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                            el, val
                        )
                filled_count += 1

        except Exception as e:
            fill_errors.append(f"Error filling #{i}: {str(e)[:100]}")

    result = {"found": len(empty_fields), "filled": filled_count, "errors": fill_errors}
    print(f"[second-pass] Done: {filled_count}/{len(empty_fields)} filled. Errors: {fill_errors}", flush=True)
    return result


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
        elif parsed.path == '/api/status':
            update_info = globals().get('_update_info', {})
            self._send_json(200, {
                "ready": True,
                "update": update_info,
                "version": "4.1",
                "files": ["bubblegum_app.py", "bubblegum.html"],
                "modules": ["selenium", "form-extractor", "form-filler", "ai-engine", "captcha-solver"]
            })
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
        elif parsed.path == '/filler/trigger-captcha':
            self._handle_trigger_captcha()
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

        # Kill any leftover browser from a previous session
        _close_filler_driver()

        # Open visible browser and navigate
        with _filler_lock:
            try:
                driver = _get_filler_driver()
                driver.get(first_url)
                time.sleep(2)
            except Exception:
                print("[filler] Browser timed out on start — restarting...", flush=True)
                _close_filler_driver()
                try:
                    driver = _get_filler_driver()
                    driver.get(first_url)
                    time.sleep(2)
                except Exception as e2:
                    self._send_json(500, {"error": f"Browser launch failed after retry: {e2}"})
                    return

        # Fill first row
        with _filler_lock:
            result = _fill_current_row()

        # Second pass: fill any empty required fields the CSV missed
        with _filler_lock:
            try:
                sp = _fill_all_empty(_filler_driver)
                if sp:
                    result['second_pass'] = sp
            except Exception as e:
                print(f"[second-pass] Error: {e}", flush=True)

        result['columns'] = headers
        result['total'] = len(rows)
        result['url_column'] = url_col
        self._send_json(200, result)

    def _handle_filler_fill(self):
        """Re-fill the current row (e.g., after page reload)."""
        with _filler_lock:
            result = _fill_current_row()
        with _filler_lock:
            try:
                sp = _fill_all_empty(_filler_driver)
                if sp:
                    result['second_pass'] = sp
            except Exception as e:
                print(f"[second-pass] Error: {e}", flush=True)
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
            except Exception:
                # Browser died — kill it, spawn fresh, retry once
                print("[filler] Browser timed out — restarting...", flush=True)
                _close_filler_driver()
                try:
                    driver = _get_filler_driver()
                    driver.get(next_url)
                    _filler_url = next_url
                    time.sleep(2)
                except Exception as e2:
                    self._send_json(500, {"error": f"Navigation failed after retry: {e2}"})
                    return

            result = _fill_current_row()

            try:
                sp = _fill_all_empty(_filler_driver)
                if sp:
                    result['second_pass'] = sp
            except Exception as e:
                print(f"[second-pass] Error: {e}", flush=True)

        result['url_changed'] = url_changed
        self._send_json(200, result)

    def _handle_trigger_captcha(self):
        """Click the reCAPTCHA/hCaptcha checkbox to make captcha visible for extension."""
        if not _filler_driver:
            self._send_json(400, {"error": "No browser open"})
            return
        try:
            from selenium.webdriver.common.by import By
            with _filler_lock:
                # Try reCAPTCHA iframe
                iframes = _filler_driver.find_elements(By.CSS_SELECTOR, 'iframe[src*="recaptcha/api2/anchor"]')
                if iframes:
                    _filler_driver.switch_to.frame(iframes[0])
                    checkbox = _filler_driver.find_element(By.CSS_SELECTOR, '.recaptcha-checkbox-border, #recaptcha-anchor')
                    checkbox.click()
                    _filler_driver.switch_to.default_content()
                    self._send_json(200, {"ok": True, "message": "Clicked reCAPTCHA checkbox"})
                    return

                # Try hCaptcha iframe
                iframes = _filler_driver.find_elements(By.CSS_SELECTOR, 'iframe[src*="hcaptcha.com"]')
                if iframes:
                    _filler_driver.switch_to.frame(iframes[0])
                    checkbox = _filler_driver.find_element(By.CSS_SELECTOR, '#checkbox')
                    checkbox.click()
                    _filler_driver.switch_to.default_content()
                    self._send_json(200, {"ok": True, "message": "Clicked hCaptcha checkbox"})
                    return

                self._send_json(200, {"ok": False, "message": "No captcha checkbox found on page"})
        except Exception as e:
            try:
                _filler_driver.switch_to.default_content()
            except Exception:
                pass
            self._send_json(200, {"ok": False, "message": str(e)[:200]})

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
