# CARTE — presentation and demo

A 10-minute talk (20 slides, speaker notes timed to 10:00) and a 3:51 demo video of the interface.

| file | what |
|---|---|
| `slides.src.html` | editable source of the deck (HTML/CSS, one `<section>` per slide, notes in `<aside class="notes">`) |
| `build_deck.py` | draws the charts from the saved experiment runs (`charts/data_used.json` lists every plotted value and its source), inlines charts and icons into `slides.html`, prints `CARTE_presentation.pdf`, renders `preview/slide-NN.png`, writes `speaker_notes.md` |
| `make_figures.py` | the images of the vision slides, from the real rasters (one map window at each processing step, and what each U-Net version reads) |
| `speaker_notes.md` | what to say on each slide, with times |
| `../../interface/scripts/demo_video.py` | records `demo/CARTE_demo.webm` and `demo/demo_chapters.md` (chapter times and pause points) from the running app |

Built files (`slides.html`, the PDF, `preview/`, `charts/`) and the images cut from the map scan (`img/`) are in git.
The demo video (145 MB, over GitHub's file limit) is only in the release archive. To rebuild them you need the data of the release archive (see the main
README, « Data and models ») and Chromium for Playwright:

```bash
.venv/bin/python docs/presentation/make_figures.py      # img/ step_*, ver_*, var_* (needs the U-Net rasters)
.venv/bin/python docs/presentation/build_deck.py        # charts, slides.html, CARTE_presentation.pdf, previews, notes
interface/run.sh && .venv/bin/python interface/scripts/demo_video.py   # demo video (server and language model running)
```

The screenshots in `img/` come from `interface/scripts/ui_smoke.py`. The Swiss Siegfried crop comes from the ProbRoadClass
figure (MIT licence, geodata © swisstopo).

Licences of the bundled assets: IBM Plex fonts, SIL Open Font License 1.1 (`fonts/LICENSE-OFL.txt`); icons from Lucide
(via react-icons), ISC licence.
