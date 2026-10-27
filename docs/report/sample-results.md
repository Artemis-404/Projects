# UQAB laboratory demo results

Generated 2026-10-27 02:35 by `python -m experiments.report` from the local
`uqab.sqlite` database. Every number below comes from a run recorded by
`python -m experiments.compare`; nothing is typed in by hand.

## Headline comparison

| Config | Meaning | Runs | Loads | Median load (ms) | Overhead | Accuracy | F1 | 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | plain Tor (no cover) | 3 | 60 | 977 | 1.041x | 0.944 | 0.913 | 0.722–1.000 |
| B | constant-rate cover (static `constant` persona) | 3 | 60 | 1201 | 2.793x | 0.884 | 0.850 | 0.612–1.000 |
| C | persona cover (timetable driven) | 3 | 60 | 989 | 3.477x | 0.943 | 0.919 | 0.774–1.000 |

Lower F1 means the observer had more trouble telling ACTIVE from IDLE browsing.
The 95% interval is a percentile bootstrap over the pooled cross-validation
predictions (NFR-05).

## Interpretation

Plain Tor was classified with F1 0.913 (95% CI 0.722–1.000); persona cover with F1 0.919 (95% CI 0.774–1.000). The difference in F1 is smaller than 0.05 and inside the reported intervals, so this dataset does not support a claim either way. Cost: the median page load was 1.01x plain Tor and the cover overhead was 3.477x the page bytes. Constant-rate cover (B) scored F1 0.850 at 2.793x overhead; it is the baseline that persona cover has to beat on cost without giving up inference resistance.

## Runs

| Run | Config | Persona | Started | Duration (s) | Real bytes | Cover bytes | Overhead | p95 slot error (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20261027-023500-139006068-C-high | C | high | 2026-10-27T02:35:00 | 58.4 | 303568 | 1056768 | 3.481x | 1.19 |
| 20261027-023400-135613391-C-high | C | high | 2026-10-27T02:34:00 | 59.7 | 295650 | 1028096 | 3.477x | 1.20 |
| 20261027-023258-900252263-C-high | C | high | 2026-10-27T02:32:58 | 60.9 | 324445 | 1126400 | 3.472x | 1.18 |
| 20261027-023159-079912338-B-constant | B | constant | 2026-10-27T02:31:59 | 59.5 | 145470 | 405504 | 2.788x | 1.26 |
| 20261027-023100-237459732-B-constant | B | constant | 2026-10-27T02:31:00 | 58.5 | 142740 | 398336 | 2.791x | 1.23 |
| 20261027-022959-533758053-B-constant | B | constant | 2026-10-27T02:29:59 | 60.4 | 145160 | 406528 | 2.801x | 1.30 |
| 20261027-022858-809178302-A-balanced | A | balanced | 2026-10-27T02:28:58 | 60.2 | 530885 | 552865 | 1.041x | - |
| 20261027-022800-014851044-A-balanced | A | balanced | 2026-10-27T02:28:00 | 58.5 | 519200 | 540710 | 1.041x | - |
| 20261027-022701-576396269-A-balanced | A | balanced | 2026-10-27T02:27:01 | 58.2 | 505453 | 526317 | 1.041x | - |

## Classifier scores

| Run | Classifier | Folds | Accuracy | Precision | Recall | F1 | 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 20261027-023500-139006068-C-high | logreg | 5 | 0.947 | 0.950 | 0.905 | 0.927 | 0.829–1.000 |
| 20261027-023500-139006068-C-high | random_forest | 5 | 0.947 | 0.950 | 0.905 | 0.927 | 0.829–1.000 |
| 20261027-023400-135613391-C-high | logreg | 5 | 0.931 | 0.870 | 0.952 | 0.909 | 0.800–0.979 |
| 20261027-023400-135613391-C-high | random_forest | 5 | 0.948 | 0.875 | 1.000 | 0.933 | 0.837–1.000 |
| 20261027-023258-900252263-C-high | logreg | 5 | 0.949 | 0.900 | 0.947 | 0.923 | 0.824–1.000 |
| 20261027-023258-900252263-C-high | random_forest | 5 | 0.932 | 0.895 | 0.895 | 0.895 | 0.774–0.979 |
| 20261027-023159-079912338-B-constant | logreg | 5 | 0.879 | 0.773 | 0.895 | 0.829 | 0.686–0.936 |
| 20261027-023159-079912338-B-constant | random_forest | 5 | 0.879 | 0.750 | 0.947 | 0.837 | 0.696–0.944 |
| 20261027-023100-237459732-B-constant | logreg | 5 | 0.842 | 0.704 | 0.950 | 0.809 | 0.667–0.913 |
| 20261027-023100-237459732-B-constant | random_forest | 5 | 0.807 | 0.667 | 0.900 | 0.766 | 0.612–0.872 |
| 20261027-022959-533758053-B-constant | logreg | 5 | 0.949 | 0.870 | 1.000 | 0.930 | 0.826–1.000 |
| 20261027-022959-533758053-B-constant | random_forest | 5 | 0.949 | 0.870 | 1.000 | 0.930 | 0.826–1.000 |
| 20261027-022858-809178302-A-balanced | logreg | 5 | 0.948 | 0.938 | 0.882 | 0.909 | 0.774–1.000 |
| 20261027-022858-809178302-A-balanced | random_forest | 5 | 0.983 | 0.944 | 1.000 | 0.971 | 0.900–1.000 |
| 20261027-022800-014851044-A-balanced | logreg | 5 | 0.930 | 0.938 | 0.833 | 0.882 | 0.733–0.974 |
| 20261027-022800-014851044-A-balanced | random_forest | 5 | 0.895 | 0.773 | 0.944 | 0.850 | 0.722–0.955 |
| 20261027-022701-576396269-A-balanced | logreg | 5 | 0.929 | 0.941 | 0.842 | 0.889 | 0.743–0.974 |
| 20261027-022701-576396269-A-balanced | random_forest | 5 | 0.982 | 0.950 | 1.000 | 0.974 | 0.909–1.000 |

## Totals

| metric | value |
| --- | --- |
| runs recorded | 9 |
| configurations | 3 |
| page loads attempted | 180 |
| page loads succeeded | 180 |

## Notes and caveats

- none

- Laboratory runs (no Tor binary) exercise the whole pipeline but not Tor's
  circuits; quote them as pipeline results, and quote real-bridge runs for claims
  about the Tor network. See `docs/LAB_MODE.md`.
- Configuration A in lab mode relays straight to the demo origin (see D7 in
  `docs/DESIGN_DECISIONS.md`).
- Personas, windows and seeds are recorded per run, so every row can be replayed.
