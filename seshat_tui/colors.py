"""colors.py — warm-amber visual language tokens.

Values are lifted verbatim from seshat_tui_reference.html's :root block
(the locked palette). Domain accents match the reference's per-domain
glyph colors (TOP() in the reference script).
"""

COLORS = {
    "bg":         "#0C0A07",
    "surface":    "#16120D",
    "surface_2":  "#1E1912",
    "surface_3":  "#292219",
    "edge":       "#33291B",
    "text":       "#EDE0C6",
    "text_2":     "#C3B492",
    "text_3":     "#9A8B6E",
    "decor":      "#5F5340",
    "amber":      "#E8AE52",
    "amber_hi":   "#F6C56E",
    "amber_dim":  "#A07E3E",
    "amber_deep": "#241708",
    "green":      "#74C767",
    "red":        "#DD6E5A",
    "blue":       "#6EA8C4",
    "purple":     "#B78FE0",
    "cyan":       "#63C6BE",
    "orange":     "#E8A052",
}

STATUS_GLYPHS = {
    "running":  "●",   # ●
    "stopped":  "○",   # ○
    "conflict": "✗",   # ✗
    "error":    "⚠",   # ⚠
    "degraded": "◐",   # ◐
}

STATUS_COLORS = {
    "running":  COLORS["green"],
    "stopped":  COLORS["text_3"],
    "conflict": COLORS["red"],
    "error":    COLORS["orange"],
    "degraded": COLORS["orange"],
}

# Per-domain accent color, used for the domain-strip glyph and badges.
# "projects" has no accent in the reference (neutral/amber-on-active only).
DOMAIN_ACCENTS = {
    "projects":    None,
    "agreements":  COLORS["green"],
    "receipts":    COLORS["purple"],
    "invariant":   COLORS["cyan"],
    "revocations": COLORS["red"],
    "vault":       COLORS["blue"],
}

# Locked glyph set (seshat_tui_CORRECTION_b_real_textual_reference.md §0) —
# every glyph here was checked against DejaVu Sans Mono and confirmed to
# render on common terminal fonts. This closes TUI-Q1: no font-detection,
# no fallback mechanism, no ~/.seshat/tui.json config. 𓋇/☰/⛓/⚿ and emoji
# are excluded because they box (render as tofu) on common terminal fonts.
EMBLEM = "❖"  # ❖

DOMAIN_GLYPHS = {
    "projects":    "◆",  # ◆
    "agreements":  "≡",  # ≡  (replaces ☰, which boxes)
    "receipts":    "◈",  # ◈
    "invariant":   "◇",  # ◇
    "revocations": "⊘",  # ⊘
    "vault":       "⌸",  # ⌸  (replaces ⚿, which boxes)
}
