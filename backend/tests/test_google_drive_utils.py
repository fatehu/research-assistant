from app.services.google_drive_utils import (
    build_google_drive_confirm_url,
    extract_google_drive_download_form,
    extract_google_drive_confirm_token,
    extract_google_drive_file_id,
    is_google_drive_url,
)


def test_google_drive_utils_detect_and_extract_file_id():
    url = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing"

    assert is_google_drive_url(url) is True
    assert extract_google_drive_file_id(url) == "1AbCdEfGhIjKlMnOp"


def test_google_drive_utils_extract_confirm_token_from_cookie_and_html():
    html = '<html><a href="/uc?export=download&amp;confirm=t9Xy&amp;id=demo">download</a></html>'

    assert extract_google_drive_confirm_token(
        html,
        cookies={"download_warning_123": "cookieToken"},
    ) == "cookieToken"
    assert extract_google_drive_confirm_token(html, cookies={}) == "t9Xy"


def test_google_drive_utils_build_confirm_url_from_view_url():
    url = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing&resourcekey=demo"

    confirm_url = build_google_drive_confirm_url(url, "cookieToken")

    assert confirm_url.startswith("https://drive.google.com/uc?")
    assert "export=download" in confirm_url
    assert "id=1AbCdEfGhIjKlMnOp" in confirm_url
    assert "confirm=cookieToken" in confirm_url
    assert "resourcekey=demo" in confirm_url


def test_google_drive_utils_extract_download_form_hidden_fields():
    html = """
    <html>
      <body>
        <form id="download-form" action="https://drive.usercontent.google.com/download" method="get">
          <input type="hidden" name="id" value="demo-id">
          <input type="hidden" name="export" value="download">
          <input type="hidden" name="confirm" value="t">
          <input type="hidden" name="uuid" value="demo-uuid">
          <input type="submit" id="uc-download-link" value="Download anyway">
        </form>
      </body>
    </html>
    """

    action, fields = extract_google_drive_download_form(html)

    assert action == "https://drive.usercontent.google.com/download"
    assert fields == {
        "id": "demo-id",
        "export": "download",
        "confirm": "t",
        "uuid": "demo-uuid",
    }
