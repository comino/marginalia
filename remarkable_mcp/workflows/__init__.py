"""Workflow layer: tools that interpret ink instead of just exposing it.

It sits on top of the core (transports, documents, core tools) and turns raw
reMarkable data into compact, structured results a small agent can act on.
One subpackage per workflow: the engine next to the MCP ``*tools`` module.

- ``ink``        strokes in PDF point space (``page``), mark classification
                 (``marks``), handwriting recognition backends
- ``review``     pen review of Markdown drafts, code diffs and compiled LaTeX
- ``forms``      paper forms, questions, triage sheets, clarifications
- ``inbox``      handwritten requests -> agent tasks
- ``structure``  sketches -> diagrams, tables, wireframes, math, regions
- ``reading``    web articles to the tablet, highlights back as quotes
- ``live``       the near-live change watcher and the autopilot daemon

Shared here: ``tools`` (registers everything), ``overview`` (whats_new),
``prompts``, ``cloud`` (transport glue), ``state`` (sidecar state) and
``safety`` (parameter guards).
"""
