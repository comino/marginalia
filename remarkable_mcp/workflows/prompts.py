"""MCP prompts that walk an agent through the tablet workflows."""

from remarkable_mcp.server import mcp


def _user(text: str) -> list:
    return [{"role": "user", "content": text}]


@mcp.prompt(
    name="tablet_check_in",
    title="What's New on My Tablet",
    description="Process everything waiting on the tablet: reviews, answers, inbox, highlights",
)
def tablet_check_in_prompt() -> list:
    return _user(
        "Call remarkable_whats_new(). For every item that needs attention, call the tool named "
        "in its 'next' field and handle the result: apply review change requests to the draft "
        "and send the next version, act on answered forms, carry out pending inbox requests and "
        "acknowledge them with remarkable_inbox_done (with short replies), and summarise new "
        "reading highlights. Finish with a short list of what you did."
    )


@mcp.prompt(
    name="review_draft",
    title="Send a Draft for Pen Review",
    description="Send a Markdown draft to the tablet and later apply the handwritten feedback",
)
def review_draft_prompt(source_path: str) -> list:
    return _user(
        f"Send {source_path} for review with remarkable_review_send(source_path=...). "
        "Tell me the document name. When I say I'm done, call remarkable_review_collect, "
        "apply each change request to the file (use src_line/src_lines; 'delete' removes the "
        "target text, 'replace' uses the note as the replacement, 'change'/'comment' are "
        "instructions to follow), then send the next version with responses listing what you "
        "did for each request id."
    )


@mcp.prompt(
    name="ask_on_tablet",
    title="Ask Me on the Tablet",
    description="Put a decision on the tablet as a tick-box question",
)
def ask_on_tablet_prompt(question: str) -> list:
    return _user(
        f"Ask me this on the tablet with remarkable_ask: {question!r}. Offer 2-4 short options "
        "if it is not a yes/no question, and put the background I need into context. Later, "
        "read the answer with remarkable_form_read."
    )
