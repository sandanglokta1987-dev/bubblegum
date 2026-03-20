import sys, os, time
print("Starting selenium test from PyInstaller exe...")
try:
    from selenium.webdriver import Edge, EdgeOptions
    print("  Import OK")
    opts = EdgeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    d = Edge(options=opts)
    print("  Driver created")
    d.get("https://example.com")
    print(f"  Page loaded: {len(d.page_source)} bytes")
    d.quit()
    print("SUCCESS")
except Exception as e:
    print(f"FAILED: {e}")
    import traceback; traceback.print_exc()
