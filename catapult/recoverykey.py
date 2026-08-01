"""Recovery key generation and human-transferable encoding.

The recovery key is a generated 128-bit secret — never user-chosen — that
unwraps the vault's data key. It is the one thing a user carries to a second
Mac, usually by reading it off a screen or retyping it from a printout.

Crockford Base32 with the mod-37 check symbol:
  - decodes O to 0 and I/L to 1, so the classic transcription errors self-heal
  - is case-insensitive
  - ignores hyphens and spaces, so grouping is cosmetic
  - the check symbol catches a single-character typo before any network call

No KDF is applied to this value anywhere. It is uniformly random, so the
offline attack floor is already 2**128; stretching a generated key buys nothing
and would add a parameter-tuning problem across the old Intel Macs that macOS
14 still supports. This is the same reasoning behind 1Password's unstretched
Secret Key.
"""

from __future__ import annotations

import secrets

KEY_BYTES = 16
KEY_BITS = KEY_BYTES * 8

PREFIX = "CAT1"
GROUP_SIZE = 5

# Crockford Base32: no I, L, O, or U.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# Check symbols extend the alphabet to 37 values.
_CHECK_ALPHABET = _ALPHABET + "*~$=U"
_CHECK_MODULUS = 37

# 128 bits at 5 bits per symbol.
_SYMBOLS = -(-KEY_BITS // 5)  # 26

_DECODE_MAP = {char: value for value, char in enumerate(_ALPHABET)}
_DECODE_MAP.update({
    "O": 0,
    "I": 1,
    "L": 1,
})
_CHECK_DECODE_MAP = {char: value for value, char in enumerate(_CHECK_ALPHABET)}
_CHECK_DECODE_MAP.update({"o": 0, "i": 1, "l": 1})

_IGNORED = {"-", " ", "\t", "\n", "\r", "_"}

# Applied to the whole string, including the prefix.
_CONFUSABLES = str.maketrans({"O": "0", "I": "1", "L": "1"})


def generate() -> bytes:
    """A fresh 128-bit recovery key."""
    return secrets.token_bytes(KEY_BYTES)


def encode(key: bytes) -> str:
    """Render a key as CAT1-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX-XX."""
    if len(key) != KEY_BYTES:
        raise ValueError(f"Recovery key must be {KEY_BYTES} bytes")

    value = int.from_bytes(key, "big")
    symbols = []
    remaining = value
    for _ in range(_SYMBOLS):
        symbols.append(_ALPHABET[remaining & 0x1F])
        remaining >>= 5
    body = "".join(reversed(symbols))
    body += _CHECK_ALPHABET[value % _CHECK_MODULUS]

    groups = [body[i:i + GROUP_SIZE] for i in range(0, len(body), GROUP_SIZE)]
    return "-".join([PREFIX, *groups])


def decode(text: str) -> bytes:
    """Parse a recovery key, raising ValueError on anything malformed.

    Accepts lowercase, missing prefix, absent or arbitrary grouping, and the
    O/0 and I/L/1 confusions.
    """
    if not text or not text.strip():
        raise ValueError("Enter your recovery key")

    cleaned = "".join(ch for ch in text.strip() if ch not in _IGNORED).upper()
    # Fold the confusables before anything else, so a retyped "CATI" prefix is
    # recognised the same as "CAT1".
    cleaned = cleaned.translate(_CONFUSABLES)
    if cleaned.startswith(PREFIX):
        cleaned = cleaned[len(PREFIX):]

    if len(cleaned) != _SYMBOLS + 1:
        raise ValueError(
            f"A recovery key has {_SYMBOLS + 1} characters after the prefix, "
            f"but this one has {len(cleaned)}"
        )

    body, check_symbol = cleaned[:-1], cleaned[-1]

    value = 0
    for char in body:
        digit = _DECODE_MAP.get(char)
        if digit is None:
            raise ValueError(f"'{char}' is not valid in a recovery key")
        value = (value << 5) | digit

    expected_check = _CHECK_DECODE_MAP.get(check_symbol)
    if expected_check is None:
        raise ValueError(f"'{check_symbol}' is not a valid check character")
    if value % _CHECK_MODULUS != expected_check:
        raise ValueError("That recovery key has a typo in it")

    # The top bits beyond 128 must be zero for a well-formed key.
    if value >> KEY_BITS:
        raise ValueError("That is not a valid recovery key")

    return value.to_bytes(KEY_BYTES, "big")
