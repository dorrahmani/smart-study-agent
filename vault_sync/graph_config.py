"""
vault_sync/graph_config.py — Generate .obsidian/graph.json with course-aware
color groups for the Smart Study Agent vault.

Obsidian's graph view supports `colorGroups`: each entry has a tag/path query
and a color. We emit:
  - one color per course (matched by `tag:#{course_key}` OR by folder path)
  - a distinct color for concepts, announcements, assignments, NotebookLM
  - a "weakness" group highlighted in oxidized red (matches `tag:#weakness`)

Idempotent. Re-running overwrites the file with the canonical config.
"""

from __future__ import annotations

import json
from pathlib import Path

from config.paths import VAULT_ROOT, COURSE_ORDER

# Quiet Cartography palette — restrained, ink-on-paper, single accent in red.
# Obsidian wants {"a": alpha-int, "rgb": packed-int} where rgb = R<<16|G<<8|B.
def _rgb(r: int, g: int, b: int, a: float = 1.0) -> dict:
    return {"a": a, "rgb": (r << 16) | (g << 8) | b}


# Five courses — distinct but harmonious tones.
COURSE_COLORS = {
    'calculus_2':       _rgb(178,  98,  72),   # terracotta
    'linear_algebra_2': _rgb( 80, 116, 138),   # slate blue
    'discrete_math_2':  _rgb(120, 130,  76),   # olive
    'microeconomics':   _rgb(176, 142,  72),   # ochre
    'intro_to_cs':      _rgb( 92,  92, 108),   # graphite
}

# Type colors — quieter than course colors so courses dominate.
TYPE_COLORS = {
    'concept':      _rgb(180, 170, 140),   # parchment
    'announcement': _rgb(140, 110,  88),   # sepia
    'assignment':   _rgb( 96,  88,  78),   # bistre
    'notebooklm':   _rgb(150, 130, 160),   # muted lavender
}

WEAKNESS_COLOR = _rgb(196, 58, 46)   # the ONE accent — oxidized red, brighter for graph pop

GRAPH_OUT = VAULT_ROOT / "Smart Study Agent" / ".obsidian" / "graph.json"


def build_graph_config() -> dict:
    color_groups = []

    # 1) Weakness — top priority so it overrides course color when present
    color_groups.append({
        "query": "tag:#weakness",
        "color": WEAKNESS_COLOR,
    })

    # 2) Per-course groups — match by tag OR by course_key in path
    for ck in COURSE_ORDER:
        color_groups.append({
            "query": f"tag:#{ck}",
            "color": COURSE_COLORS[ck],
        })

    # 3) Type groups — concept hubs, announcements, assignments, NotebookLM
    color_groups.append({"query": "path:מושגים",      "color": TYPE_COLORS['concept']})
    color_groups.append({"query": "path:📢 הודעות",   "color": TYPE_COLORS['announcement']})
    color_groups.append({"query": "path:📋 מטלות",    "color": TYPE_COLORS['assignment']})
    color_groups.append({"query": "path:🎓 NotebookLM", "color": TYPE_COLORS['notebooklm']})

    return {
        "collapse-filter": True,
        "search": "",
        "showTags": False,
        "showAttachments": False,
        "hideUnresolved": False,
        "showOrphans": True,
        "collapse-color-groups": False,
        "colorGroups": color_groups,
        "collapse-display": False,
        "showArrow": False,
        "textFadeMultiplier": 0.4,
        "nodeSizeMultiplier": 1.0,
        "lineSizeMultiplier": 0.6,
        "collapse-forces": False,
        "centerStrength": 0.5,
        "repelStrength": 18,
        "linkStrength": 0.9,
        "linkDistance": 280,
        "scale": 1,
        "close": True,
    }


# ─── Filter presets (saved via the "saved filters" plugin convention).
# Obsidian core graph doesn't persist named filters, but we can dump a
# companion file the user can paste into the search box.
PRESETS = {
    "all": "",
    "weaknesses_only": "tag:#weakness",
    "concepts_hub":    "path:מושגים",
    "this_week":       "[date:>now-7d]",
    "active_courses":  " OR ".join(f"tag:#{ck}" for ck in COURSE_ORDER),
}


def write() -> Path:
    GRAPH_OUT.parent.mkdir(parents=True, exist_ok=True)
    cfg = build_graph_config()
    GRAPH_OUT.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    presets_path = GRAPH_OUT.parent / "graph-presets.md"
    lines = [
        "# Graph view presets",
        "",
        "Paste any of these into the search box at the top of the graph view:",
        "",
    ]
    for name, q in PRESETS.items():
        lines.append(f"- **{name}** — `{q or '(all)'}`")
    presets_path.write_text("\n".join(lines) + "\n", encoding='utf-8')

    return GRAPH_OUT


if __name__ == '__main__':
    out = write()
    print(f"wrote {out}")
