#!/usr/bin/env python3
"""
Collect the lecturer's own materials — slides, notes, handouts — and hand them
to the summarizer as reference material.

A recording plus a transcript is what the class *said*. The slides are what it
*meant*: correct spellings of technical terms, the notation actually used, the
numbering of the sections. Feeding them in alongside the transcript is the
cheapest available fix for ASR mangling of domain vocabulary.

Sources ("resource specs") are given with --resources (repeatable) or the
RESOURCES env var, and may be:

    https://github.com/owner/repo                 default branch, whole repo
    https://github.com/owner/repo@lecture-03      that branch
    https://github.com/owner/repo/tree/main/wk4   a branch and a subdirectory
    /srv/course/week4/slides.pdf                  a local file
    ~/course/week4                                a local directory

GitHub sources are shallow-cloned into RESOURCE_CACHE_DIR and reused; a clone
that already exists is fetched and hard-reset to the requested branch, so a
re-run picks up edited slides without re-downloading history. Private repos
work when GITHUB_TOKEN is set (the token is injected into the remote URL for
the duration of the clone and never printed).

Two things come out of a bundle:

  * text  — extracted from .md/.txt/.pdf/.pptx/.docx and friends, truncated to
            RESOURCE_MAX_CHARS in total so a 300-page book can't push the
            transcript out of the model's context.
  * images — page/slide renders (PDF via pdftoppm, PPTX via LibreOffice, plus
            any plain image files), embedded in the PDF export. Off with
            RESOURCE_SLIDE_IMAGES=0, and silently skipped when the converters
            aren't installed — reference *text* is the part that matters.

Everything here is best-effort by design: a missing converter, an unreadable
PDF or an unreachable repo degrades the summary, it does not fail the run. The
exceptions are a spec that names a local path that doesn't exist, and a spec
that names a text file (.md, .txt, ...) whose bytes are a PDF, a zip/OOXML
document or anything with NULs in it — both are almost always a mistake, and
are reported as errors.

A Markdown file may open with YAML frontmatter describing the reference:

    ---
    course: Computer Networks I
    source: "Kurose & Ross, Computer Networking (7th ed.)"
    citation_label: Kurose      # the summary cites "(Kurose §2.4)"
    coverage: "Chapters 1-3 only"
    ---

`course` and `source` are expected (a missing one is a warning; `course`
falls back to the file name), the rest are optional, unknown keys are
ignored. The frontmatter is stripped from the text and its fields become the
attributes of a <course_reference> element around that file's text, which the
lecture-reference prompt's citation rules are written against. Nothing about a
file without frontmatter changes. Parsed by hand: flat `key: value` lines are
all it needs, and pyyaml would be a dependency for that.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MAX_CHARS = 40000
DEFAULT_MAX_FILE_MB = 25
DEFAULT_CACHE_DIR = "/opt/meeting-bot/resources"

# Extensions we can pull text out of, cheapest first.
TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst", ".tex", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".org", ".adoc",
}
CODE_SUFFIXES = {".py", ".c", ".h", ".cpp", ".java", ".js", ".ts", ".sql",
                 ".sh", ".r", ".m"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
PDF_SUFFIXES = {".pdf"}
SLIDE_SUFFIXES = {".pptx", ".ppt", ".odp"}
DOC_SUFFIXES = {".docx", ".odt"}

# Directories that never hold teaching material.
SKIP_DIRS = {".git", ".github", "node_modules", "__pycache__", ".venv",
             "venv", ".idea", ".vscode", "dist", "build", ".pytest_cache"}

GITHUB_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/@#]+?)"
    r"(?:\.git)?"
    r"(?:/tree/(?P<tree_ref>[^/]+)(?:/(?P<subpath>.*))?)?"
    r"(?:[@#](?P<ref>[^/]+))?$"
)


@dataclass
class ResourceFile:
    """One file from a resource source."""
    path: Path
    label: str          # human-facing name, e.g. "slides/week4.pdf"
    origin: str         # the spec it came from
    text: str = ""
    images: list = field(default_factory=list)
    # Frontmatter fields (course, source, citation_label, coverage) when the
    # file had frontmatter; None otherwise.
    meta: dict = None

    @property
    def suffix(self):
        return self.path.suffix.lower()


@dataclass
class ResourceBundle:
    files: list = field(default_factory=list)
    sources: list = field(default_factory=list)   # provenance strings
    notes: list = field(default_factory=list)     # warnings worth surfacing
    truncated: bool = False

    def __bool__(self):
        return bool(self.files)

    def images(self):
        out = []
        for f in self.files:
            out.extend(f.images)
        return out

    def has_metadata(self):
        return any(f.meta is not None for f in self.files)

    def text_block(self, lecture_language=None):
        """The reference material as one prompt-ready string.

        A file with frontmatter is wrapped in <course_reference> with its
        fields as attributes (plus the lecture's language, so bilingual rules
        need no hardcoded language pair); any other file is exactly the
        `### label` section it always was.
        """
        chunks = []
        for f in self.files:
            if not f.text.strip():
                continue
            if f.meta is not None:
                attrs = dict(f.meta)
                if lecture_language:
                    attrs["lecture_language"] = lecture_language
                attr_text = " ".join(f'{k}="{_attr(v)}"' for k, v in attrs.items() if v)
                chunks.append(f"<course_reference {attr_text}>\n"
                              f"{f.text.strip()}\n</course_reference>")
                continue
            chunks.append(f"### {f.label}\n\n{f.text.strip()}")
        body = "\n\n".join(chunks)
        if self.truncated:
            body += ("\n\n*(reference material truncated at "
                     f"{max_chars()} characters)*")
        return body

    def provenance(self):
        return "; ".join(self.sources)


def _attr(value):
    return (str(value).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


FRONTMATTER_FIELDS = ("course", "source", "citation_label", "coverage")
MARKDOWN_SUFFIXES = {".md", ".markdown"}


def parse_frontmatter(text):
    """Split YAML-ish frontmatter off a Markdown document.

    Returns (meta, body, problems). meta is None when the text has no
    frontmatter at all (then body is the text unchanged); otherwise a dict of
    the known fields that were present. problems lists lines that could not be
    read — reported, never fatal.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text, []
    for end in range(1, len(lines)):
        if lines[end].strip() in ("---", "..."):
            break
    else:
        # An opening fence with no closing one is a horizontal rule, not
        # frontmatter.
        return None, text, []
    meta, problems = {}, []
    for raw in lines[1:end]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            problems.append(line)
            continue
        value = value.strip()
        if value[:1] in ('"', "'"):
            close = value.find(value[0], 1)
            value = value[1:close] if close > 0 else value[1:]
        else:
            value = re.sub(r"\s+#.*$", "", value).strip()
        key = key.strip()
        if key in FRONTMATTER_FIELDS:
            meta[key] = value
    return meta, "".join(lines[end + 1:]).lstrip("\n"), problems


def looks_binary(path):
    """True for a file whose bytes say it is not text, whatever its name."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return False
    return (head.startswith(b"%PDF") or head.startswith(b"PK\x03\x04")
            or b"\x00" in head)


BINARY_MESSAGE = "this looks like a binary document; convert it to Markdown first."


def max_chars():
    try:
        return int(os.environ.get("RESOURCE_MAX_CHARS", DEFAULT_MAX_CHARS))
    except ValueError:
        return DEFAULT_MAX_CHARS


def _max_file_bytes():
    try:
        return int(float(os.environ.get("RESOURCE_MAX_FILE_MB",
                                        DEFAULT_MAX_FILE_MB)) * 1024 * 1024)
    except ValueError:
        return DEFAULT_MAX_FILE_MB * 1024 * 1024


def _want_images():
    return (os.environ.get("RESOURCE_SLIDE_IMAGES", "1").strip().lower()
            not in ("0", "false", "no"))


def cache_dir():
    return Path(os.environ.get("RESOURCE_CACHE_DIR", DEFAULT_CACHE_DIR))


# --- source resolution ------------------------------------------------------

def parse_spec(spec):
    """Classify one resource spec.

    Returns a dict: {kind: "github"|"local", ...}. Raises ValueError for a
    local path that doesn't exist — that's a typo, not a degraded input.
    """
    spec = spec.strip()
    if not spec:
        raise ValueError("empty resource spec")

    m = GITHUB_RE.match(spec)
    if m:
        ref = m.group("ref") or m.group("tree_ref")
        return {
            "kind": "github",
            "owner": m.group("owner"),
            "repo": m.group("repo"),
            "ref": ref,
            "subpath": (m.group("subpath") or "").strip("/"),
            "spec": spec,
        }

    if spec.startswith(("http://", "https://")):
        raise ValueError(
            f"unsupported resource URL: {spec}\n"
            "  Supported: a github.com repository URL (optionally "
            "@branch or /tree/<branch>/<subdir>), or a local file/directory "
            "path."
        )

    path = Path(spec).expanduser()
    if not path.exists():
        raise ValueError(f"resource path does not exist: {path}")
    return {"kind": "local", "path": path, "spec": spec}


def _run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _redact(text):
    """Strip a token that git may have echoed back inside a remote URL."""
    return re.sub(r"https://[^@\s]*@github\.com", "https://github.com", text)


def fetch_github(source, dest_root=None):
    """Shallow-clone (or refresh) a GitHub repo. Returns (path, description).

    Raises RuntimeError when the clone fails — the caller decides whether that
    is fatal.
    """
    if not shutil.which("git"):
        raise RuntimeError("git is not installed; cannot fetch GitHub resources")

    dest_root = Path(dest_root) if dest_root else cache_dir()
    ref = source.get("ref")
    slug = f"{source['owner']}_{source['repo']}" + (f"_{ref}" if ref else "")
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", slug)
    dest = dest_root / slug
    dest_root.mkdir(parents=True, exist_ok=True)

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        remote = (f"https://x-access-token:{token}@github.com/"
                  f"{source['owner']}/{source['repo']}.git")
    else:
        remote = f"https://github.com/{source['owner']}/{source['repo']}.git"

    if (dest / ".git").is_dir():
        _run(["git", "-C", str(dest), "remote", "set-url", "origin", remote])
        fetch = _run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin"]
                     + ([ref] if ref else []))
        if fetch.returncode == 0:
            _run(["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"])
        else:
            print(f"  note: could not refresh {source['spec']} — using the "
                  f"cached copy", file=sys.stderr)
    else:
        cmd = ["git", "clone", "--depth", "1"]
        if ref:
            cmd += ["--branch", ref]
        cmd += [remote, str(dest)]
        proc = _run(cmd)
        if proc.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise RuntimeError(
                f"git clone failed for {source['spec']}: "
                f"{_redact(proc.stderr.strip())}"
            )

    # Put the remote back without the token so it never sits on disk.
    if token:
        _run(["git", "-C", str(dest), "remote", "set-url", "origin",
              f"https://github.com/{source['owner']}/{source['repo']}.git"])

    commit = _run(["git", "-C", str(dest), "rev-parse", "--short", "HEAD"])
    sha = commit.stdout.strip() if commit.returncode == 0 else "unknown"
    root = dest / source["subpath"] if source.get("subpath") else dest
    if not root.exists():
        raise RuntimeError(
            f"{source['spec']}: subdirectory {source['subpath']!r} not found "
            f"in the repository"
        )
    desc = (f"github.com/{source['owner']}/{source['repo']}"
            + (f"@{ref}" if ref else "")
            + (f"/{source['subpath']}" if source.get("subpath") else "")
            + f" ({sha})")
    return root, desc


# --- text extraction --------------------------------------------------------

def _read_text_file(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _pdf_text(path):
    """PDF text via poppler's pdftotext. Empty string when unavailable."""
    if not shutil.which("pdftotext"):
        return ""
    proc = _run(["pdftotext", "-layout", "-q", str(path), "-"])
    return proc.stdout if proc.returncode == 0 else ""


_XML_TAG_RE = re.compile(r"<[^>]+>")


def _ooxml_text(path, member_glob):
    """Text out of an OOXML container (pptx/docx) with the stdlib only.

    python-pptx/python-docx would be tidier, but they're another two
    dependencies for something a zipfile and a regex do well enough: we want
    the words, not the layout.
    """
    out = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = sorted(n for n in zf.namelist() if member_glob(n))
            for name in names:
                try:
                    xml = zf.read(name).decode("utf-8", errors="replace")
                except (KeyError, OSError):
                    continue
                # Paragraph and line breaks become newlines so slide bullets
                # don't run together into one unreadable line.
                xml = re.sub(r"</a:p>|</w:p>|<a:br/>|<w:br/>", "\n", xml)
                text = _XML_TAG_RE.sub("", xml)
                text = (text.replace("&amp;", "&").replace("&lt;", "<")
                            .replace("&gt;", ">").replace("&quot;", '"')
                            .replace("&apos;", "'"))
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                if lines:
                    out.append("\n".join(lines))
    except (zipfile.BadZipFile, OSError):
        return ""
    return "\n\n".join(out)


def extract_text(path):
    """Best-effort plain text for one file. Empty string if we can't."""
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES or suffix in CODE_SUFFIXES:
        return _read_text_file(path)
    if suffix in PDF_SUFFIXES:
        return _pdf_text(path)
    if suffix == ".pptx":
        return _ooxml_text(path, lambda n: n.startswith("ppt/slides/slide")
                           and n.endswith(".xml"))
    if suffix == ".docx":
        return _ooxml_text(path, lambda n: n == "word/document.xml")
    return ""


# --- slide rendering --------------------------------------------------------

def _render_pdf_pages(pdf_path, out_dir, limit=40):
    """PDF -> page JPEGs via poppler's pdftoppm. Returns the list of images."""
    if not shutil.which("pdftoppm"):
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / "page"
    proc = _run(["pdftoppm", "-jpeg", "-r", "100", "-l", str(limit),
                 str(pdf_path), str(stem)])
    if proc.returncode != 0:
        return []
    return sorted(str(p) for p in out_dir.glob("page*.jpg"))


def _office_to_pdf(path, out_dir):
    """PPTX/ODP -> PDF via LibreOffice. Returns the PDF path or None."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = _run([soffice, "--headless", "--convert-to", "pdf",
                 "--outdir", str(out_dir), str(path)], timeout=300)
    if proc.returncode != 0:
        return None
    candidate = out_dir / (path.stem + ".pdf")
    return candidate if candidate.is_file() else None


def render_images(path, cache_root):
    """Page/slide images for one file, or []."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return [str(path)]
    digest = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:12]
    out_dir = Path(cache_root) / "render" / f"{path.stem}_{digest}"
    existing = sorted(str(p) for p in out_dir.glob("page*.jpg")) \
        if out_dir.is_dir() else []
    if existing:
        return existing
    if suffix in PDF_SUFFIXES:
        return _render_pdf_pages(path, out_dir)
    if suffix in SLIDE_SUFFIXES:
        pdf = _office_to_pdf(path, out_dir)
        if pdf:
            return _render_pdf_pages(pdf, out_dir)
    return []


# --- assembly ---------------------------------------------------------------

def _iter_files(root):
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _interesting(path):
    suffix = path.suffix.lower()
    return (suffix in TEXT_SUFFIXES or suffix in PDF_SUFFIXES
            or suffix in SLIDE_SUFFIXES or suffix in DOC_SUFFIXES
            or suffix in IMAGE_SUFFIXES or suffix in CODE_SUFFIXES)


def collect(specs, cache_root=None, want_images=None):
    """Build a ResourceBundle from a list of specs.

    Never raises for a source that merely fails to fetch; a bad *local path*
    is reported as ValueError by parse_spec before we get here.
    """
    bundle = ResourceBundle()
    if not specs:
        return bundle
    cache_root = Path(cache_root) if cache_root else cache_dir()
    if want_images is None:
        want_images = _want_images()

    budget = max_chars()
    used = 0
    size_cap = _max_file_bytes()

    for spec in specs:
        source = parse_spec(spec)
        if source["kind"] == "github":
            try:
                root, desc = fetch_github(source, cache_root)
            except RuntimeError as exc:
                bundle.notes.append(str(exc))
                print(f"  !! resource unavailable: {exc}", file=sys.stderr)
                continue
        else:
            root, desc = source["path"], str(source["path"])
        bundle.sources.append(desc)

        base = root if root.is_dir() else root.parent
        for path in _iter_files(root):
            if not _interesting(path):
                continue
            try:
                if path.stat().st_size > size_cap:
                    bundle.notes.append(f"{path.name}: skipped (over "
                                        f"{size_cap // (1024 * 1024)}MB)")
                    continue
            except OSError:
                continue

            try:
                label = str(path.relative_to(base))
            except ValueError:
                label = path.name

            suffix = path.suffix.lower()
            if (suffix in TEXT_SUFFIXES or suffix in CODE_SUFFIXES) and looks_binary(path):
                if root.is_file():
                    # Named directly: a PDF saved as notes.md, say. Fail now,
                    # not after the model has been handed garbage.
                    raise ValueError(f"{path}: {BINARY_MESSAGE}")
                bundle.notes.append(f"{label}: skipped ({BINARY_MESSAGE})")
                print(f"  !! {label}: skipped — {BINARY_MESSAGE}", file=sys.stderr)
                continue

            meta = None
            text = ""
            if used < budget:
                text = extract_text(path).strip()
                if suffix in MARKDOWN_SUFFIXES:
                    meta, text, problems = parse_frontmatter(text)
                    text = text.strip()
                    for bad in problems:
                        bundle.notes.append(f"{label}: unreadable frontmatter line: {bad}")
                        print(f"  !! {label}: frontmatter line ignored: {bad}",
                              file=sys.stderr)
                    if meta is not None:
                        if not meta.get("course"):
                            meta["course"] = path.stem
                            print(f"  !! {label}: frontmatter has no 'course' — "
                                  f"using {path.stem!r}", file=sys.stderr)
                        if not meta.get("source"):
                            print(f"  !! {label}: frontmatter has no 'source'",
                                  file=sys.stderr)
                        meta.setdefault("citation_label", meta["course"])
                if text:
                    remaining = budget - used
                    if len(text) > remaining:
                        text = text[:remaining]
                        bundle.truncated = True
                    used += len(text)

            images = render_images(path, cache_root) if want_images else []
            if not text and not images:
                continue
            bundle.files.append(ResourceFile(path=path, label=label,
                                             origin=desc, text=text,
                                             images=images, meta=meta))
    return bundle


def parse_specs_arg(value):
    """Split a --resources / RESOURCES value into individual specs.

    Newline- or comma-separated. Commas are safe: neither a GitHub URL nor a
    sane path contains one, and the alternative (repeating the flag) still
    works.
    """
    if not value:
        return []
    parts = []
    for line in str(value).splitlines():
        for piece in line.split(","):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    return parts


def check_specs(specs):
    """The cheap, offline checks pipeline.sh runs before anything is paid for.

    Returns a list of error strings (fatal) and prints warnings. A GitHub spec
    is only parsed — fetching it is the summarize stage's job.
    """
    errors = []
    budget = max_chars()
    for spec in specs:
        try:
            source = parse_spec(spec)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if source["kind"] == "github":
            continue
        path = source["path"]
        if path.is_file():
            suffix = path.suffix.lower()
            if (suffix in TEXT_SUFFIXES or suffix in CODE_SUFFIXES) and looks_binary(path):
                errors.append(f"{path}: {BINARY_MESSAGE}")
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if suffix in TEXT_SUFFIXES and size > budget:
                print(f"WARNING: --resources {path.name} is {size:,} bytes; only the "
                      f"first {budget:,} characters (RESOURCE_MAX_CHARS) reach the "
                      f"model, and they dominate every request.", file=sys.stderr)
    return errors


def _main(argv):
    """CLI: `python3 lib/resources.py <spec> [...]` — show what we'd collect.

    `resources.py check <spec> [...]`: the offline pre-flight (exit 1 on an
    error). `resources.py is-binary <file>`: exit 0 if the file is binary.
    """
    if len(argv) >= 2 and argv[1] == "check":
        errors = check_specs(argv[2:])
        for err in errors:
            print(f"ERROR: --resources: {err}", file=sys.stderr)
        return 1 if errors else 0
    if len(argv) == 3 and argv[1] == "is-binary":
        return 0 if looks_binary(argv[2]) else 1
    specs = argv[1:]
    if not specs:
        print("Usage: resources.py <github-url-or-path> [...]", file=sys.stderr)
        return 2
    try:
        bundle = collect(specs)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"sources: {bundle.provenance()}")
    print(f"files:   {len(bundle.files)}   images: {len(bundle.images())}")
    for f in bundle.files:
        print(f"  {f.label:<50} text={len(f.text):>6}  images={len(f.images)}")
        if f.meta is not None:
            print(f"      frontmatter: {f.meta}")
    for note in bundle.notes:
        print(f"  note: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
