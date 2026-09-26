"""Pure HTML rendering for document-review results shown by the HUD."""
from __future__ import annotations

from html import escape

_REVIEW_MARKS = {
    "serious": ("RED", "▲"),
    "caution": ("ACC2", "●"),
    "note": ("PRI_DIM", "·"),
}


def _text(value: object) -> str:
    return escape(str(value or "")).replace("\n", "<br>")


def render_review_html(summary: str, findings, unclear, palette) -> str:
    """Render escaped review data using attributes from the active palette."""
    parts = [f'<div style="color:{palette.TEXT}; font-family:Courier New;">']
    if summary:
        parts.append(
            f'<div style="color:{palette.WHITE}; border-left:2px solid {palette.PRI};'
            f' padding-left:8px; margin-bottom:10px;">{_text(summary)}</div>'
        )
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        key, mark = _REVIEW_MARKS.get(finding.get("severity"), ("PRI_DIM", "·"))
        colour = getattr(palette, key)
        parts.append('<div style="margin-bottom:11px;">')
        parts.append(
            f'<span style="color:{colour}; font-weight:bold;">{mark}</span> '
            f'<span style="color:{palette.WHITE}; font-weight:bold;">'
            f'{_text(finding.get("heading"))}</span>'
        )
        if finding.get("detail"):
            parts.append(f'<div style="margin-left:12px;">{_text(finding["detail"])}</div>')
        if finding.get("quote"):
            parts.append(
                f'<div style="margin-left:12px; color:{palette.TEXT_DIM};'
                f' border-left:1px solid {palette.BORDER}; padding-left:7px;">'
                f'&ldquo;{_text(finding["quote"])}&rdquo;</div>'
            )
        if finding.get("suggestion"):
            parts.append(
                f'<div style="margin-left:12px; color:{palette.PRI};">'
                f'&rarr; {_text(finding["suggestion"])}</div>'
            )
        parts.append("</div>")
    if unclear:
        parts.append(
            f'<div style="margin-top:6px; border-top:1px solid {palette.BORDER};'
            f' padding-top:7px; color:{palette.TEXT_MED};">'
            "The document does not settle:</div>"
        )
        for item in unclear:
            parts.append(
                f'<div style="margin-left:12px; color:{palette.TEXT_MED};">'
                f'&middot; {_text(item)}</div>'
            )
    parts.append("</div>")
    return "".join(parts)
