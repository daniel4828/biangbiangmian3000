#!/usr/bin/env python3
"""Build annotate/baseline_es.txt — the Spanish A1 baseline (#1294).

Reads the bundled lexicon (resources/es-lexicon.json.gz, #1293) and writes the
N most frequent lemmas (default 300), proper nouns excluded, one per line in
rank order.

    python scripts/build_es_baseline.py [--top 500] [--lexicon PATH] [--out PATH]

Why an approximation: the real A1 list would be ELELex (the CEFRLex Spanish
list), which could not be downloaded from the build environment. The 500 most
frequent lemmas of the OpenSubtitles frequency table are a reasonable stand-in
for what a solid A1 learner has met.

Why lemmas only: since #1293 the romance annotator judges known-ness by lemma
(a token is known if ANY of its candidate lemmas is known), so inflected forms
would be dead weight.
"""
import argparse
import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEXICON = ROOT / "resources" / "es-lexicon.json.gz"
DEFAULT_OUT = ROOT / "annotate" / "baseline_es.txt"
DEFAULT_TOP = 500


def select_lemmas(lexicon: dict, top: int) -> list[str]:
    """Lemmas with rank 1..top (inclusive), no proper nouns, in rank order.

    Lemmas without a numeric rank are skipped; ties keep alphabetical order so
    the output is deterministic."""
    picked = []
    for lemma, info in (lexicon.get("lemmas") or {}).items():
        if not isinstance(info, dict):
            continue
        rank = info.get("r")
        if not isinstance(rank, int) or isinstance(rank, bool):
            continue
        if not 1 <= rank <= top or info.get("p") == "prop":
            continue
        picked.append((rank, lemma))
    picked.sort()
    return [lemma for _rank, lemma in picked]


def render(lemmas: list[str], top: int) -> str:
    header = f"""# Baseline vocabulary: Spanish A1 (approximation) (#1294)
#
# Words Daniel already knows before this app saw them (placement test: solid A1).
# annotate/romance.py unions this list into "known" alongside entry_forms and
# known_words. NOTHING here becomes a card: this is "stop showing me this".
#
# This is an APPROXIMATION of CEFR A1: the {top} most frequent lemmas (rank
# 1..{top}, OpenSubtitles frequency, via resources/es-lexicon.json.gz from #1293),
# proper nouns excluded. The proper source would be ELELex (CEFRLex Spanish),
# which was not downloadable from the build environment.
#
# Lemmas only, no inflected forms: since #1293 the romance annotator checks every
# candidate lemma of a token, so `hablamos` is known when `hablar` is listed.
#
# Rebuild: python scripts/build_es_baseline.py [--top {top}]
"""
    return header + "\n".join(lemmas) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--top", type=int, default=DEFAULT_TOP)
    ap.add_argument("--lexicon", type=Path, default=DEFAULT_LEXICON)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    if args.top < 1:
        print("--top must be >= 1", file=sys.stderr)
        return 2
    with gzip.open(args.lexicon, "rt", encoding="utf-8") as f:
        lexicon = json.load(f)
    lemmas = select_lemmas(lexicon, args.top)
    if not lemmas:
        print(f"no lemmas with rank 1..{args.top} in {args.lexicon}", file=sys.stderr)
        return 1
    args.out.write_text(render(lemmas, args.top), encoding="utf-8")
    print(f"wrote {len(lemmas)} lemmas to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
