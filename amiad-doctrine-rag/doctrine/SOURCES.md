# Source documents (the 5 PDFs used)

The PDFs are not stored in this repository. Put them in this folder (`doctrine/`) under exactly these names, then run `docrag index` (see the main README, step 1). Check each file with its SHA-256: if the hash is the same, the index you build is the same as ours (checked on the AEP file: same hash, 315 passages out of 315 identical).

All five are public, unclassified NATO or allied documents. Where the file came from is not recorded in the project files: use the organisers' hand-out or the public source of each document.

| File | PDF pages | Passages | Reading reliability | Bytes | SHA-256 |
|---|---|---|---|---|---|
| `AEP-3.12.1.5-MILITARY-LOAD-CLASSIFICATION.pdf` | 94 | 315 | high | 4399562 | `d3365412d14b2e41cb4e9864b0de255edff2aa3809f8f642a45be17b89f43528` |
| `NP_allied_joint_doctrine_for_movement_AJP-4.4.pdf` | 58 | 287 | high | 836388 | `87fa66f746023ba68654681ecbb2cd334851dac02163df51137d7405cd78cf75` |
| `NP_allied_joint_doctrine_joint_logistic_support_group_AJP-4.6.pdf` | 57 | 278 | high | 497342 | `69cb4be5b9d746571c23c4fe1c23e34f10d8b465bfcc027c65bf739270c7799c` |
| `STANAG_2015.pdf` | 282 | 1331 | medium | 6192636 | `157209125eb330dc25323ff77971032ec7a99daf45b7d7e037aaaf843fcd3ecf` |
| `STANAG_2174_alternative.pdf` | 322 | 1275 | medium | 3704327 | `557517a21226a390ae625988f9239e0c0136d0c0455cbd0afded3cb89a80bae0` |

Reading reliability (from `work_doctrine/reading_report.json` and `reliability_overrides.json`): **high** = digital PDF with a good text layer; **medium** = scanned field manual read through its OCR text layer, so every rule taken from it must be compared with the PDF page.
