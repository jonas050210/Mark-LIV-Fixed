"""
Verify every fallback path explicitly when modern backends are missing or raise exceptions.
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

def test_document_qa_fallback_chain():
    from actions.document_qa import _extract_pdf
    import pymupdf as fitz

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        pdf_path = Path(tf.name)
    
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "FALLBACK TIER TEST: PDFPLUMBER OR PYPDF2")
    doc.save(str(pdf_path))
    doc.close()

    try:
        # 1. Simulate PyMuPDF missing -> pdfplumber succeeds
        with patch.dict("sys.modules", {"pymupdf": None, "fitz": None}):
            text, err = _extract_pdf(pdf_path, None)
            assert not err, f"pdfplumber fallback error: {err}"
            assert "FALLBACK TIER TEST" in text
            print("  [PASS] PyMuPDF unavailable -> pdfplumber fallback succeeded")

        # 2. Simulate PyMuPDF and pdfplumber missing -> PyPDF2 succeeds
        with patch.dict("sys.modules", {"pymupdf": None, "fitz": None, "pdfplumber": None}):
            text, err = _extract_pdf(pdf_path, None)
            assert not err, f"PyPDF2 fallback error: {err}"
            assert "FALLBACK TIER TEST" in text
            print("  [PASS] PyMuPDF & pdfplumber unavailable -> PyPDF2 fallback succeeded")
    finally:
        if pdf_path.exists():
            pdf_path.unlink()


def test_summarize_bs4_fallback():
    from actions.summarize import _from_url

    sample_html = b"""
    <!DOCTYPE html>
    <html>
      <head><title>Fallback Article</title></head>
      <body>
        <article>
          <h1>BS4 Fallback Verification Heading</h1>
          <p>When trafilatura is unavailable or fails, BeautifulSoup cleanly extracts paragraph text from the document tree.</p>
          <p>This verification paragraph contains enough characters to satisfy the minimum length threshold required by the summarizer action.</p>
        </article>
      </body>
    </html>
    """
    mock_resp = Mock()
    mock_resp.iter_content = lambda chunk_size: [sample_html]
    mock_resp.encoding = "utf-8"
    mock_resp.raise_for_status = lambda: None

    with patch.dict("sys.modules", {"trafilatura": None}), \
         patch("requests.get", return_value=mock_resp), \
         patch("actions.summarize._blocked_host", return_value=False):
        text, err = _from_url("https://example.com/fallback-test")
        assert not err, f"URL extraction error: {err}"
        assert "When trafilatura is unavailable or fails" in text
        print("  [PASS] trafilatura unavailable -> BeautifulSoup fallback succeeded")


def test_youtube_video_fallback():
    import actions.youtube_video as yt
    from actions.youtube_video import _scrape_video_info, _get_transcript

    sample_html = '<html><body>{"title":{"runs":[{"text":"Scraped Fallback Title"}]}}</body></html>'
    with patch.dict("sys.modules", {"yt_dlp": None}), \
         patch("requests.get", return_value=Mock(ok=True, text=sample_html)):
        info = _scrape_video_info("dummy123")
        assert info["title"] == "Scraped Fallback Title"
        print("  [PASS] yt-dlp unavailable -> regex HTML scraper fallback succeeded")

    mock_transcript = Mock()
    mock_transcript.fetch.return_value = [{"text": "Transcript line from primary api"}]
    mock_list = Mock()
    mock_list.find_manually_created_transcript.return_value = mock_transcript
    mock_api = Mock()
    mock_api.list_transcripts.return_value = mock_list

    with patch.object(yt, "YouTubeTranscriptApi", mock_api, create=True), \
         patch.object(yt, "_TRANSCRIPT_OK", True):
        t = _get_transcript("dummy123")
        assert t == "Transcript line from primary api"
        print("  [PASS] Primary youtube_transcript_api intact and operational")


def test_computer_control_pyautogui_fallback():
    mock_pyautogui = Mock()
    with patch.dict("sys.modules", {"pyautogui": mock_pyautogui}):
        import actions.computer_control as cc
        with patch.object(cc, "_UIA_AVAILABLE", False), \
             patch.object(cc, "pyautogui", mock_pyautogui):
            res = cc.computer_control({"action": "click", "x": 200, "y": 300})
            assert "clicked (200, 300)" in res.lower()
            mock_pyautogui.click.assert_called_once_with(200, 300, button="left", clicks=1)
            print("  [PASS] UIA unavailable -> PyAutoGUI coordinate fallback succeeded")


if __name__ == "__main__":
    test_document_qa_fallback_chain()
    test_summarize_bs4_fallback()
    test_youtube_video_fallback()
    test_computer_control_pyautogui_fallback()
    print("\nALL FALLBACK PATHS VERIFIED!")
