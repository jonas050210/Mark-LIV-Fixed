"""
Focused regression and verification tests for modernized open-source backends in Mark LIV:
1. PyMuPDF in actions/document_qa.py
2. trafilatura in actions/summarize.py
3. yt-dlp in actions/youtube_video.py
4. Shortcut caching in actions/open_app.py
5. UI Automation fallback structure in actions/computer_control.py
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

def test_pymupdf_extraction_and_fallback():
    from actions.document_qa import _extract_pdf
    import pymupdf as fitz

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        pdf_path = Path(tf.name)
    
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "MARK LIV PYMUPDF VERIFICATION TEST 2026")
    doc.save(str(pdf_path))
    doc.close()

    try:
        text, err = _extract_pdf(pdf_path, None)
        assert not err, f"Extraction failed with error: {err}"
        assert "MARK LIV PYMUPDF VERIFICATION TEST" in text

        text_p1, _ = _extract_pdf(pdf_path, "1")
        assert "MARK LIV PYMUPDF VERIFICATION TEST" in text_p1
        _, err_p2 = _extract_pdf(pdf_path, "5")
        assert "outside the document" in err_p2
    finally:
        if pdf_path.exists():
            pdf_path.unlink()


def test_trafilatura_web_extraction():
    from actions.summarize import _from_url

    sample_html = b"""
    <!DOCTYPE html>
    <html>
      <head><title>Trafilatura Test</title></head>
      <body>
        <nav>Navigation Bar - Skip This Header Link Link</nav>
        <article>
          <h1>Jarvis Modernization Article</h1>
          <p>Mark LIV has successfully adopted trafilatura for high precision web article extraction.</p>
          <p>By extracting only the essential article body and discarding boilerplate navigational menus, token consumption is reduced by over fifty percent.</p>
        </article>
        <footer>Privacy Policy Terms of Service Copyright 2026 All Rights Reserved</footer>
      </body>
    </html>
    """

    mock_resp = Mock()
    mock_resp.iter_content = lambda chunk_size: [sample_html]
    mock_resp.encoding = "utf-8"
    mock_resp.raise_for_status = lambda: None

    with patch("requests.get", return_value=mock_resp), \
         patch("actions.summarize._blocked_host", return_value=False):
        text, err = _from_url("https://example.com/test-article")
        assert not err, f"URL extraction error: {err}"
        assert "Mark LIV has successfully adopted trafilatura" in text
        assert "Privacy Policy Terms of Service" not in text
        assert "Navigation Bar" not in text


def test_ytdlp_metadata_and_transcript_fallback():
    from actions.youtube_video import _scrape_video_info, _get_transcript

    mock_meta = {
        "title": "Jarvis Architecture Overview",
        "uploader": "Tony Stark",
        "view_count": 1250000,
        "duration": 345,
        "like_count": 89000,
        "automatic_captions": {
            "en": [{"url": "https://example.com/captions.vtt"}]
        }
    }

    mock_cm = MagicMock()
    mock_cm.__enter__.return_value.extract_info.return_value = mock_meta

    with patch("yt_dlp.YoutubeDL", return_value=mock_cm), \
         patch("actions.youtube_video._TRANSCRIPT_OK", False), \
         patch("requests.get", return_value=Mock(ok=True, text="WEBVTT\n00:00:01.000 --> 00:00:05.000\nHello Jarvis transcript test line with sufficient length to pass the threshold check.")):
        
        info = _scrape_video_info("dQw4w9WgXcQ")
        assert info["title"] == "Jarvis Architecture Overview"
        assert info["channel"] == "Tony Stark"
        assert "1,250,000" in info["views"]
        assert info["duration"] == "5:45"

        transcript = _get_transcript("dQw4w9WgXcQ")
        assert transcript is not None
        assert "Hello Jarvis transcript test line" in transcript


def test_open_app_shortcut_cache():
    import actions.open_app as open_app

    open_app._APP_CACHE = {}
    open_app._APP_CACHE_LOADED = True

    with patch("actions.open_app._load_app_cache", return_value={"custom_app": sys.executable}):
        resolved = open_app._resolve_windows_executable("custom_app")
        assert resolved == sys.executable


def test_computer_control_uia_dispatch():
    import actions.computer_control as cc

    with patch.object(cc, "_UIA_AVAILABLE", True), \
         patch.object(cc, "_uia_click", return_value="UIA Clicked 'Submit'") as mock_click:
        res = cc.computer_control({"action": "click", "description": "Submit"})
        assert res == "UIA Clicked 'Submit'"
        mock_click.assert_called_once_with("Submit", control_type="button")


if __name__ == "__main__":
    test_pymupdf_extraction_and_fallback()
    print("test_pymupdf_extraction_and_fallback: PASS")
    test_trafilatura_web_extraction()
    print("test_trafilatura_web_extraction: PASS")
    test_ytdlp_metadata_and_transcript_fallback()
    print("test_ytdlp_metadata_and_transcript_fallback: PASS")
    test_open_app_shortcut_cache()
    print("test_open_app_shortcut_cache: PASS")
    test_computer_control_uia_dispatch()
    print("test_computer_control_uia_dispatch: PASS")
    print("\nALL MODERN BACKEND TESTS PASSED!")
