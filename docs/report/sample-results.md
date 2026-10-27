# UQAB laboratory demo results

Generated 2026-10-27 03:14 from the local `uqab.sqlite` database. Every number
below comes from runs recorded by `experiments.run` — nothing is typed in by
hand. Regenerate it with `python -m experiments.report`, or produce a whole run
end to end (experiment + tables) with `python scripts/demo.py`; each run stores
its persona, seed and window counts so it can be replayed.

## Headline comparison

| Config | Meaning | Runs | Loads | Median load (ms) | Overhead | Wire kbit/s | IDLE B/s | Accuracy | F1 | 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | plain Tor (no cover) | 3 | 36 | 1938 | 1.041x | 111.0 | 994.9 | 0.970 | 0.973 | 0.786–1.000 |
| B | constant-rate cover (static `constant` persona) | 3 | 36 | 2003 | 6.059x | 166.9 | 21278.8 | 0.518 | 0.495 | 0.091–0.811 |
| C | persona cover (timetable driven) | 3 | 36 | 2008 | 5.991x | 164.9 | 21336.6 | 0.438 | 0.487 | 0.154–0.833 |

Lower F1 means the observer had more trouble telling ACTIVE from IDLE browsing.
The 95% interval is a percentile bootstrap over the pooled cross-validation
predictions (NFR-05).

## Interpretation

Plain Tor was classified with F1 0.973 (95% CI 0.786–1.000); persona cover with F1 0.487 (95% CI 0.154–0.833). Persona cover reduced the observer's ACTIVE/IDLE F1 by 0.486 in this dataset, i.e. it made the inference task harder. Cost: the median page load was 1.04x plain Tor and the cover overhead was 5.991x the page bytes. Because the tunnel is persistent (D21) that overhead is a *rate*, not a constant: it is paid every second, so it grows the longer the user stays idle. The wire rate was 164.9 kbit/s and the observed traffic inside IDLE windows was 21336.6 B/s (plain Tor: 994.9 B/s). Constant-rate cover (B) scored F1 0.495 at 6.059x overhead; it is the baseline that persona cover has to beat on cost without giving up inference resistance.

## Runs

| Run | Config | Persona | Started | Duration (s) | Real bytes | Cover bytes | Overhead | p95 slot error (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20261027-031320-494313464-C-high | C | high | 2026-10-27T03:13:20 | 48.1 | 161636 | 991232 | 6.132x | 1.23 |
| 20261027-031231-598807689-C-high | C | high | 2026-10-27T03:12:31 | 48.7 | 172912 | 1011712 | 5.851x | 1.29 |
| 20261027-031143-195582723-C-high | C | high | 2026-10-27T03:11:43 | 48.2 | 164469 | 985088 | 5.990x | 1.23 |
| 20261027-031054-807331684-B-constant | B | constant | 2026-10-27T03:10:54 | 48.2 | 161636 | 1007616 | 6.234x | 1.24 |
| 20261027-031005-920072584-B-constant | B | constant | 2026-10-27T03:10:05 | 48.7 | 173540 | 1013760 | 5.842x | 1.23 |
| 20261027-030917-671457329-B-constant | B | constant | 2026-10-27T03:09:17 | 48.0 | 164469 | 1003520 | 6.102x | 1.22 |
| 20261027-030830-649012502-A-balanced | A | balanced | 2026-10-27T03:08:30 | 46.8 | 585936 | 610094 | 1.041x | - |
| 20261027-030741-025244898-A-balanced | A | balanced | 2026-10-27T03:07:41 | 49.4 | 646556 | 673277 | 1.041x | - |
| 20261027-030653-250957209-A-balanced | A | balanced | 2026-10-27T03:06:53 | 47.6 | 607822 | 632937 | 1.041x | - |

## Classifier scores

| Run | Classifier | Folds | Accuracy | Precision | Recall | F1 | 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 20261027-031320-494313464-C-high | logreg | 5 | 0.357 | 0.400 | 0.571 | 0.471 | 0.222–0.667 |
| 20261027-031320-494313464-C-high | random_forest | 5 | 0.321 | 0.381 | 0.571 | 0.457 | 0.250–0.634 |
| 20261027-031231-598807689-C-high | logreg | 5 | 0.562 | 0.556 | 0.882 | 0.682 | 0.500–0.833 |
| 20261027-031231-598807689-C-high | random_forest | 5 | 0.469 | 0.500 | 0.529 | 0.514 | 0.303–0.700 |
| 20261027-031143-195582723-C-high | logreg | 5 | 0.500 | 0.500 | 0.333 | 0.400 | 0.154–0.606 |
| 20261027-031143-195582723-C-high | random_forest | 5 | 0.417 | 0.412 | 0.389 | 0.400 | 0.194–0.585 |
| 20261027-031054-807331684-B-constant | logreg | 5 | 0.433 | 0.412 | 0.500 | 0.452 | 0.222–0.667 |
| 20261027-031054-807331684-B-constant | random_forest | 5 | 0.500 | 0.455 | 0.357 | 0.400 | 0.118–0.643 |
| 20261027-031005-920072584-B-constant | logreg | 5 | 0.600 | 0.667 | 0.400 | 0.500 | 0.200–0.720 |
| 20261027-031005-920072584-B-constant | random_forest | 5 | 0.600 | 0.579 | 0.733 | 0.647 | 0.414–0.811 |
| 20261027-030917-671457329-B-constant | logreg | 5 | 0.472 | 0.455 | 0.278 | 0.345 | 0.091–0.562 |
| 20261027-030917-671457329-B-constant | random_forest | 5 | 0.500 | 0.500 | 0.833 | 0.625 | 0.450–0.764 |
| 20261027-030830-649012502-A-balanced | logreg | 5 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000–1.000 |
| 20261027-030830-649012502-A-balanced | random_forest | 5 | 0.912 | 0.857 | 1.000 | 0.923 | 0.811–1.000 |
| 20261027-030741-025244898-A-balanced | logreg | 5 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000–1.000 |
| 20261027-030741-025244898-A-balanced | random_forest | 5 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000–1.000 |
| 20261027-030653-250957209-A-balanced | logreg | 5 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000–1.000 |
| 20261027-030653-250957209-A-balanced | random_forest | 5 | 0.906 | 0.842 | 1.000 | 0.914 | 0.786–1.000 |

## Totals

| metric | value |
| --- | --- |
| runs recorded | 9 |
| configurations | 3 |
| page loads attempted | 108 |
| page loads succeeded | 108 |

## Notes and caveats

- none

- Laboratory runs (no Tor binary) exercise the whole pipeline but not Tor's
  circuits; quote them as pipeline results, and quote real-bridge runs for claims
  about the Tor network. See `docs/LAB_MODE.md`.
- Configuration A in lab mode relays straight to the demo origin (see D7 in
  `docs/DESIGN_DECISIONS.md`).
- Personas, windows and seeds are recorded per run, so every row can be replayed.
