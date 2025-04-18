# 从 BableDOC 的翻译结果（中间体）中识别被错判为公式的字体的非公式字体

import json

import babeldoc.tools.italic_assistance as italic_assistance
from babeldoc.document_il.midend.styles_and_formulas import StylesAndFormulas
from babeldoc.translation_config import TranslationConfig
from rich.console import Console
from rich.table import Table

console = Console()

json_path = italic_assistance.find_latest_il_json()
print(f"中间体: {json_path}")

fonts = []

# 读取中间体
with json_path.open(encoding="utf-8") as f:
    data = json.load(f)

for page_index, page in enumerate(data["page"]):
    for paragraph_index, paragraph_content in enumerate(page["pdf_paragraph"]):
        font_debug_id = paragraph_content["debug_id"]
        if font_debug_id:
            # 创建页面字体映射
            page_font_map = {}
            for font in page["pdf_font"]:
                if "font_id" in font and "name" in font:
                    page_font_map[font["font_id"]] = (font["font_id"], font["name"])

            # 提取段落中的字体
            name_list = []
            paragraph_fonts = italic_assistance.extract_fonts_from_paragraph(
                paragraph_content, page_font_map
            )
            for _font_id, font_name in paragraph_fonts:
                name_list.append(font_name)

            font_list = []
            for each in fonts:
                font_list.append(each[1])

            for each_name in name_list:
                if each_name not in font_list:
                    fonts.append(
                        (page_index, each_name, paragraph_index, font_debug_id)
                    )

# 初始化检查器


translation_config = TranslationConfig(
    *[None for _ in range(3)], lang_out="zh_cn", doc_layout_model=1
)
checker = StylesAndFormulas(translation_config)

# 创建表格
table = Table(title="字体识别结果")
table.add_column("页码", justify="center", style="cyan")
table.add_column("段落码", justify="center", style="cyan")
table.add_column("DEBUG_ID", justify="center", style="cyan")
table.add_column("字体名称", style="magenta")
table.add_column("识别结果", justify="center")

# 输出结果
for each_font in fonts:
    page_index, font_name, paragraph_index, font_debug_id = each_font

    if checker.is_formulas_font(font_name):
        table.add_row(
            str(page_index),
            str(paragraph_index),
            str(font_debug_id),
            font_name,
            "[bold red]公式字体[/bold red]",
        )
    # else:
    #     table.add_row(str(page_index), font_name, "[bold blue]非公式字体[/bold blue]")

# 输出表格
console.print(table)
