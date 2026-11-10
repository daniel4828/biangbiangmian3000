"""Surface forms of multi-word verb expressions (#1508).

Dictionary headwords like "tener prisa", "irle bien a alguien" or "darse
cuenta de algo" never appear verbatim in a generated sentence: the AI writes
"Tengo prisa." or "Me di cuenta." Matching on the headword alone therefore
missed them, and the card got the fallback sentence. This module derives the
forms such an expression can take: every known form of the head verb (from
the bundled lexicon, #1293) followed by the rest of the expression, with the
reflexive/indirect pronoun in front where the headword carries a clitic.
Placeholders (alguien, algo, qn, qch ...) and the preposition that introduces
them are dropped, because the sentence replaces them with something concrete.

Never raises: any problem means "no forms" and the caller falls back to the
plain headword match. Chinese and single words are not expressions -> [].
"""
import logging
import re

logger = logging.getLogger(__name__)

_PLACEHOLDERS = {
    "es": {"alguien", "algo", "alg.", "algn", "alguno", "alguna"},
    "fr": {"qn", "qch", "qc", "quelqu'un", "quelque", "chose"},
}
_PREPOSITIONS = {
    "es": {"a", "de", "con", "en", "por", "para", "sobre"},
    "fr": {"à", "de", "avec", "en", "sur", "pour", "contre"},
}
_ES_HEAD = re.compile(r"^(.*?[aeií]r)((?:me|te|se|nos|os|le|les|lo|la|los|las){0,2})$")
_ES_CLITIC = re.compile(r"nos|los|las|les|me|te|se|os|le|lo|la")
_ES_REFLEXIVE = ["me", "te", "se", "nos", "os"]
_FR_REFLEXIVE = ["me", "te", "se", "nous", "vous", "m'", "t'", "s'"]
_ACCENTS = str.maketrans("áéóú", "aeou")


def _strip_placeholders(tokens: list[str], lang: str) -> list[str]:
    placeholders = _PLACEHOLDERS.get(lang, set())
    preps = _PREPOSITIONS.get(lang, set())
    out: list[str] = []
    for tok in tokens:
        if tok in placeholders:
            if len(out) > 1 and out[-1] in preps:
                out.pop()
            continue
        out.append(tok)
    return out


def _es_head(token: str):
    """(verb forms, first clitic or "") for a Spanish head token, or None."""
    import lexicon
    m = _ES_HEAD.match(token)
    if not m:
        return None
    inf, clitic_part = m.group(1), m.group(2)
    candidates = [inf]
    if inf.endswith("ír"):
        stripped = inf[:-2].translate(_ACCENTS) + "ír"
        candidates.append(stripped)
        candidates.append(inf[:-2].translate(_ACCENTS) + "ir")
    else:
        candidates.append(inf.translate(_ACCENTS))
    for cand in dict.fromkeys(candidates):
        forms = lexicon.forms_of(cand, "es")
        if forms:
            clitics = _ES_CLITIC.findall(clitic_part)
            return forms, (clitics[0] if clitics else "")
    return None


def _fr_head(tokens: list[str]):
    """(verb forms, clitic, remaining tokens) for a French expression, or None."""
    import lexicon
    clitic = ""
    if tokens[0] == "se" and len(tokens) > 1:
        clitic, verb, rest = "se", tokens[1], tokens[2:]
    elif tokens[0].startswith("s'") and len(tokens[0]) > 2:
        clitic, verb, rest = "se", tokens[0][2:], tokens[1:]
    else:
        verb, rest = tokens[0], tokens[1:]
    forms = lexicon.forms_of(verb, "fr")
    if not forms:
        return None
    return forms, clitic, rest


def expression_forms(word: str, lang: str) -> list[str]:
    """Lowercase surface forms of a verb-initial expression, longest first.

    [] for Chinese, single words, or when the head verb can't be resolved."""
    try:
        if not word or lang not in ("es", "fr"):
            return []
        tokens = _strip_placeholders(word.casefold().split(), lang)
        if len(word.split()) < 2 or not tokens:
            return []

        if lang == "es":
            head = _es_head(tokens[0])
            if head is None:
                return []
            verb_forms, clitic = head
            rest_tokens = tokens[1:]
            if clitic == "se":
                prefixes = _ES_REFLEXIVE
            elif clitic in ("le", "les"):
                prefixes = ["le", "les"]
            elif clitic:
                prefixes = [clitic]
            else:
                prefixes = []
        else:
            head_fr = _fr_head(tokens)
            if head_fr is None:
                return []
            verb_forms, clitic, rest_tokens = head_fr
            prefixes = _FR_REFLEXIVE if clitic == "se" else []

        rest = " ".join(rest_tokens)
        if not rest and not clitic:
            return []  # just a verb, not an expression

        out: set[str] = set()
        for f in verb_forms:
            out.add(f"{f} {rest}" if rest else f)
            for p in prefixes:
                sep = "" if p.endswith("'") else " "
                out.add(f"{p}{sep}{f}" + (f" {rest}" if rest else ""))
        out.add(" ".join(tokens))
        out.add(word.casefold().strip())
        return sorted((f for f in out if f), key=len, reverse=True)
    except Exception as e:
        logger.debug("expression_forms failed for %r — %s", word, e)
        return []
