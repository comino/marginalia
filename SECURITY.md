# Security

Marginalia runs with your rights and is driven by agents that read untrusted
content (web pages, handwritten requests, documents). It is built so that a
prompt-injected agent cannot use it to read your secrets or reach your network.

## What is protected

- **Local files** a tool is asked to read (drafts, PDFs, images) must be regular
  files with an allowed suffix and under a size cap, outside sensitive locations
  (`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config`, `~/.rmapi`, `/proc`, `/etc`, …).
  Set `REMARKABLE_ALLOWED_ROOTS` to restrict reads to specific directories.
- **URLs** (`remarkable_clip`) must be http(s); hosts resolving to loopback,
  private, link-local, carrier-grade NAT / tailnet (`100.64.0.0/10`) or other
  non-public addresses are refused, and every redirect hop is re-checked.
- **Write access** to the tablet and the TRMNL display can be switched off with
  `REMARKABLE_READ_ONLY=1`; destructive tools ask for confirmation.
- **Credentials** stay on your machine: the reMarkable token in `~/.rmapi`, the
  TRMNL plugin UUID in `~/.config/trmnl/config.json` (both mode 0600). They are
  never logged, returned by a tool, or needed in the MCP registration.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting
(**Security → Report a vulnerability** on this repository) instead of a public
issue. Include what an attacker controls (a web page, a document, a tool
argument), what they gain, and the smallest reproduction you have. You'll get a
first response within a week.
