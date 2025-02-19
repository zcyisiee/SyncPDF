"""Sphinx configuration."""

project = "BabelDOC"
author = "funstory.ai"
copyright = "2025, funstory.ai"
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx_click",
    "myst_parser",
]
autodoc_typehints = "description"
html_theme = "furo"

myst_enable_extensions = [
    "amsmath",
    "attrs_inline",
    "colon_fence",
    "deflist",
    "dollarmath",
    "fieldlist",
    "html_admonition",
    "html_image",
    "linkify",
    "replacements",
    "smartquotes",
    "strikethrough",
    "substitution",
    "tasklist",
    "admonition",
]

# Map GitHub-style alerts to Sphinx admonitions
myst_admonition_aliases = {
    "note": "note",
    "tip": "tip",
    "important": "important",
    "caution": "caution",
    "warning": "warning",
}
