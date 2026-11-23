"""The developer handbook, served to the staff portal and offered as a download.

Master copy: docs/HANDBOOK.md, with its diagrams in docs/handbook/. Images are inlined as
data URIs, so the page and both downloads are single self-contained files (an <img> tag
cannot send the bearer token, and a downloaded file must work offline).
"""

from __future__ import annotations

import base64
import html
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HANDBOOK = ROOT / "docs" / "HANDBOOK.md"
IMAGE_DIR = ROOT / "docs" / "handbook"
_IMAGE = re.compile(r"\]\((handbook/[A-Za-z0-9_.-]+\.png)\)")
TITLE = "Bank Data Agent - Engineering Design & Developer Handbook"


class HandbookMissing(FileNotFoundError):
    pass


def _data_uri(rel: str) -> str:
    path = (ROOT / "docs" / rel).resolve()
    if path.parent != IMAGE_DIR.resolve() or not path.is_file():
        return rel  # unknown or outside the image folder: leave the reference as is
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def load_markdown(inline_images: bool = True) -> str:
    if not HANDBOOK.is_file():
        raise HandbookMissing(str(HANDBOOK))
    text = HANDBOOK.read_text(encoding="utf-8")
    if inline_images:
        text = _IMAGE.sub(lambda m: f"]({_data_uri(m.group(1))})", text)
    return text


def updated_at() -> str:
    return datetime.fromtimestamp(HANDBOOK.stat().st_mtime).isoformat(timespec="minutes")


_CSS = """
:root { color-scheme: light; }
body { font: 15px/1.6 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; color: #0f172a;
       max-width: 980px; margin: 32px auto; padding: 0 24px; background: #fff; }
h1 { font-size: 28px; margin: 0 0 8px; } h2 { font-size: 22px; margin-top: 40px;
     border-bottom: 1px solid #e2e8f0; padding-bottom: 6px; }
h3 { font-size: 18px; margin-top: 28px; } h4 { font-size: 15px; margin-top: 20px; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13.5px; }
th, td { border: 1px solid #cbd5e1; padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: #f1f5f9; }
code { font: 12.5px/1.4 ui-monospace, Menlo, Consolas, monospace; background: #f1f5f9;
       padding: 1px 4px; border-radius: 4px; }
pre { background: #0f172a; color: #e2e8f0; padding: 12px; border-radius: 8px; overflow-x: auto; }
pre code { background: none; color: inherit; padding: 0; }
img { max-width: 100%; border: 1px solid #e2e8f0; border-radius: 8px; }
a { color: #0f766e; }
@media print { body { margin: 0; max-width: none; } pre { white-space: pre-wrap; }
  h2 { break-before: page; } table, pre, img { break-inside: avoid; } }
"""


def to_html(markdown_text: str) -> str:
    import markdown

    body = markdown.markdown(
        markdown_text,
        extensions=["tables", "fenced_code", "sane_lists", "toc"],
        extension_configs={"toc": {"toc_depth": "2-3"}},
        output_format="html",
    )
    return (
        "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(TITLE)}</title><style>{_CSS}</style></head>"
        f"<body>{body}</body></html>\n"
    )
