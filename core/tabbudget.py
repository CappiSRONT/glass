"""
Tab memory budget
=================
Pure decision logic for background-tab discarding, kept free of Qt so it can be
tested on its own. Given the live tabs (ordered least-recently-used first), the
current tab, and how many renderers we allow to stay alive, decide which tabs to
put to sleep. The current tab is never chosen.
"""

from __future__ import annotations


def victims_to_suspend(live_order, current, max_live):
    live = [t for t in live_order if t is not None]
    over = len(live) - max(1, int(max_live))
    if over <= 0:
        return []
    victims = []
    for t in live:                 # LRU first
        if over <= 0:
            break
        if t is current:
            continue
        victims.append(t)
        over -= 1
    return victims
