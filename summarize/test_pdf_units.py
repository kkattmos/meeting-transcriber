#!/usr/bin/env python3
"""
Unit tests for the PDF export: summarize/framecrop.py and summarize/pdf.py.

The parts that need no third-party library (citation rewriting, document
splitting, crop geometry) are tested unconditionally. The end-to-end render is
skipped when weasyprint/markdown/Pillow aren't installed, which is exactly the
condition under which the pipeline degrades to markdown-only.

    python3 summarize/test_pdf_units.py
"""
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "lib"))

import framecrop  # noqa: E402
import mathrender  # noqa: E402
import pdf as pdf_export  # noqa: E402
from llm_client import FrameMeta  # noqa: E402

try:
    from PIL import Image
except ImportError:
    Image = None

HAVE_RENDERER = True
try:
    import weasyprint  # noqa: F401
    import markdown  # noqa: F401
except ImportError:
    HAVE_RENDERER = False


def make_frame(path, size=(960, 540), bg=(20, 20, 22), slide=None):
    """A synthetic frame: dark 'UI' with an optional bright 'slide' rectangle."""
    img = Image.new("RGB", size, bg)
    if slide:
        box, colour = slide
        for x in range(box[0], box[2]):
            for y in range(box[1], box[3]):
                img.putpixel((x, y), colour)
    img.save(path, "JPEG", quality=90)
    return path


@unittest.skipIf(Image is None, "Pillow is not installed")
class CropTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_slide_region_is_found_in_a_dark_frame(self):
        src = make_frame(self.dir / "f.jpg",
                         slide=((100, 60, 800, 460), (245, 245, 240)))
        box = framecrop.detect_crop(src, mode="slide")
        self.assertIsNotNone(box)
        left, top, right, bottom = box
        # JPEG and the downscaled analysis pass both blur the edges; ask only
        # that we landed on the bright rectangle, not on the whole frame.
        self.assertLess(abs(left - 100), 30)
        self.assertLess(abs(top - 60), 30)
        self.assertLess(abs(right - 800), 30)
        self.assertLess(abs(bottom - 460), 30)

    def test_letterbox_bars_are_trimmed_in_border_mode(self):
        src = make_frame(self.dir / "bars.jpg", bg=(0, 0, 0),
                         slide=((0, 100, 960, 440), (200, 200, 200)))
        box = framecrop.detect_crop(src, mode="border")
        self.assertIsNotNone(box)
        _, top, _, bottom = box
        self.assertGreater(top, 60)
        self.assertLess(bottom, 480)

    def test_a_uniformly_bright_frame_is_not_cropped_to_itself(self):
        # A full-screen camera shot has no slide; cropping must decline rather
        # than confidently return the whole frame as a "slide".
        src = make_frame(self.dir / "bright.jpg", bg=(180, 180, 180))
        self.assertIsNone(framecrop.detect_crop(src, mode="slide"))

    def test_tiny_bright_speck_is_rejected(self):
        # A cursor highlight or a white logo must not become "the slide".
        src = make_frame(self.dir / "speck.jpg",
                         slide=((10, 10, 40, 40), (255, 255, 255)))
        box = framecrop.detect_crop(src, mode="slide")
        if box:
            width = box[2] - box[0]
            self.assertGreater(width, 200)

    def test_mode_none_never_crops(self):
        src = make_frame(self.dir / "n.jpg",
                         slide=((100, 60, 800, 460), (245, 245, 240)))
        self.assertIsNone(framecrop.detect_crop(src, mode="none"))

    def test_crop_frame_writes_a_downscaled_copy(self):
        src = make_frame(self.dir / "big.jpg", size=(1920, 1080),
                         slide=((200, 120, 1600, 900), (250, 250, 245)))
        out = framecrop.crop_frame(src, self.dir / "out.jpg", mode="slide",
                                   max_width=640)
        self.assertTrue(Path(out).is_file())
        with Image.open(out) as img:
            self.assertLessEqual(img.width, 640)

    def test_crop_frame_survives_a_corrupt_image(self):
        bad = self.dir / "bad.jpg"
        bad.write_bytes(b"not an image")
        out = framecrop.crop_frame(bad, self.dir / "out2.jpg")
        self.assertTrue(Path(out).exists())


class DocumentSplitTest(unittest.TestCase):
    DOC = """<!-- meeting-transcriber
     source: https://youtu.be/abc
     model: anthropic/claude-opus-5
     prompt: lecture-claude.md
     run_id: yt_abc_20260904_120000
     generated: 2026-09-04
-->

Chapter N — <topic> (<date>)

# Graph Algorithms

Youtube Link: `https://youtu.be/abc`

<details>
    <summary> View Transcript </summary>

    hello there
    second line
</details>
<br>

Body text citing *(Frame 2 @ 30.0s)* and again (Frame 2).

<br><br>
"""

    def test_provenance_is_parsed_and_removed(self):
        body, transcript, prov = pdf_export._split_document(self.DOC)
        self.assertEqual(prov["model"], "anthropic/claude-opus-5")
        self.assertEqual(prov["run_id"], "yt_abc_20260904_120000")
        self.assertNotIn("meeting-transcriber", body)

    def test_transcript_is_lifted_out_and_unindented(self):
        body, transcript, _ = pdf_export._split_document(self.DOC)
        self.assertIn("hello there", transcript)
        self.assertTrue(transcript.startswith("hello there"))
        self.assertNotIn("View Transcript", body)

    def test_body_survives(self):
        body, _, _ = pdf_export._split_document(self.DOC)
        self.assertIn("# Graph Algorithms", body)
        self.assertIn("Frame 2", body)

    def test_a_plain_document_without_a_wrapper_is_untouched(self):
        body, transcript, prov = pdf_export._split_document("# Hi\n\nbody")
        self.assertEqual(body, "# Hi\n\nbody")
        self.assertEqual(transcript, "")
        self.assertEqual(prov, {})


class CombinedDocumentTest(unittest.TestCase):
    """--combine: several videos summarized as one, rendered into one PDF.

    The frames of every video go through the model and the PDF under ONE
    numbering, assigned once over the whole set (pdf.load_part_manifests).
    Numbering each manifest on its own is how two videos both end up with a
    "Frame 2" naming different pictures — a PDF that looks perfectly fine
    and has half its pictures wrong. The other half of the contract is that a
    frame's timestamp stays relative to its own video, so the caption has to
    say which video it is from.
    """

    def _manifest(self, directory, count, start_at=0.0):
        import json
        directory.mkdir(parents=True, exist_ok=True)
        frames = []
        for i in range(count):
            path = directory / f"f{i}.jpg"
            if Image is not None:
                make_frame(path, slide=((100, 60, 800, 480), (240, 240, 240)))
            else:
                path.write_bytes(b"not a jpeg")
            frames.append({"timestamp_s": start_at + i * 30.0,
                           "kind": "periodic", "path": str(path)})
        out = directory / "manifest.json"
        out.write_text(json.dumps({"frames": frames}))
        return out

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_part_manifests_are_numbered_once_in_video_order(self):
        a = self._manifest(self.dir / "a", 3)
        b = self._manifest(self.dir / "b", 2)
        c = self._manifest(self.dir / "c", 4)
        frames = pdf_export.load_part_manifests([a, b, c])
        self.assertEqual([f.number for f in frames],
                         [1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual([f.part for f in frames],
                         [1, 1, 1, 2, 2, 3, 3, 3, 3])
        # Every number resolves to a file from the right video, and the
        # timestamps restart with each one.
        by_number = {f.number: f for f in frames}
        self.assertIn("/a/", by_number[1].path)
        self.assertIn("/b/", by_number[4].path)
        self.assertEqual(by_number[4].timestamp_s, 0.0)
        self.assertIn("/c/", by_number[6].path)

    def test_a_video_with_no_frames_still_counts_as_a_part(self):
        # Drop the empty slot and every later video is tagged with the wrong
        # number — its frames would then be captioned as another video's.
        a = self._manifest(self.dir / "a", 2)
        c = self._manifest(self.dir / "c", 2)
        frames = pdf_export.load_part_manifests([a, None, c])
        self.assertEqual([f.number for f in frames], [1, 2, 3, 4])
        self.assertEqual([f.part for f in frames], [1, 1, 3, 3])

    def test_a_single_manifest_is_not_tagged_with_a_part(self):
        a = self._manifest(self.dir / "a", 2)
        frames = pdf_export.load_manifest_frames(a)
        self.assertEqual([f.part for f in frames], [0, 0])
        self.assertEqual([f.number for f in frames], [0, 0])   # caller numbers

    @unittest.skipIf(Image is None, "Pillow not installed")
    def test_citations_resolve_to_the_right_video_end_to_end(self):
        """The whole point: one citation per video, each to its own picture."""
        sys.path.insert(0, str(SCRIPT_DIR))
        import document

        a = self._manifest(self.dir / "a", 3)
        b = self._manifest(self.dir / "b", 3)
        frames = pdf_export.load_part_manifests([a, b])
        doc = document.build_document(
            "Video 1 shows (Frame 2 @ video 1 30.0s); video 2 shows "
            "(Frame 5 @ video 2 30.0s).",
            source="https://youtu.be/A", source_kind="youtube",
            transcript="words for A\n\nwords for B",
            videos=[{"source": "https://youtu.be/A", "kind": "youtube",
                     "title": "Lecture A"},
                    {"source": "https://youtu.be/B", "kind": "youtube",
                     "title": "Lecture B"}])

        body, transcript, prov = pdf_export._split_document(doc)
        self.assertEqual(prov.get("source_type"), "combined")
        self.assertIn("words for B", transcript)
        cited = pdf_export._cited_frame_numbers(body)
        self.assertEqual(sorted(cited), [2, 5])

        prepared = pdf_export._prepare_frames(
            frames, self.dir / "work", wanted=set(cited))
        self.assertEqual(sorted(prepared), [2, 5])
        self.assertIn(str(self.dir / "a"),
                      [f.path for f in frames if f.number == 2][0])
        self.assertIn(str(self.dir / "b"),
                      [f.path for f in frames if f.number == 5][0])
        # The same 30.0s in two videos: the caption tells them apart.
        self.assertEqual(prepared[2]["part"], 1)
        self.assertEqual(prepared[5]["part"], 2)
        sheet = pdf_export._appendix_frames(prepared)
        self.assertIn("Frame 2 — Video 1, 00:30", sheet)
        self.assertIn("Frame 5 — Video 2, 00:30", sheet)

    def test_a_single_recording_caption_names_no_video(self):
        prepared = {3: {"path": "/x.jpg", "timestamp": 30.0,
                        "kind": "periodic", "part": 0}}
        sheet = pdf_export._appendix_frames(prepared)
        self.assertIn("Frame 3 — 00:30", sheet)
        self.assertNotIn("Video", sheet)

    @unittest.skipUnless(HAVE_RENDERER and Image is not None,
                         "weasyprint/markdown/Pillow not installed")
    def test_a_combined_document_renders_to_one_pdf(self):
        sys.path.insert(0, str(SCRIPT_DIR))
        import document

        a = self._manifest(self.dir / "a", 3)
        b = self._manifest(self.dir / "b", 3)
        frames = pdf_export.load_part_manifests([a, b])
        doc = document.build_document(
            "Lecture A shows (Frame 2 @ video 1 30.0s); B shows "
            "(Frame 5 @ video 2 30.0s).",
            source="https://youtu.be/A", source_kind="youtube",
            transcript="words for A and B",
            videos=[{"source": "https://youtu.be/A", "kind": "youtube",
                     "title": "Lecture A"},
                    {"source": "https://youtu.be/B", "kind": "youtube"}])

        out = self.dir / "chapter.pdf"
        pdf_export.render(doc, out, frames=frames, title="chapter")
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 1000)
        # The scratch directory render() invented is its own to remove.
        self.assertFalse((self.dir / ".pdf-frames").exists())


class CitationTest(unittest.TestCase):
    def prepared(self, *numbers):
        return {n: {"path": f"/frames/frame_{n}.jpg", "timestamp": 30.0 * n,
                    "kind": "scene_change"} for n in numbers}

    def test_first_citation_becomes_a_figure(self):
        out = pdf_export._inline_citations(
            "<p>See *(Frame 2 @ 60.0s)* here.</p>", self.prepared(2), [])
        self.assertIn("<figure", out)
        self.assertIn("/frames/frame_2.jpg", out)

    def test_repeat_citations_do_not_repeat_the_image(self):
        out = pdf_export._inline_citations(
            "<p>(Frame 2) and later (Frame 2) again.</p>", self.prepared(2), [])
        self.assertEqual(out.count("<figure"), 1)

    def test_unknown_frame_numbers_are_left_alone(self):
        out = pdf_export._inline_citations(
            "<p>(Frame 99)</p>", self.prepared(1), [])
        self.assertNotIn("<figure", out)
        self.assertIn("Frame 99", out)

    def test_bracket_form_is_matched_too(self):
        out = pdf_export._inline_citations(
            "<p>[frame 1 @ 30.0s (scene_change)]</p>", self.prepared(1), [])
        self.assertIn("<figure", out)

    def test_a_figure_never_lands_inside_a_table_cell(self):
        html = "<table><tr><td>(Frame 1)</td><td>x</td></tr></table>"
        out = pdf_export._inline_citations(html, self.prepared(1), [])
        # The figure must appear after the row closes, not inside the <td>.
        self.assertLess(out.index("</tr>"), out.index("<figure"))

    def test_slide_citations_use_the_resource_images(self):
        slides = [{"path": "/res/deck-1.jpg", "label": "deck.pdf p.1"}]
        out = pdf_export._inline_citations("<p>(Slide 1)</p>", {}, slides)
        self.assertIn("deck-1.jpg", out)

    def test_timestamp_formatting(self):
        self.assertEqual(pdf_export._fmt_timestamp(65), "01:05")
        self.assertEqual(pdf_export._fmt_timestamp(3725), "1:02:05")


class OutputToggleTest(unittest.TestCase):
    def test_defaults_are_on(self):
        import os
        for var in ("SUMMARY_WRITE_PDF", "SUMMARY_WRITE_MARKDOWN"):
            os.environ.pop(var, None)
        self.assertTrue(pdf_export.want_pdf())
        self.assertTrue(pdf_export.want_markdown())

    def test_zero_turns_them_off(self):
        import os
        os.environ["SUMMARY_WRITE_PDF"] = "0"
        try:
            self.assertFalse(pdf_export.want_pdf())
        finally:
            os.environ.pop("SUMMARY_WRITE_PDF", None)


@unittest.skipUnless(HAVE_RENDERER and Image is not None,
                     "weasyprint/markdown/Pillow not installed")
class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_render_produces_a_pdf_with_the_frame_embedded(self):
        import os
        frame_path = make_frame(self.dir / "scene_00001.jpg",
                                slide=((80, 40, 880, 500), (250, 250, 245)))
        frames = [FrameMeta(timestamp_s=30.0, kind="scene_change",
                            path=str(frame_path))]
        os.environ["PDF_FRAMES"] = "contact"
        try:
            out = pdf_export.render(
                DocumentSplitTest.DOC, self.dir / "out.pdf", frames=frames,
                work_dir=self.dir / "work")
        finally:
            os.environ.pop("PDF_FRAMES", None)
        self.assertTrue(Path(out).is_file())
        data = Path(out).read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        # An embedded JPEG makes the file substantially bigger than the text.
        self.assertGreater(len(data), 5000)

    def test_render_without_frames_still_works(self):
        out = pdf_export.render("# Title\n\nSome body text.",
                                self.dir / "plain.pdf")
        self.assertTrue(Path(out).read_bytes().startswith(b"%PDF"))

    def _captured_document(self, markdown_text, **kwargs):
        """Render with WeasyPrint stubbed out, returning the HTML it was given.

        The assembly decisions — which appendix, inline figure or not, hidden
        layer present — are visible in that string and invisible in the PDF
        without a reader dependency.
        """
        captured = {}

        class FakeHTML:
            def __init__(self, string=None, base_url=None):
                captured["doc"] = string

            def write_pdf(self, path, stylesheets=None):
                Path(path).write_bytes(b"%PDF-1.7 stub")

        import weasyprint
        with mock.patch.object(weasyprint, "HTML", FakeHTML):
            pdf_export.render(markdown_text, self.dir / "captured.pdf",
                              **kwargs)
        return captured["doc"]

    def test_contact_mode_puts_the_frames_in_an_appendix_not_the_body(self):
        import os
        frame_path = make_frame(self.dir / "scene_00001.jpg",
                                slide=((80, 40, 880, 500), (250, 250, 245)))
        frames = [FrameMeta(timestamp_s=2.0, kind="scene_change",
                            path=str(frame_path))]
        os.environ["PDF_FRAMES"] = "contact"
        try:
            doc = self._captured_document(
                "# T\n\nA point *(Frame 1 @ 2.0s)*.", frames=frames)
        finally:
            os.environ.pop("PDF_FRAMES", None)
        self.assertIn("Appendix A \u2014 Keyframes", doc)
        self.assertIn('figure class="thumb"', doc)
        self.assertNotIn('figure class="frame"', doc)
        # The citation itself survives, so the appendix has something to
        # resolve against.
        self.assertIn("Frame 1", doc)

    def test_inline_mode_still_puts_the_figure_in_the_body(self):
        import os
        frame_path = make_frame(self.dir / "scene_00001.jpg",
                                slide=((80, 40, 880, 500), (250, 250, 245)))
        frames = [FrameMeta(timestamp_s=2.0, kind="scene_change",
                            path=str(frame_path))]
        os.environ["PDF_FRAMES"] = "inline"
        try:
            doc = self._captured_document(
                "# T\n\nA point *(Frame 1 @ 2.0s)*.", frames=frames)
        finally:
            os.environ.pop("PDF_FRAMES", None)
        self.assertIn('figure class="frame"', doc)
        self.assertNotIn("Appendix A", doc)

    def test_the_sheet_is_the_summary_alone_by_default(self):
        # No keyframe appendix, no transcript layer, no reference slides:
        # the study sheet the operator prints is the notes and nothing else.
        # The markdown beside it still carries the transcript.
        frame_path = make_frame(self.dir / "scene_00001.jpg",
                                slide=((80, 40, 880, 500), (250, 250, 245)))
        frames = [FrameMeta(timestamp_s=30.0, kind="scene_change",
                            path=str(frame_path))]
        doc = self._captured_document(DocumentSplitTest.DOC, frames=frames)
        self.assertNotIn("Appendix", doc)
        self.assertNotIn("hidden-transcript", doc)
        self.assertNotIn("BEGIN_TRANSCRIPT", doc)
        self.assertNotIn("hello there", doc)
        self.assertNotIn('figure class="thumb"', doc)
        # The citation stays — faded, not gone.
        self.assertIn('<span class="cite">(Frame 2 @ 30.0s)</span>', doc)

    def test_the_transcript_can_still_be_asked_for(self):
        import os
        os.environ["PDF_TRANSCRIPT"] = "hidden"
        try:
            doc = self._captured_document(DocumentSplitTest.DOC)
        finally:
            os.environ.pop("PDF_TRANSCRIPT", None)
        self.assertIn("hidden-transcript", doc)
        self.assertIn("BEGIN_TRANSCRIPT", doc)
        self.assertNotIn("Appendix C", doc)

    def test_the_legacy_header_is_dropped_and_the_models_title_kept(self):
        # A document written before 2026-09-13 carries the chapter
        # placeholder and the video title over the model's own H1. The
        # sheet prints neither: the model's title is the one that names the
        # material.
        old = DocumentSplitTest.DOC.replace(
            "Body text citing", "# Graphs, Properly\n\nBody text citing")
        doc = self._captured_document(old)
        self.assertNotIn("Chapter N", doc)
        self.assertNotIn("Graph Algorithms", doc)
        self.assertIn("<title>Graphs, Properly</title>", doc)
        self.assertEqual(doc.count("<h1>"), 1)
        # ...and it still heads the page, above the link lines.
        self.assertLess(doc.index("<h1>"), doc.index("Youtube Link"))

    def test_the_provenance_line_sits_under_the_title(self):
        doc = self._captured_document(DocumentSplitTest.DOC)
        self.assertLess(doc.index("<h1>"), doc.index('class="docmeta"'))
        self.assertLess(doc.index("<h1>"), doc.index('class="source"'))

    def test_a_list_straight_after_a_paragraph_is_a_list(self):
        doc = self._captured_document(
            "# T\n\nThree modules:\n1. Signals\n2. Optimization\n")
        self.assertIn("<ol>", doc)

    def test_a_single_heading_is_never_dropped(self):
        doc = self._captured_document(DocumentSplitTest.DOC)
        self.assertNotIn("Chapter N", doc)
        self.assertIn("<h1>Graph Algorithms</h1>", doc)

    def test_nested_bullets_nest(self):
        doc = self._captured_document(
            "# T\n\n* top\n  * second\n    * third\n* top again\n")
        self.assertEqual(doc.count("<ul>"), 3)
        self.assertIn("third", doc)

    def test_frame_citations_are_faded_in_every_spelling(self):
        doc = self._captured_document(
            "# T\n\nA *(Video 1, Frame 52 @ 0:08:52)* b "
            "*(Frame 280 @ Video 1 [02:21:00])* c "
            "*(Video 1, Frames 20\u201326 @ 0:05:25\u20130:05:41)* d "
            "*(Video 6, [02:51:30])* e (not one) (Video 1 alone).")
        self.assertEqual(doc.count('class="cite"'), 4)
        self.assertIn(".cite { opacity: 0.3; }", pdf_export._css())

    def test_maths_reaches_the_page_as_an_image(self):
        doc = self._captured_document("# T\n\nRate $R$ bits per second.")
        self.assertIn("data:image/svg+xml;base64,", doc)
        self.assertNotIn("$R$", doc)

    def test_a_missing_frame_file_is_skipped_not_fatal(self):
        frames = [FrameMeta(timestamp_s=1.0, kind="periodic",
                            path=str(self.dir / "gone.jpg"))]
        out = pdf_export.render("# T\n\n(Frame 1)", self.dir / "missing.pdf",
                                frames=frames)
        self.assertTrue(Path(out).is_file())



class CropWorkDirLifetimeTest(unittest.TestCase):
    """Who owns the cropped-frame scratch directory.

    The cropped copies are intermediates: WeasyPrint embeds the image bytes
    into the PDF, so nothing reads them after render() returns. Left behind,
    they put a .pdf-frames directory of run-independent filenames next to the
    deliverable — which on a synced PDF_DIR meant re-uploading a directory
    nobody would ever open.
    """

    def setUp(self):
        try:
            import weasyprint  # noqa: F401
        except ImportError:
            self.skipTest("weasyprint not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "out" / "summary.pdf"
        self.md = "# Title\n\nA point *(Frame 1 @ 2.0s)*.\n"

    def test_invented_work_dir_is_removed(self):
        pdf_export.render(self.md, self.out, frames=[])
        self.assertTrue(self.out.exists())
        self.assertFalse((self.out.parent / ".pdf-frames").exists())

    def test_explicit_work_dir_is_kept(self):
        # The caller named it, so the caller owns it.
        work = Path(self.tmp.name) / "mycrops"
        pdf_export.render(self.md, self.out, frames=[], work_dir=work)
        self.assertTrue(work.is_dir())

    def test_work_dir_is_removed_even_when_rendering_fails(self):
        # `finally`, not a trailing statement — a crash must not strand it.
        with mock.patch.object(pdf_export, "_split_document",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                pdf_export.render(self.md, self.out, frames=[])
        self.assertFalse((self.out.parent / ".pdf-frames").exists())


class MathExtractionTest(unittest.TestCase):
    """Lifting LaTeX out of the markdown before python-markdown eats it."""

    def test_inline_and_display_are_both_taken(self):
        text, exprs = mathrender.extract(
            "Rate $R$ bits.\n\n$$d = \\frac{L}{R}$$\n")
        self.assertEqual(len(exprs), 2)
        self.assertTrue(any(e["display"] for e in exprs))
        self.assertTrue(any(not e["display"] for e in exprs))
        self.assertNotIn("$", text)

    def test_maths_in_a_code_fence_is_left_alone(self):
        src = "```\ncost=$5 per $GB\n```\n"
        text, exprs = mathrender.extract(src)
        self.assertEqual(exprs, [])
        self.assertEqual(text, src)

    def test_currency_is_not_mistaken_for_maths(self):
        _, exprs = mathrender.extract("It costs $5 and change $ then.")
        self.assertEqual(exprs, [])
        # ...but arithmetic that happens to start with a digit is maths.
        _, exprs = mathrender.extract("Length $4 + 4 - 1 = 7$ samples.")
        self.assertEqual(len(exprs), 1)

    def test_display_fractions_are_full_size(self):
        self.assertIn(r"\dfrac{a}{b}", mathrender._prepare(r"\frac{a}{b}", True))
        self.assertIn(r"\frac{a}{b}", mathrender._prepare(r"\frac{a}{b}", False))
        self.assertNotIn("dfrac", mathrender._prepare(r"\frac{a}{b}", False))

    def test_restore_puts_snippets_back_in_order(self):
        text, exprs = mathrender.extract("a $x$ b $y$")
        out = mathrender.restore(text, ["<X/>", "<Y/>"])
        self.assertEqual(out, "a <X/> b <Y/>")

    def test_a_display_token_alone_in_a_paragraph_replaces_it(self):
        text, _ = mathrender.extract("$$x$$")
        html_body = f"<p>{text.strip()}</p>"
        out = mathrender.restore(html_body, ['<span class="math-block"></span>'])
        self.assertNotIn("<p>", out)

    def test_a_top_level_line_break_stacks_but_an_environments_does_not(self):
        rows = mathrender._rows(
            r"a = b \\ \begin{cases} 1 \\ 2 \end{cases} \\[6pt] c")
        self.assertEqual(len(rows), 3)
        self.assertIn("\\begin{cases} 1 \\\\ 2 \\end{cases}", rows[1])

    def test_environments_are_cut_out_and_their_cells_split(self):
        segs = mathrender._segments(
            r"x = \left( \begin{array}{cc} 1 & 2 \\ 3 & 4 \end{array} \right) y")
        self.assertEqual([s[0] for s in segs], ["tex", "env", "tex"])
        _kind, name, spec, body, left, right = segs[1]
        self.assertEqual((name, spec, left, right), ("array", "cc", "(", ")"))
        self.assertNotIn("\\left", segs[0][1])
        self.assertNotIn("\\right", segs[2][1])
        self.assertEqual(mathrender._split_top_level_amp("1 & 2 \\& 3"),
                         ["1 ", " 2 \\& 3"])

    def test_nested_environments_close_at_the_right_end(self):
        found = mathrender._find_env(
            r"\begin{cases} \begin{cases} a \end{cases} \\ b \end{cases} z")
        self.assertEqual(found[2], "cases")
        self.assertTrue(found[3] is None)
        self.assertEqual(found[4].strip(), r"\begin{cases} a \end{cases} \\ b")
        self.assertEqual(found[1], len(r"\begin{cases} \begin{cases} a \end{cases} \\ b \end{cases}"))

    @unittest.skipUnless(mathrender.available(), "matplotlib not installed")
    def test_cases_and_matrices_become_one_image(self):
        # mathtext has no environments; the composer builds them from cells.
        # What the page gets is still a single baseline-aligned <img>.
        for tex in (r"u(t) = \begin{cases} 1, & t > 0 \\ 0, & t < 0 \end{cases}",
                    r"\mathbf{W} = \begin{bmatrix} 1 & 1 \\ 1 & -j \end{bmatrix}",
                    r"\begin{aligned} a &= b \\ c &= d \end{aligned}",
                    r"\begin{pmatrix} \begin{cases} a \\ b \end{cases} \\ 1 \end{pmatrix}"):
            out = mathrender._one(tex, True, mathrender._engine(), 8.0, "#000")
            self.assertEqual(out.count("<img"), 1, tex)
            self.assertNotIn("math-fallback", out, tex)
            self.assertIn("vertical-align:", out)

    @unittest.skipUnless(mathrender.available(), "matplotlib not installed")
    def test_the_composite_carries_each_glyph_once(self):
        box = mathrender._layout(
            r"\begin{bmatrix} 1 & 1 \\ 1 & 1 \end{bmatrix}",
            mathrender._engine(), 8.0, "#000")
        svg = mathrender._svg_document(box).decode()
        self.assertEqual(svg.count('<path id="'), len(box.defs))
        self.assertEqual(svg.count("<path d="), 2)   # the two brackets
        self.assertNotIn('id="figure_1"', svg)
        self.assertGreater(box.height, 2 * 8.0)      # two rows tall
        self.assertLess(box.depth, box.height / 2)   # centred on the axis

    def test_an_unknown_environment_falls_back_to_text(self):
        out = mathrender._one(r"\begin{substack} a \\ b \end{substack}",
                              False, mathrender._engine(), 8.0, "#000")
        self.assertIn("math-fallback", out)

    def test_digits_go_upright_outside_text_groups(self):
        out = mathrender._upright_digits(r"2 \times 10^8 \text{ 1 Gbps}")
        self.assertIn(r"\mathrm{2}", out)
        self.assertIn(r"\mathrm{10}", out)
        # Inside \text{} they were already upright; don't touch them.
        self.assertIn(r"\text{ 1 Gbps}", out)

    def test_unparseable_maths_degrades_to_text_not_an_exception(self):
        html_out = mathrender._one(r"\begin{cases} a \\ b \end{cases}",
                                   True, None, 8.0, "#000")
        self.assertIn("math-fallback", html_out)
        self.assertNotIn("<img", html_out)

    def test_fallback_keeps_the_symbols_readable(self):
        out = mathrender._fallback_html(r"L_{max} \times 10^6 \approx R")
        self.assertIn("<sub>max</sub>", out)
        self.assertIn("\u00d7", out)
        self.assertNotIn("\\times", out)

    def test_rendering_is_deduplicated(self):
        calls = []

        def engine(tex, size, color):
            calls.append(tex)
            return b"<svg/>", 10.0, 8.0, 2.0

        exprs = [{"tex": "L", "display": False}] * 3
        with mock.patch.object(mathrender, "_engine", return_value=engine):
            out = mathrender.render_all(exprs, size_pt=8.0)
        self.assertEqual(len(out), 3)
        self.assertEqual(len(calls), 1)

    def test_the_image_carries_its_own_baseline(self):
        def engine(tex, size, color):
            return b"<svg/>", 12.0, 9.0, 2.5

        with mock.patch.object(mathrender, "_engine", return_value=engine):
            out = mathrender.render_all([{"tex": "L", "display": False}])
        self.assertIn("vertical-align:-2.50pt", out[0])
        self.assertIn("data:image/svg+xml;base64,", out[0])


class FrameSelectionTest(unittest.TestCase):
    def test_compound_citations_yield_every_frame(self):
        numbers = pdf_export._cited_frame_numbers(
            "<p>(Frame 33 @ 0:34:40, Frame 15 @ 01:43:30)</p>")
        self.assertEqual(numbers, [33, 15])

    def test_each_frame_is_listed_once(self):
        numbers = pdf_export._cited_frame_numbers("(Frame 2) x (Frame 2)")
        self.assertEqual(numbers, [2])

    def test_modes_fall_back_to_the_default_when_misspelt(self):
        import os
        os.environ["PDF_FRAMES"] = "sideways"
        try:
            self.assertEqual(pdf_export.frames_mode(), "none")
        finally:
            os.environ.pop("PDF_FRAMES", None)

    def test_font_size_rejects_nonsense(self):
        import os
        for value, expected in (("", 8.0), ("9.5", 9.5), ("abc", 8.0),
                                ("400", 8.0)):
            os.environ["PDF_FONT_SIZE"] = value
            try:
                self.assertEqual(pdf_export._font_size(), expected)
            finally:
                os.environ.pop("PDF_FONT_SIZE", None)


class HiddenTranscriptTest(unittest.TestCase):
    def test_markers_delimit_the_layer(self):
        out = pdf_export._hidden_transcript("hello there")
        self.assertIn("BEGIN_TRANSCRIPT", out)
        self.assertIn("END_TRANSCRIPT", out)
        self.assertIn("hello there", out)

    def test_it_is_chunked_under_popplers_per_page_limit(self):
        # One block of 85k characters is complete in the PDF but comes back
        # truncated from pdftotext, which stops at ~50k per page.
        out = pdf_export._hidden_transcript("x" * 100000)
        self.assertEqual(out.count("hidden-next"), 2)
        self.assertEqual(out.count("BEGIN_TRANSCRIPT"), 1)
        self.assertEqual(out.count("END_TRANSCRIPT"), 1)

    def test_an_empty_transcript_adds_nothing(self):
        self.assertEqual(pdf_export._hidden_transcript("   "), "")

    def test_html_in_the_transcript_cannot_break_out(self):
        out = pdf_export._hidden_transcript("</div><script>x</script>")
        self.assertNotIn("<script>", out)


@unittest.skipIf(Image is None, "Pillow is not installed")
class BlankFrameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_solid_black_frame_is_blank(self):
        path = make_frame(self.dir / "black.jpg", bg=(0, 0, 0))
        self.assertTrue(framecrop.is_blank(path))

    def test_a_frame_with_a_slide_on_it_is_not(self):
        path = make_frame(self.dir / "slide.jpg",
                          slide=((100, 60, 800, 460), (245, 245, 240)))
        self.assertFalse(framecrop.is_blank(path))

    def test_blank_frames_are_dropped_from_the_selection(self):
        black = make_frame(self.dir / "b.jpg", bg=(0, 0, 0))
        good = make_frame(self.dir / "g.jpg",
                          slide=((100, 60, 800, 460), (245, 245, 240)))
        frames = [FrameMeta(timestamp_s=1.0, kind="scene_change",
                            path=str(black)),
                  FrameMeta(timestamp_s=2.0, kind="periodic", path=str(good))]
        prepared = pdf_export._prepare_frames(frames, self.dir / "work")
        self.assertNotIn(1, prepared)
        self.assertIn(2, prepared)

    def test_a_frame_keeps_its_recording_number_in_a_subset(self):
        """The PDF must resolve citations by the number the model was shown.

        Passing pdf.render() a subset of the manifest used to renumber it from
        1, so "Frame 3" printed whatever happened to be third in the subset.
        """
        import llm_client
        paths = [make_frame(self.dir / f"f{i}.jpg",
                            slide=((100, 60, 800, 460), (245, 245, 240)))
                 for i in range(4)]
        frames = llm_client.assign_numbers(
            [FrameMeta(timestamp_s=float(i), kind="periodic", path=str(p))
             for i, p in enumerate(paths)])
        prepared = pdf_export._prepare_frames(frames[2:], self.dir / "work")
        self.assertEqual(sorted(prepared), [3, 4])

    def test_only_the_wanted_frames_are_cropped(self):
        paths = [make_frame(self.dir / f"f{i}.jpg",
                            slide=((100, 60, 800, 460), (245, 245, 240)))
                 for i in range(3)]
        frames = [FrameMeta(timestamp_s=float(i), kind="periodic",
                            path=str(p)) for i, p in enumerate(paths)]
        prepared = pdf_export._prepare_frames(frames, self.dir / "work",
                                              wanted={2})
        self.assertEqual(list(prepared), [2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
