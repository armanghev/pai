"""Reference drafts of assignments, on request. Sonnet writes; the file is labeled as a sample."""

from __future__ import annotations

import base64
import html
import io
import re
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from pai import prompts
from pai.canvas import Assignment, CanvasClient, parse_ts
from pai.links import MAX_BYTES, faithful_text_and_links

LABEL = "REFERENCE DRAFT: a worked example to study from, not for submission."


PAGE_LINK = re.compile(r"/(courses|groups)/(\d+)/pages/([^/?#\s]+)")
GROUP_TOPIC = re.compile(r"/groups/(\d+)/discussion_topics/(\d+)")
FILE_LINK = re.compile(r"/files/(\d+)")
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # the API's per-image limit
MAX_LINKED = 5
# Attachments seen in discussion threads, by file id. Classmates' files 403 on
# /api/v1/files/<id>, but the thread hands out signed download links that work.
_SEEN_FILES: OrderedDict[int, dict] = OrderedDict()
MAX_SEEN_FILES = 500  # linked pages and files followed per assignment
URL_ID = re.compile(r"/courses/(\d+)/(assignments|quizzes|discussion_topics)/(\d+)")
STOP = {"the", "a", "an", "for", "my", "of", "on", "to", "assignment", "draft", "one", "that"}


def normalize(text: str) -> str:
    """Lowercase, punctuation to spaces, and 'cs/it' -> 'csit' so course codes line up."""
    text = re.sub(r"\b([a-z]{1,4})\s*/\s*([a-z]{1,4})\b", r"\1\2", text.lower())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _score(a: Assignment, words: list[str]) -> float:
    haystack = normalize(f"{a.course} {a.title}").split()
    hay = set(haystack)
    got = 0.0
    for w in words:
        if w in hay:
            got += 1
        elif not w.isdigit() and len(w) >= 3 and any(h.startswith(w) for h in hay):
            got += 0.7  # "conv" ~ "converter"
        elif w.isdigit():
            return 0.0  # a number that doesn't match (assignment 6 vs 7) rules it out
    return got / len(words)


def find(items: list[Assignment], query: str, now: datetime | None = None) -> list[Assignment]:
    """Best matches for a link, an exact name or a loose description; best first."""
    now = now or datetime.now(UTC)
    m = URL_ID.search(query)
    if m:
        kind, num = m.group(2), m.group(3)
        hits = [a for a in items if a.html_url.rstrip("/").endswith(f"/{kind}/{num}")]
        if kind == "assignments":
            hits = hits or [a for a in items if str(a.id) == num]
        if hits:
            return hits
        query = URL_ID.sub(" ", query)
    words = [w for w in normalize(query).split() if w not in STOP]
    if not words:
        return []
    scored = [(s, a) for a in items if (s := _score(a, words)) >= 0.6]
    far = datetime.max.replace(tzinfo=UTC)

    def rank(pair):
        s, a = pair
        upcoming = a.due_at is not None and a.due_at >= now
        return (-round(s, 2), not upcoming, abs(((a.due_at or far) - now).total_seconds()))

    return [a for _, a in sorted(scored, key=rank)]


def relative_due(a: Assignment, now: datetime, tz) -> str:
    """'due today (Sat 10/10 23:59)', 'due tomorrow (...)', 'due in 3 days (...)', 'was due ...'.
    Spelled out in code because small models slip on date arithmetic."""
    if a.due_at is None:
        return "no due date"
    local = a.due_at.astimezone(tz)
    days = (local.date() - now.astimezone(tz).date()).days
    when = f"({local:%a %m/%d %H:%M})"
    if days == 0:
        return f"due today {when}" if a.due_at >= now else f"was due earlier today {when}"
    if days == 1:
        return f"due tomorrow {when}"
    if days > 1:
        return f"due in {days} days {when}"
    return f"was due {-days} day(s) ago {when}"


class Pick(BaseModel):
    assignment_id: int | None


async def pick_with_model(agent, items: list[Assignment], query: str, now: datetime, tz):
    """Last resort for vague asks ("the essay due Friday"): Haiku picks from recent work."""
    recent = [
        a
        for a in items
        if a.due_at is None or now - timedelta(days=45) <= a.due_at <= now + timedelta(days=60)
    ]
    if not recent:
        return None
    listing = "\n".join(
        f"{a.id} | {a.course} | {a.title} | {relative_due(a, now, tz)}" for a in recent
    )
    system = (
        "Pick the one assignment the student means from the list, or null if none clearly "
        "fits. Use the current date for words like 'Friday' or 'next week'. A weekday "
        "with no other hint means the next upcoming one. If several fit, prefer the "
        "nearest upcoming assignment."
    )
    user = (
        f"Now: {now.astimezone(tz):%A %Y-%m-%d %H:%M}\nThey said: {query}\n\n"
        f"Assignments:\n{listing}"
    )
    pick = await agent.structured("chat", system, user, Pick, job="draft_lookup")
    return next((a for a in recent if a.id == pick.assignment_id), None)


class NoInstructions(LookupError):
    """Canvas returned no instructions; drafting from just a title would be a guess."""


@dataclass
class Brief:
    """What the draft writer gets: the instructions as text, plus any PDFs they link to."""

    text: str
    documents: list[dict] = field(default_factory=list)


def _docx_text(data: bytes) -> str:
    xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    lines = ["".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", p)) for p in paras]
    return html.unescape("\n".join(lines)).strip()


async def _linked(canvas: CanvasClient, links: list[str], brief: Brief) -> None:
    """Pull in Canvas pages and files the instructions link to; that's often where the
    real handout lives. Other links stay as URLs in the text."""
    seen: set[str] = set()
    for link in links:
        if len(seen) >= MAX_LINKED:
            break
        if link.startswith("http") and not link.startswith(canvas.base):
            continue
        page, file = PAGE_LINK.search(link), FILE_LINK.search(link)
        key = page.group(0) if page else file.group(0) if file else None
        if key is None or key in seen:
            continue
        seen.add(key)
        try:
            if page:
                ctx_kind, ctx_id, slug = page.groups()
                r = await canvas.http.get(
                    f"{canvas.base}/api/v1/{ctx_kind}/{ctx_id}/pages/{slug}",
                    headers=canvas.headers,
                )
                r.raise_for_status()
                j = r.json()
                body = faithful_text_and_links(j.get("body") or "")[0]
                brief.text += f"\n\nLinked page: {j.get('title') or slug}\n{body}"
            else:
                await _linked_file(canvas, file.group(1), brief)
        except Exception as e:  # one bad link shouldn't sink the draft
            brief.text += f"\n\n(Couldn't open linked {link}: {type(e).__name__})"


async def _linked_file(canvas: CanvasClient, file_id: str, brief: Brief) -> None:
    r = await canvas.http.get(f"{canvas.base}/api/v1/files/{file_id}", headers=canvas.headers)
    r.raise_for_status()
    await _add_file(canvas, r.json(), brief, "Linked file")


def _kind(f: dict) -> str:
    """A short file type for listings: 'PDF', 'Word', 'image', 'video' or the raw type."""
    ctype = (f.get("content-type") or "").lower()
    name = (f.get("display_name") or "").lower()
    if ctype == "application/pdf":
        return "PDF"
    if "wordprocessingml" in ctype or name.endswith((".docx", ".doc")):
        return "Word"
    return ctype.split("/")[0] if ctype.startswith(("image/", "video/", "audio/")) else ctype


def _size(n: int | None) -> str:
    if not n:
        return ""
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} KB"


def _remember(f: dict) -> None:
    if f.get("id") and f.get("url"):
        _SEEN_FILES[f["id"]] = f
        _SEEN_FILES.move_to_end(f["id"])
        while len(_SEEN_FILES) > MAX_SEEN_FILES:
            _SEEN_FILES.popitem(last=False)


async def file_meta(canvas: CanvasClient, file_id: int) -> dict:
    """A file's metadata: from a thread or listing already read (its signed link works for
    classmates' files), else from the files API."""
    meta = _SEEN_FILES.get(file_id)
    if meta is None:
        r = await canvas.http.get(f"{canvas.base}/api/v1/files/{file_id}", headers=canvas.headers)
        r.raise_for_status()
        meta = r.json()
    return meta


async def download(canvas: CanvasClient, meta: dict) -> bytes:
    got = await canvas.http.get(meta["url"], headers=canvas.headers, follow_redirects=True)
    got.raise_for_status()
    return got.content


async def file_brief(canvas: CanvasClient, file_id: int) -> Brief:
    """One Canvas file, read on request: a PDF as a document, .docx or text inline."""
    meta = await file_meta(canvas, file_id)
    brief = Brief("")
    await _add_file(canvas, meta, brief, "File")
    brief.text = brief.text.strip()
    return brief


async def _add_file(canvas: CanvasClient, meta: dict, brief: Brief, label: str) -> None:
    """Read a Canvas file into the brief: PDFs as documents, .docx and text inline."""
    _remember(meta)
    name = meta.get("display_name") or meta.get("filename") or f"file {meta.get('id')}"
    if meta.get("id") and label != "File":
        name += f" [file {meta['id']}]"
    ctype = (meta.get("content-type") or "").lower()
    if (meta.get("size") or 0) > MAX_BYTES or not meta.get("url"):
        brief.text += f"\n\n{label}: {name} (too big or not downloadable)"
        return
    if ctype.startswith(("video/", "audio/")):
        link = meta.get("preview_url") or meta.get("url")
        brief.text += f"\n\n{label}: {name} ({ctype}; can't watch or listen here: {link})"
        return
    got = await canvas.http.get(meta["url"], headers=canvas.headers, follow_redirects=True)
    got.raise_for_status()
    data = got.content
    if ctype == "application/pdf" or data[:5] == b"%PDF-":
        brief.documents.append(
            {
                "type": "document",
                "title": name,
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(data).decode(),
                },
            }
        )
        brief.text += f"\n\n{label}: {name} (attached as a PDF)"
    elif ctype in IMAGE_TYPES and len(data) <= MAX_IMAGE_BYTES:
        source = {"type": "base64", "media_type": ctype, "data": base64.b64encode(data).decode()}
        brief.documents.append({"type": "image", "source": source})
        brief.text += f"\n\n{label}: {name} (attached as an image)"
    elif name.lower().endswith(".docx"):
        brief.text += f"\n\n{label}: {name}\n{_docx_text(data)}"
    elif ctype.startswith("text/") or "html" in ctype:
        text = data.decode(errors="replace")
        text = faithful_text_and_links(text)[0] if "html" in ctype else text
        brief.text += f"\n\n{label}: {name}\n{text}"
    else:
        brief.text += f"\n\n{label}: {name} ({ctype or 'unknown type'}, not readable here)"


async def details(canvas: CanvasClient, a: Assignment, pasted: str = "") -> Brief:
    """The assignment's full instructions as plain text: the description, any Canvas pages
    and files it links to, the discussion prompt for discussion posts, and the rubric."""
    r = await canvas.http.get(
        f"{canvas.base}/api/v1/courses/{a.course_id}/assignments/{a.id}",
        params={"include[]": "rubric"},
        headers=canvas.headers,
    )
    r.raise_for_status()
    j = r.json()
    body = j.get("description") or ""
    topic = j.get("discussion_topic") or {}
    if topic.get("message") and topic["message"] not in body:
        body += topic["message"]  # discussion posts keep their prompt on the topic
    desc, links = faithful_text_and_links(body)
    if not desc and not pasted:
        raise NoInstructions(a.title)
    lines = [
        f"Course: {a.course}",
        f"Assignment: {a.title}",
        f"Points: {a.points or 0:g}",
        f"Submission type: {', '.join(j.get('submission_types') or []) or 'unknown'}",
        "",
        "Instructions:",
        desc or "(none on Canvas; see the pasted instructions below)",
    ]
    if pasted:
        lines += ["", "Instructions pasted by the student:", pasted.strip()]
    brief = Brief("\n".join(lines))
    await _linked(canvas, links, brief)
    rubric = j.get("rubric") or []
    if rubric:
        out = ["", "", "Rubric:"]
        for c in rubric:
            extra = c.get("long_description") or ""
            out.append(f"- {c.get('description')} ({c.get('points') or 0:g} pts) {extra}".rstrip())
            for rt in c.get("ratings") or []:
                more = rt.get("long_description") or ""
                rating = f"  - {rt.get('points') or 0:g} pts: {rt.get('description') or ''}"
                out.append(f"{rating} {more}".rstrip())
        brief.text += "\n".join(out)
    return brief


def _thread(entries: list[dict], me: int, names: dict, tz, parent: str = "") -> list[str]:
    """Every post and reply in a discussion, in thread order, with authors and local times;
    the student's own are "by you"."""
    out: list[str] = []
    for e in entries:
        mine = e.get("user_id") == me
        who = "you" if mine else names.get(e.get("user_id")) or "a classmate"
        if not e.get("deleted"):
            at = parse_ts(e.get("created_at"))
            when = f"{at.astimezone(tz):%a %m/%d %H:%M}" if at else "undated"
            head = f"Reply to {parent}" if parent else "Reply" if e.get("parent_id") else "Post"
            text = faithful_text_and_links(e.get("message") or "")[0]
            files = e.get("attachments") or ([e["attachment"]] if e.get("attachment") else [])
            for f in files:  # listed, not downloaded: Pai opens the ones it needs
                _remember(f)
                info = [f"file {f['id']}"] if f.get("id") else []
                info += [t for t in [_kind(f), _size(f.get("size"))] if t]
                text += f"\n[attached: {f.get('display_name') or 'a file'} ({', '.join(info)})]"
            out.append(f"{head} by {who} ({when}):\n{text}")
        out += _thread(e.get("replies") or [], me, names, tz, who)
    return out


async def _discussion_view(canvas: CanvasClient, course_id: int, topic: dict) -> dict:
    """The thread for a discussion. Group discussions keep their posts on each group's own
    copy of the topic, so read the copy for a group the student is in."""
    path = f"courses/{course_id}/discussion_topics/{topic['id']}"
    children = topic.get("group_topic_children") or []
    if children:
        r = await canvas.http.get(f"{canvas.base}/api/v1/users/self/groups", headers=canvas.headers)
        r.raise_for_status()
        mine = {g.get("id") for g in r.json()}
        child = next((c for c in children if c.get("group_id") in mine), None)
        if child is None:
            return {"view": [], "note": "(you're not in any group for this discussion)"}
        path = f"groups/{child['group_id']}/discussion_topics/{child['id']}"
    r = await canvas.http.get(f"{canvas.base}/api/v1/{path}/view", headers=canvas.headers)
    r.raise_for_status()
    return r.json()


async def assignment_for_group_topic(canvas: CanvasClient, link: str) -> str | None:
    """Map a /groups/<g>/discussion_topics/<t> link to its course assignment's path, so the
    usual lookup (and the grade) work for group discussions."""
    m = GROUP_TOPIC.search(link)
    if not m:
        return None
    group_id, topic_id = m.groups()
    api = f"{canvas.base}/api/v1"
    r = await canvas.http.get(f"{api}/groups/{group_id}", headers=canvas.headers)
    r.raise_for_status()
    course_id = r.json().get("course_id")
    r = await canvas.http.get(
        f"{api}/groups/{group_id}/discussion_topics/{topic_id}", headers=canvas.headers
    )
    r.raise_for_status()
    topic = r.json()
    assignment_id = topic.get("assignment_id")
    if not assignment_id and topic.get("root_topic_id") and course_id:
        r = await canvas.http.get(
            f"{api}/courses/{course_id}/discussion_topics/{topic['root_topic_id']}",
            headers=canvas.headers,
        )
        r.raise_for_status()
        assignment_id = r.json().get("assignment_id")
    if not (assignment_id and course_id):
        return None
    return f"/courses/{course_id}/assignments/{assignment_id}"


async def submission(canvas: CanvasClient, a: Assignment, tz) -> Brief:
    """What the student turned in, read-only: text, files, link or discussion posts, plus
    the grade, rubric assessment and instructor comments."""
    base = f"{canvas.base}/api/v1/courses/{a.course_id}"
    r = await canvas.http.get(
        f"{base}/assignments/{a.id}", headers=canvas.headers, params={"include[]": "rubric"}
    )
    r.raise_for_status()
    assignment = r.json()
    r = await canvas.http.get(
        f"{base}/assignments/{a.id}/submissions/self",
        params={"include[]": ["submission_comments", "rubric_assessment"]},
        headers=canvas.headers,
    )
    r.raise_for_status()
    sub = r.json()
    brief = Brief(f"Your submission for {a.course} {a.title}:")
    when = parse_ts(sub.get("submitted_at"))
    if when is None and sub.get("workflow_state") in (None, "unsubmitted"):
        brief.text += "\nNothing submitted yet."
    status = [f"submitted {when.astimezone(tz):%a %m/%d %H:%M}" if when else "not submitted"]
    status += [w for w in ("late", "missing", "excused") if sub.get(w)]
    if (sub.get("attempt") or 0) > 1:
        status.append(f"attempt {sub['attempt']}")
    if sub.get("score") is not None:
        status.append(f"score {sub['score']:g}/{a.points or 0:g} (grade {sub.get('grade')})")
    elif sub.get("workflow_state") == "submitted":
        status.append("not graded yet")
    brief.text += f"\nStatus: {', '.join(status)}"

    kind = sub.get("submission_type")
    if sub.get("body"):
        brief.text += "\n\nSubmitted text:\n" + faithful_text_and_links(sub["body"])[0]
    if sub.get("url"):
        brief.text += f"\n\nSubmitted link: {sub['url']}"
    for f in (sub.get("attachments") or [])[:MAX_LINKED]:
        try:
            await _add_file(canvas, f, brief, "Submitted file")
        except Exception as e:  # one bad file shouldn't hide the rest
            brief.text += f"\n\n(Couldn't open submitted {f.get('display_name')}: {e})"
    topic = assignment.get("discussion_topic") or {}
    if topic.get("id") and kind in ("discussion_topic", None):
        view = await _discussion_view(canvas, a.course_id, topic)
        names = {p.get("id"): p.get("display_name") for p in view.get("participants") or []}
        # Entries posted after Canvas cached the thread come back in new_entries.
        entries = (view.get("view") or []) + (view.get("new_entries") or [])
        posts = _thread(entries, sub.get("user_id"), names, tz)
        mine = sum(p.split(" (", 1)[0].endswith(" by you") for p in posts)
        brief.text += f"\n\nDiscussion thread ({len(posts)} posts, {mine} by you):\n" + (
            "\n\n".join(posts) if posts else view.get("note") or "(no posts yet)"
        )
    if kind == "online_quiz":
        brief.text += "\n\n(Quiz answers aren't readable here; only the score.)"

    names = {c.get("id"): c.get("description") for c in assignment.get("rubric") or []}
    rated = sub.get("rubric_assessment") or {}
    if rated:
        brief.text += "\n\nRubric assessment:"
        for cid, got in rated.items():
            pts = got.get("points")
            line = f"\n- {names.get(cid, cid)}: {'?' if pts is None else f'{pts:g}'} pts"
            if got.get("comments"):
                line += f" ({got['comments']})"
            brief.text += line
    comments = sub.get("submission_comments") or []
    if comments:
        brief.text += "\n\nComments:"
        for c in comments:
            brief.text += (
                f"\n- {c.get('author_name') or 'someone'} ({(c.get('created_at') or '')[:10]}): "
                f"{c.get('comment') or ''}"
            )
    return brief


async def write(agent, brief: Brief, notes: str = "") -> str:
    system = prompts.render("draft.md", agent.config)
    text = brief.text + (f"\n\nExtra instructions from the student: {notes}" if notes else "")
    user: str | list = (
        [*brief.documents, {"type": "text", "text": text}] if brief.documents else text
    )
    body = await agent.complete("planner", system, user, job="draft")
    return f"> {LABEL}\n\n{body}\n"


def filename(title: str) -> str:
    name = " ".join(re.sub(r'[\\/:*?"<>|]+', " ", title).split())[:60] or "assignment"
    return f"Draft - {name}.md"
