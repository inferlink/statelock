# SPDX-License-Identifier: Apache-2.0
"""Which currencies a value names, so amounts in different currencies never compare equal.

ISO 4217 codes and unambiguous symbols map to a code: "€5" and "5 EUR" are both
EUR. A code counts only in upper case and next to the number, before or after it,
with only spaces, signs, parentheses or a currency symbol between ("USD 5",
"5 EUR", "(5.00 EUR)", "USD -5"), so words such as "Top up" or "ALL items" name
no currency. A code in lower or mixed case next to the number ("usd 5", "5 Eur")
is not a code, but it makes the amount unclear: parse_number refuses it, so
"5 sek" and "5 nok" never compare equal as numbers. "$" and "¥" are used by several currencies, so each is its own
currency, equal only to itself: "$5" is not "USD 5". A prefixed form names the
code ("US$", "C$", "A$", "CN¥"). Any other currency symbol is its own currency.
"""

from __future__ import annotations

import re
import unicodedata

from statelock.policy.text import normalize_text

# Active ISO 4217 codes, plus funds, precious metals and the test/no-currency codes.
_ISO_4217 = """
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE
CHF CHW CLF CLP CNY COP COU CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ
GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD
MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG
QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD
TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU
XTS XUA XXX YER ZAR ZMW ZWG ZWL
"""
ISO_4217_CODES = frozenset(_ISO_4217.split())

# Symbols that name one currency.
SYMBOL_CODES = {
    "€": "EUR",
    "£": "GBP",
    "₹": "INR",
    "₩": "KRW",
    "₽": "RUB",
    "₺": "TRY",
    "₴": "UAH",
    "₦": "NGN",
    "₱": "PHP",
    "₪": "ILS",
    "₫": "VND",
    "₡": "CRC",
    "₲": "PYG",
    "₵": "GHS",
    "₸": "KZT",
    "₼": "AZN",
    "₾": "GEL",
    "₭": "LAK",
    "₮": "MNT",
    "฿": "THB",
    "៛": "KHR",
}

# "$" and "¥" with a country prefix.
PREFIXED_CODES = {
    "US$": "USD",
    "U$S": "USD",
    "C$": "CAD",
    "CA$": "CAD",
    "A$": "AUD",
    "AU$": "AUD",
    "NZ$": "NZD",
    "HK$": "HKD",
    "S$": "SGD",
    "SG$": "SGD",
    "NT$": "TWD",
    "R$": "BRL",
    "MX$": "MXN",
    "CN¥": "CNY",
    "JP¥": "JPY",
}

_PREFIXED = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(p) for p in sorted(PREFIXED_CODES, key=len, reverse=True)) + ")"
)
_WORD = re.compile(r"(?<![A-Za-z])[A-Za-z]{3}(?![A-Za-z])")
_SIGNS_AND_PARENTHESES = "()+-\u2212"


def _is_separator(char: str) -> bool:
    """What may stand between a currency code and its number."""
    return char.isspace() or char in _SIGNS_AND_PARENTHESES or unicodedata.category(char) == "Sc"


def _next_to_number(text: str, start: int, end: int) -> bool:
    before = start
    while before > 0 and _is_separator(text[before - 1]):
        before -= 1
    after = end
    while after < len(text) and _is_separator(text[after]):
        after += 1
    return (before > 0 and text[before - 1].isdigit()) or (after < len(text) and text[after].isdigit())


def _iso_words(text: str) -> list[re.Match[str]]:
    """Three-letter ISO 4217 codes in any case next to the number."""
    return [
        match
        for match in _WORD.finditer(text)
        if match.group().upper() in ISO_4217_CODES and _next_to_number(text, match.start(), match.end())
    ]


def code_matches(text: str) -> list[re.Match[str]]:
    """The currency codes in ``text`` (normalize_text with casefold=False): upper case, next to the number."""
    return [match for match in _iso_words(text) if match.group().isupper()]


def has_unclear_code(text: str) -> bool:
    """Whether an ISO code in lower or mixed case is next to the number ("usd 5"): the
    amount's currency is unclear (``text`` as for code_matches)."""
    return any(not match.group().isupper() for match in _iso_words(text))


def currencies(value: str) -> set[str]:
    """The currencies ``value`` names: ISO codes, or "$" / "¥" / another symbol for a
    symbol without a single code. Empty when it names none."""
    text = normalize_text(value, casefold=False)
    found: set[str] = set()
    for match in _PREFIXED.finditer(text):
        found.add(PREFIXED_CODES[match.group()])
    text = _PREFIXED.sub(" ", text)
    for char in text:
        if unicodedata.category(char) == "Sc":
            found.add(SYMBOL_CODES.get(char, char))
    found.update(match.group() for match in code_matches(text))
    return found
