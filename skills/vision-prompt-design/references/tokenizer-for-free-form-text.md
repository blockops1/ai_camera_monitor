# Tokenizer for free-form text matching

Canonical tokenizer for free-form user-supplied strings (distinctive features, clothing descriptions, notable details, scene captions) that feed into set-Jaccard or token-set matching in vision-pipeline matchers.

Origin: §11.86.2 `_feature_tokens()` in `infra/animal_matcher.py` (Phase 6B.165, 2026-08-30). Lesson: `jill-workflow-style/references/session-corrections-2026-08-30.md` lesson D.

## The bug this solves

Vision models return features in inconsistent forms:

| Vision returns | Without fix | With fix |
|---|---|---|
| `"white-tipped tail"` | `{white-tipped, tail}` | `{white, tipped, tail}` |
| `"scar on left shoulder"` | `{scar, on, left, shoulder}` | `{scar, left, shoulder}` |
| `"collar, blue"` | `{collar,, blue}` or `{collar, blue}` (depends on strip scope) | `{collar, blue}` |

Three failure modes:

1. **Hyphens preserved as part of tokens** → `"white-tipped"` and `"white tipped"` are treated as different words by Jaccard, even though they describe the same visual feature.
2. **Stopwords (the, a, on, in, with...) dominating token sets** → Jaccard over-penalizes matches that differ only in stopword usage ("on shoulder" vs "shoulder").
3. **Edge punctuation mishandled** → commas, periods, parentheses stuck to words, breaking exact match.

## Canonical implementation

```python
def tokenize_feature(text: str) -> set[str]:
    """Tokenize free-form feature text for set-Jaccard matching.

    Punctuation handling:
    - Hyphens split (white-tipped → {white, tipped}).
    - Edge punctuation stripped (. , ; : ' " ! ? ( ) [ ]).
    - Stopwords dropped (the, a, an, is, on, in, with, and, or, of, to, no, yes).

    Returns a set of lowercased word tokens. Order is not preserved.
    """
    STOPWORDS = frozenset({
        "the", "a", "an", "is", "on", "in", "with", "and", "or", "of", "to", "no", "yes",
    })
    # Step 1: lowercase.
    # Step 2: replace hyphens with spaces (white-tipped → white tipped).
    # Step 3: strip EDGE punctuation only (.strip(".,;:'\"!?()[]")).
    # Step 4: split on whitespace.
    # Step 5: filter empty tokens and stopwords.
    cleaned = text.lower().replace("-", " ").strip(".,;:'\"!?()[]")
    return {t for t in cleaned.split() if t and t not in STOPWORDS}
```

## Why each line matters

| Step | Without it |
|---|---|
| `.lower()` | Case-mismatched duplicates (`"Collar"` vs `"collar"`) inflate the set. |
| `.replace("-", " ")` | `"white-tipped"` (with hyphen) and `"white tipped"` (with space) become different tokens. Vision returns both forms. |
| `.strip(".,;:'\"!?()[]")` | Edge punctuation sticks to words (`"collar,"` ≠ `"collar"`). |
| `.split()` | Single token (whole string) — defeats the purpose. |
| Stopword filter | `"scar on shoulder"` returns `{scar, on, shoulder}` instead of `{scar, shoulder}` — `on` inflates the union and dilutes Jaccard. |

**Don't try to strip interior punctuation.** `text.replace(".", " ")` would corrupt abbreviations ("Mr." → "Mr"). Edge-strip is the safe default.

**Don't drop content negation.** `no`, `not`, `without` are kept in this set as stopwords because for set-Jaccard on distinctive features, the absence of a feature is already encoded in the *mismatch* (e.g., "no collar" vs "blue collar" have 0 overlap on collar tokens, which IS the right signal). If you need to distinguish "no collar" from "collar" explicitly, drop `no` and `not` from the stopword set.

## The 3 mandatory test patterns

Every tokenizer for free-form text matching MUST pass these three tests. If your tokenizer fails any of them, it's wrong. Add them to the test file:

```python
def test_hyphen_splits_into_two_tokens():
    assert tokenize_feature("white-tipped tail") == {"white", "tipped", "tail"}

def test_stopword_on_dropped():
    assert tokenize_feature("scar on left shoulder") == {"scar", "left", "shoulder"}

def test_edge_comma_stripped():
    assert tokenize_feature("collar, blue") == {"collar", "blue"}
```

Optional 4th test for completeness:

```python
def test_combined_punctuation_and_hyphen():
    assert tokenize_feature("Limp in left rear leg!") == {"limp", "left", "rear", "leg"}
```

## Stopword set composition

The minimal stopword set for English distinctive-feature matching:

```python
STOPWORDS = {"the", "a", "an", "is", "on", "in", "with", "and", "or", "of", "to", "no", "yes"}
```

13 words. Anything more is over-engineering. Words to consider keeping (NOT stopwords):

- `not`, `without` — useful negation signal if your matcher cares about absence.
- `left`, `right`, `front`, `rear` — directional signals that matter for individual ID (`"left ear notched"` ≠ `"right ear notched"`).
- Color words (`brown`, `black`, `white`, `tan`, `gray`, `red`, `blue`) — encoding, not stopwords.

## When to extend the stopword set

Add a word to the stopword set ONLY when:
- It appears in >30% of feature strings in production data, AND
- Removing it does NOT change any meaningful match/mismatch signal.

Example: if `"scar"` appears in 50% of distinctive features and isn't differentiating, add it. But if `"left"` appears in 50% and is differentiating (because left-ear vs right-ear matters), keep it.

**Don't extend the stopword set from theory.** Collect actual feature strings from production, count frequencies, then decide.

## Reusable across matchers

This tokenizer is identical for:
- `animal_matcher._feature_tokens` — distinctive features for individual animal ID
- `person_matcher.<future>._feature_tokens` — clothing, accessories, posture quirks (if matcher is extended to free-form text)
- Any future pipeline that does set-Jaccard on vision-emitted strings

The implementation is small enough to copy verbatim into each module. The test patterns are universal. If you find yourself needing a different rule (e.g., a different stopword set for a non-English locale), fork it intentionally and document why.
