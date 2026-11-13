# Which configuration covers the road network best, and which is the most precise?

*Every U-Net road network of this work on the Hackathon sheet, out of fold, against BD TOPO roads of the U-Net's label classes (paths, tracks and stone tracks excluded), within 25 m. Routes and exercises from the evaluations of each experiment (`examples` data only).*

| network | km | completeness (coverage) | main roads | minor roads | correctness (precision) | quality | largest connected part | pixel IoU | route score town | pairs reached | 10 exercises: length gap BD TOPO / OSM | overlap BD TOPO / OSM |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| finetuned (6k, Hackathon only) | 9,495 | 68.6% | 81.6% | 63.5% | 84.2% | 61.4% | 98.4% | 0.418 | 0.153 | 90.0% | 7.4% / 8.5% | 69.7% / 70.2% |
| mix12k | 9,675 | 69.7% | 82.3% | 64.8% | 84.0% | 62.2% | 98.6% | 0.420 | 0.119 | 89.6% | 5.1% / 6.2% | 73.7% / 73.2% |
| mix12k + TTA | 9,270 | 68.5% | 82.2% | 63.2% | 85.7% | 62.1% | 99.1% | 0.425 | 0.131 | 88.8% | 5.7% / 6.8% | 73.6% / 74.2% |
| mix12k no Dice | 10,178 | 71.1% | 83.5% | 66.2% | 82.2% | 62.3% | 96.6% | 0.406 | 0.096 | 85.3% | 4.0% / 5.1% | 72.1% / 73.4% |
| mix12k no Dice + TTA | 9,657 | 69.9% | 82.8% | 64.8% | 84.2% | 62.5% | 97.4% | 0.411 | 0.118 | 86.9% | 5.1% / 6.3% | 75.1% / 75.3% |
| mix12k half Dice | 9,805 | 70.0% | 82.3% | 65.1% | 83.5% | 62.1% | 98.0% | 0.416 | 0.119 | 89.6% | 5.8% / 6.9% | 73.0% / 67.4% |
| mix12k half Dice + TTA | 9,397 | 68.8% | 81.8% | 63.6% | 85.0% | 62.0% | 98.5% | 0.420 | 0.126 | 88.4% | 5.6% / 6.7% | 75.2% / 74.9% |
| mix12k clDice | 9,067 | 66.3% | 80.0% | 61.0% | 85.0% | 60.0% | 98.9% | 0.414 | 0.153 | 88.0% | 6.0% / 7.1% | 75.2% / 76.1% |
| mix12k BCE x1 (traced at 0.25) | 10,423 | 71.1% | 84.2% | 65.9% | 80.6% | 61.4% | 94.4% | 0.352 | - | - | 4.5% / 5.6% | 70.4% / 72.2% |
