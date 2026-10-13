from datetime import date, datetime, time
from types import SimpleNamespace

import pytest
from conftest import usage

from pai import flashcards, links, photo
from pai.agent import Agent
from pai.config import Config

TZ = Config(timezone="America/Los_Angeles").tz
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=TZ)


def item(**kw):
    base = dict(title="Essay 2", date=date(2026, 10, 15), time=None, kind="due", course="ENGL")
    base.update(kw)
    return photo.Item(**base)


def test_remind_at_rules():
    assert photo.remind_at(item(), TZ, NOW) == datetime(2026, 10, 14, 18, 0, tzinfo=TZ)
    # Due tomorrow morning: the evening before has passed? No, it's still ahead.
    assert photo.remind_at(item(date=date(2026, 10, 13)), TZ, NOW).hour == 18
    # Due today: evening-before is past, so 9am today... also past at 10:00 -> None.
    assert photo.remind_at(item(date=date(2026, 10, 12)), TZ, NOW) is None
    ev = item(kind="event", time=time(14, 30), date=date(2026, 10, 12))
    assert photo.remind_at(ev, TZ, NOW) == datetime(2026, 10, 12, 13, 30, tzinfo=TZ)
    assert photo.remind_at(item(kind="task", date=date(2026, 10, 20)), TZ, NOW).hour == 9


def test_reminder_text():
    assert photo.reminder_text(item()) == "ENGL: Essay 2 is due Thu 10/15"
    ev = item(kind="event", title="Club fair", course=None, time=time(12))
    assert photo.reminder_text(ev) == "Club fair on Thu 10/15 at 12:00"


def test_photo_request_has_image_and_date():
    blocks = photo.request(b"\xff\xd8jpeg", "image/jpeg", "my syllabus", NOW, TZ)
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/jpeg"
    assert "Monday 2026-10-12" in blocks[1]["text"] and "my syllabus" in blocks[1]["text"]


@pytest.mark.parametrize(
    "text,url",
    [
        ("https://example.com/a", "https://example.com/a"),
        ("  summarize: https://example.com/a ", "https://example.com/a"),
        ("tldr https://example.com/a", "https://example.com/a"),
        ("what do you think of https://example.com/a", None),
        ("no link here", None),
    ],
)
def test_wants_summary(text, url):
    assert links.wants_summary(text) == url


def test_html_to_text_keeps_article_drops_chrome():
    html = """<html><head><title> My  Post </title><style>p{}</style></head><body>
    <nav>Home About Contact</nav><script>var x=1;</script>
    <p>This is the first real paragraph of the article, with enough words to count.</p>
    <p>Short.</p><footer>Copyright 2026 and some footer text that is long enough</footer>
    </body></html>"""
    title, body = links.html_to_text(html)
    assert title == "My Post"
    assert "first real paragraph" in body and "Short." in body
    assert "Home About" not in body and "var x" not in body and "Copyright" not in body


def test_sm2_schedule():
    ease, interval, reps = flashcards.schedule(2.5, 0, 0, 4)
    assert (interval, reps) == (1, 1)
    ease, interval, reps = flashcards.schedule(ease, interval, reps, 4)
    assert interval == 3
    ease, interval, reps = flashcards.schedule(ease, interval, reps, 5)
    assert interval >= 7
    assert flashcards.schedule(ease, interval, reps, 0)[1:] == (0, 0)
    assert flashcards.schedule(1.3, 1, 1, 0)[0] == 1.3


def test_deck_review_flow(conn):
    today = date(2026, 10, 12)
    cards = [flashcards.Card(front=f"Q{i}", back=f"A{i}") for i in range(3)]
    assert flashcards.save(conn, "CS135", cards, today) == 3
    first = flashcards.next_due(conn, today, "CS135")
    assert first["front"] == "Q0"
    assert flashcards.rate(conn, first["id"], "again", today) == 0
    # The failed card doesn't come straight back while others are due.
    assert flashcards.next_due(conn, today, "cs135", skip=first["id"])["front"] != "Q0"
    for _ in range(5):
        c = flashcards.next_due(conn, today)
        if c is None:
            break
        flashcards.rate(conn, c["id"], "good", today)
    assert flashcards.due_count(conn, today) == 0
    assert flashcards.next_due_date(conn) == "2026-10-13"
    d = flashcards.decks(conn, today)[0]
    assert (d["deck"], d["total"], d["due"]) == ("CS135", 3, 0)


async def test_make_cards_uses_haiku_structured(conn, config):
    calls = []

    async def parse(**kw):
        calls.append(kw)
        return SimpleNamespace(
            parsed_output=flashcards.Cards(cards=[flashcards.Card(front="f", back="b")]),
            stop_reason="end_turn",
            model="claude-haiku-5-5",
            usage=usage(),
        )

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)))
    agent = Agent(client, conn, config, [], system_prompt="x")
    n = await flashcards.make(
        agent, conn, "Bio", "Mitochondria are the powerhouse " * 3, date.today()
    )
    assert n == 1 and calls[0]["model"] == "claude-haiku-5-5"
    assert calls[0]["output_format"] is flashcards.Cards
    with pytest.raises(ValueError):
        await flashcards.make(agent, conn, "Bio", "too short", date.today())


def test_lead_names_recorded_and_categories_rotate(conn):
    from pai import leads

    assert leads.record(conn, ["Rosa's Tacos", " Ace Auto ", ""], "food") == [
        "Rosa's Tacos",
        "Ace Auto",
    ]
    assert leads.already_suggested(conn) == ["Ace Auto", "Rosa's Tacos"]
    cats = ["a", "b", "c"]
    assert leads.category_for(cats, date(2026, 10, 12)) != leads.category_for(
        cats, date(2026, 10, 19)
    )


def test_draft_lookup_handles_names_links_and_loose_words():
    from factories import assignment, utc

    from pai.drafts import find

    now = utc(2026, 10, 10, 12)
    a6 = assignment(6, due=utc(2026, 10, 12), title="Assignment 6: Unit Converter")
    a7 = assignment(7, due=utc(2026, 10, 19), title="Assignment 7: Loops")
    a6.html_url = "https://canvas.example.edu/courses/68241/assignments/1700006"
    items = [a6, a7]
    assert find(items, "csit 135 assignment 6", now) == [a6]
    assert find(items, "CS/IT 135: Assignment 6: Unit Converter", now) == [a6]
    assert find(items, "the unit conv one", now)[0] is a6
    assert find(items, "draft https://canvas.example.edu/courses/68241/assignments/1700006") == [a6]
    assert find(items, "assignment 8", now) == []


def test_faithful_text_keeps_code_menus_and_short_lines():
    html = (
        "<p><strong>Setup</strong></p><pre><code>#include &lt;iostream&gt;\n"
        "using namespace std;\n</code></pre><p>Menu:</p><pre>1. Miles\n2. Quit</pre>"
        "<ul><li>km = miles * 1.60934</li><li>Quit</li></ul>"
    )
    text = links.html_to_faithful_text(html)
    assert "**Setup**" in text
    assert "```\n#include <iostream>\nusing namespace std;" in text
    assert "1. Miles\n2. Quit" in text
    assert "- km = miles * 1.60934" in text and "- Quit" in text


async def test_draft_details_refuse_to_guess_without_instructions():
    from types import SimpleNamespace as N

    from factories import assignment

    from pai import drafts

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"description": "", "submission_types": ["online_upload"]}

    async def get(*a, **k):
        return Resp()

    canvas = N(http=N(get=get), base="https://c.test", headers={})
    with pytest.raises(drafts.NoInstructions):
        await drafts.details(canvas, assignment(1))
    brief = await drafts.details(canvas, assignment(1), pasted="Write a C++ program.")
    assert "Instructions pasted by the student:\nWrite a C++ program." in brief.text


def test_faithful_text_keeps_links_embeds_and_table_cells():
    html = (
        '<p>Follow <a href="https://c.test/courses/1/pages/lab-4">the Lab 4 handout</a>.</p>'
        '<table><tr><th>Input</th><th>Output</th></tr><tr><td>5</td><td>"8.05 km"</td></tr>'
        '</table><iframe src="https://docs.google.com/d/x" title="Spec"></iframe>'
        '<img src="u.png" alt="UML diagram">'
    )
    text, found = links.faithful_text_and_links(html)
    assert "the Lab 4 handout (https://c.test/courses/1/pages/lab-4)" in text
    assert "Input | Output" in text and '5 | "8.05 km"' in text
    assert "[Spec: https://docs.google.com/d/x]" in text
    assert "[image: UML diagram]" in text
    assert found == ["https://c.test/courses/1/pages/lab-4", "https://docs.google.com/d/x"]


async def test_draft_details_follow_linked_pages_files_and_rubric_ratings():
    from types import SimpleNamespace as N

    from factories import assignment

    from pai import drafts

    pdf = b"%PDF-1.4 spec"
    routes = {
        "https://c.test/api/v1/courses/100/assignments/1": {
            "description": (
                '<p>Short intro. See <a href="https://c.test/courses/100/pages/lab-1-spec">'
                'the spec</a> and <a href="/courses/100/files/7/download">rubric.pdf</a>. '
                '<a href="https://www.example.com/x">Outside</a></p>'
            ),
            "submission_types": ["online_upload"],
            "rubric": [
                {
                    "description": "Correct output",
                    "points": 10,
                    "ratings": [
                        {"description": "Full", "points": 10, "long_description": "All cases"},
                        {"description": "None", "points": 0},
                    ],
                }
            ],
        },
        "https://c.test/api/v1/courses/100/pages/lab-1-spec": {
            "title": "Lab 1 Spec",
            "body": "<p>Print exactly:</p><pre>Enter miles: </pre>",
        },
        "https://c.test/api/v1/files/7": {
            "display_name": "rubric.pdf",
            "content-type": "application/pdf",
            "size": len(pdf),
            "url": "https://c.test/files/7/download?verifier=v",
        },
    }
    fetched = []

    class Resp:
        def __init__(self, url):
            self.url = url
            self.content = pdf

        def raise_for_status(self):
            pass

        def json(self):
            return routes[self.url]

    async def get(url, **k):
        fetched.append(url)
        return Resp(url)

    canvas = N(http=N(get=get), base="https://c.test", headers={})
    brief = await drafts.details(canvas, assignment(1))
    assert "Linked page: Lab 1 Spec\nPrint exactly:\n\n```\nEnter miles: \n```" in brief.text
    assert "Linked file: rubric.pdf (attached as a PDF)" in brief.text
    assert brief.documents[0]["title"] == "rubric.pdf"
    assert "Outside (https://www.example.com/x)" in brief.text
    assert not any("example.com" in u for u in fetched)  # only Canvas links are followed
    assert "- Correct output (10 pts)\n  - 10 pts: Full All cases\n  - 0 pts: None" in brief.text


async def test_discussion_prompt_counts_as_instructions():
    from types import SimpleNamespace as N

    from factories import assignment

    from pai import drafts

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "description": "",
                "submission_types": ["discussion_topic"],
                "discussion_topic": {"message": "<p>Post 250 words on chapter 3.</p>"},
            }

    async def get(*a, **k):
        return Resp()

    canvas = N(http=N(get=get), base="https://c.test", headers={})
    brief = await drafts.details(canvas, assignment(1))
    assert "Post 250 words on chapter 3." in brief.text


def _canvas_with(routes):
    from types import SimpleNamespace as N

    class Resp:
        def __init__(self, url):
            self.url, self.content = url, b"%PDF-1.4 handout"

        def raise_for_status(self):
            pass

        def json(self):
            return routes[self.url]

    async def get(url, **k):
        return Resp(url)

    return N(http=N(get=get), base="https://c.test", headers={})


async def test_chat_tool_returns_full_instructions(ctx):
    from factories import assignment

    from pai.tools import drafts as draft_tools

    canvas = _canvas_with(
        {
            "https://c.test/api/v1/courses/100/assignments/4": {
                "description": "<p>Write a converter.</p><pre>Enter miles: </pre>",
                "submission_types": ["online_upload"],
            }
        }
    )

    async def all_assignments():
        return [assignment(4)]

    canvas.all_assignments = all_assignments
    ctx.canvas = canvas
    out = await draft_tools.assignment_instructions({"assignment": "lab 4"}, ctx)
    assert isinstance(out, str)
    assert "Write a converter." in out and "```\nEnter miles: \n```" in out
    assert "Canvas link: https://canvas.test/a/4" in out


async def test_chat_tool_attaches_pdfs_and_says_when_cut(ctx, monkeypatch):
    from factories import assignment

    from pai.tools import drafts as draft_tools

    canvas = _canvas_with(
        {
            "https://c.test/api/v1/courses/100/assignments/4": {
                "description": '<p>See <a href="/courses/100/files/9">handout.pdf</a></p>'
                + "<p>x</p>" * 50,
                "submission_types": ["online_upload"],
            },
            "https://c.test/api/v1/files/9": {
                "display_name": "handout.pdf",
                "content-type": "application/pdf",
                "size": 16,
                "url": "https://c.test/files/9/download",
            },
        }
    )

    async def all_assignments():
        return [assignment(4)]

    canvas.all_assignments = all_assignments
    ctx.canvas = canvas
    monkeypatch.setattr(draft_tools, "CHAT_LIMIT", 120)
    out = await draft_tools.assignment_instructions({"assignment": "lab 4"}, ctx)
    doc, text = out
    assert doc["type"] == "document" and doc["title"] == "handout.pdf"
    assert text["type"] == "text" and "more characters; tell the owner)" in text["text"]


async def test_tool_results_pass_content_blocks_through(conn, config, ctx):
    from conftest import FakeClient, response, text_block, tool_block

    from pai.agent import Agent
    from pai.tools import Tool

    blocks = [{"type": "document", "source": {}}, {"type": "text", "text": "hi"}]

    async def blocks_fn(args, ctx):
        return blocks

    async def rows_fn(args, ctx):
        return [{"id": 1, "title": "Lab 1"}]  # plain data still goes as JSON

    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    tools = [Tool("b", "b", schema, blocks_fn), Tool("r", "r", schema, rows_fn)]
    client = FakeClient(
        response([tool_block("b", {}, "t1"), tool_block("r", {}, "t2")], stop_reason="tool_use"),
        response([text_block("ok")]),
    )
    await Agent(client, conn, config, tools, system_prompt="x").run("go", ctx)
    results = client.requests[1]["messages"][-1]["content"]
    assert results[0]["content"] == blocks
    assert results[1]["content"] == '[{"id": 1, "title": "Lab 1"}]'


async def test_submission_reads_text_files_grade_rubric_and_comments():
    from zoneinfo import ZoneInfo

    from factories import assignment

    from pai import drafts

    canvas = _canvas_with(
        {
            "https://c.test/api/v1/courses/100/assignments/4": {
                "rubric": [{"id": "c1", "description": "Correct output", "points": 10}],
            },
            "https://c.test/api/v1/courses/100/assignments/4/submissions/self": {
                "user_id": 42,
                "submission_type": "online_upload",
                "workflow_state": "graded",
                "submitted_at": "2026-10-07T06:10:00Z",
                "late": True,
                "attempt": 2,
                "score": 8.5,
                "grade": "B",
                "body": "<p>My notes: <code>miles * 1.60934</code></p>",
                "attachments": [
                    {
                        "display_name": "lab4.pdf",
                        "content-type": "application/pdf",
                        "size": 16,
                        "url": "https://c.test/files/1/download",
                    }
                ],
                "rubric_assessment": {"c1": {"points": 8.5, "comments": "Rounding off"}},
                "submission_comments": [
                    {
                        "author_name": "Prof Lee",
                        "created_at": "2026-10-08T01:00:00Z",
                        "comment": "Round to 2 places.",
                    }
                ],
            },
        }
    )
    brief = await drafts.submission(canvas, assignment(4), ZoneInfo("America/Los_Angeles"))
    text = brief.text
    assert "Status: submitted Tue 10/06 23:10, late, attempt 2, score 8.5/10 (grade B)" in text
    assert "Submitted text:\nMy notes: miles * 1.60934" in text
    assert "Submitted file: lab4.pdf (attached as a PDF)" in text
    assert brief.documents[0]["title"] == "lab4.pdf"
    assert "- Correct output: 8.5 pts (Rounding off)" in text
    assert "- Prof Lee (2026-10-08): Round to 2 places." in text


async def test_submission_shows_whole_discussion_thread():
    from zoneinfo import ZoneInfo

    from factories import assignment

    from pai import drafts

    canvas = _canvas_with(
        {
            "https://c.test/api/v1/courses/100/assignments/5": {"discussion_topic": {"id": 77}},
            "https://c.test/api/v1/courses/100/assignments/5/submissions/self": {
                "user_id": 42,
                "submission_type": "discussion_topic",
                "workflow_state": "submitted",
                "submitted_at": "2026-10-09T18:00:00Z",
            },
            "https://c.test/api/v1/courses/100/discussion_topics/77/view": {
                "participants": [
                    {"id": 42, "display_name": "Alex"},
                    {"id": 7, "display_name": "Sam"},
                ],
                "view": [
                    {
                        "user_id": 42,
                        "created_at": "2026-10-09T18:00:00Z",
                        "message": "<p>My main post.</p>",
                        "replies": [
                            {
                                "user_id": 7,
                                "created_at": "2026-10-09T20:00:00Z",
                                "message": "<p>Nice point.</p>",
                            }
                        ],
                    },
                    {
                        "user_id": 7,
                        "created_at": "2026-10-09T19:00:00Z",
                        "message": "<p>Sam's post</p>",
                        "attachments": [
                            {
                                "id": 31,
                                "display_name": "Sam essay draft.docx",
                                "content-type": "application/msword",
                                "size": 24_000,
                            }
                        ],
                        "replies": [
                            {
                                "user_id": 42,
                                "created_at": "2026-10-10T01:00:00Z",
                                "message": "<p>My reply.</p>",
                            }
                        ],
                    },
                    {"user_id": 9, "deleted": True},
                ],
                "new_entries": [
                    {
                        "user_id": 42,
                        "parent_id": 9,
                        "created_at": "2026-10-10T02:00:00Z",
                        "message": "<p>A fresh reply.</p>",
                    }
                ],
            },
        }
    )
    brief = await drafts.submission(canvas, assignment(5), ZoneInfo("America/Los_Angeles"))
    text = brief.text
    assert "not graded yet" in text
    assert "Discussion thread (5 posts, 3 by you):" in text
    assert "Post by you (Fri 10/09 11:00):\nMy main post." in text
    assert "Reply to you by Sam (Fri 10/09 13:00):\nNice point." in text
    assert (
        "Post by Sam (Fri 10/09 12:00):\nSam's post\n"
        "[attached: Sam essay draft.docx (file 31, Word, 23 KB)]" in text
    )
    assert "Reply to Sam by you (Fri 10/09 18:00):\nMy reply." in text
    assert "Reply by you (Fri 10/09 19:00):\nA fresh reply." in text


async def test_group_discussion_reads_own_groups_copy_and_resolves_group_links(ctx):
    from zoneinfo import ZoneInfo

    from factories import assignment

    from pai import drafts
    from pai.tools import drafts as draft_tools

    topic = {
        "id": 77,
        "group_topic_children": [{"id": 501, "group_id": 11}, {"id": 502, "group_id": 12}],
    }
    canvas = _canvas_with(
        {
            "https://c.test/api/v1/courses/100/assignments/5": {
                "description": "<p>Discuss in your group.</p>",
                "discussion_topic": topic,
            },
            "https://c.test/api/v1/courses/100/assignments/5/submissions/self": {
                "user_id": 42,
                "submission_type": None,
                "workflow_state": "unsubmitted",
            },
            "https://c.test/api/v1/users/self/groups": [{"id": 12}],
            "https://c.test/api/v1/groups/12/discussion_topics/502/view": {
                "participants": [{"id": 7, "display_name": "Sam"}],
                "view": [
                    {
                        "user_id": 7,
                        "created_at": "2026-10-09T19:00:00Z",
                        "message": "<p>Group 12 post</p>",
                    }
                ],
            },
            "https://c.test/api/v1/groups/12": {"id": 12, "course_id": 100},
            "https://c.test/api/v1/groups/12/discussion_topics/502": {
                "id": 502,
                "root_topic_id": 77,
                "assignment_id": None,
            },
            "https://c.test/api/v1/courses/100/discussion_topics/77": {"assignment_id": 5},
        }
    )
    brief = await drafts.submission(canvas, assignment(5), ZoneInfo("America/Los_Angeles"))
    assert "Discussion thread (1 posts, 0 by you):\nPost by Sam" in brief.text
    assert "Group 12 post" in brief.text

    async def all_assignments():
        return [assignment(4), assignment(5)]

    canvas.all_assignments = all_assignments
    ctx.canvas = canvas
    out = await draft_tools.assignment_instructions(
        {
            "assignment": "https://c.test/groups/12/discussion_topics/502",
            "include_submission": True,
        },
        ctx,
    )
    assert "Assignment: Lab 5" in out and "Group 12 post" in out


async def test_chat_tool_includes_submission_on_request(ctx):
    from factories import assignment

    from pai.tools import drafts as draft_tools

    canvas = _canvas_with(
        {
            "https://c.test/api/v1/courses/100/assignments/4": {
                "description": "<p>Write a converter.</p>",
                "submission_types": ["online_text_entry"],
            },
            "https://c.test/api/v1/courses/100/assignments/4/submissions/self": {
                "user_id": 42,
                "submission_type": "online_text_entry",
                "workflow_state": "submitted",
                "submitted_at": "2026-10-07T06:10:00Z",
                "body": "<p>Here is my converter.</p>",
            },
        }
    )

    async def all_assignments():
        return [assignment(4)]

    canvas.all_assignments = all_assignments
    ctx.canvas = canvas
    plain = await draft_tools.assignment_instructions({"assignment": "lab 4"}, ctx)
    assert "Your submission" not in plain
    out = await draft_tools.assignment_instructions(
        {"assignment": "lab 4", "include_submission": True}, ctx
    )
    assert "Write a converter." in out and "Submitted text:\nHere is my converter." in out


async def test_read_canvas_file_opens_only_the_files_asked_for(ctx):
    import io
    import zipfile
    from types import SimpleNamespace as N

    from pai.tools import drafts as draft_tools

    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr(
            "word/document.xml",
            "<w:document><w:body><w:p><w:r><w:t>Sam &amp; the thesis</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )
    blobs = {
        "https://c.test/dl/31": docx.getvalue(),
        "https://c.test/dl/32": b"\x89PNG fake",
    }
    meta = {
        "https://c.test/api/v1/files/31": {
            "display_name": "Sam essay.docx",
            "content-type": "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document",
            "size": 100,
            "url": "https://c.test/dl/31",
        },
        "https://c.test/api/v1/files/32": {
            "display_name": "diagram.png",
            "content-type": "image/png",
            "size": 10,
            "url": "https://c.test/dl/32",
        },
        "https://c.test/api/v1/files/33": {
            "display_name": "talk.mp4",
            "content-type": "video/mp4",
            "size": 10,
            "url": "https://c.test/dl/33",
        },
    }
    fetched = []

    class Resp:
        def __init__(self, url):
            self.url, self.content = url, blobs.get(url, b"")

        def raise_for_status(self):
            if self.url.endswith("/files/34"):
                raise RuntimeError("403")

        def json(self):
            return meta[self.url]

    async def get(url, **k):
        fetched.append(url)
        return Resp(url)

    ctx.canvas = N(http=N(get=get), base="https://c.test", headers={})
    out = await draft_tools.read_canvas_file({"file_ids": [31, 32, 33, 34]}, ctx)
    image, text = out
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    t = text["text"]
    assert t.startswith("(Opened the first 3 of 4; ask for the rest.)")
    assert "File: Sam essay.docx\nSam & the thesis" in t
    assert "File: diagram.png (attached as an image)" in t
    assert "talk.mp4 (video/mp4; can't watch or listen here" in t
    assert "https://c.test/dl/33" not in fetched  # media is never downloaded
    assert not any("/files/34" in u for u in fetched)  # beyond the 3-file cap

    one = await draft_tools.read_canvas_file({"file_ids": [34]}, ctx)
    assert one.startswith("File 34: couldn't open it (RuntimeError).")


async def test_thread_attachments_open_through_their_signed_links(ctx):
    from types import SimpleNamespace as N
    from zoneinfo import ZoneInfo

    from pai import drafts
    from pai.tools import drafts as draft_tools

    entries = [
        {
            "user_id": 7,
            "message": "<p>draft</p>",
            "attachments": [
                {
                    "id": 55,
                    "display_name": "notes.txt",
                    "content-type": "text/plain",
                    "size": 5,
                    "url": "https://c.test/files/55/download?verifier=abc",
                }
            ],
        }
    ]
    drafts._thread(entries, 42, {7: "Sam"}, ZoneInfo("America/Los_Angeles"))
    fetched = []

    class Resp:
        content = b"hello"

        def raise_for_status(self):
            pass

    async def get(url, **k):
        fetched.append(url)
        return Resp()

    ctx.canvas = N(http=N(get=get), base="https://c.test", headers={})
    out = await draft_tools.read_canvas_file({"file_ids": [55]}, ctx)
    assert out == "File: notes.txt\nhello"
    assert fetched == ["https://c.test/files/55/download?verifier=abc"]  # no /api/v1/files


async def test_send_canvas_file_sends_originals_within_limits(ctx):
    from types import SimpleNamespace as N

    from pai.tools import drafts as draft_tools

    meta = {
        "https://c.test/api/v1/files/1": {
            "id": 1,
            "display_name": "Lab 4 handout.pdf",
            "size": 100,
            "url": "https://c.test/dl/1",
        },
        "https://c.test/api/v1/files/2": {
            "id": 2,
            "display_name": "lecture.mp4",
            "size": 80 * 1024 * 1024,
            "url": "https://c.test/dl/2",
        },
    }
    fetched, sent = [], []

    class Resp:
        def __init__(self, url):
            self.url, self.content = url, b"%PDF-1.4 handout"

        def raise_for_status(self):
            if self.url.endswith("/files/3"):
                raise RuntimeError("403")

        def json(self):
            return meta[self.url]

    async def get(url, **k):
        fetched.append(url)
        return Resp(url)

    async def send_file(name, fileobj, caption):
        sent.append((name, fileobj.read(), caption))

    ctx.canvas = N(http=N(get=get), base="https://c.test", headers={})
    ctx.send_file = send_file
    out = await draft_tools.send_canvas_file({"file_ids": [1, 2, 3, 4]}, ctx)
    assert sent == [
        ("Lab 4 handout.pdf", b"%PDF-1.4 handout", "📎 Lab 4 handout.pdf (from Canvas)")
    ]
    assert "Sent only the first 3 of 4" in out
    assert "lecture.mp4 is over Telegram's 50 MB limit; link: https://c.test/files/2" in out
    assert "File 3: couldn't get it (RuntimeError)" in out
    assert "https://c.test/dl/2" not in fetched  # too big: never downloaded
    assert not any(u.endswith("/files/4") for u in fetched)


async def test_linked_and_submitted_files_show_their_ids():
    from factories import assignment

    from pai import drafts

    canvas = _canvas_with(
        {
            "https://c.test/api/v1/courses/100/assignments/4": {
                "description": '<p><a href="/courses/100/files/9">handout</a></p>',
                "submission_types": ["online_upload"],
            },
            "https://c.test/api/v1/files/9": {
                "id": 9,
                "display_name": "handout.pdf",
                "content-type": "application/pdf",
                "size": 16,
                "url": "https://c.test/files/9/download",
            },
        }
    )
    brief = await drafts.details(canvas, assignment(4))
    assert "Linked file: handout.pdf [file 9] (attached as a PDF)" in brief.text
    assert drafts._SEEN_FILES[9]["url"] == "https://c.test/files/9/download"


LEADS_JSON = """Checked a few directories.

```json
{
  "leads": [
    {"name": "Taco <Spot> & Co", "neighborhood": "Midtown", "address": "100 Main St, Midtown",
     "what": "Tacos & burritos", "phone": "(555) 555-0100", "website": "",
     "website_note": "no website found", "instagram": "@tacospot.midtown",
     "facebook": "facebook.com/tacospotmidtown", "tiktok": "",
     "yelp": "https://www.yelp.com/biz/taco-spot-midtown", "google_maps": "",
     "weakness": "Menu only on Instagram.", "draft_note": "Hi! Loved your al pastor."},
    {"name": "Ace Auto", "neighborhood": "Eastside", "website": "aceauto.example",
     "website_note": "not mobile friendly", "instagram": "https://evil.example/ace",
     "google_maps": "https://maps.app.goo.gl/abc123", "weakness": "Tiny text on phones.",
     "draft_note": "Hello!"}
  ],
  "most_promising": "Taco Spot: busy, no site."
}
```"""


class _LeadAgent:
    def __init__(self, text):
        self.text, self.calls = text, []

    async def complete(self, route, system, user, job, tools=None):
        self.calls.append(user)
        return self.text


async def test_leads_search_the_configured_area_one_message_each(conn, config):
    from pai import leads

    config.leads.area = "Springfield"
    config.leads.neighborhoods = ["Midtown", "Eastside"]
    agent = _LeadAgent(LEADS_JSON)
    out = await leads.find(agent, conn, config, date(2026, 10, 12), "restaurants and cafes")
    user = agent.calls[0]
    assert user.startswith("Area: Springfield (anywhere in it, e.g. Midtown, Eastside;")
    assert f"Find up to {config.leads.count} businesses." in user
    header, taco, ace = out  # a header, then one message per lead
    assert header.startswith("<b>Leads: restaurants and cafes in Springfield</b> · 2 found")
    assert "⭐ Taco Spot: busy, no site." in header
    assert taco.startswith("<b>1/2. Taco &lt;Spot&gt; &amp; Co</b> (Midtown)")
    assert '<a href="https://www.instagram.com/tacospot.midtown/">Instagram</a>' in taco
    assert '<a href="https://facebook.com/tacospotmidtown">Facebook</a>' in taco
    assert '<a href="https://www.yelp.com/biz/taco-spot-midtown">Yelp</a>' in taco
    assert (
        '<a href="https://www.google.com/maps/search/?api=1&amp;query='
        'Taco+%3CSpot%3E+%26+Co+100+Main+St%2C+Midtown">Google Maps</a>'
    ) in taco
    assert "🌐 no website found" in taco and "Website</a>" not in taco
    assert "📞 (555) 555-0100" in taco
    assert '<a href="https://aceauto.example">Website</a>' in ace
    assert "evil.example" not in ace  # an "Instagram" URL that isn't Instagram is dropped
    assert '<a href="https://maps.app.goo.gl/abc123">Google Maps</a>' in ace
    assert leads.already_suggested(conn) == ["Ace Auto", "Taco <Spot> & Co"]


async def test_leads_fall_back_to_a_structured_parse(conn, config):
    from pai import leads

    class Agent(_LeadAgent):
        async def structured(self, route, system, user, schema, job):
            assert job == "leads_parse" and "Rosa" in user
            return schema(leads=[leads.Lead(name="Rosa's", instagram="rosas")])

    out = await leads.find(Agent("1. *Rosa's* on IG @rosas"), conn, config, date(2026, 10, 12))
    assert len(out) == 2 and "https://www.instagram.com/rosas/" in out[1]


def test_social_and_maps_links():
    from pai import leads

    assert leads.social_url("tiktok", "@rosas_tacos") == "https://www.tiktok.com/@rosas_tacos"
    assert leads.social_url("instagram", "instagram.com/rosas") == "https://instagram.com/rosas"
    assert leads.social_url("yelp", "rosas") is None  # Yelp needs a found URL
    assert leads.social_url("instagram", "not a handle!") is None
    lead = leads.Lead(name="Ace Auto", neighborhood="Eastside")
    assert leads.maps_url(lead, "Springfield") == (
        "https://www.google.com/maps/search/?api=1&query=Ace+Auto+Eastside"
    )
