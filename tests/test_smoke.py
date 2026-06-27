"""Smoke tests for the two highest-risk subsystems: the adapter build pipeline
and the vault health checker. Both run the real scripts via subprocess and only
depend on the Python standard library, so CI needs nothing beyond pytest.

Adapted from the test added by the bmassenz fork (the only fork that shipped
any automated test). See FORK_INSIGHTS.md items #47/#48.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _json_from_stdout(stdout: str) -> dict:
    """vault_health.py prints a couple of human-readable lines before the JSON
    payload even in --json mode. Scan for the first line that opens the object."""
    lines = stdout.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "{":
            return json.loads("\n".join(lines[index:]))
    raise AssertionError(f"JSON payload not found in stdout:\n{stdout}")


def test_codex_cli_build_generates_expected_files():
    """The codex-cli adapter must emit the AGENTS.md manual and one native Codex
    Agent Skill per command (.agents/skills/<name>/SKILL.md). This guards the
    adapter pipeline that every command change depends on."""
    result = subprocess.run(
        ["bash", "scripts/build.sh", "--platform", "codex-cli"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (REPO_ROOT / "dist/codex-cli/AGENTS.md").is_file()
    skill = REPO_ROOT / "dist/codex-cli/.agents/skills/obsidian-save/SKILL.md"
    assert skill.is_file()
    # Native skills require name + description frontmatter for discovery.
    head = skill.read_text(encoding="utf-8")[:400]
    assert "name: obsidian-save" in head
    assert "description:" in head


def test_hermes_build_generates_native_skills():
    """The hermes adapter must emit one native Hermes skill per command at
    skills/<category>/<name>/SKILL.md, with the required frontmatter Hermes
    needs to load it (name, description, version, author, license)."""
    result = subprocess.run(
        ["bash", "scripts/build.sh", "--platform", "hermes"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    skill = REPO_ROOT / "dist/hermes/skills/vault/obsidian-save/SKILL.md"
    assert skill.is_file()
    head = skill.read_text(encoding="utf-8")[:500]
    for field in ("name: obsidian-save", "description:", "version:", "author:", "license:"):
        assert field in head, field
    # Calendar/scheduled commands are Claude-only and must not leak to Hermes.
    assert not (REPO_ROOT / "dist/hermes/skills/vault/obsidian-calendar").exists()

    # Scheduled agents emit as opt-in blueprint skills under optional-skills/
    # (not auto-armed skills/), each carrying a cron schedule.
    nightly = REPO_ROOT / "dist/hermes/optional-skills/obsidian-nightly/SKILL.md"
    assert nightly.is_file()
    blueprint = nightly.read_text(encoding="utf-8")
    assert "blueprint:" in blueprint
    assert 'schedule: "0 22 * * *"' in blueprint
    # The opt-in arming surface, not the auto-loaded one.
    assert not (REPO_ROOT / "dist/hermes/skills/scheduled").exists()
    hooks_doc = REPO_ROOT / "dist/hermes/HOOKS.md"
    assert hooks_doc.is_file()
    # The on_session_end lifecycle hook (PostCompact analog) and its config ship.
    assert (REPO_ROOT / "dist/hermes/hooks/obsidian-hermes-session-end.sh").is_file()
    assert (REPO_ROOT / "dist/hermes/hooks/hermes-hooks.cli-config.example.yaml").is_file()
    assert "on_session_end" in hooks_doc.read_text(encoding="utf-8")


def test_vault_health_json_reports_clean_linked_vault(tmp_path):
    """A minimal two-note vault with reciprocal wikilinks should report zero
    issues: no orphans, no broken links, no missing frontmatter."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Home.md").write_text(
        "# Home\n\nSee [[Project Alpha]].\n",
        encoding="utf-8",
    )
    (vault / "Project Alpha.md").write_text(
        "---\n"
        "type: project\n"
        "aliases:\n"
        "  - Project Alpha\n"
        "---\n"
        "# Project Alpha\n\nBack to [[Home]].\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "scripts/vault_health.py", "--path", str(vault), "--json"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = _json_from_stdout(result.stdout)
    assert payload["total_notes"] == 2
    assert payload["total_issues"] == 0
    assert payload["counts"]["Broken links"] == 0
    assert payload["counts"]["Orphans"] == 0


def test_substitution_check_passes_on_repo():
    """The repo source must be free of banned substitution characters in prose
    (the CI gate). Characters inside code fences/spans are allowed."""
    result = subprocess.run(
        [sys.executable, "scripts/sweep_non_ascii.py", "--check"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_substitution_check_flags_prose_em_dash(tmp_path):
    """--check must fail (exit 1) when a banned character appears in prose, and
    must NOT fail when it only appears inside an inline code span."""
    # Build the em-dash from its code point so this test's own source stays
    # ASCII (the CI gate scans .py files too); the written fixtures get the
    # real character.
    em = "\u2014"
    bad = tmp_path / "bad.md"
    bad.write_text(f"A prose line with an em{em}dash.\n", encoding="utf-8")
    flagged = subprocess.run(
        [sys.executable, "scripts/sweep_non_ascii.py", "--check", str(bad)],
        cwd=REPO_ROOT, check=False, capture_output=True, text=True,
    )
    assert flagged.returncode == 1, flagged.stdout

    ok = tmp_path / "ok.md"
    ok.write_text(f"A filename in code: `2026-01-01 {em} note.md` is fine.\n", encoding="utf-8")
    passed = subprocess.run(
        [sys.executable, "scripts/sweep_non_ascii.py", "--check", str(ok)],
        cwd=REPO_ROOT, check=False, capture_output=True, text=True,
    )
    assert passed.returncode == 0, passed.stdout


def test_health_normalizes_dashes_in_links(tmp_path):
    """Regression for #63: a wikilink written with a regular hyphen must resolve
    against a filename written with an em-dash (the #31 behavior). The non-ASCII
    sweep once rewrote _normalize_dashes()'s operands into ASCII hyphens, turning
    it into a no-op; this locks the behavior so an automated pass cannot silently
    undo it again. Em-dash built from its code point so this source stays ASCII."""
    em = "\u2014"
    (tmp_path / f"2026-05-22 {em} Learnings Review.md").write_text(
        "---\ntype: concept\n---\n# Learnings Review\n\nBack to [[Home]].\n",
        encoding="utf-8",
    )
    (tmp_path / "Home.md").write_text(
        "# Home\n\nSee [[2026-05-22 - Learnings Review]].\n", encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, "scripts/vault_health.py", "--path", str(tmp_path), "--json"],
        cwd=REPO_ROOT, check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"broken_link"' not in result.stdout, (
        "hyphen-written link to em-dash filename was flagged broken:\n" + result.stdout
    )


def _run_health_json(tmp_path):
    """Run vault_health.py --json and return the parsed result (skips the stdout header)."""
    result = subprocess.run(
        [sys.executable, "scripts/vault_health.py", "--path", str(tmp_path), "--json"],
        cwd=REPO_ROOT, check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout[result.stdout.index("{"):])


def test_health_duplicates_exempt_dated_series(tmp_path):
    """Issue #82: dated-series notes that share a descriptive title (a weekly
    review every Friday) must NOT be flagged as duplicates, but two genuinely
    same-named notes in normal folders still are."""
    reviews = tmp_path / "Reviews"
    reviews.mkdir()
    for d in ("2026-06-19", "2026-06-26"):
        (reviews / f"{d} - Weekly Review.md").write_text(
            f"---\ntype: review\n---\n# Weekly Review\nWeek of {d}.\n", encoding="utf-8"
        )
    # A real cross-folder duplicate with near-identical content.
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    body = "---\ntype: note\n---\n# Onboarding\nStep one, step two, step three.\n"
    (tmp_path / "A" / "Onboarding.md").write_text(body, encoding="utf-8")
    (tmp_path / "B" / "Onboarding.md").write_text(body, encoding="utf-8")

    data = _run_health_json(tmp_path)
    dup_msgs = [i["message"] for i in data["issues"] if i["type"] == "duplicate"]
    assert not any("weekly review" in m.lower() for m in dup_msgs), dup_msgs
    assert any("onboarding" in m.lower() for m in dup_msgs), dup_msgs


def test_health_excludes_export_bundle(tmp_path):
    """Issue #82 follow-up: the OKF export bundle (_export/) is a full copy of the
    vault, so scanning it made every note a duplicate of its export twin. _export
    must be excluded - the note and its copy should not be flagged or counted."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / "_export" / "okf" / "wiki").mkdir(parents=True)
    body = "---\ntype: note\n---\n# Spec\nThe spec body.\n"
    (tmp_path / "wiki" / "Spec.md").write_text(body, encoding="utf-8")
    (tmp_path / "_export" / "okf" / "wiki" / "Spec.md").write_text(body, encoding="utf-8")

    data = _run_health_json(tmp_path)
    assert data["total_notes"] == 1, data["total_notes"]
    assert not [i for i in data["issues"] if i["type"] == "duplicate"]


def test_health_broken_links_ignore_code_examples(tmp_path):
    """Issue #82: example wikilinks inside code fences / inline code must not be
    flagged broken; a real dangling link in prose still is."""
    (tmp_path / "Doc.md").write_text(
        "---\ntype: note\n---\n# Doc\n\n"
        "Use a link like ```\n[[Related Project]]\n``` or inline `[[Placeholder]]`.\n\n"
        "But this real one dangles: [[Nonexistent Target]].\n",
        encoding="utf-8",
    )
    data = _run_health_json(tmp_path)
    broken = [i["message"] for i in data["issues"] if i["type"] == "broken_link"]
    assert any("Nonexistent Target" in m for m in broken), broken
    assert not any("Related Project" in m or "Placeholder" in m for m in broken), broken


def _load_vault_ops():
    """Import the MCP connector's vault_ops module (pure stdlib, no mcp dep)."""
    import importlib

    mod_dir = REPO_ROOT / "integrations" / "obsidian-mcp-server"
    sys.path.insert(0, str(mod_dir))
    try:
        return importlib.import_module("vault_ops")
    finally:
        sys.path.remove(str(mod_dir))


def test_mcp_vault_ops_save_read_search_roundtrip(tmp_path, monkeypatch):
    """The MCP connector's core data tools must round-trip against a real vault:
    save_note writes an AI-first note (frontmatter + preamble + source: mcp marker)
    to Inbox/, read_note returns it, search finds it. Pure stdlib path - exercises
    the logic the MCP server wraps without needing the mcp package."""
    vault_ops = _load_vault_ops()
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))

    saved = vault_ops.save_note(
        "Hermes connector test",
        "A note about the Hermes agent reading the vault over MCP.",
        note_type="note",
        tags=["mcp", "hermes"],
    )
    rel = saved["saved"]
    assert rel.startswith("Inbox/")

    note = (vault / rel).read_text(encoding="utf-8")
    assert "ai-first: true" in note
    assert "source: mcp" in note
    assert "## For future Claude" in note

    read_back = vault_ops.read_note(rel)
    assert "Hermes agent" in read_back["content"]

    hits = vault_ops.search("hermes", limit=5)
    assert any(h["path"] == rel for h in hits)


def test_mcp_vault_ops_read_guards_path_escape(tmp_path, monkeypatch):
    """read_note must refuse paths that escape the vault root."""
    vault_ops = _load_vault_ops()
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "secret.md").write_text("outside the vault\n", encoding="utf-8")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))

    assert vault_ops.read_note("../secret.md").get("error")


def test_mcp_vault_ops_skills_exclude_niche(monkeypatch):
    """list_skills exposes the real commands but never the niche/agent-only ones,
    and get_skill blocks the excluded set (the #60 contract)."""
    vault_ops = _load_vault_ops()
    names = {s["name"] for s in vault_ops.list_skills()}
    assert "obsidian-save" in names
    assert names.isdisjoint({"obsidian-health", "obsidian-challenge", "create-command"})
    assert vault_ops.get_skill("obsidian-health").get("error")
    assert "instructions" in vault_ops.get_skill("obsidian-save")


def test_mcp_vault_ops_update_note_guarded_edit(tmp_path, monkeypatch):
    """update_note appends a section and merges scalar frontmatter on an existing
    note, preserves the tags block, stamps `updated`, and refuses a path escape
    and a non-existent note (curator-mode guards, #79)."""
    vault_ops = _load_vault_ops()
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    note = vault / "Project Alpha.md"
    note.write_text(
        "---\ntype: project\nstatus: active\ntags:\n  - work\nai-first: true\n---\n\n"
        "## For future Claude\nAlpha.\n",
        encoding="utf-8",
    )

    res = vault_ops.update_note(
        "Project Alpha.md",
        append="Shipped the adapter.",
        heading="Update",
        set_fields={"status": "done"},
    )
    assert res.get("updated") == "Project Alpha.md"
    text = note.read_text(encoding="utf-8")
    assert "status: done" in text and "status: active" not in text
    assert "updated:" in text
    assert "## Update" in text and "Shipped the adapter." in text
    assert "  - work" in text  # list frontmatter preserved verbatim

    # Guards: never create, never escape.
    assert vault_ops.update_note("Nope.md", append="x").get("error")
    assert vault_ops.update_note("../escape.md", append="x").get("error")


def test_mcp_vault_ops_validate_and_backlinks_and_health(tmp_path, monkeypatch):
    """validate_note flags a missing preamble + unresolved wikilink; backlinks
    finds the referencing note; vault_health reports the broken link."""
    vault_ops = _load_vault_ops()
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    (vault / "Home.md").write_text(
        "---\ntype: note\ndate: 2026-06-27\ntags:\n  - x\nai-first: true\n---\n\n"
        "## For future Claude\nSee [[Project Alpha]] and [[Ghost Note]].\n",
        encoding="utf-8",
    )
    (vault / "Project Alpha.md").write_text(
        "---\ntype: project\n---\n# Alpha\n",
        encoding="utf-8",
    )

    v = vault_ops.validate_note("Project Alpha.md")
    assert v["ok"] is False
    joined = " ".join(v["issues"])
    assert "For future Claude" in joined
    assert "date" in joined  # missing required key

    bl = vault_ops.backlinks("Project Alpha")
    assert "Home.md" in bl["backlinks"]

    health = vault_ops.vault_health()
    assert any(b["link"] == "Ghost Note" for b in health["broken_links"]["sample"])


def test_mcp_vault_ops_skips_claude_dir(tmp_path, monkeypatch):
    """The MCP connector must not scan a vault-local .claude/ config dir as notes
    (issue #80). search and vault_health should ignore it entirely."""
    vault_ops = _load_vault_ops()
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / ".claude" / "commands").mkdir(parents=True)
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    (vault / "wiki" / "Real.md").write_text("---\ntype: note\n---\nwidget content\n", encoding="utf-8")
    (vault / ".claude" / "CLAUDE.md").write_text("widget config\n", encoding="utf-8")
    (vault / ".claude" / "commands" / "save.md").write_text("widget command\n", encoding="utf-8")

    hits = {h["path"] for h in vault_ops.search("widget", limit=10)}
    assert "wiki/Real.md" in hits
    assert not any(p.startswith(".claude") for p in hits)
    assert vault_ops.vault_health()["notes_scanned"] == 1


def test_architect_scan_emits_manifest(tmp_path):
    """architect_scan.py must produce a JSON manifest with the expected shape
    on a minimal project (no network, no install)."""
    proj = tmp_path / "proj"
    (proj / "src" / "billing").mkdir(parents=True)
    (proj / "src" / "billing" / "charge.py").write_text("def charge():\n    pass\n", encoding="utf-8")
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "paymentbot"\ndependencies = ["requests"]\n', encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, "scripts/architect_scan.py", "--path", str(proj)],
        cwd=REPO_ROOT, check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    data = _json_from_stdout(result.stdout)
    assert data["name"] == "paymentbot"
    assert data["kind"] == "python"
    assert any(m["name"] == "billing" for m in data["modules"])
    assert "requests" in data["dependencies"]
    assert any(lang["language"] == "Python" for lang in data["languages"])
