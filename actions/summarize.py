"""
summarize.py — condense something the user is already looking at.

Sources, and why the list stops here
────────────────────────────────────
clipboard : what the user just copied — the highest-value case, and free.
url       : an article, fetched as text.
file      : a plain-text file (.txt, .md, .log, .csv, .json, source code).
text      : whatever the model passes straight through.

The screen is deliberately NOT a source: the inline `screen_process` tool in
main.py already captures and describes what is on the display, and a second
screenshot path would only be a second way to get it wrong. Office and PDF
documents are deliberately NOT a source either — `document_qa` owns those, so
each parser exists exactly once in the codebase.

Two guards matter here because both inputs are attacker-influenced: a URL comes
from the model (so the scheme is whitelisted and loopback/private addresses are
refused — a voice assistant must not become an SSRF probe), and a file path
comes from the model (so it is resolved through symlinks and must stay inside
the user's own directories).
"""
from __future__ import annotations

import ipaddress
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse

_MAX_INPUT_CHARS = 60_000        # protect the model's context window
_MAX_URL_BYTES = 4_000_000       # stop before a huge or endless response
_MAX_TEXT_BYTES = 5_000_000
_URL_TIMEOUT = 20
_ALLOWED_SCHEMES = ("http", "https")
_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm",
    ".py", ".js", ".ts", ".css", ".sh", ".bat", ".ps1", ".sql", ".tex",
}
_LENGTHS = {
    "short": ("two or three sentences", 400),
    "medium": ("one short paragraph of about five sentences", 900),
    "detailed": ("a structured summary with the key points as a short list", 2500),
}

_SAFE_ROOTS = (Path.home(), Path(tempfile.gettempdir()))


def _safe_file(raw) -> Path | None:
    """Resolve a model-supplied path, refusing anything outside the user's space.

    Symlinks are resolved *before* the boundary check, so a link that lives
    inside the home directory cannot be used to read a system file.
    """
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return None
    try:
        resolved = Path(text).expanduser().resolve()
    except Exception:
        return None
    for root in _SAFE_ROOTS:
        try:
            root_resolved = root.resolve()
            if resolved == root_resolved or resolved.is_relative_to(root_resolved):
                return resolved
        except Exception:
            continue
    return None


def _blocked_host(host: str) -> bool:
    """True when the URL points at this machine or a private network.

    Only answered when the name actually resolves; an unresolvable host is left
    to the HTTP client, which produces the better error message.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, socket.gaierror):
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        if addr.is_loopback or addr.is_private or addr.is_link_local \
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified:
            return True
    return False


def _from_clipboard() -> tuple[str, str]:
    try:
        import pyperclip
    except ImportError:
        return "", "The clipboard helper is not installed."
    try:
        text = pyperclip.paste() or ""
    except Exception:
        return "", "I could not read the clipboard."
    if not text.strip():
        return "", "The clipboard is empty."
    return text, ""


def _from_url(raw) -> tuple[str, str]:
    # Validate before importing anything: whether a fetch is allowed must not
    # depend on which optional packages happen to be installed, and a refused
    # address should say so instead of blaming the web helpers.
    parsed = urlparse(str(raw or "").strip())
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.netloc:
        return "", "That is not an http or https address I can fetch."
    host = parsed.hostname or ""
    if not host or _blocked_host(host):
        # Say why, but not which address: the reply is spoken aloud and the
        # resolved IP is nobody's business.
        return "", "I will not fetch addresses on this machine or its local network."

    try:
        import requests
    except ImportError:
        return "", "The web helper packages are not installed."

    try:
        response = requests.get(parsed.geturl(), timeout=_URL_TIMEOUT, stream=True,
                                headers={"User-Agent": "MARK-LIV/1.0 (+local assistant)"})
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=8192):
            size += len(chunk or b"")
            if size > _MAX_URL_BYTES:
                break
            chunks.append(chunk)
        html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    except Exception as exc:
        return "", f"I could not fetch that page: {type(exc).__name__}."

    # Tier 1: trafilatura (strips boilerplate, ads, cookie notices, and extracts clean main article)
    text = ""
    try:
        import trafilatura
        extracted = trafilatura.extract(html, include_links=False, include_images=False)
        if extracted and len(extracted.strip()) >= 80:
            text = extracted.strip()
    except ImportError:
        pass
    except Exception:
        pass

    # Tier 2: BeautifulSoup fallback
    if not text:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript", "template", "svg", "form"]):
                tag.decompose()
            text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines())
        except Exception:
            return "", "I fetched the page but could not read its text."

    text = "\n".join(line for line in (l.strip() for l in text.splitlines()) if line)
    if len(text.strip()) < 80:
        return "", "That page has almost no readable text — it may need a browser."
    return text, ""


def _from_file(raw) -> tuple[str, str]:
    path = _safe_file(raw)
    if path is None:
        return "", "That path is outside the folders I am allowed to read."
    if not path.exists() or not path.is_file():
        return "", f"I cannot find that file: {path.name}"
    if path.suffix.lower() not in _TEXT_SUFFIXES:
        return "", (f"'{path.suffix or path.name}' is not a plain-text file. "
                    "For PDF, Word, Excel or PowerPoint use document_qa.")
    try:
        if path.stat().st_size > _MAX_TEXT_BYTES:
            return "", "That text file is too large to summarize."
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return "", f"I could not read that file: {type(exc).__name__}."
    if not text.strip():
        return "", "That file is empty."
    return text, ""


def summarize_action(parameters: dict, player=None) -> str:
    """
    Summarize the clipboard, a web page, a plain-text file or given text.

    parameters:
        source   : "clipboard" | "url" | "file" | "text"  (required)
        content  : the text itself            (source=text)
        url      : the page address           (source=url)
        path     : the file path              (source=file)
        length   : "short" | "medium" | "detailed"
        language : language to answer in; defaults to the source language
    """
    p = parameters if isinstance(parameters, dict) else {}
    source = str(p.get("source") or "").strip().lower()

    if source == "clipboard":
        text, error = _from_clipboard()
    elif source == "url":
        text, error = _from_url(p.get("url"))
    elif source == "file":
        text, error = _from_file(p.get("path"))
    elif source == "text":
        text = str(p.get("content") or "")
        error = "" if text.strip() else "There is no text to summarize."
    else:
        return ("Tell me what to summarize: the clipboard, a web address, "
                "a text file, or the text itself.")

    if error:
        return error

    style, cap = _LENGTHS.get(str(p.get("length") or "").strip().lower(),
                              _LENGTHS["medium"])
    language = str(p.get("language") or "").strip()
    lang_rule = (f"Write the summary in {language}."
                 if language else "Write the summary in the same language as the source text.")

    body = text[:_MAX_INPUT_CHARS]
    truncated = len(text) > _MAX_INPUT_CHARS
    prompt = (
        f"Summarize the text below as {style}. Keep only what actually matters, "
        f"do not invent anything that is not in the text, and do not add your own "
        f"opinion. {lang_rule}\n"
        f"{'Note: the text was cut short, so summarize only what is present.' if truncated else ''}\n\n"
        f"--- TEXT START ---\n{body}\n--- TEXT END ---"
    )

    try:
        from core import gemini
    except Exception as exc:
        return f"The summarizer is unavailable: {type(exc).__name__}."

    summary = gemini.text(prompt, tier=gemini.SMART, timeout_ms=60_000,
                          default="")
    if not summary.strip():
        return "I could not produce a summary — the model did not answer."

    summary = summary.strip()
    origin = {"clipboard": "your clipboard", "url": "that page",
              "file": Path(str(p.get("path") or "")).name or "that file",
              "text": "that text"}.get(source, "the text")

    if player is not None:
        try:
            player.show_content(f"SUMMARY — {origin[:38]}", summary)
        except Exception:
            pass
        try:
            player.write_log(f"SUMMARY: {origin}, {len(summary)} chars"
                             + (" (source truncated)" if truncated else ""))
        except Exception:
            pass

    return f"Summary of {origin}: {summary}"


TOOL = {
    "name": "summarize",
    "description": (
        "Condenses text into a shorter summary and shows the full result on screen. "
        "Sources: what the user just copied to the clipboard, a web page address, a "
        "plain-text file such as notes, logs, markdown or source code, or text passed "
        "directly. Use it when the user asks to summarize, shorten, give the gist of, "
        "or explain briefly something they are looking at. For PDF, Word, Excel or "
        "PowerPoint files use document_qa instead; for describing what is currently on "
        "the screen use screen_process."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "source": {
                "type": "STRING",
                "description": "Where the text comes from: clipboard, url, file or text.",
            },
            "content": {
                "type": "STRING",
                "description": "The text itself. Only used when source is 'text'.",
            },
            "url": {
                "type": "STRING",
                "description": "Full http(s) address of the page. Only used when "
                               "source is 'url'.",
            },
            "path": {
                "type": "STRING",
                "description": "Path of a plain-text file. Only used when source "
                               "is 'file'.",
            },
            "length": {
                "type": "STRING",
                "description": "How long the summary may be: short, medium "
                               "(default) or detailed.",
            },
            "language": {
                "type": "STRING",
                "description": "Optional language for the summary, e.g. 'German'. "
                               "Defaults to the language of the source text.",
            },
        },
        "required": ["source"],
    },
    "handler": summarize_action,
}
