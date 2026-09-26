# TRMNL display tools

Marginalia also ships a small, independent tool set for a
[TRMNL](https://trmnl.com) e-ink display (800×480, black and white). It lives
in `remarkable_mcp/trmnl/` and shares nothing with the reMarkable side except
the MCP server it is registered on. Nothing updates the display on its own:
an agent decides what to show and calls these tools, e.g. after reading the
tablet, or for anything else entirely.

The tools are registered only when a TRMNL is configured on the machine.

## Tools

| Tool | What it does |
|---|---|
| `trmnl_status` | Config source, pushes left this hour, what the display shows |
| `trmnl_get` / `trmnl_dashboard` | The stored merge variables / the six slots as text |
| `trmnl_set_slots` | Update several of the six text slots in **one** push (others are kept) |
| `trmnl_set_slot` / `trmnl_clear_slots` | One slot / blank some or all slots |
| `trmnl_send` / `trmnl_send_list` / `trmnl_push` | Replace everything with a title + message / a list / raw variables |
| `trmnl_clear` | Wipe the display |
| `trmnl_image` | A full-screen image (needs the image plugin UUID) |

With `REMARKABLE_READ_ONLY=1` only the read tools are registered.

## Rules the tools enforce

- 6 slots, at most 3 lines per slot and 45 characters per line (longer lines
  are cut with `...`); no emoji (they render as garbage on e-ink).
- **12 pushes per hour** for the whole display, shared by every agent. Put
  several slots into one `trmnl_set_slots` call, and preview with
  `dry_run=true`, which costs no push. The quota is tracked in
  `~/.local/state/trmnl/`.
- The device itself fetches new content every few minutes (its refresh
  interval), so a push shows up with that delay.

## Configuration

`~/.config/trmnl/config.json` (mode 0600), or `$TRMNL_CONFIG`:

```json
{"plugin_uuid": "…", "image_plugin_uuid": "…"}
```

`TRMNL_PLUGIN_UUID` / `TRMNL_IMAGE_PLUGIN_UUID` override the file. The plugin
UUID is a write credential for the display: keep it out of MCP registrations,
logs and repositories (`trmnl_status` shows it masked).
