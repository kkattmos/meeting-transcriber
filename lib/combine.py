#!/usr/bin/env python3
"""
The glue for a --combine run: turn the member runs' state into the parts
file summarize.py --parts reads.

    combine.py parts --runs-dir DIR --out PARTS.json MEMBER_ID...
    combine.py run-key MEMBER_ID...

`parts` reads each member's state.json and emits one entry per member, in the
order given, with its transcript (.txt and .srt), its frames manifest, and
what the document should link to. It fails — naming the member and the
stage — when a member has no finished transcript, because the combine run
must never quietly summarize fewer videos than it was asked for. A missing
frames manifest is reported the same way: run_one.sh re-extracts frames
before calling this, so reaching here without them is a bug, not a state.

`run-key` prints the string a combine run stores as its `input`. It is what
pipeline.sh's auto-resume matches on (`runstate find --input`), so the same
set of members in the same order lands on the same combine run.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runstate import RunState, DONE  # noqa: E402


def run_key(member_ids):
    return "combine:" + "+".join(member_ids)


def run_safe_name(member_ids):
    """A short, stable stem for the combine run id.

    The member ids themselves would make a 200-character directory name for
    five lectures; a hash of them is as unique and fits in `--list`.
    """
    digest = hashlib.sha1("\n".join(member_ids).encode()).hexdigest()[:10]
    return f"combine_{len(member_ids)}x_{digest}"


def _canonical_source(state, runs_dir):
    """What the document links to for one member — see run_one.sh SOURCE_URL."""
    source = state.get("input") or ""
    kind = state.get("input_type") or ""
    if kind == "kaltura":
        try:
            import kaltura
            ref = kaltura.parse_input(source)
            source = ref.canonical_url
        except Exception:  # noqa: BLE001 - best-effort, the blob still works
            pass
    return source, kind


def _kaltura_title(run_dir):
    facts = run_dir / "kaltura.json"
    if not facts.is_file():
        return None
    try:
        return json.loads(facts.read_text()).get("title") or None
    except (OSError, ValueError):
        return None


def build_parts(runs_dir, member_ids):
    runs_dir = Path(runs_dir)
    parts, problems = [], []
    for member in member_ids:
        run_dir = runs_dir / member
        if not (run_dir / "state.json").is_file():
            problems.append(f"{member}: no such run under {runs_dir}")
            continue
        state = RunState(run_dir)
        if state.status("transcribe") != DONE:
            problems.append(f"{member}: transcribe is "
                            f"{state.status('transcribe')}, not done")
            continue
        artifacts = state.stage("transcribe").get("artifacts", {})
        txt = artifacts.get("txt")
        if not txt or not Path(txt).is_file():
            problems.append(f"{member}: transcript {txt!r} is missing")
            continue
        srt = artifacts.get("srt")
        if srt and not Path(srt).is_file():
            srt = None
        manifest = state.stage("frames").get("artifacts", {}).get("manifest")
        if state.status("frames") != DONE or not manifest \
                or not Path(manifest).is_file():
            problems.append(f"{member}: frames manifest {manifest!r} is "
                            f"missing (frames stage is "
                            f"{state.status('frames')})")
            continue
        source, kind = _canonical_source(state, runs_dir)
        title = _kaltura_title(run_dir) if kind == "kaltura" else None
        parts.append({
            "run_id": member,
            "source": source,
            "kind": kind,
            "title": title,
            "clip": state.get("clip") or None,
            "transcript": txt,
            "srt": srt,
            "frames_manifest": manifest,
        })
    if problems:
        raise SystemExit("cannot build the combined summary:\n  "
                         + "\n  ".join(problems))
    return {"parts": parts}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parts")
    p.add_argument("--runs-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("members", nargs="+")
    p = sub.add_parser("run-key")
    p.add_argument("members", nargs="+")
    p = sub.add_parser("safe-name")
    p.add_argument("members", nargs="+")
    args = ap.parse_args(argv)

    if args.cmd == "run-key":
        print(run_key(args.members))
        return 0
    if args.cmd == "safe-name":
        print(run_safe_name(args.members))
        return 0
    data = build_parts(args.runs_dir, args.members)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"==> {len(data['parts'])} part(s) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
