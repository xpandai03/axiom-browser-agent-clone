"""
Self-checks — name keying, and its parity with the CRM.

Run:  python3 -m tests.test_name_keys

No browser, no network, no dependencies. Every identity below is invented.

THE SHARED TABLE. The cases under [1] and [2] are the same cases the CRM asserts
in scripts/test-name-keys.ts. They exist twice on purpose: two codebases compare
names, and the only way they stay one rule is if both are pinned to the same
table. If a case here changes, the CRM's copy changes with it.
"""

import sys

from shared.name_keys import name_key, name_keys, names_agree

passed = 0
failed = 0
failures = []


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        failures.append(label)
        print(f"  FAIL {label}{f' — {detail}' if detail else ''}")


def eq(label, got, want):
    check(label, got == want, f"got {got!r}, want {want!r}")


print("\n[1] name_key — unchanged behaviour, still the CRM's nameKey")
eq("word order does not matter", name_key("Puffin, Wendell"), name_key("Wendell Puffin"))
eq("apostrophes are deleted intra-word", name_key("O'Brien"), name_key("OBrien"))
eq("diacritics fold", name_key("Renée"), "renee")
eq("curly apostrophes fold too", name_key("O’Callaghan Siobhán"), "ocallaghan siobhan")
check("a hyphen is two tokens, so a married surname still differs",
      name_key("Ashgrove-Pemberton") != name_key("Ashgrove"))
check("a middle name is a real difference",
      name_key("Wendell Michael Puffin") != name_key("Wendell Puffin"))
eq("a parenthetical is stripped", name_key("Minor (Rowan) Thistlewood"), "minor thistlewood")
eq("empty input is an empty key", name_key(""), "")
eq("None is an empty key", name_key(None), "")


print("\n[2] name_keys — every reading, never a guess at which is legal")
eq("Preferred (Legal) Last reads BOTH ways",
   name_keys("Minor (Rowan) Thistlewood"), ("minor thistlewood", "rowan thistlewood"))
eq("Legal (Nickname) Last reads both ways too — the old shape still works",
   name_keys("Wendell (Wendy) Puffin"), ("puffin wendell", "puffin wendy"))
eq("a TRAILING group annotates and contributes no reading",
   name_keys("Rosalind Ashgrove (dad)"), ("ashgrove rosalind",))
eq("no parenthetical is exactly one reading, and it is name_key",
   name_keys("Rowan Thistlewood"), (name_key("Rowan Thistlewood"),))
eq("the first reading is always name_key",
   name_keys("Minor (Rowan) Thistlewood")[0], name_key("Minor (Rowan) Thistlewood"))

print("\n[2a] Edge shapes")
eq("TWO groups: the middle one reads, the trailing one annotates",
   name_keys("Minor (Rowan) Thistlewood (dad)"), ("minor thistlewood", "rowan thistlewood"))
eq("a group in the SURNAME position is trailing, so it annotates",
   name_keys("Rowan Thistlewood (Smith)"), ("rowan thistlewood",))
eq("a group with words after it reads, wherever it sits",
   name_keys("Rowan (Thistlewood) Smith"), ("rowan smith", "smith thistlewood"))
eq("a LEADING group still reads",
   name_keys("(Rowan) Thistlewood"), ("thistlewood", "rowan thistlewood"))
eq("an empty group contributes nothing",
   name_keys("Minor () Thistlewood"), ("minor thistlewood",))
eq("an unbalanced paren is just a separator",
   name_keys("Rowan (Thistlewood"), ("rowan thistlewood",))
eq("empty input has no readings", name_keys("   "), ())
eq("None has no readings", name_keys(None), ())
eq("a hyphenated surname inside a group keeps both tokens",
   name_keys("Minor (Rowan) Thistlewood-Smith"),
   ("minor smith thistlewood", "rowan smith thistlewood"))
eq("'Minor' as an ACTUAL surname is untouched — no parenthetical, one reading",
   name_keys("Rowan Minor"), ("minor rowan",))


print("\n[3] names_agree — intersection, not subset")
check("THE CASE THIS BUILD EXISTS FOR: survey legal name vs chart preferred form",
      names_agree("Rowan Thistlewood", "Minor (Rowan) Thistlewood"))
check("...and symmetrically", names_agree("Minor (Rowan) Thistlewood", "Rowan Thistlewood"))
check("the old shape still agrees", names_agree("Wendell Puffin", "Wendell (Wendy) Puffin"))
check("a nickname on the survey agrees with the chart's parenthetical",
      names_agree("Wendy Puffin", "Wendell (Wendy) Puffin"))
check("word order still does not matter",
      names_agree("Thistlewood, Rowan", "Minor (Rowan) Thistlewood"))
check("a trailing annotation is ignored on either side",
      names_agree("Rosalind Ashgrove", "Rosalind Ashgrove (dad)"))

print("\n[3a] What it must still REFUSE")
check("a hyphenated surname is NOT the same person",
      not names_agree("Rowan Thistlewood", "Rowan Thistlewood-Smith"))
check("a middle name is a real difference — subset would have passed this",
      not names_agree("Rowan Thistlewood", "Rowan James Thistlewood"))
check("a different first name with the same surname",
      not names_agree("Rowan Thistlewood", "Minor (Rosalind) Thistlewood"))
check("the preferred token alone is not the person",
      not names_agree("Minor Thistlewood", "Rowan Thistlewood"))
check("an empty name agrees with nothing", not names_agree("", "Rowan Thistlewood"))
check("...in either position", not names_agree("Rowan Thistlewood", ""))
check("a surname alone does not agree with a full name",
      not names_agree("Thistlewood", "Minor (Rowan) Thistlewood"))


print("\n[4] The looseness this replaces — whole-element subset is gone")
# The old row comparison tokenised tr.innerText and tested subset, so a survey
# name passed when its tokens merely appeared SOMEWHERE in the row.
row_text = "Zzother Zzperson 1/1/1980 505-555-0100 BlueCross Rowan Thistlewood"
check("survey tokens appearing elsewhere in a row no longer match the name cell",
      not names_agree("Rowan Thistlewood", "Zzother Zzperson"))
check("...and the row's own text is never what is compared now",
      not names_agree("Rowan Thistlewood", row_text))


print("\n[5] Parity contract with the CRM")
# Both codebases must produce these exact strings for these exact inputs. The
# CRM's scripts/test-name-keys.ts asserts the same table.
PARITY = [
    ("Minor (Rowan) Thistlewood", ["minor thistlewood", "rowan thistlewood"]),
    ("Wendell (Wendy) Puffin", ["puffin wendell", "puffin wendy"]),
    ("Rosalind Ashgrove (dad)", ["ashgrove rosalind"]),
    ("Rowan Thistlewood", ["rowan thistlewood"]),
    ("Thistlewood, Rowan", ["rowan thistlewood"]),
    ("Siobhán O'Callaghan", ["ocallaghan siobhan"]),
    ("Ashgrove-Pemberton, Rosalind", ["ashgrove pemberton rosalind"]),
    ("Minor (Rowan) Thistlewood (dad)", ["minor thistlewood", "rowan thistlewood"]),
    ("(Rowan) Thistlewood", ["thistlewood", "rowan thistlewood"]),
    ("Rowan (Thistlewood) Smith", ["rowan smith", "smith thistlewood"]),
    ("Minor () Thistlewood", ["minor thistlewood"]),
    ("Rowan Minor", ["minor rowan"]),
]
for raw, want in PARITY:
    eq(f"parity: {raw!r}", list(name_keys(raw)), want)


print(f"\n{'=' * 60}")
print(f"  {passed} passed, {failed} failed")
if failures:
    for f in failures:
        print(f"    - {f}")
print(f"{'=' * 60}\n")
sys.exit(1 if failed else 0)
