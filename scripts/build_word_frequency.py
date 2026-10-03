"""Regenerate bundled ranks from an official SUBTLEX-CH-WF ZIP download.

Usage: python scripts/build_word_frequency.py /path/to/subtlexchwf.zip
"""
import csv
import io
import json
from pathlib import Path
import sys
import zipfile


def build(archive: Path) -> None:
    raw = zipfile.ZipFile(archive).read('SUBTLEX-CH-WF')
    rows = csv.DictReader(io.StringIO(raw.decode('gb18030').split('\n', 2)[2]), delimiter='\t')
    counts = {row['Word']: int(row['WCount']) for row in rows if row['Word']}
    words = sorted(counts, key=lambda word: (-counts[word], word))
    ranks = {word: rank for rank, word in enumerate(words, 1)}
    target = Path(__file__).resolve().parents[1] / 'resources' / 'subtlex-ch-ranks.json'
    target.write_text(json.dumps(ranks, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')


if __name__ == '__main__':
    build(Path(sys.argv[1]))
