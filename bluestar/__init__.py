"""BLUESTAR Macro Briefing Engine v8.1.

Institutional-grade engine that ingests market + calendar data, applies the
BLUESTAR v8.1 logic (regime, catalysts, central banks, positioning overlay,
asset selection, risk scenarios), validates the output, and renders the final
HTML briefing.

Design philosophy: "Le prix decide, le macro explique, le COT mesure le risque
de positionnement." The COT module never triggers a trade on its own; it only
adjusts conviction, squeeze risk and sizing. When data is missing the engine
degrades honestly to [N/A] / [PROXY] and never fabricates a figure.
"""

__version__ = "8.1.0"
