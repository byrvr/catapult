"""Recovery key codec.

The key is the one thing a user must carry to a second Mac, usually by reading
it off a screen or a printout. Crockford Base32 is chosen over Bech32 and
BIP-39 because it *decodes* O to 0 and I/L to 1, is case-insensitive, and
ignores stray hyphens — worth more to a stressed user retyping than a stronger
checksum would be.
"""

import pytest

from catapult import recoverykey


def test_generate_returns_128_bits():
    assert len(recoverykey.generate()) == 16


def test_generate_is_not_deterministic():
    assert recoverykey.generate() != recoverykey.generate()


def test_round_trips():
    key = recoverykey.generate()

    assert recoverykey.decode(recoverykey.encode(key)) == key


def test_encoded_form_is_grouped_and_prefixed():
    encoded = recoverykey.encode(bytes(16))

    assert encoded.startswith("CAT1-")
    body = encoded.removeprefix("CAT1-")
    assert all(len(group) <= 5 for group in body.split("-"))
    # 128 bits of Crockford Base32 is 26 symbols, plus one check symbol.
    assert len(body.replace("-", "")) == 27


def test_decode_is_case_insensitive():
    key = recoverykey.generate()
    encoded = recoverykey.encode(key)

    assert recoverykey.decode(encoded.lower()) == key


def test_decode_accepts_confusable_characters():
    """O/o read as 0, and I/i/L/l read as 1 — Crockford's whole point."""
    key = recoverykey.generate()
    encoded = recoverykey.encode(key)
    mangled = encoded.replace("0", "O").replace("1", "I")

    assert recoverykey.decode(mangled) == key


def test_decode_ignores_separators_and_whitespace():
    key = recoverykey.generate()
    encoded = recoverykey.encode(key)

    assert recoverykey.decode(encoded.replace("-", "")) == key
    assert recoverykey.decode(f"  {encoded}  ") == key
    assert recoverykey.decode(encoded.replace("-", " ")) == key


def test_decode_tolerates_a_missing_prefix():
    key = recoverykey.generate()
    body = recoverykey.encode(key).removeprefix("CAT1-")

    assert recoverykey.decode(body) == key


def test_rejects_a_single_character_typo():
    """The check symbol is what turns a typo into a clear message instead of a
    500MB download that fails to decrypt."""
    encoded = recoverykey.encode(recoverykey.generate())
    # Flip one data symbol to a different valid one.
    body = encoded.removeprefix("CAT1-").replace("-", "")
    swapped = ("Z" if body[0] != "Z" else "Y") + body[1:]

    with pytest.raises(ValueError):
        recoverykey.decode(swapped)


def test_rejects_wrong_length():
    with pytest.raises(ValueError):
        recoverykey.decode("CAT1-ABCDE")


def test_rejects_characters_outside_the_alphabet():
    body = recoverykey.encode(recoverykey.generate()).removeprefix("CAT1-").replace("-", "")

    with pytest.raises(ValueError):
        recoverykey.decode("!" + body[1:])


def test_rejects_empty_input():
    with pytest.raises(ValueError):
        recoverykey.decode("")
