import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.html_page_semantics import (
    classify_html_page_semantics,
    extract_html_page_semantics,
)


def test_html_page_semantics_classifies_google_drive_virus_scan_warning():
    html = """
    <html>
      <head><title>Google Drive - Virus scan warning</title></head>
      <body>
        <div>Google Drive can't scan this file for viruses.</div>
        <form id="download-form" action="https://drive.usercontent.google.com/download" method="get">
          <input type="submit" id="uc-download-link" value="Download anyway">
          <input type="hidden" name="id" value="demo-id">
          <input type="hidden" name="export" value="download">
          <input type="hidden" name="confirm" value="t">
        </form>
      </body>
    </html>
    """

    semantics = extract_html_page_semantics(
        html,
        url="https://drive.google.com/file/d/demo/view",
        final_url="https://drive.google.com/file/d/demo/view",
        content_type="text/html; charset=utf-8",
    )
    classified = classify_html_page_semantics(semantics)

    assert semantics["title"] == "Google Drive - Virus scan warning"
    assert "virus_scan_warning" in semantics["signals"]
    assert classified["page_kind"] == "download_gate"
    assert classified["diagnosis"] == "gdrive_confirm_required"
    assert classified["suggested_next_action"] == "download_with_confirm_cookie_helper"
