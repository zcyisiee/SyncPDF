"""PP-DocLayoutV3 vocabulary and its explicit downstream semantic roles."""

PADDLE_LABELS = (
    "abstract",
    "algorithm",
    "aside_text",
    "chart",
    "content",
    "display_formula",
    "doc_title",
    "figure_title",
    "footer",
    "footer_image",
    "footnote",
    "formula_number",
    "header",
    "header_image",
    "image",
    "inline_formula",
    "number",
    "paragraph_title",
    "reference",
    "reference_content",
    "seal",
    "table",
    "text",
    "vertical_text",
    "vision_footnote",
)

# Preserve native text even in protected regions: a protected label must remain
# consumable by ParagraphFinder. Figures/tables/formulas retain their object path.
PADDLE_TO_LAYOUT = {
    "abstract": "abstract",
    "algorithm": "code",
    "aside_text": "aside_text",
    "chart": "figure",
    "content": "content",
    "display_formula": "isolate_formula",
    "doc_title": "doc_title",
    "figure_title": "figure_caption",
    "footer": "footer",
    "footer_image": "figure",
    "footnote": "page_footnote",
    "formula_number": "page_number",
    "header": "header",
    "header_image": "figure",
    "image": "figure",
    "inline_formula": "formula",
    "number": "page_number",
    "paragraph_title": "paragraph_title",
    "reference": "reference",
    "reference_content": "reference",
    "seal": "seal",
    "table": "table",
    "text": "plain text",
    "vertical_text": "plain text",
    "vision_footnote": "figure_caption",
}

PADDLE_ROLES = {
    label: (
        "protected"
        if label
        in {
            "algorithm",
            "aside_text",
            "chart",
            "display_formula",
            "footer",
            "footer_image",
            "footnote",
            "formula_number",
            "header",
            "header_image",
            "image",
            "inline_formula",
            "number",
            "reference",
            "reference_content",
            "seal",
            "table",
        }
        else "translate"
    )
    for label in PADDLE_LABELS
}
