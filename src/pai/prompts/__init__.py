"""Prompt files, with the owner's details filled in from config ([profile], [leads]).

Rendered once per process from config, so each prompt stays byte-stable for caching.
"""

from __future__ import annotations

import re
from importlib import resources


def render(name: str, config) -> str:
    text = resources.files(__name__).joinpath(name).read_text().strip()
    p = config.profile
    about = f"About the owner: {p.about.strip()}" if p.about.strip() else ""
    for key, value in {
        "{about}": about,
        "{home}": p.home,
        "{school}": p.school,
        "{leads_for}": config.leads.pitch_for,
    }.items():
        text = text.replace(key, value)
    return re.sub(r"\n{3,}", "\n\n", text)
