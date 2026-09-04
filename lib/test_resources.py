#!/usr/bin/env python3
"""
Unit tests for lib/resources.py — the slides/reference-material collector.

No network: the GitHub path is exercised against a real *local* git repository
created in a temp dir, which is what `git clone` sees anyway. Tests that need a
converter we may not have installed (pdftotext, LibreOffice) are skipped rather
than failed — the module's contract is that a missing converter degrades the
summary, not that every box has one.

    python3 lib/test_resources.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import resources  # noqa: E402


class ParseSpecTest(unittest.TestCase):
    def test_plain_github_url(self):
        s = resources.parse_spec("https://github.com/acme/course")
        self.assertEqual((s["kind"], s["owner"], s["repo"]),
                         ("github", "acme", "course"))
        self.assertIsNone(s["ref"])

    def test_at_branch(self):
        s = resources.parse_spec("https://github.com/acme/course@week4")
        self.assertEqual(s["ref"], "week4")

    def test_tree_url_carries_branch_and_subpath(self):
        s = resources.parse_spec(
            "https://github.com/acme/course/tree/main/lectures/week4")
        self.assertEqual(s["ref"], "main")
        self.assertEqual(s["subpath"], "lectures/week4")

    def test_dot_git_suffix_is_stripped(self):
        self.assertEqual(resources.parse_spec(
            "https://github.com/acme/course.git")["repo"], "course")

    def test_scheme_is_optional(self):
        self.assertEqual(resources.parse_spec("github.com/acme/course")["kind"],
                         "github")

    def test_local_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = resources.parse_spec(tmp)
            self.assertEqual(s["kind"], "local")
            self.assertEqual(s["path"], Path(tmp))

    def test_missing_local_path_is_an_error(self):
        # A typo'd path is worth failing on: the alternative is a summary that
        # silently used none of the material you asked for.
        with self.assertRaises(ValueError):
            resources.parse_spec("/definitely/not/here/slides.pdf")

    def test_other_urls_are_rejected_with_guidance(self):
        with self.assertRaises(ValueError) as ctx:
            resources.parse_spec("https://gitlab.com/acme/course")
        self.assertIn("github.com", str(ctx.exception))

    def test_specs_split_on_commas_and_newlines(self):
        self.assertEqual(
            resources.parse_specs_arg("a, b\nc ,, d"),
            ["a", "b", "c", "d"])

    def test_empty_specs(self):
        self.assertEqual(resources.parse_specs_arg(""), [])
        self.assertEqual(resources.parse_specs_arg(None), [])


class ExtractTextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_markdown_is_read_verbatim(self):
        p = self.dir / "notes.md"
        p.write_text("# Week 4\n\nBellman-Ford runs in O(VE).")
        self.assertIn("Bellman-Ford", resources.extract_text(p))

    def test_unknown_binary_extension_yields_nothing(self):
        p = self.dir / "movie.mp4"
        p.write_bytes(b"\x00\x01\x02")
        self.assertEqual(resources.extract_text(p), "")

    def test_pptx_text_comes_out_of_the_slide_xml(self):
        # A minimal but real .pptx: two slides, each one <a:t> run. The
        # stdlib zipfile path is what runs on a box without python-pptx.
        p = self.dir / "deck.pptx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("ppt/slides/slide1.xml",
                        '<p:sld><a:p><a:t>Dijkstra</a:t></a:p></p:sld>')
            zf.writestr("ppt/slides/slide2.xml",
                        '<p:sld><a:p><a:t>Priority &amp; queue</a:t></a:p></p:sld>')
        text = resources.extract_text(p)
        self.assertIn("Dijkstra", text)
        self.assertIn("Priority & queue", text)

    def test_docx_text_comes_out_of_document_xml(self):
        p = self.dir / "handout.docx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("word/document.xml",
                        '<w:document><w:p><w:r><w:t>Handout body</w:t>'
                        '</w:r></w:p></w:document>')
        self.assertIn("Handout body", resources.extract_text(p))

    def test_a_corrupt_office_file_is_not_fatal(self):
        p = self.dir / "broken.pptx"
        p.write_bytes(b"not a zip")
        self.assertEqual(resources.extract_text(p), "")


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cache = self.dir / "cache"
        self.material = self.dir / "material"
        (self.material / "sub").mkdir(parents=True)
        (self.material / "week4.md").write_text("Bellman-Ford is O(VE).")
        (self.material / "sub" / "notes.txt").write_text("second file")
        (self.material / "ignore.bin").write_bytes(b"\x00")

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, specs, **kw):
        return resources.collect(specs, cache_root=self.cache,
                                 want_images=False, **kw)

    def test_local_directory_is_walked(self):
        bundle = self.collect([str(self.material)])
        labels = sorted(f.label for f in bundle.files)
        self.assertEqual(labels, ["sub/notes.txt", "week4.md"])

    def test_uninteresting_files_are_skipped(self):
        bundle = self.collect([str(self.material)])
        self.assertNotIn("ignore.bin", [f.label for f in bundle.files])

    def test_single_file_spec(self):
        bundle = self.collect([str(self.material / "week4.md")])
        self.assertEqual([f.label for f in bundle.files], ["week4.md"])

    def test_text_block_labels_each_file(self):
        block = self.collect([str(self.material)]).text_block()
        self.assertIn("### week4.md", block)
        self.assertIn("Bellman-Ford", block)

    def test_skipped_dirs_are_not_walked(self):
        (self.material / "node_modules").mkdir()
        (self.material / "node_modules" / "readme.md").write_text("noise")
        bundle = self.collect([str(self.material)])
        self.assertNotIn("node_modules/readme.md",
                         [f.label for f in bundle.files])

    def test_budget_truncates_and_says_so(self):
        old = os.environ.get("RESOURCE_MAX_CHARS")
        os.environ["RESOURCE_MAX_CHARS"] = "10"
        try:
            bundle = self.collect([str(self.material)])
            self.assertTrue(bundle.truncated)
            self.assertIn("truncated", bundle.text_block())
            self.assertLessEqual(sum(len(f.text) for f in bundle.files), 10)
        finally:
            if old is None:
                os.environ.pop("RESOURCE_MAX_CHARS", None)
            else:
                os.environ["RESOURCE_MAX_CHARS"] = old

    def test_oversized_files_are_skipped_with_a_note(self):
        old = os.environ.get("RESOURCE_MAX_FILE_MB")
        os.environ["RESOURCE_MAX_FILE_MB"] = "0.000001"   # ~1 byte
        try:
            bundle = self.collect([str(self.material)])
            self.assertFalse(bundle.files)
            self.assertTrue(any("skipped" in n for n in bundle.notes))
        finally:
            if old is None:
                os.environ.pop("RESOURCE_MAX_FILE_MB", None)
            else:
                os.environ["RESOURCE_MAX_FILE_MB"] = old

    def test_empty_specs_gives_an_empty_bundle(self):
        self.assertFalse(self.collect([]))


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitHubTest(unittest.TestCase):
    """The GitHub path, against a local repo — no network involved.

    fetch_github() only ever runs `git clone`/`fetch`, so a file:// origin
    exercises exactly the same code path a real repository would.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.origin = self.dir / "origin"
        self.origin.mkdir()
        run = lambda *a: subprocess.run(a, cwd=self.origin, check=True,
                                        capture_output=True)
        run("git", "init", "-q", "-b", "main")
        run("git", "config", "user.email", "t@example.com")
        run("git", "config", "user.name", "Test")
        (self.origin / "slides.md").write_text("Chapter 4 — Dijkstra")
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "initial")

    def tearDown(self):
        self.tmp.cleanup()

    def test_clone_then_reuse(self):
        source = {"kind": "github", "owner": "acme", "repo": "course",
                  "ref": None, "subpath": "", "spec": str(self.origin)}
        # Point the clone at the local repo by pre-seeding the cache with it,
        # which is the same "already cloned" path a second run takes.
        cache = self.dir / "cache"
        dest = cache / "acme_course"
        cache.mkdir()
        subprocess.run(["git", "clone", "-q", str(self.origin), str(dest)],
                       check=True, capture_output=True)
        root, desc = resources.fetch_github(source, cache)
        self.assertEqual(root, dest)
        self.assertIn("github.com/acme/course", desc)
        self.assertTrue((root / "slides.md").is_file())

    def test_missing_subpath_is_reported(self):
        cache = self.dir / "cache"
        dest = cache / "acme_course"
        cache.mkdir()
        subprocess.run(["git", "clone", "-q", str(self.origin), str(dest)],
                       check=True, capture_output=True)
        source = {"kind": "github", "owner": "acme", "repo": "course",
                  "ref": None, "subpath": "nope", "spec": "acme/course"}
        with self.assertRaises(RuntimeError) as ctx:
            resources.fetch_github(source, cache)
        self.assertIn("nope", str(ctx.exception))

    def test_unfetchable_repo_degrades_to_a_note(self):
        # collect() must not raise for a source it can't reach: the run should
        # continue without the material rather than die before summarizing.
        bundle = resources.collect(
            ["https://github.com/this-org-does-not-exist-xyz/nope@main"],
            cache_root=self.dir / "cache2", want_images=False)
        self.assertFalse(bundle.files)
        self.assertTrue(bundle.notes)

    def test_token_is_redacted_from_errors(self):
        text = resources._redact(
            "fatal: could not read https://x-access-token:ghp_secret@github.com/a/b")
        self.assertNotIn("ghp_secret", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
