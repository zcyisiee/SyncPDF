from __future__ import annotations

import argparse
import time
from pathlib import Path

import pymupdf


def add_tiled_watermark(input_file: Path, output_file: Path, asset_file: Path) -> None:
    start = time.monotonic()
    source_doc = pymupdf.open(input_file)
    watermark_doc = pymupdf.open(asset_file)
    try:
        watermark_page = watermark_doc[0]
        watermark_width = watermark_page.rect.width
        watermark_height = watermark_page.rect.height
        page_sizes = _collect_page_sizes(source_doc)
        templates = {
            size: _create_tiled_template(
                size[0],
                size[1],
                watermark_doc,
                watermark_width,
                watermark_height,
            )
            for size in page_sizes
        }
        try:
            for size, page_numbers in page_sizes.items():
                template_doc = templates[size]
                for page_number in page_numbers:
                    page = source_doc[page_number]
                    page.show_pdf_page(page.rect, template_doc, 0, overlay=True)
            source_doc.save(output_file)
        finally:
            for template_doc in templates.values():
                template_doc.close()
    finally:
        watermark_doc.close()
        source_doc.close()
    _ = time.monotonic() - start


def add_corner_watermark(
    input_file: Path,
    output_file: Path,
    black_asset_file: Path,
    white_asset_file: Path,
) -> None:
    source_doc = pymupdf.open(input_file)
    black_doc = pymupdf.open(black_asset_file)
    white_doc = pymupdf.open(white_asset_file)
    try:
        if len(black_doc) == 0 or len(white_doc) == 0:
            raise ValueError("watermark asset must contain at least one page")
        for page in source_doc:
            _show_corner_asset(page, black_doc, "right")
            _show_corner_asset(page, white_doc, "left")
        source_doc.save(output_file)
    finally:
        black_doc.close()
        white_doc.close()
        source_doc.close()


def _collect_page_sizes(doc) -> dict[tuple[float, float], list[int]]:
    page_sizes: dict[tuple[float, float], list[int]] = {}
    for page_number in range(len(doc)):
        page = doc[page_number]
        size_key = (page.rect.width, page.rect.height)
        page_sizes.setdefault(size_key, []).append(page_number)
    return page_sizes


def _create_tiled_template(
    page_width: float,
    page_height: float,
    watermark_doc,
    watermark_width: float,
    watermark_height: float,
):
    template_doc = pymupdf.open()
    template_page = template_doc.new_page(width=page_width, height=page_height)
    spacing_x, spacing_y = _calculate_grid_spacing(
        page_width,
        page_height,
        watermark_width,
        watermark_height,
    )
    pos_x = -(watermark_width * 0.5)
    while pos_x <= page_width:
        pos_y = -(watermark_height * 0.5)
        while pos_y <= page_height:
            watermark_rect = pymupdf.Rect(
                pos_x,
                pos_y,
                pos_x + watermark_width,
                pos_y + watermark_height,
            )
            template_page.show_pdf_page(watermark_rect, watermark_doc, 0, overlay=True)
            pos_y += spacing_y
        pos_x += spacing_x
    return template_doc


def _calculate_grid_spacing(
    page_width: float,
    page_height: float,
    watermark_width: float,
    watermark_height: float,
) -> tuple[float, float]:
    spacing_x = watermark_width * 1.5
    for spacing_scale in [3, 2.5, 2, 1.5]:
        candidate = watermark_width * spacing_scale
        if int(page_width / candidate) >= 3:
            spacing_x = candidate
            break
    watermarks_per_width = int(page_width / spacing_x)
    adjusted_spacing_x = page_width / watermarks_per_width

    spacing_y = watermark_height * 1.5
    for spacing_scale in [3, 2.5, 2, 1.5]:
        candidate = watermark_height * spacing_scale
        if int(page_height / candidate) >= 3:
            spacing_y = candidate
            break
    watermarks_per_height = int(page_height / spacing_y)
    adjusted_spacing_y = page_height / watermarks_per_height
    return adjusted_spacing_x, adjusted_spacing_y


def _show_corner_asset(page, asset_doc, side: str) -> None:
    asset_page = asset_doc[0]
    page_width = page.rect.width
    page_height = page.rect.height
    margin = min(page_width, page_height) * 0.005
    target_height = min(page_height * 0.0075, asset_page.rect.height)
    target_width = target_height * (asset_page.rect.width / asset_page.rect.height)
    if side == "right":
        x0 = page_width - margin - target_width
    else:
        x0 = margin
    y0 = page_height - margin - target_height
    rect = pymupdf.Rect(x0, y0, x0 + target_width, y0 + target_height)
    page.show_pdf_page(rect, asset_doc, 0, overlay=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one PDF watermark transform.")
    parser.add_argument("operation", choices=["watermark1", "watermark2"])
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--asset", action="append", required=True)
    args = parser.parse_args()

    input_file = Path(args.input)
    output_file = Path(args.output)
    if args.operation == "watermark1":
        if len(args.asset) != 1:
            raise ValueError("watermark1 requires exactly one --asset")
        add_tiled_watermark(input_file, output_file, Path(args.asset[0]))
    else:
        if len(args.asset) != 2:
            raise ValueError("watermark2 requires exactly two --asset values")
        add_corner_watermark(
            input_file,
            output_file,
            Path(args.asset[0]),
            Path(args.asset[1]),
        )


if __name__ == "__main__":
    main()
