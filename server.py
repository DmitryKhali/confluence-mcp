import os
import re
import json
import subprocess
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from mcp.server.fastmcp import FastMCP

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL = os.getenv("CONFLUENCE_BASE_URL", "")
if not BASE_URL:
    raise RuntimeError("CONFLUENCE_BASE_URL environment variable is required")
MAX_CHARS = int(os.getenv("CONFLUENCE_MAX_CHARS", "50000"))
KEYCHAIN_SERVICE = os.getenv("CONFLUENCE_KEYCHAIN_SERVICE", "confluence_pat")


def get_token() -> str:
    token = os.getenv("CONFLUENCE_TOKEN", "").strip()
    if token:
        return token
    result = subprocess.run(
        ["security", "find-generic-password", "-a", os.getenv("USER", ""), "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True
    )
    return result.stdout.strip()


mcp = FastMCP("confluence")


def api(method, path, **kwargs):
    """Call Confluence REST API v1 with Bearer token auth."""
    url = urljoin(BASE_URL, f"/rest/api{path}")
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {get_token()}"
    headers.setdefault("Accept", "application/json")
    resp = requests.request(method, url, headers=headers, **kwargs)
    if resp.status_code >= 400:
        msg = f"API {method} {path} failed: {resp.status_code}"
        try:
            detail = resp.json()
            msg = f"{msg}\n{json.dumps(detail, ensure_ascii=False, indent=2)}"
        except Exception:
            msg = f"{msg}\n{resp.text}"
        raise Exception(msg)
    if resp.content:
        return resp.json()
    return {}


# ── Storage Format → Markdown Converter ───────────────────────────────────────

class MarkdownBuilder:
    """Builder for Markdown that collects lines, headings, and tables."""

    def __init__(self):
        self.lines = []
        self.headings = []
        self.tables = []
        self.current_table_rows = []
        self.current_table_heading = None
        self._last_was_blank = False

    def add_blank(self):
        if not self._last_was_blank and self.lines:
            self.lines.append("")
            self._last_was_blank = True

    def add_line(self, text):
        self.lines.append(text)
        self._last_was_blank = (text == "")

    def add_heading(self, level, text):
        self.add_blank()
        self.add_line(f'{"#" * level} {text}')
        self.add_line("")
        self._last_was_blank = True
        self.headings.append({"level": level, "text": text})
        # Set as potential table heading
        self.current_table_heading = {"level": level, "text": text}

    def add_paragraph(self, text):
        if text.strip():
            self.add_line(text.strip())
            self.add_line("")

    def add_blockquote(self, text):
        self.add_blank()
        for line in text.strip().split("\n"):
            self.add_line(f"> {line}")
        self.add_line("")

    def add_code_block(self, code, language=""):
        self.add_blank()
        self.add_line(f"```{language}")
        self.add_line(code.rstrip())
        self.add_line("```")
        self.add_line("")

    def add_list_item(self, text, depth=0, ordered=False, index=0):
        indent = "  " * depth
        if ordered:
            marker = f"{index}. "
        else:
            marker = "- "
        self.add_line(f"{indent}{marker}{text.strip()}")
        self._last_was_blank = False

    def add_horizontal_rule(self):
        self.add_blank()
        self.add_line("---")
        self.add_line("")

    def add_raw(self, text):
        self.lines.append(text)
        self._last_was_blank = False

    def start_table(self):
        self.current_table_rows = []

    def add_table_row(self, cells):
        self.current_table_rows.append(cells)

    def finish_table(self):
        if not self.current_table_rows:
            return
        self.tables.append({
            "heading": self.current_table_heading,
            "rows": self.current_table_rows,
        })
        # Render markdown table
        rows = self.current_table_rows
        max_cols = max(len(r) for r in rows)
        normalized = []
        for r in rows:
            row = list(r)
            while len(row) < max_cols:
                row.append("")
            normalized.append([c.replace("\n", " ").replace("|", "\\|") for c in row])

        self.add_blank()
        header = normalized[0]
        self.add_line("| " + " | ".join(header) + " |")
        self.add_line("| " + " | ".join(["---"] * max_cols) + " |")
        for row in normalized[1:]:
            self.add_line("| " + " | ".join(row) + " |")
        self.add_line("")
        self.current_table_rows = []

    def get_markdown(self):
        return "\n".join(self.lines).strip()


# Map of known macro names to their rendering behavior
MACRO_RENDERERS = {
    "code": "code",
    "noformat": "code",
    "info": "panel",
    "note": "panel",
    "warning": "panel",
    "tip": "panel",
    "panel": "panel",
    "expand": "expand",
    "detailssummary": "expand",
}


def convert_node(node, b: MarkdownBuilder, depth=0, list_state=None):
    """Recursively convert a BeautifulSoup node tree to Markdown."""
    if list_state is None:
        list_state = {"in_list": False, "list_tag": None, "counter": 0}

    if isinstance(node, NavigableString):
        text = str(node)
        return _normalize_ws(text)

    if not isinstance(node, Tag):
        return ""

    tag_name = node.name.lower() if node.name else ""

    # ── Hidden / Skipped elements ────────────────────────────────────────
    if tag_name in ("ac:link-body", "ac:plain-text-link-body"):
        return convert_children(node, b, depth, list_state)

    if tag_name in ("ac:parameter",):
        # Parameters like code language, title, etc — handled by parent
        return ""

    # ── Headings ─────────────────────────────────────────────────────────
    if re.match(r"^h([1-6])$", tag_name):
        level = int(tag_name[1])
        text = _collect_inline_text(node)
        b.add_heading(level, text)
        return ""

    # ── Paragraphs ───────────────────────────────────────────────────────
    if tag_name == "p":
        text = _collect_inline_text(node)
        # Skip empty paragraphs
        if not text.strip():
            b.add_blank()
            return ""
        b.add_paragraph(text)
        return ""

    # ── Lists ────────────────────────────────────────────────────────────
    if tag_name in ("ul", "ol"):
        prev_list = list_state.copy()
        list_state["in_list"] = True
        list_state["list_tag"] = tag_name
        list_state["counter"] = 0
        for child in node.children:
            if isinstance(child, Tag) and child.name == "li":
                list_state["counter"] += 1
                text = _collect_inline_text(child).strip()
                if text:
                    b.add_list_item(text, depth, ordered=(tag_name == "ol"),
                                    index=list_state["counter"])
        list_state.update(prev_list)
        return ""

    if tag_name == "li":
        # Handled by ul/ol parent
        return convert_children(node, b, depth, list_state)

    # ── Tables ───────────────────────────────────────────────────────────
    if tag_name == "table":
        b.start_table()
        convert_table(node, b)
        b.finish_table()
        return ""

    # ── Code blocks ──────────────────────────────────────────────────────
    if tag_name == "pre":
        code = node.get_text()
        b.add_code_block(code)
        return ""

    # ── Horizontal rule ──────────────────────────────────────────────────
    if tag_name == "hr":
        b.add_horizontal_rule()
        return ""

    # ── Blockquote ───────────────────────────────────────────────────────
    if tag_name == "blockquote":
        text = _collect_inline_text(node)
        b.add_blockquote(text)
        return ""

    # ── Macros (ac:structured-macro) ─────────────────────────────────────
    if tag_name == "ac:structured-macro":
        macro_name = (node.get("ac:name") or "").lower()
        render_as = MACRO_RENDERERS.get(macro_name, "unknown")

        if render_as == "code":
            lang = ""
            for param in node.find_all("ac:parameter", attrs={"ac:name": "language"}):
                lang = param.get_text(strip=True)
            body_tag = node.find("ac:plain-text-body")
            code_text = body_tag.get_text() if body_tag else node.get_text()
            b.add_code_block(code_text, lang)
            return ""

        elif render_as == "panel":
            title_param = node.find("ac:parameter", attrs={"ac:name": "title"})
            title_text = ""
            if title_param:
                title_text = title_param.get_text(strip=True)
            prefix = f"**{macro_name.upper()}**"
            if title_text:
                prefix += f" — {title_text}"
            body_tag = node.find("ac:rich-text-body") or node.find("div", class_="panelContent")
            if body_tag:
                inner = _collect_inline_text(body_tag)
            else:
                inner = _collect_inline_text(node)
            lines = inner.strip().split("\n")
            b.add_blank()
            b.add_line(f"> {prefix}")
            for line in lines:
                b.add_line(f"> {line}")
            b.add_line("")
            return ""

        elif render_as == "expand":
            title_param = node.find("ac:parameter", attrs={"ac:name": "title"})
            title_text = title_param.get_text(strip=True) if title_param else "Click to expand"
            body_tag = node.find("ac:rich-text-body")
            inner = _collect_inline_text(body_tag) if body_tag else ""
            b.add_blank()
            b.add_line(f"<details>")
            b.add_line(f"<summary>{title_text}</summary>")
            b.add_line("")
            for line in inner.strip().split("\n"):
                b.add_line(line)
            b.add_line("")
            b.add_line("</details>")
            b.add_line("")
            return ""

        elif macro_name == "toc":
            return ""  # Skip table of contents
        elif macro_name == "children":
            return ""  # Skip children list
        elif macro_name in ("attachments", "gallery"):
            return ""  # Skip attachment lists
        else:
            # Unknown macro: try to extract text
            body = node.find("ac:rich-text-body")
            if body:
                return convert_children(body, b, depth, list_state)
            return ""

    # ── Inline formatting ────────────────────────────────────────────────
    if tag_name in ("strong", "b"):
        return f"**{convert_children(node, b, depth, list_state)}**"
    if tag_name in ("em", "i"):
        return f"*{convert_children(node, b, depth, list_state)}*"
    if tag_name in ("code", "tt"):
        return f"`{convert_children(node, b, depth, list_state)}`"
    if tag_name in ("del", "s", "strike"):
        return f"~~{convert_children(node, b, depth, list_state)}~~"
    if tag_name in ("ins", "u"):
        return f"<u>{convert_children(node, b, depth, list_state)}</u>"
    if tag_name == "sub":
        return f"<sub>{convert_children(node, b, depth, list_state)}</sub>"
    if tag_name == "sup":
        return f"<sup>{convert_children(node, b, depth, list_state)}</sup>"
    if tag_name == "br":
        return "\n"

    # ── Links ────────────────────────────────────────────────────────────
    if tag_name == "a":
        href = node.get("href", "")
        text = _collect_inline_text(node)
        if href:
            return f"[{text}]({href})"
        return text

    if tag_name == "ac:link":
        # Confluence internal links
        anchor = node.get("ac:anchor", "")
        link_body = node.find("ac:link-body") or node.find("ac:plain-text-link-body")
        text = _collect_inline_text(link_body) if link_body else ""
        if anchor:
            return f"[{text}](#{anchor})"
        # Check for ri:page
        ri_page = node.find("ri:page")
        if ri_page:
            page_title = ri_page.get("ri:content-title", text)
            page_id = ri_page.get("ri:content-id", "")
            return f"[{page_title}](page://{page_id})"
        ri_attachment = node.find("ri:attachment")
        if ri_attachment:
            filename = ri_attachment.get("ri:filename", text)
            return f"[{filename}](attachment://{filename})"
        return text

    # ── Images ───────────────────────────────────────────────────────────
    if tag_name == "ac:image":
        alt = ""
        src = ""
        ri_attach = node.find("ri:attachment")
        if ri_attach:
            alt = ri_attach.get("ri:filename", "")
            src = f"attachment://{alt}"
        ri_url = node.find("ri:url")
        if ri_url:
            src = ri_url.get("ri:value", src)
            if not alt:
                alt = src
        return f"\n\n![{alt}]({src})\n\n"

    # ── Task lists ───────────────────────────────────────────────────────
    if tag_name == "ac:task-body":
        text = convert_children(node, b, depth, list_state)
        # Check for task status later when rendering
        return text

    # ── Generic block elements ───────────────────────────────────────────
    if tag_name == "div":
        return convert_children(node, b, depth, list_state)

    # ── Span / font / formatting wrappers ────────────────────────────────
    if tag_name in ("span", "font", "small", "big"):
        return convert_children(node, b, depth, list_state)

    # ── Body / root elements ─────────────────────────────────────────────
    if tag_name in ("body", "html", "[document]", "root"):
        return convert_children(node, b, depth, list_state)

    # ── Default: recurse into children ───────────────────────────────────
    return convert_children(node, b, depth, list_state)


def convert_table(table_node, b: MarkdownBuilder):
    """Convert an HTML table to rows in the builder."""
    rows = table_node.find_all("tr") or table_node.find_all("ac:tr")
    for tr in rows:
        cells = []
        for cell in tr.find_all(["th", "td", "ac:th", "ac:td"]):
            cells.append(_collect_inline_text(cell))
        if cells:
            b.add_table_row(cells)


def convert_children(node, b: MarkdownBuilder, depth=0, list_state=None):
    """Convert all children of a node, joining the results."""
    parts = []
    for child in node.children:
        result = convert_node(child, b, depth, list_state)
        if result:
            parts.append(result)
    return _normalize_ws(" ".join(parts))


def _collect_inline_text(node):
    """Get all text from a node and its descendants, preserving inline formatting."""
    if isinstance(node, NavigableString):
        return _normalize_ws(str(node))

    if not isinstance(node, Tag):
        return ""

    tag_name = node.name.lower() if node.name else ""

    if tag_name in ("strong", "b"):
        return f"**{_collect_inline_text_plain(node)}**"
    if tag_name in ("em", "i"):
        return f"*{_collect_inline_text_plain(node)}*"
    if tag_name in ("code", "tt"):
        return f"`{_collect_inline_text_plain(node)}`"
    if tag_name in ("del", "s", "strike"):
        return f"~~{_collect_inline_text_plain(node)}~~"
    if tag_name in ("ins", "u"):
        return f"<u>{_collect_inline_text_plain(node)}</u>"
    if tag_name == "sub":
        return f"<sub>{_collect_inline_text_plain(node)}</sub>"
    if tag_name == "sup":
        return f"<sup>{_collect_inline_text_plain(node)}</sup>"
    if tag_name == "a":
        href = node.get("href", "")
        text = _collect_inline_text_plain(node)
        if href:
            return f"[{text}]({href})"
        return text
    if tag_name == "ac:link":
        anchor = node.get("ac:anchor", "")
        text = _collect_inline_text_plain(node)
        if anchor:
            return f"[{text}](#{anchor})"
        ri_page = node.find("ri:page")
        if ri_page:
            page_title = ri_page.get("ri:content-title", text)
            return f"[{page_title}]"
        return text
    if tag_name == "ac:image":
        ri_attach = node.find("ri:attachment")
        if ri_attach:
            alt = ri_attach.get("ri:filename", "")
            return f"[🖼 {alt}]"
        return "[🖼]"
    if tag_name in ("br",):
        return "\n"
    if tag_name in ("ac:parameter", "ac:link-body", "ac:plain-text-link-body",
                    "ac:plain-text-body", "ac:rich-text-body", "ac:task-body",
                    "ri:page", "ri:attachment", "ri:url", "ri:user"):
        return _collect_inline_text_plain(node)
    if tag_name in ("span", "font", "small", "big", "div"):
        return _collect_inline_text_plain(node)

    # Block elements — treat as paragraph break
    if tag_name in ("p",) or re.match(r"^h[1-6]$", tag_name):
        return _collect_inline_text_plain(node)

    return _collect_inline_text_plain(node)


def _collect_inline_text_plain(node):
    """Get plain text from node without formatting marks."""
    if isinstance(node, NavigableString):
        return _normalize_ws(str(node))
    if not isinstance(node, Tag):
        return ""
    parts = []
    for child in node.children:
        parts.append(_collect_inline_text_plain(child))
    return "".join(parts) if parts else node.get_text() if isinstance(node, Tag) else ""


def _normalize_ws(text):
    """Collapse whitespace but preserve single spaces."""
    return re.sub(r"\s+", " ", text)


# ── Page Conversion ───────────────────────────────────────────────────────────

def convert_page_body(page_data):
    """Convert a Confluence page from storage format to Markdown.

    Returns: (markdown_text, tables_list, headings_list)
    """
    body = page_data.get("body", {})
    storage = body.get("storage", {})
    html = storage.get("value", "")

    if not html:
        return "", [], []

    soup = BeautifulSoup(html, "html.parser")
    builder = MarkdownBuilder()
    convert_node(soup, builder)
    md = builder.get_markdown()

    return md, builder.tables, builder.headings


# ── Table JSON Rendering ─────────────────────────────────────────────────────

def render_tables_json(tables):
    """Convert extracted tables to JSON-serializable structure."""
    result = []
    for t in tables:
        heading = t.get("heading", {})
        result.append({
            "heading": heading.get("text") if heading else None,
            "heading_level": heading.get("level") if heading else None,
            "columns": len(t["rows"][0]) if t["rows"] else 0,
            "row_count": len(t["rows"]),
            "rows": t["rows"],
        })
    return result


# ── Heading Extraction ────────────────────────────────────────────────────────

def extract_section(md_text, heading_title):
    """Extract content from a heading until next heading of same or higher level."""
    lines = md_text.split("\n")
    target_level = None
    start_idx = None

    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if title.lower() == heading_title.lower():
                target_level = level
                start_idx = i
                break

    if start_idx is None:
        return None

    result_lines = []
    for i in range(start_idx, len(lines)):
        if i > start_idx:
            m = re.match(r"^(#{1,6})\s+(.+)$", lines[i])
            if m and len(m.group(1)) <= target_level:
                break
        result_lines.append(lines[i])

    return "\n".join(result_lines).strip()


# ── Attachment Extraction ─────────────────────────────────────────────────────

def extract_attachments(page_data):
    """Extract attachment list from page data."""
    children = page_data.get("children", {})
    attachments = children.get("attachment", {})
    results = attachments.get("results", [])
    out = []
    for a in results:
        ext = a.get("extensions", {})
        out.append({
            "id": a.get("id"),
            "title": a.get("title"),
            "fileName": ext.get("fileName", a.get("title", "")),
            "fileSize": ext.get("fileSize", 0),
            "mediaType": ext.get("mediaType", ""),
            "downloadUrl": urljoin(BASE_URL, a.get("_links", {}).get("download", "")),
        })
    return out


# ── Truncation ────────────────────────────────────────────────────────────────

def apply_truncation(md_text, headings, max_chars=MAX_CHARS):
    """Apply truncation with heading hints."""
    total = len(md_text)
    if total <= max_chars:
        return {
            "content": md_text,
            "truncated": False,
            "total_chars": total,
        }

    truncated = md_text[:max_chars]
    last_newline = truncated.rfind("\n\n")
    if last_newline > max_chars * 0.8:
        truncated = truncated[:last_newline]

    heading_list = [h["text"] for h in headings]

    return {
        "content": truncated.strip(),
        "truncated": True,
        "delivered_chars": len(truncated),
        "total_chars": total,
        "hint": "Use get_page_section() to fetch specific sections. Available headings:",
        "available_headings": heading_list,
    }


# ── MCP Tools ─────────────────────────────────────────────────────────────────


@mcp.tool()
def get_page(page_id: str, include_attachments: bool = True, include_tables: bool = True) -> str:
    """Get Confluence page content as Markdown with truncation metadata.

    Args:
        page_id: Confluence page ID (numeric string, e.g. "672887405")
        include_attachments: Include attachment list at end (default true); set false to save tokens
        include_tables: Include tables rendered as Markdown in content (default true); set false to exclude
    Returns:
        JSON with: content (Markdown), truncated (bool), total_chars, tables (structured JSON),
        attachments, page metadata.
    """
    page = api("GET", f"/content/{page_id}?expand=body.storage,version,space,children.attachment")

    md_text, tables, headings = convert_page_body(page)

    # If tables disabled, remove markdown tables from content
    if not include_tables and tables:
        lines = md_text.split("\n")
        filtered = []
        skip = False
        for line in lines:
            stripped = line.strip()
            # Start of a markdown table: line starts with |
            if stripped.startswith("|") and not skip:
                skip = True
                continue
            if skip:
                # Separator line: |---|---|
                if re.match(r"^\|[\s\-:|]+\|$", stripped):
                    continue
                # Continue table rows
                if stripped.startswith("|"):
                    continue
                # No longer a table line
                skip = False
            # Skip blank lines that are table-adjacent (reduce excess whitespace)
            if not filtered and stripped == "":
                continue
            filtered.append(line)
        md_text = "\n".join(filtered)

    # Attachment list
    attachment_list = extract_attachments(page) if include_attachments else []

    # Truncation
    result = apply_truncation(md_text, headings)

    # Tables as structured JSON (always included, lightweight)
    result["tables"] = render_tables_json(tables)

    # Attachments
    if include_attachments:
        result["attachments"] = attachment_list
        result["attachment_count"] = len(attachment_list)

    # Page metadata
    result["page_id"] = page.get("id")
    result["title"] = page.get("title")
    result["space"] = page.get("space", {}).get("key", "")
    result["version"] = page.get("version", {}).get("number", 0)

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def get_page_section(page_id: str, heading: str) -> str:
    """Fetch a single section from a Confluence page by heading title (case-insensitive).

    Args:
        page_id: Confluence page ID (numeric string, e.g. "91428198")
        heading: Exact heading text (case-insensitive), e.g. "Изменения FE"
    Returns:
        JSON with section content if found, or list of available headings.
    """
    page = api("GET", f"/content/{page_id}?expand=body.storage,version,space")

    md_text, _, headings = convert_page_body(page)

    section = extract_section(md_text, heading)
    if section is None:
        return json.dumps({
            "found": False,
            "searched_heading": heading,
            "available_headings": [h["text"] for h in headings],
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "found": True,
        "heading": heading,
        "content": section,
        "total_chars": len(section),
        "page_id": page.get("id"),
        "page_title": page.get("title"),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def get_page_tables(page_id: str) -> str:
    """Extract only tables from a Confluence page as structured JSON (no content).

    Args:
        page_id: Confluence page ID (numeric string)
    Returns:
        JSON array of tables, each with heading context, columns, row_count, rows.
    """
    page = api("GET", f"/content/{page_id}?expand=body.storage")

    _, tables, _ = convert_page_body(page)

    return json.dumps({
        "page_id": page.get("id"),
        "page_title": page.get("title"),
        "table_count": len(tables),
        "tables": render_tables_json(tables),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def get_page_metadata(page_id: str) -> str:
    """Get lightweight metadata for a Confluence page — no content, always fast.

    Args:
        page_id: Confluence page ID (numeric string)
    Returns:
        JSON with title, version, author, dates, space, labels, url.
    """
    page = api("GET", f"/content/{page_id}?expand=version,space,metadata.labels,history")

    history = page.get("history", {})
    created_by = history.get("createdBy", {})
    version = page.get("version", {})
    last_updated_by = version.get("by", {})

    metadata = page.get("metadata", {})
    labels = []
    for l in metadata.get("labels", {}).get("results", []):
        labels.append(l.get("name", ""))

    return json.dumps({
        "id": page.get("id"),
        "title": page.get("title"),
        "type": page.get("type"),
        "status": page.get("status"),
        "space": page.get("space", {}).get("key", ""),
        "space_name": page.get("space", {}).get("name", ""),
        "version": version.get("number", 0),
        "created_date": history.get("createdDate", ""),
        "created_by": created_by.get("displayName", ""),
        "last_updated": version.get("when", ""),
        "last_updated_by": last_updated_by.get("displayName", ""),
        "labels": labels,
        "url": urljoin(BASE_URL, page.get("_links", {}).get("webui", "")),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def search_pages(query: str, space_key: str = None, limit: int = 10) -> str:
    """Search Confluence pages by text using CQL (Confluence Query Language).

    Args:
        query: Text to search for in title and content
        space_key: Optional space key to limit search, e.g. "DEV"
        limit: Max results (default 10, max 50)
    Returns:
        JSON with matching pages: id, title, space, excerpt, url.
    """
    limit = min(limit, 50)
    cql = f'text ~ "{query}"'
    if space_key:
        cql += f' AND space = "{space_key}"'
    cql += " AND type = page ORDER BY lastModified DESC"

    result = api("GET", f"/content/search?cql={requests.utils.quote(cql)}&limit={limit}&expand=space")

    results = result.get("results", [])
    pages = []
    for r in results:
        excerpt = r.get("excerpt", "")
        excerpt = re.sub(r"@@@(end)?hl@@@", "", excerpt)
        excerpt = re.sub(r"<[^>]+>", "", excerpt).strip()
        pages.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "space": r.get("space", {}).get("key", ""),
            "url": urljoin(BASE_URL, r.get("_links", {}).get("webui", "")),
            "excerpt": excerpt[:300] if excerpt else "",
        })

    return json.dumps({
        "query": query,
        "total": result.get("size", len(pages)),
        "results": pages,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def get_page_children(page_id: str, limit: int = 25) -> str:
    """Get direct child pages of a Confluence page.

    Args:
        page_id: Parent page ID (numeric string)
        limit: Max results (default 25, max 50)
    Returns:
        JSON with child pages: id, title, space, url.
    """
    limit = min(limit, 50)
    result = api("GET", f"/content/{page_id}/child/page?limit={limit}&expand=space")

    results = result.get("results", [])
    children = []
    for r in results:
        children.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "space": r.get("space", {}).get("key", ""),
            "url": urljoin(BASE_URL, r.get("_links", {}).get("webui", "")),
        })

    return json.dumps({
        "page_id": page_id,
        "count": len(children),
        "children": children,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def get_page_attachments(page_id: str) -> str:
    """Get the list of attachments on a Confluence page (explicit, when needed).

    Args:
        page_id: Confluence page ID (numeric string)
    Returns:
        JSON with attachments: id, fileName, fileSize, mediaType, downloadUrl.
    """
    page = api("GET", f"/content/{page_id}?expand=children.attachment")
    attachments = extract_attachments(page)

    return json.dumps({
        "page_id": page.get("id"),
        "page_title": page.get("title"),
        "count": len(attachments),
        "attachments": attachments,
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
