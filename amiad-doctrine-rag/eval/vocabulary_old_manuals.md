# Vocabulary of the two old manuals, for LEXICON (graph.py)

Requested by the teammate: "Dans les deux vieux manuels (STANAG_2015 et 2174), note les termes qui reviennent et leurs abréviations. Je les ajouterai dans LEXICON (graph.py)."

**Which file is which**
- `STANAG_2015.pdf` = **FM 5-36**, *Route Reconnaissance and Classification* (US Army, 1970). It implements the NATO route-classification STANAG.
- `STANAG_2174_alternative.pdf` = **FM 55-30**, *Army Motor Transport Units and Operations* (change 1, 1997). It is the file named after STANAG 2174.

**How the counts were made:** on the text layer of the extracted pages (`data/kb_extract/`, OCR layer left out), words and two-word phrases counted after removing common English words. **The counts are approximate and depend on the extraction tool.** Example: "reconnaissance" gives 475 with `pdftotext -layout`, 555 with the default `pdftotext`, and 568 with Firefox's Ctrl+F on the same PDF. Use the counts to rank terms, not as exact numbers.

## 1. Recurring terms, FM 5-36 (STANAG_2015)

| Term | Count | Meaning in this manual |
|---|---|---|
| reconnaissance | 473 | route, bridge, ford and tunnel reconnaissance (the subject of the manual) |
| bridge | 422 | bridge classification and bridge reconnaissance (chapter 2, section VI and VII) |
| route | 342 | a road path classified for military traffic |
| military load class | 60 (phrase) | the load class number of a bridge or vehicle. **The manual does not write "MLC" (0 times in the text)** |
| traveled way | 79 (phrase) | the carriageway a vehicle uses. The width of the route is measured on it |
| route classification | 18 (phrase) | classification of a route by width, route type, load class and obstructions |
| overhead clearance | 41 (phrase) | the least distance between the traveled way and an overhead obstruction |
| class | 194 | load class number (for example "class 50") or vehicle class |
| width | 195 | width of the traveled way or of a vehicle |
| serial number | 58 (phrase) | the number of a bridge, tunnel or other site in the report forms. Not a convoy serial |
| tunnel | (see section 4) | tunnel and underpass reports, TUNNELREP |
| water | 171 | water sources and water obstacles (fords, ferries) |
| trailer, truck, vehicle | 135, 210, 430 | vehicle types in the appendix tables |

**Abbreviations used in the vehicle tables (Appendix D), read in context:**

| Abbreviation | Reading in context | Example |
|---|---|---|
| mdl | model | "Euclid mdl 5FD" |
| mtd | mounted | "trlr mtd" (trailer mounted) |
| whl | wheel(s) | "4 whl" (four wheels) |
| trlr | trailer | "Trailer, low bed" |
| trk | truck | "trk mtd" |
| cu yd | cubic yards | "13 cu yd" (dump capacity) |
| T | short tons (weight class) | "7 T, 4 whl" |

These are read from context, not from a glossary. Check them before adding them to LEXICON as final.

## 2. Recurring terms, FM 55-30 (STANAG_2174)

| Term | Count | Meaning in this manual |
|---|---|---|
| convoy | 488 (words) | a column of six or more vehicles that moves together (§5-3) |
| movement | 553 | the movement of units and convoys (movement control, movement credit) |
| vehicles | 600 | vehicles in convoys, transport and maintenance |
| march unit | 61 (phrase) | the smallest element of a convoy (§5-3). Also "march units" (31) |
| march | 154 | march rate, march unit, march plan |
| movement credit | 47 (phrase) | permission to use a route at a given time (§5-2) |
| movement bid | 38 (phrase) | the request for movement credit (§5-4) |
| movement control | 71 (phrase) | the organisation that plans movements |
| halt | 129 | a stop. "rest halt" appears 23 times |
| column | 165 | a formation of vehicles on a road (close, open or infiltration) |
| serial | 58 | a group of march units in a convoy (§5-3) |
| convoy commander | 82 (phrase) | the person who commands the convoy |
| road movement | 65 (phrase) | movement by road |
| highway regulation | 31 (phrase) | the rules for using a highway |
| MLC | 9 | used in the convoy checklist: "HEAVIEST VEH/WT/MLC" and "weight and MLC" (military load class). **Not defined in the glossary** |
| axle load, weight | 34, 150 | weight of the vehicle, per axle and in total |

## 3. Abbreviations from the FM 55-30 glossary (pages 312 to 314, the glossary's own text)

| Abbreviation | Meaning (glossary) | Notes |
|---|---|---|
| ASR | alternate supply route | route status (APP-06 code 330400) |
| MSR | main supply route | route status (APP-06 code 330300) |
| CP | check point / command post / control point / critical point | **ambiguous**: use the meaning in context |
| RP | release point | end of a convoy's movement |
| SP | start point | start of a convoy's movement, not "self-propelled" |
| RSOP | reconnaissance, selection, occupation party | prepares halt areas |
| OIC / NCOIC | officer / noncommissioned officer in charge | |
| SOP | standing operating procedure | |
| CMO | convoy movement order | |
| CCN | convoy clearance number | |
| ITO | installation transportation office(r) | |
| DMC | defense movements coordinator; division movement control | |
| DTO | division transportation office/officer | |
| MC | movement control | |
| MOBCON | mobilization control | |
| SMCC | state movement control center | |
| TCMD | transportation control and movement document | |
| GVW | gross vehicle weight | |
| HET | heavy equipment transporter | a vehicle type |
| POL | petroleum, oils, and lubricants | |
| C2 | command and control | |
| CSS | combat service support | |
| COMMZ | communications zone | |
| EAC | echelon above corps | |
| CONUS | continental United States | US-specific |
| PLL | prescribed load list | |
| CUCV | commercial utility cargo vehicle | a vehicle type |

## 4. Other terms from FM 5-36 (chapters 2 and 3), with the section

| Term | Section | Meaning |
|---|---|---|
| OB | §2-8, §2-9 | obstruction, written in brackets after the route formula |
| route formula | §2-9 | e.g. `7m Y 50 (OB)`: 7 m wide, all-weather with limited traffic, class 50, with obstructions |
| route type X, Y, Z | §2-6 | X all-weather; Y all-weather with limited traffic; Z fair-weather |
| single flow / double flow | §2-5 | one column with passing vehicles / two columns |
| gradient, percent of slope | §2-15 to §2-17 | 7 percent or more is an obstruction |
| radius of curvature | §2-18 to §2-20 | under 30 m is an obstruction |
| TUNNELREP | §2-23, §2-28 | message format for a tunnel report (confirmed in the text) |
| DA Form 1250 | §2-29 | tunnel reconnaissance report form |
| reconnaissance overlay | §2-11 | the map overlay with the route symbols |

## 5. Suggested LEXICON entries (regex form, as in graph.py)

Draft only. Check against the real documents and the teammate's conventions before use.

```python
LEXICON_ADDITIONS = {
    "military load class": [r"military load class(?:es)?", r"\bMLC\b", r"load class(?:es)?"],
    "traveled way": [r"traveled way", r"traveled-way", r"carriageway"],
    "overhead clearance": [r"overhead clearances?", r"headroom", r"underpass"],
    "route classification": [r"route classif\w*", r"route formula"],
    "movement credit": [r"movement credits?"],
    "march unit": [r"march units?", r"march elements?"],
    "convoy halt": [r"rest halts?", r"\bhalts?\b"],
    "march table": [r"march tables?"],
    "column formation": [r"close column", r"open column", r"infiltration"],
    "serial": [r"serials?"],
    "convoy commander": [r"convoy commanders?"],
}
```

Notes: "halt" alone is too broad (it also means "to a halt" in FM 5-36), so keep "rest halt" as the specific term. "serial number" in FM 5-36 is not the convoy serial, so the pattern "serial" alone can give false matches in FM 5-36.
