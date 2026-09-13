"""default / latex 两种产物的并排页面渲染（目视验收辅助）。

用法：
    python3 experiments/render_compare.py <pdf_a> <pdf_b> --pages 1,4,17 -o <outdir>
        [--labels default,latex] [--dpi 150] [--clip x0,y0,x1,y1] [--gap 12]

对每个指定页（1-based）分别渲染 A、B 两份 PDF 的同一页，横向并排保存为
``<outdir>/page-<n>.png``；``--clip`` 给出 PDF 点坐标矩形时只渲染该区域
（用于放大核对首行缩进、公式基线、行尾对齐）。PNG 不提交仓库。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf

#: 标签条高度（pt）与左右留白（pt）。
_LABEL_HEIGHT = 16.0
_PADDING = 4.0


def _parse_pages(spec: str, page_count: int) -> list[int]:
    """解析 ``1,4,17`` / ``all`` / ``1-5`` 形式的页号（返回 1-based 页号）。"""
    if spec.strip().lower() == "all":
        return list(range(1, page_count + 1))
    pages: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, end = chunk.split("-", 1)
            pages.extend(range(int(start), int(end) + 1))
        else:
            pages.append(int(chunk))
    return pages


def _parse_clip(spec: str | None) -> pymupdf.Rect | None:
    if not spec:
        return None
    values = [float(part) for part in spec.split(",")]
    if len(values) != 4:
        raise ValueError("--clip 需要 4 个逗号分隔的数值：x0,y0,x1,y1")
    return pymupdf.Rect(*values)


def render_side_by_side(
    pdf_a: Path,
    pdf_b: Path,
    page_number: int,
    out_path: Path,
    *,
    label_a: str,
    label_b: str,
    dpi: int,
    clip: pymupdf.Rect | None,
) -> tuple[int, int]:
    """渲染同一页的两份 PDF 并并排保存 PNG，返回图像像素尺寸。"""
    with pymupdf.open(pdf_a) as doc_a, pymupdf.open(pdf_b) as doc_b:
        pix_a = doc_a[page_number - 1].get_pixmap(dpi=dpi, clip=clip, alpha=False)
        pix_b = doc_b[page_number - 1].get_pixmap(dpi=dpi, clip=clip, alpha=False)

        scale = 72.0 / dpi  # 像素 → pt
        width_a, height_a = pix_a.width * scale, pix_a.height * scale
        width_b, height_b = pix_b.width * scale, pix_b.height * scale
        body_height = max(height_a, height_b)
        total_width = _PADDING * 3 + width_a + width_b
        total_height = _LABEL_HEIGHT + _PADDING + body_height

        out = pymupdf.open()
        page = out.new_page(width=total_width, height=total_height)
        page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
        page.insert_text(
            (_PADDING, _LABEL_HEIGHT - 4),
            f"{label_a}  (p{page_number})",
            fontsize=8,
            color=(0, 0, 0),
        )
        page.insert_text(
            (_PADDING * 2 + width_a, _LABEL_HEIGHT - 4),
            f"{label_b}  (p{page_number})",
            fontsize=8,
            color=(0, 0, 0.6),
        )
        top = _LABEL_HEIGHT + _PADDING
        page.insert_image(
            pymupdf.Rect(_PADDING, top, _PADDING + width_a, top + height_a),
            pixmap=pix_a,
        )
        page.insert_image(
            pymupdf.Rect(
                _PADDING * 2 + width_a,
                top,
                _PADDING * 2 + width_a + width_b,
                top + height_b,
            ),
            pixmap=pix_b,
        )
        pixels = page.get_pixmap(dpi=dpi, alpha=False)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pixels.save(out_path)
        size = (pixels.width, pixels.height)
        out.close()
    return size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf_a", type=Path, help="对照 PDF（如 default 产物）")
    parser.add_argument("pdf_b", type=Path, help="对照 PDF（如 latex 产物）")
    parser.add_argument("--pages", default="1", help="页号（1-based），如 1,4,17 或 all")
    parser.add_argument("-o", "--out-dir", type=Path, default=None, help="PNG 输出目录（默认 ./renders）")
    parser.add_argument("--labels", default="default,latex", help="两列标签，逗号分隔")
    parser.add_argument("--dpi", type=int, default=150, help="渲染分辨率（整数，默认 150）")
    parser.add_argument("--clip", default=None, help="只渲染该区域：x0,y0,x1,y1（PDF 点）")
    args = parser.parse_args()

    out_dir = args.out_dir or Path("renders")

    for path in (args.pdf_a, args.pdf_b):
        if not path.is_file():
            parser.error(f"PDF 不存在: {path}")

    labels = [part.strip() for part in args.labels.split(",")]
    if len(labels) != 2:
        parser.error("--labels 需要恰好两个标签：default,latex")
    clip = _parse_clip(args.clip)

    with pymupdf.open(args.pdf_a) as doc_a, pymupdf.open(args.pdf_b) as doc_b:
        page_count = min(len(doc_a), len(doc_b))
        if len(doc_a) != len(doc_b):
            print(f"警告：页数不一致（{len(doc_a)} vs {len(doc_b)}），只比较前 {page_count} 页")
    pages = _parse_pages(args.pages, page_count)
    if not pages:
        parser.error("--pages 未解析出任何页号")

    suffix = "-clip" if clip else ""
    for page_number in pages:
        if page_number < 1 or page_number > page_count:
            print(f"跳过越界页 {page_number}（共 {page_count} 页）")
            continue
        out_path = out_dir / f"page-{page_number:03d}{suffix}.png"
        size = render_side_by_side(
            args.pdf_a,
            args.pdf_b,
            page_number,
            out_path,
            label_a=labels[0],
            label_b=labels[1],
            dpi=args.dpi,
            clip=clip,
        )
        print(f"{out_path}  {size[0]}×{size[1]}px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
