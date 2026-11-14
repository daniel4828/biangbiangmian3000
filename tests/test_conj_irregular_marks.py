"""Rote Punkte für unregelmäßige Formen in der Konjugationstabelle (#1537).

Seit #1539 zählt nur die Aussprache: Schreibwechsel (busqué, traído, trayendo)
sind regelmäßig."""
import pytest

import verb_slots

f = verb_slots.form_is_irregular


@pytest.mark.parametrize("lemma,tense,person,form,expected", [
    ("resolver", "Presente", "yo", "resuelvo", True),           # o→ue
    ("resolver", "Presente", "nosotros", "resolvemos", False),  # Stamm unverändert
    ("resolver", "Pretérito Perfecto", "yo", "he resuelto", True),
    ("resolver", "Pretérito Indefinido", "yo", "resolví", False),
    ("resolver", "Imperfecto de subjuntivo", "yo", "resolviera / resolviese", False),
    ("resolver", "Participio", "", "resuelto", True),
    ("hablar", "Pretérito Perfecto", "él/ella", "ha hablado", False),
    ("hablar", "Condicional", "yo", "hablaría", False),
    ("tener", "Futuro", "yo", "tendré", True),
    ("tener", "Condicional", "tú", "tendrías", True),
    ("buscar", "Pretérito Indefinido", "yo", "busqué", False),  # nur Schreibung
    ("coger", "Presente", "yo", "cojo", False),
    ("leer", "Pretérito Indefinido", "él/ella", "leyó", False),
    ("traer", "Participio", "", "traído", False),
    ("traer", "Gerundio", "", "trayendo", False),
    ("traer", "Presente", "yo", "traigo", True),
    ("vivir", "Presente de subjuntivo", "nosotros", "vivamos", False),
    ("rendirse", "Presente", "yo", "me rindo", True),
    ("rendirse", "Presente", "nosotros", "nos rendimos", False),
    ("rendirse", "Pretérito Perfecto", "nosotros", "nos hemos rendido", False),
    ("hablar", "Gerundio", "", "hablando", False),
    ("dormir", "Gerundio", "", "durmiendo", True),
    # Imperativo (bejahend)
    ("traer", "Imperativo", "usted", "traiga", True),
    ("traer", "Imperativo", "tú", "trae", False),
    ("hablar", "Imperativo", "vosotros", "hablad", False),
    ("resolver", "Imperativo", "ustedes", "resuelvan", True),
])
def test_form_is_irregular(lemma, tense, person, form, expected):
    assert f(lemma, tense, person, form, "es") is expected


def test_no_verdict_cases():
    assert f("hablar", "Imperativo negativo", "tú", "no hables", "es") is None
    assert f("parler", "Présent", "je", "parle", "fr") is None
    assert f("hablar", "Presente", "yo", "", "es") is None
    assert f("hablar", "Presente", "", "hablo", "es") is None  # person missing
