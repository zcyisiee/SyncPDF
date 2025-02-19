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
