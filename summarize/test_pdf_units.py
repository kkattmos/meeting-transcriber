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
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "lib"))

import framecrop  # noqa: E402
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
        frame_path = make_frame(self.dir / "scene_00001.jpg",
                                slide=((80, 40, 880, 500), (250, 250, 245)))
        frames = [FrameMeta(timestamp_s=30.0, kind="scene_change",
                            path=str(frame_path))]
        out = pdf_export.render(
            DocumentSplitTest.DOC, self.dir / "out.pdf", frames=frames,
            work_dir=self.dir / "work")
        self.assertTrue(Path(out).is_file())
        data = Path(out).read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        # An embedded JPEG makes the file substantially bigger than the text.
        self.assertGreater(len(data), 5000)

    def test_render_without_frames_still_works(self):
        out = pdf_export.render("# Title\n\nSome body text.",
                                self.dir / "plain.pdf")
        self.assertTrue(Path(out).read_bytes().startswith(b"%PDF"))

    def test_a_missing_frame_file_is_skipped_not_fatal(self):
        frames = [FrameMeta(timestamp_s=1.0, kind="periodic",
                            path=str(self.dir / "gone.jpg"))]
        out = pdf_export.render("# T\n\n(Frame 1)", self.dir / "missing.pdf",
                                frames=frames)
        self.assertTrue(Path(out).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
