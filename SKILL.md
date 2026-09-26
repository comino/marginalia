---
name: remarkable
description: >
  Access reMarkable tablet documents, notebooks, PDFs, and EPUBs.
  Use when the user wants to read, search, browse, or extract text
  from their reMarkable tablet. Supports handwriting OCR, typed text,
  annotations, highlights, and page rendering to PNG/SVG.
  Triggers: "reMarkable", "tablet notes", "handwritten notes",
  "remarkable read", "remarkable search", "remarkable browse".
---

# reMarkable Tablet

Access your reMarkable tablet as a second brain. Browse, search, read,
and extract content from all documents including handwritten notes via OCR.

## Setup

Add to `openclaw.json`:

```json
{
  "mcpServers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--usb"]
    }
  }
}
```

USB mode is recommended — connect via USB and enable "USB web interface"
in tablet Settings → Storage. No subscription or developer mode needed.

### Alternative Modes

SSH (requires developer mode):
```json
{
  "mcpServers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--ssh"]
    }
  }
}
```

Cloud (requires Connect subscription):
```json
{
  "mcpServers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp"],
      "env": {
        "REMARKABLE_TOKEN": "your-token"
      }
    }
  }
}
```

### OCR Support

For handwriting OCR, add a Google Vision API key:
```json
{
  "mcpServers": {
    "remarkable": {
      "command": "uvx",
      "args": ["remarkable-mcp", "--usb"],
      "env": {
        "GOOGLE_VISION_API_KEY": "your-api-key"
      }
    }
  }
}
```

## Tools

| Tool | Purpose |
|------|---------|
| `remarkable_read` | Read document content with text extraction, pagination, grep |
| `remarkable_browse` | Browse folders, search by name, filter by tags |
| `remarkable_search` | Multi-document content search with OCR |
| `remarkable_recent` | List recently modified documents |
| `remarkable_image` | Render pages as PNG or SVG |
| `remarkable_status` | Check connection and device info |
| `remarkable_annotations` | What the pen marks on a document mean (strike, underline, circle, notes) |
| `remarkable_review_send` | Send a Markdown draft to the tablet for pen review |
| `remarkable_review_collect` | Get the review's marks as change requests with source line numbers |
| `remarkable_review_list` | See which reviews have new marks |
| `remarkable_ask` / `remarkable_form_*` | Questions and forms answered with ticks on the tablet |
| `remarkable_inbox*` | Handwritten requests in an "Agent Inbox" become agent tasks |

## Workflow

1. Use `remarkable_browse` or `remarkable_search` to find documents
2. Use `remarkable_read` to extract text content
3. Use `remarkable_image` to render visual pages
4. Use `page` parameter to paginate through multi-page documents

## Review workflow

1. `remarkable_review_send(source_path="/path/draft.md")` uploads a review PDF to `/Review`
2. The user marks it up with the pen (strike = delete, circle/underline + note = change)
3. `remarkable_review_collect("<review>")` returns change requests with `src_line`
4. Edit the draft, then `remarkable_review_send(source_path=..., responses=[{id, status, reply}])`

