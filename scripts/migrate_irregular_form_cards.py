#!/usr/bin/env python3
"""Migrate the Spanish irregular-verb form cards to the new rules (#1539).

Old: 25 verbs x 8 tenses, judged by spelling. New: every Spanish verb in the
collection gets one form card per tense in which a form SOUNDS irregular
(Presente, Indefinido, Imperfecto, Futuro, Imperativo, Participio, Gerundio),
rotating over the irregular persons only.

What the migration does per verb (see irregular_cards.plan_migration):
  keep    an old card whose tense is still wanted (progress is kept)
  rename  the old "Perfecto" card becomes "<verb> · Participio"
  delete  every other old card (soft delete: review history stays)
  create  a wanted tense without a card; deck and due date are copied from the
          verb's earliest old form card (else its creating card); cards parked
          in Saved stay suspended

Dry run by default. The database is the one DB_PATH points to (default
data/srs.db); on the server that is the production database, so --write needs
Daniel's go-ahead. The server keeps the review queue in memory, so after
--write restart the service to see the changes at once.

    python scripts/migrate_irregular_form_cards.py            # print the plan
    python scripts/migrate_irregular_form_cards.py --json     # plan as JSON
    python scripts/migrate_irregular_form_cards.py --write    # apply it
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import irregular_cards
import lexicon


def _print_plan(plan: dict) -> None:
    lemmas = sorted({it["lemma"] for key in ("keep", "rename", "delete", "create")
                     for it in plan[key] if it.get("lemma")})
    for lemma in lemmas:
        print(f"\n{lemma}")
        for it in plan["keep"]:
            if it["lemma"] == lemma:
                print(f"  keep    {it['word_zh']}  [{it['deck']}, due {it['due']}, {it['state']}]")
        for it in plan["rename"]:
            if it["lemma"] == lemma:
                print(f"  rename  {it['word_zh']} -> {it['new_word_zh']}  "
                      f"[{it['deck']}, due {it['due']}, {it['state']}]")
        for it in plan["delete"]:
            if it["lemma"] == lemma:
                print(f"  delete  {it['word_zh']}  [{it['deck']}, due {it['due']}, {it['state']}]")
        for it in plan["create"]:
            if it["lemma"] == lemma:
                print(f"  create  {it['word_zh']}  [{it['deck']}, due {it['due']}, "
                      f"{it['state']}]  persons: {', '.join(p or '-' for p in it['persons'])}")
    if plan["skipped"]:
        print("\nSkipped:")
        for it in plan["skipped"]:
            print(f"  {it['word_zh']}: {it['reason']}")
    print(f"\nTotals: keep {len(plan['keep'])}, rename {len(plan['rename'])}, "
          f"delete {len(plan['delete'])}, create {len(plan['create'])}, "
          f"skipped {len(plan['skipped'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true", help="apply the plan (default: dry run)")
    ap.add_argument("--json", action="store_true", help="print the plan as JSON")
    args = ap.parse_args()

    if not lexicon.available("es"):
        print("Spanish lexicon is not available (resources/es-lexicon.json.gz).", file=sys.stderr)
        return 1
    database.init_db()
    plan = irregular_cards.plan_migration(database.anki_today().isoformat())
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        _print_plan(plan)
    if not args.write:
        if not args.json:
            print("\nDry run — nothing written. Add --write to apply.")
        return 0

    counts = irregular_cards.apply_migration(plan)
    print(f"\nApplied: kept {counts['kept']}, renamed {counts['renamed']}, "
          f"deleted {counts['deleted']}, created {counts['created']}, "
          f"skipped {counts['skipped']}")
    for err in counts["errors"]:
        print(f"ERROR {err}", file=sys.stderr)
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
