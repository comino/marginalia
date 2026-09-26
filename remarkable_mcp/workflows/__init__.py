"""Workflow layer: tools that interpret ink instead of just exposing it.

The modules here sit on top of the transport/extraction core and turn raw
reMarkable data into compact, structured results a small agent can act on:

- ``ink``        pen strokes of a document, mapped into PDF point space
- ``marks``      stroke clustering + classification (underline, strike, circle...)
- ``review_pdf`` review-ready PDFs whose blocks map back to source lines
- ``state``      sidecar state (manifests, seen marks) kept outside the tablet
"""
