"""`doc guide`: docs/notion/*.md -> reference pages under the "AI Primer" page, refreshed in place."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.config import Settings
from pipeline.context import AppContext
from pipeline.guide_cmds import GUIDE_DIR, child_pages, publish_guide, split_title
from pipeline.notion import markdown_to_blocks, plain_text
from tests.fake_notion import FakeNotionBackend

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ctx(tmp_path, fake, parent, monkeypatch):
    monkeypatch.setenv("AIPRIMER_NOTION_PAGE_ID", parent)
    return AppContext(Settings(repo_root=tmp_path, config={}), notion_backend=fake)


def test_split_title_takes_the_first_heading_out_of_the_body():
    assert split_title("# Command guide\n\nHow you talk.\n", "x") == ("Command guide", "How you talk.\n")
    assert split_title("\n\n# T \n## Sub\n", "x") == ("T", "## Sub\n")
    assert split_title("No heading first\n# T\n", "fallback") == ("fallback", "No heading first\n# T\n")


def test_publish_guide_creates_pages_then_refreshes_them_in_place(tmp_path, monkeypatch):
    (tmp_path / GUIDE_DIR).mkdir(parents=True)
    (tmp_path / GUIDE_DIR / "10-command-guide.md").write_text("# Command guide\n\nType `/ingest`.\n", encoding="utf-8")
    (tmp_path / GUIDE_DIR / "20-how-it-works.md").write_text("# How it works\n\n## Layers\n\n- one\n- two\n", encoding="utf-8")
    fake = FakeNotionBackend()
    parent = fake.add_page("AI Primer")
    fake.create_database(parent, "Authors", {"Name": {"title": {}}})  # a table under the page must survive untouched
    ctx = _ctx(tmp_path, fake, parent, monkeypatch)

    first = publish_guide(ctx)
    assert [p["title"] for p in first["pages"]] == ["Command guide", "How it works"]
    assert all(p["created"] for p in first["pages"]) and first["parent_url"] == fake.page_url(parent)
    assert first["pages"][0]["file"] == "docs/notion/10-command-guide.md"
    ids = {p["title"]: p["page_id"] for p in first["pages"]}
    assert child_pages(fake, parent) == ids
    assert fake.page_plain_text(ids["Command guide"]) == "Type /ingest."  # the title is not repeated in the body
    assert plain_text(fake.pages[ids["Command guide"]]["properties"]["title"]["title"]) == "Command guide"

    (tmp_path / GUIDE_DIR / "10-command-guide.md").write_text("# Command guide\n\nType `/ingest` or `/summarize`.\n", encoding="utf-8")
    second = publish_guide(ctx)
    assert {p["title"]: p["page_id"] for p in second["pages"]} == ids and not any(p["created"] for p in second["pages"])
    assert fake.page_plain_text(ids["Command guide"]) == "Type /ingest or /summarize."
    assert set(fake.child_databases(parent)) == {"Authors"} and len(child_pages(fake, parent)) == 2

    only = publish_guide(ctx, only="how-it")
    assert [p["title"] for p in only["pages"]] == ["How it works"]
    with pytest.raises(RuntimeError, match="no markdown file"):
        publish_guide(ctx, only="nothing-like-this")


def test_publish_guide_needs_notion_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("AIPRIMER_NOTION_PAGE_ID", raising=False)
    with pytest.raises(RuntimeError, match="Notion mode is off"):
        publish_guide(AppContext(Settings(repo_root=tmp_path, config={})))


def test_the_real_reference_documents_publish_cleanly(tmp_path, monkeypatch):
    """The committed docs/notion files must convert to Notion blocks and publish without hitting a limit."""
    files = sorted((REPO_ROOT / GUIDE_DIR).glob("*.md"))
    assert [f.name for f in files] == ["10-command-guide.md", "20-how-it-works.md"]
    fake = FakeNotionBackend()
    parent = fake.add_page("AI Primer")
    ctx = _ctx(REPO_ROOT, fake, parent, monkeypatch)
    result = publish_guide(ctx)
    assert [p["title"] for p in result["pages"]] == ["Command guide", "How the pipeline works"]
    for path, page in zip(files, result["pages"]):
        title, body = split_title(path.read_text(encoding="utf-8"), path.stem)
        types = {b["type"] for b in markdown_to_blocks(body)}
        assert {"heading_2", "table"} <= types, path.name
        text = fake.page_plain_text(page["page_id"])
        assert "—" not in body, "style: no em dashes in owner-facing text"
        assert "ingest" in text.lower() and "notion" in text.lower()


# --------------------------------------------------------------------------- the pages describe the current pipeline


def test_reference_pages_cover_the_skills_the_tables_and_the_cost_gate():
    from pipeline.config import load_settings
    from pipeline.models import TAB_MODELS

    commands = (REPO_ROOT / GUIDE_DIR / "10-command-guide.md").read_text(encoding="utf-8")
    how = (REPO_ROOT / GUIDE_DIR / "20-how-it-works.md").read_text(encoding="utf-8")
    skills = sorted(p.name for p in (REPO_ROOT / ".claude" / "skills").iterdir() if p.is_dir())
    assert skills and all(f"`/{s}" in commands for s in skills), skills
    assert all(f"| {table} |" in how for table in TAB_MODELS), list(TAB_MODELS)
    dollars = f"{int(load_settings(repo_root=REPO_ROOT).config['apify']['cost_confirm_usd'])} dollars"
    assert dollars in commands and dollars in how


# --------------------------------------------------------------------------- the push gate (scripts/hooks/guide_gate.py)

import json
import os
import subprocess
import sys

GATE = REPO_ROOT / "scripts" / "hooks" / "guide_gate.py"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path):
    """A working clone with an `origin` that already holds the first commit on `main`."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "checkout", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    for rel, text in (("pipeline/x.py", "x = 1\n"), ("docs/notion/10-command-guide.md", "# Command guide\n\nold\n"), ("tests/test_x.py", "def test(): pass\n")):
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return work


def _gate(work, command="git push -u origin main", tool="Bash", check=False, **env):
    payload = {"tool_name": tool, "tool_input": {"command": command}}
    e = {k: v for k, v in os.environ.items() if k not in ("AIPRIMER_NOTION_PAGE_ID", "AIPRIMER_GUIDE_PUBLISH_CMD")}
    e.update(env)
    args = [sys.executable, str(GATE)] + (["--check"] if check else [])
    return subprocess.run(args, cwd=work, input=json.dumps(payload), capture_output=True, text=True, env=e)


def _commit(work, rel, text, message):
    (work / rel).write_text(text)
    _git(work, "add", rel)
    _git(work, "commit", "-q", "-m", message)


def test_gate_in_hook_mode_ignores_other_commands_and_tools(tmp_path):
    work = _repo(tmp_path)
    _commit(work, "pipeline/x.py", "x = 2\n", "change the pipeline")
    for command in ("git status", "python -m pytest -q", "git commit -m x"):
        r = _gate(work, command)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == "", command
    assert _gate(work, tool="Read").returncode == 0
    assert _gate(work).returncode == 2  # the same state, but a push


def test_gate_blocks_a_pipeline_change_without_a_guide_update_until_marked_or_fixed(tmp_path):
    work = _repo(tmp_path)
    _commit(work, "tests/test_x.py", "def test(): assert True\n", "tests only")
    assert _gate(work, check=True).returncode == 0  # tests are not the pipeline
    _commit(work, "pipeline/x.py", "x = 2\n", "change the pipeline")
    blocked = _gate(work, check=True)
    assert blocked.returncode == 2 and "reference pages" in blocked.stderr and "pipeline/x.py" in blocked.stderr and "Guide: unchanged" in blocked.stderr
    # in hook mode a push inside a compound command with retries is still a push
    assert _gate(work, "for d in 0 2; do git push -u origin main && break; sleep $d; done").returncode == 2
    _commit(work, "pipeline/y.py", "y = 1\n", "more pipeline\n\nGuide: unchanged, internal refactor")
    ok = _gate(work, check=True)
    assert ok.returncode == 0 and "nothing the owner sees has changed" in ok.stderr
    # the marker is per push range: a later pipeline commit without it is blocked again
    _git(work, "push", "-q", "origin", "main")
    _commit(work, "pipeline/z.py", "z = 1\n", "again")
    assert _gate(work, check=True).returncode == 2
    _commit(work, "docs/notion/10-command-guide.md", "# Command guide\n\nnew\n", "guide: describe z")
    allowed = _gate(work, check=True)  # Notion mode off here: allowed with a note, nothing to publish from
    assert allowed.returncode == 0 and "Notion mode is off" in allowed.stderr


def test_gate_republishes_changed_guides_before_the_push_and_holds_it_when_publishing_fails(tmp_path):
    work = _repo(tmp_path)
    _commit(work, "docs/notion/10-command-guide.md", "# Command guide\n\nnew\n", "guide: wording")
    page = {"AIPRIMER_NOTION_PAGE_ID": "0123456789abcdef0123456789abcdef"}
    ok_cmd = f"{sys.executable} -c \"import json; print(json.dumps({{'ok': True, 'pages': [{{'title': 'Command guide', 'created': False}}]}}))\""
    r = _gate(work, check=True, AIPRIMER_GUIDE_PUBLISH_CMD=ok_cmd, **page)
    assert r.returncode == 0 and "republished" in r.stderr and "Command guide (refreshed)" in r.stderr
    bad_cmd = f"{sys.executable} -c \"import json; print(json.dumps({{'ok': False, 'error': 'NotionError: 401 unauthorized'}}))\""
    r = _gate(work, check=True, AIPRIMER_GUIDE_PUBLISH_CMD=bad_cmd, **page)
    assert r.returncode == 2 and "push is held" in r.stderr and "401 unauthorized" in r.stderr
    crash_cmd = f"{sys.executable} -c \"import sys; sys.stderr.write('Traceback: boom\\n'); sys.exit(1)\""
    r = _gate(work, check=True, AIPRIMER_GUIDE_PUBLISH_CMD=crash_cmd, **page)
    assert r.returncode == 2 and "boom" in r.stderr


def test_gate_on_a_new_branch_compares_against_the_remote_default_branch(tmp_path):
    work = _repo(tmp_path)
    _git(work, "remote", "set-head", "origin", "main")
    _git(work, "checkout", "-q", "-b", "claude/feature")
    _commit(work, "pipeline/x.py", "x = 3\n", "feature")
    assert _gate(work, "git push -u origin claude/feature").returncode == 2
    _commit(work, "docs/notion/20-how-it-works.md", "# How the pipeline works\n\nx is 3\n", "guide")
    assert _gate(work, "git push -u origin claude/feature").returncode == 0
