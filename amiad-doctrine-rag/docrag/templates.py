"""Rule templates (illustrative catalogue). Replace or extend to match the planner's real rule types.

Each template: label, accepted units, default bound, and whether a vehicle scope is normally needed.
bound: max (value must not be exceeded), min (value must be reached), exact, flag (no value).
"""
from __future__ import annotations

TEMPLATES: dict[str, dict] = {
    "SEG.SPEED": {"label": "Maximum speed", "units": {"km/h", "mph"}, "default_bound": "max", "needs_scope": True},
    "NET.GAP": {"label": "Gap between vehicles, serials or convoys", "units": {"m", "km", "min", "s"},
                "default_bound": "min", "needs_scope": False},
    "SEG.WIDTH": {"label": "Road width", "units": {"m", "ft"}, "default_bound": "min", "needs_scope": True},
    "SEG.HEIGHT": {"label": "Height or clearance", "units": {"m", "ft"}, "default_bound": "max", "needs_scope": True},
    "SEG.MLC": {"label": "Bridge load class (MLC)", "units": {"MLC"}, "default_bound": "min", "needs_scope": True},
    "SEG.WEIGHT": {"label": "Weight limit", "units": {"t"}, "default_bound": "max", "needs_scope": True},
    "SEG.GRADE": {"label": "Maximum gradient", "units": {"%"}, "default_bound": "max", "needs_scope": True},
    "SEG.TURN": {"label": "Minimum turn radius", "units": {"m"}, "default_bound": "min", "needs_scope": True},
    "SCH.HALT": {"label": "Halt interval or duration", "units": {"min", "h"}, "default_bound": "exact",
                 "needs_scope": False},
    "SEG.TOWN": {"label": "Town population threshold to avoid", "units": {"inhabitants"}, "default_bound": "max",
                 "needs_scope": False},
    "NET.DISTINCT": {"label": "Separation between itineraries", "units": {"km", "m"}, "default_bound": "min",
                     "needs_scope": False},
    "PRO.HAZMAT": {"label": "Hazardous cargo restriction", "units": set(), "default_bound": "flag",
                   "needs_scope": False},
    "PRO.ESCORT": {"label": "Escort required", "units": set(), "default_bound": "flag", "needs_scope": False},
    # Not a planner rule type. A statement that looks relevant but whose meaning is not known yet (docrag.extract). It is shown to
    # the reviewer and can NEVER be exported (contract.py refuses it); the planner never receives this template.
    "EVID.UNCLASSIFIED": {"label": "Unclassified evidence (meaning not established)", "units": set(), "default_bound": "flag",
                          "needs_scope": False},
}
NOT_FOR_PLANNER = frozenset({"EVID.UNCLASSIFIED"})

# Words that show which template a number belongs to (searched in the text just before the number).
KEYWORDS: dict[str, str] = {
    "SEG.TURN": r"turn(?:ing)? radius|radius|curvature|sharp turns?|hairpin",
    "SEG.HEIGHT": r"clearance|headroom|height|taller|higher|overhead",
    "SEG.WIDTH": r"width|\bwide\b|narrower|wider|narrow",
    "NET.DISTINCT": r"\bapart\b|separated|separation|disjoint|not intersect",
    "SCH.HALT": r"halts?|rest (?:stop|period)|\bbreaks?\b|\bstops?\b",
    "NET.GAP": r"\bgaps?\b|spacing|intervals?|headway|distance between|distance of|following distance",
    "SEG.GRADE": r"gradients?|slopes?|steeper|incline|\bgrade\b",
    "SEG.SPEED": r"speed|km/h|kph|mph|velocity",
    "SEG.TOWN": r"inhabitants|population|towns?|built-up",
    "SEG.WEIGHT": r"weight|heavier|axle",
}
# Priority when several keywords occur in the same window (first wins).
PRIORITY = ["SEG.TURN", "SEG.HEIGHT", "SEG.WIDTH", "NET.DISTINCT", "SCH.HALT", "NET.GAP", "SEG.GRADE",
            "SEG.SPEED", "SEG.TOWN", "SEG.WEIGHT"]

STRICTNESS_TIER = {"must": "T1", "should": "T2", "may": "T3", "none": "T3"}