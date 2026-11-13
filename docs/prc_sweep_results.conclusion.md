## 7. Conclusions

- **Longer training does not help on this sheet.** The 6,000-step run stays the best by out-of-fold road IoU (0.418 against 0.391-0.415), road AUC (0.960) and importance accuracy (80.3%). The held-out curves (section 2) peak at 6,000-9,000 steps for every run.
- **A learning rate of 3e-4 overfits beyond that**: s24k falls from 0.418 (6k) to about 0.40 (24k), and batch 32 behaves the same at half the steps. At 1e-4 (s24k_lr1e-4) the score holds steady but never passes the 6k run.
- **Only the structure output (bridges, overpasses) gains from long training** (AUC 0.655 -> 0.735 for s24k); it does not steer routes.
- **Routes agree with the pixels on the 251 independent pairs** (section 3): the 6k network has the best route score (0.153 / 0.142 against 0.156-0.180) and is the only one that reaches 90% of the pairs; the longer runs' networks are more fragmented (86-87%).
- **On the 10 exercises, s24k is about one point closer to BD TOPO** (length gap 6.5% against 7.4%, overlap 74% against 70%). The 10 exercises are 4 distinct trips, so this is within their noise, and choosing the model on the exercises we report would bias them; the 6k model is kept, and the s24k routes are given as an alternative (section 5b).
- **Without BD TOPO or OSM** the final model (6k configuration, every block) gives routes within 1-5% of the out-of-fold ones (section 6), as expected since it saw this sheet's labels. Their accuracy on a new sheet is not measured: that needs a second labelled IGN sheet.
- **Fix made during the run**: the final model's seed was the fold number (-1), which NumPy refuses; it is now 99 for the final model, and the final training and the inference were rerun.
