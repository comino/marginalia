"""MCP tools: review a pull request or git diff with the pen."""

from __future__ import annotations

import asyncio
import re
import subprocess
import uuid
from typing import List, Optional

from mcp.types import ToolAnnotations

from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.workflows import cloud, handwriting
from remarkable_mcp.workflows.code_review import (
    collect_comments,
    comment_body,
    github_event,
    parse_diff,
    read_verdict,
    render_diff,
)
from remarkable_mcp.workflows.ink import load_document_ink_from_zip
from remarkable_mcp.workflows.state import Store, now_iso, slugify

DEFAULT_FOLDER = "/Review/Code"
MAX_DIFF_LINES = 4000

_SEND = ToolAnnotations(
    title="Send Code Review to Tablet",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
_COLLECT = ToolAnnotations(
    title="Collect Code Review Comments",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


def _store() -> Store:
    return Store("code-reviews")


_PR = re.compile(r"^(\d+|https://github\.com/[\w.-]+/[\w.-]+/pull/\d+)$")
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_REF = re.compile(r"[\w./@{}^~+-]+")


def _get_diff(pr, repo, repo_path, base, paths) -> str:
    """Fetch the diff. Every user-supplied value is validated so none can be
    read as an option by git/gh (e.g. base="--output=/some/file")."""
    if pr is not None:
        if not _PR.fullmatch(str(pr)):
            raise ValueError("pr must be a number or a GitHub pull request URL.")
        cmd = ["gh", "pr", "diff", str(pr)]
        if repo:
            if not _REPO.fullmatch(repo):
                raise ValueError("repo must look like owner/name.")
            cmd += ["-R", repo]
    else:
        if not repo_path:
            raise ValueError("Pass pr= (GitHub) or repo_path= (local git diff).")
        from remarkable_mcp.workflows.safety import check_local_dir

        repo_path = str(check_local_dir(repo_path, "repository"))
        if not _REF.fullmatch(base) or base.startswith("-"):
            raise ValueError(f"Not a valid git ref: {base!r}")
        cmd = [
            "git",
            "-C",
            repo_path,
            "-c",
            "core.quotePath=false",
            "diff",
            "--no-color",
            "--no-textconv",
            "--no-ext-diff",
            f"{base}...HEAD",
        ]
        if paths:
            cmd += ["--", *paths]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=repo_path or None)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"{cmd[0]} failed")
    return res.stdout


async def remarkable_code_review_send(
    pr: Optional[str] = None,
    repo: Optional[str] = None,
    repo_path: Optional[str] = None,
    base: str = "origin/main",
    paths: Optional[List[str]] = None,
    title: Optional[str] = None,
    folder: str = DEFAULT_FOLDER,
) -> str:
    """
    <usecase>Send a pull request or git diff to the tablet for review with the pen.</usecase>
    <instructions>
    Renders the diff with line numbers (added lines shaded, removed lines grey)
    and a note margin, plus Approve / Request changes / Comment boxes at the
    end. Strike through code to ask for removal, circle or underline and write
    a note to comment, or write in the margin next to a line.
    remarkable_code_review_collect turns the marks into GitHub review comments
    on the exact path/line.
    Diff source: `pr` (via the gh CLI; `repo` = owner/name, or run from
    `repo_path`) or a local range `base...HEAD` in `repo_path`.
    </instructions>
    <parameters>
    - pr: Pull request number or URL.
    - repo: owner/name for gh (optional when repo_path is a clone).
    - repo_path: Local clone (for local diffs, or as gh's working directory).
    - base: Base ref for local diffs (default "origin/main").
    - paths: Limit a local diff to these paths.
    - title: Document title (default derived from the PR / range).
    - folder: Tablet folder (default "/Review/Code").
    </parameters>
    <examples>
    - remarkable_code_review_send(pr="42", repo="comino/remarkable-mcp")
    - remarkable_code_review_send(repo_path="/home/me/app", base="main")
    </examples>
    """
    if not cloud.is_cloud():
        return make_error("unsupported_transport", "Uploads need cloud mode.", "Use cloud mode.")
    try:
        diff = await asyncio.to_thread(_get_diff, pr, repo, repo_path, base, paths)
    except Exception as exc:
        return make_error("diff_failed", str(exc), "Check the PR number / repository.")
    files = parse_diff(diff)
    if not files:
        return make_error("empty_diff", "The diff has no changes.", "Check base / PR.")
    n_lines = sum(len(lines) for f in files for _, lines in f.hunks)
    if n_lines > MAX_DIFF_LINES:
        return make_error(
            "diff_too_large",
            f"{n_lines} diff lines is too much for paper review.",
            "Pass paths=[...] to review part of it.",
        )
    label = title or (f"PR {pr}" + (f" · {repo}" if repo else "") if pr else f"{base}...HEAD")
    subtitle = f"{len(files)} files"

    def work():
        with cloud.MUPDF_LOCK:
            rendered = render_diff(label, files, subtitle)
        doc = cloud.upload_pdf(rendered.pdf, f"Code review · {label}"[:90], folder)
        return rendered, doc

    try:
        rendered, doc = await asyncio.to_thread(work)
    except Exception as exc:
        return make_error("send_failed", str(exc), "Check remarkable_status().")
    review_id = f"{slugify(label, 40)}-{uuid.uuid4().hex[:4]}"
    _store().put(
        review_id,
        {
            "id": review_id,
            "title": label,
            "pr": pr,
            "repo": repo,
            "repo_path": repo_path,
            "doc_id": doc.id,
            "sent_at": now_iso(),
            "ink_at_send": cloud.EMPTY_INK,
            "rows": rendered.rows,
            "verdict_areas": [
                {"field": a.field_id, "option": a.option, "page": a.page, "rect": list(a.rect)}
                for a in rendered.verdict_areas
            ],
            "seen_strokes": [],
        },
    )
    return make_response(
        {
            "review": review_id,
            "pages": rendered.page_count,
            "files": rendered.files,
            "changed_lines": rendered.changed_lines,
        },
        f"On the tablet in {folder}. Later: remarkable_code_review_collect('{review_id}').",
    )


async def remarkable_code_review_collect(
    review: str, only_new: bool = True, include_images: bool = False, mark_seen: bool = True
):
    """
    <usecase>Turn pen marks on a code review into GitHub review comments.</usecase>
    <instructions>
    Returns comments as {path, line, side, body} (the GitHub pull request
    review API format) plus the ticked verdict and the matching review
    "event". Post them with the gh CLI, e.g.
    gh api repos/OWNER/REPO/pulls/N/reviews --input review.json
    where review.json = {"event": ..., "body": ..., "comments": [...]}.
    Handwriting away from any code line (e.g. a summary under the diff) is
    returned as "general" remarks for the review body; ticks on the verdict
    boxes are the verdict, never comments.
    Notes without a handwriting backend have body placeholders: call again
    with include_images=true, only_new=false, mark_seen=false and write the
    bodies from the crops.
    </instructions>
    <parameters>
    - review: Id from remarkable_code_review_send.
    - only_new: Only marks not returned before (default true).
    - include_images: Attach crops of handwritten notes.
    - mark_seen: Remember returned marks (default true); false to peek.
    </parameters>
    """
    store = _store()
    record = store.get(review)
    if record is None:
        return make_error("review_not_found", f"No code review '{review}'.", "Check the id.")

    def work():
        c = cloud.client()
        cloud.refresh(c)
        doc = cloud.find_by_id(c, record["doc_id"])
        if doc is None:
            raise LookupError(record["title"])
        zip_bytes = cloud.download_zip(c, doc)
        with cloud.MUPDF_LOCK:
            ink = load_document_ink_from_zip(zip_bytes)
        pages = {p.pdf_page + 1: p for p in ink.pages if p.pdf_page is not None}
        comments, general = collect_comments(pages, record["rows"], exclude=record["verdict_areas"])
        verdict = read_verdict(record["verdict_areas"], pages)
        seen = set(record.get("seen_strokes", []))
        if only_new:  # stroke-level: a note added later to a seen mark is new again
            comments = [c for c in comments if not set(c.seen_keys) <= seen]
            general = [c for c in general if not set(c.seen_keys) <= seen]
        everything = comments + general
        crops = [
            handwriting.render_strokes_png(c.note_strokes, c.note_rect) if c.note_strokes else None
            for c in everything
        ]
        todo = [i for i, cr in enumerate(crops) if cr is not None]
        texts = handwriting.transcribe_many(
            [crops[i] for i in todo], strokes=[everything[i].note_strokes for i in todo]
        )
        text_at = {i: t for i, (t, _) in zip(todo, texts)}
        return doc, comments, general, verdict, crops, text_at

    try:
        doc, comments, general, verdict, crops, text_at = await asyncio.to_thread(work)
    except LookupError:
        return make_error("document_missing", "The review document is gone.", "Send it again.")
    except Exception as exc:
        return make_error("collect_failed", str(exc), "Check remarkable_status().")

    out, images = [], []
    for i, c in enumerate(comments):
        note = text_at.get(i)
        item = {
            "id": c.mark_id,
            "path": c.path,
            "line": c.line,
            "side": c.side,
            "body": comment_body(c, note),
            "mark": c.kind,
        }
        if c.note_strokes:
            item["note_status"] = "transcribed" if note else "not_transcribed"
            if include_images:
                images.append((c.mark_id, f"note on {c.path}:{c.line}", crops[i]))
        out.append(item)
    remarks = []
    for j, c in enumerate(general, start=len(comments)):
        if not c.note_strokes:
            continue  # a stray tick or line away from the code carries no message
        remarks.append({"id": c.mark_id, "note": text_at.get(j)})
        if include_images:
            images.append((c.mark_id, "general remark", crops[j]))

    def merge(cur):
        if cur is None:
            return None
        if mark_seen:
            seen = set(cur.get("seen_strokes", []))
            seen.update(k for c in comments + general for k in c.seen_keys)
            cur["seen_strokes"] = sorted(seen)
            cur["last_read"] = {"at": now_iso(), "ink": cloud.ink_token(doc)}
        return cur

    store.update(review, merge)
    body = "Reviewed on paper (reMarkable)."
    written = [r["note"] for r in remarks if r["note"]]
    if written:
        body += "\n\n" + "\n\n".join(written)
    gh = {
        "event": github_event(verdict),
        "body": body,
        "comments": [{k: c[k] for k in ("path", "line", "side", "body")} for c in out],
    }
    hint = f"{len(out)} comment(s), verdict: {verdict or 'none ticked'}."
    if record.get("pr") and record.get("repo"):
        hint += (
            f" To post: gh api repos/{record['repo']}/pulls/{record['pr']}/reviews "
            "--input <file with the 'github' object>."
        )
    if any(c.get("note_status") == "not_transcribed" for c in out) and not include_images:
        hint += (
            " Some notes are untranscribed: call again with include_images=true, "
            "only_new=false, mark_seen=false to read them."
        )
    payload = make_response(
        {"review": review, "verdict": verdict, "comments": out, "general": remarks, "github": gh},
        hint,
    )
    return cloud.with_images(payload, images) if images else payload


def register(mcp, write_enabled: bool) -> None:
    mcp.tool(annotations=_COLLECT)(remarkable_code_review_collect)
    if write_enabled:
        mcp.tool(annotations=_SEND)(remarkable_code_review_send)
