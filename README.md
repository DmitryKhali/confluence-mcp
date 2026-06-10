# confluence-mcp

MCP server for Confluence (Server / Data Center). Provides read access to pages, sections, tables, and search via the Confluence REST API v1.

> **Confluence Server / Data Center only.** Uses Personal Access Token (PAT) for authentication.

## Tools

| Tool | Description |
|---|---|
| `get_page` | Get page content as Markdown with `truncated` flag — agent always knows if the page was cut |
| `get_page_section` | Fetch a single heading section by title (case-insensitive), e.g. get 10 lines instead of 200K |
| `get_page_tables` | Extract only tables as structured JSON — skip irrelevant wide tables eating >30% tokens |
| `search_pages` | Search by CQL (Confluence Query Language) |
| `get_page_metadata` | Lightweight: title, version, author, dates, labels — no content, always fast |
| `get_page_children` | Direct child pages of a page |
| `get_page_attachments` | Explicit attachment list (opt-in — `include_attachments: false` skips it) |

### Key features

- **No silent truncation** — `truncated: true/false` + `available_headings` list so the agent can suggest `get_page_section()` as a follow-up
- **`include_tables: false`** — hides wide tables from Markdown (they remain in `tables` JSON field)
- **`include_attachments: false`** — skips the attachment list entirely, saving 5-10K tokens
- Section extraction stops at the next heading of same or higher level

## Setup

### 1. Install dependencies

Requires **Python 3.11+**.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Generate a Personal Access Token (PAT)

In Confluence: **your avatar → Settings → Personal Access Tokens → Create token**.

Copy the token — it's shown only once.

### 3. Configure environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `CONFLUENCE_BASE_URL` | ✅ | — | Your Confluence instance URL, e.g. `https://confluence.example.com` |
| `CONFLUENCE_TOKEN` | — | — | Personal Access Token (PAT). On macOS can use Keychain instead (see below) |
| `CONFLUENCE_KEYCHAIN_SERVICE` | — | `confluence_pat` | macOS Keychain service name (alternative to `CONFLUENCE_TOKEN`) |
| `CONFLUENCE_MAX_CHARS` | — | `50000` | Max characters before truncation kicks in |

**macOS Keychain alternative** — store the token once, no env var needed:
```bash
security add-generic-password -a $USER -s confluence_pat -w <your_token>
```

### 4. Add to MCP client config

With Keychain (recommended on macOS):
```json
{
  "mcpServers": {
    "confluence": {
      "command": "/path/to/confluence-mcp/venv/bin/python",
      "args": ["/path/to/confluence-mcp/server.py"],
      "env": {
        "CONFLUENCE_BASE_URL": "https://confluence.example.com"
      }
    }
  }
}
```

Or with env var token:
```json
{
  "mcpServers": {
    "confluence": {
      "command": "/path/to/confluence-mcp/venv/bin/python",
      "args": ["/path/to/confluence-mcp/server.py"],
      "env": {
        "CONFLUENCE_BASE_URL": "https://confluence.example.com",
        "CONFLUENCE_TOKEN": "your_token_here"
      }
    }
  }
}
```

## Notes

- Works with **Confluence Server / Data Center** (REST API v1). Not tested with Confluence Cloud.
- Content is converted from Confluence Storage Format (XML) to Markdown. Supports code blocks, info panels, expand macros, tables, and lists.
- Token is never logged or written to disk — retrieved from macOS Keychain at runtime via `security find-generic-password`.
