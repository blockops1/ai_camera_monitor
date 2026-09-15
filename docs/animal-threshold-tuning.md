# Animal Threshold Tuning Report

Generated: 2026-09-14 22:47:52 UTC

## Summary

**15 frames** analyzed from Finnegan replay (15 positive, 15 synthetic other-dog negative).

## Tier-1 Threshold (VM2 Feature Scoring)

Finnegan Tier-1 scores range from 2.75 to 3.5 (mean: 3.042). Synthetic-other max Tier-1: 2.75. Separation: 0.3.

Tier-1 weights: species=1.0, size=1.0, color=1.5, distinctive=1.5.

### Finnegan per-frame Tier-1 scores

  Frame 1: 3.5
  Frame 2: 2.75
  Frame 3: 3.05
  Frame 4: 2.75
  Frame 5: 3.5
  Frame 6: 3.35
  Frame 7: 3.0
  Frame 8: 2.75
  Frame 9: 3.05
  Frame 10: 2.75
  Frame 11: 3.5
  Frame 12: 2.75
  Frame 13: 2.75
  Frame 14: 3.05
  Frame 15: 3.125

### Synthetic-other Tier-1 scores

  Frame 1: 2.75
  Frame 2: 2.75
  Frame 3: 2.75
  Frame 4: 2.75
  Frame 5: 2.75
  Frame 6: 2.75
  Frame 7: 2.75
  Frame 8: 2.75
  Frame 9: 2.75
  Frame 10: 2.75
  Frame 11: 2.75
  Frame 12: 2.75
  Frame 13: 2.75
  Frame 14: 2.75
  Frame 15: 2.75

## Tier-2 Threshold (MegaDescriptor Cosine)

Finnegan cosine scores range from 0.216 to 0.2964 (mean: 0.2593). Synthetic-other max: 0.0912.

### Threshold recommendation

**Recommended Tier-2 cosine threshold: 0.85**

- Recall (Finnegan correctly accepted): 0.0
- Specificity (other-dog correctly rejected): 0.0
- F1 score: 0.0

### Threshold sweep

| Threshold | Recall | Specificity |
|-----------|--------|-------------|
| 0.50 | 0.000 | 1.000 |
| 0.51 | 0.000 | 1.000 |
| 0.52 | 0.000 | 1.000 |
| 0.53 | 0.000 | 1.000 |
| 0.54 | 0.000 | 1.000 |
| 0.55 | 0.000 | 1.000 |
| 0.56 | 0.000 | 1.000 |
| 0.57 | 0.000 | 1.000 |
| 0.58 | 0.000 | 1.000 |
| 0.59 | 0.000 | 1.000 |
| 0.60 | 0.000 | 1.000 |
| 0.61 | 0.000 | 1.000 |
| 0.62 | 0.000 | 1.000 |
| 0.63 | 0.000 | 1.000 |
| 0.64 | 0.000 | 1.000 |
| 0.65 | 0.000 | 1.000 |
| 0.66 | 0.000 | 1.000 |
| 0.67 | 0.000 | 1.000 |
| 0.68 | 0.000 | 1.000 |
| 0.69 | 0.000 | 1.000 |
| 0.70 | 0.000 | 1.000 |
| 0.71 | 0.000 | 1.000 |
| 0.72 | 0.000 | 1.000 |
| 0.73 | 0.000 | 1.000 |
| 0.74 | 0.000 | 1.000 |
| 0.75 | 0.000 | 1.000 |
| 0.76 | 0.000 | 1.000 |
| 0.77 | 0.000 | 1.000 |
| 0.78 | 0.000 | 1.000 |
| 0.79 | 0.000 | 1.000 |
| 0.80 | 0.000 | 1.000 |
| 0.81 | 0.000 | 1.000 |
| 0.82 | 0.000 | 1.000 |
| 0.83 | 0.000 | 1.000 |
| 0.84 | 0.000 | 1.000 |
| 0.85 | 0.000 | 1.000 |
| 0.86 | 0.000 | 1.000 |
| 0.87 | 0.000 | 1.000 |
| 0.88 | 0.000 | 1.000 |
| 0.89 | 0.000 | 1.000 |
| 0.90 | 0.000 | 1.000 |
| 0.91 | 0.000 | 1.000 |
| 0.92 | 0.000 | 1.000 |
| 0.93 | 0.000 | 1.000 |
| 0.94 | 0.000 | 1.000 |
| 0.95 | 0.000 | 1.000 |
| 0.96 | 0.000 | 1.000 |
| 0.97 | 0.000 | 1.000 |
| 0.98 | 0.000 | 1.000 |

## Finnegan Enrolled Signature

- Species: dog
- Size: medium
- Color patterns: ['tan', 'brown']
- Distinctive features: ['black collar', 'curly fur', 'blue collar', 'curled tail']

## Synthetic Other-Dog Signature

- Species: dog
- Size: medium
- Color patterns: ['brown', 'tan']
- Distinctive features: ['white paws', 'long tail']

## Conclusion

The Tier-2 cosine threshold of **0.85** provides 0% recall while rejecting the synthetic other-dog at 0% specificity.

Update `animal_matcher/config.py` to set `TIER2_THRESHOLD` to 0.85.

