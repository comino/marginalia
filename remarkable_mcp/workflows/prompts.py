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


@mcp.prompt(
    name="triage_on_paper",
    title="Triage on Paper",
    description="Put open issues/PRs on the tablet as a tick sheet, then apply the decisions",
)
def triage_on_paper_prompt(source: str = "my open Linear issues") -> list:
    return _user(
        f"Collect {source} (use the relevant tools, e.g. Linear or gh). Send them with "
        "remarkable_triage_send: one row per item (id, short title, one-line subtitle with type/"
        "priority), options fitting the source (e.g. Now / Next / Later / Close). Tell me the "
        "document name. When I say I'm done, read it with remarkable_form_read, apply every "
        "decision in the source system, turn remarks next to a row into comments on that item, "
        "and report what changed. Items I skipped stay untouched."
    )


@mcp.prompt(
    name="meeting_pack",
    title="Meeting Pack",
    description="Agenda on the tablet with note space per item; afterwards file notes and actions",
)
def meeting_pack_prompt(meeting: str) -> list:
    return _user(
        f"Prepare a meeting pack for: {meeting}. Build the agenda (ask me if unclear), then send "
        "it with remarkable_form_send: a heading, an info field with the goal, and per agenda "
        "item one text field (4-6 lines) labelled with the topic; end with a text field "
        "'Decisions' and one 'Action items (who · what · when)'. After the meeting, read it with "
        "remarkable_form_read (include_images=true if notes are untranscribed), summarise per "
        "item, and propose the action items as tasks in the right tracker for my approval."
    )


@mcp.prompt(
    name="research_on_paper",
    title="Research from the Inbox",
    description="Answer #research questions written in the Agent Inbox with a cited brief",
)
def research_on_paper_prompt() -> list:
    return _user(
        "Call remarkable_inbox(). For each pending entry tagged #research (or ending with '?'), "
        "research the question with the tools you have, write a short brief with sources as "
        "Markdown, send it with remarkable_clip(markdown=..., title=...) so my highlights on it "
        "come back via remarkable_reading_notes, and acknowledge the entry "
        "with remarkable_inbox_done, replying with the document name."
    )


@mcp.prompt(
    name="daily_ink_digest",
    title="Daily Ink Digest",
    description="Summarise and route everything I wrote on the tablet today",
)
def daily_ink_digest_prompt() -> list:
    return _user(
        "Call remarkable_ink_digest(since_hours=24). Summarise what I wrote per document in a few "
        "bullets, flag anything that looks like a task, idea or question, and suggest where each "
        "belongs (project, tracker, notes). If handwriting is untranscribed, read it with "
        "include_images=true first. Don't move or change anything on the tablet."
    )
