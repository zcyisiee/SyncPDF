# BabelDOC 中文增强版

本项目基于上游 [BabelDOC](https://github.com/funstory-ai/BabelDOC)，用于把英文论文翻译成中文 PDF，并生成原文与译文对照版。

## 相对上游的改动

### 超链接保留与对应

上游流程在译文长度变化、跨行或文本被改写后，可能把链接放到错误的文字上。本项目增加了以下处理：

- 解析阶段保存每个链接的源页、矩形、动作、目标页和覆盖字符。
- 重建阶段按优先级定位译文中的对应区域：LaTeX `bdoclink` 标记、存活字符、段落内文字锚点、段落比例映射。
- 链接矩形直接更新到现有 PDF 注释，保留 `/A`、`/Dest`、URI、内部跳转、远程文件动作和边框属性。
- 链接跨行时拆成多个注释，并保留同一个逻辑链接 ID。
- 更新矩形时清理过期 `QuadPoints`，避免点击区域仍停留在旧位置。
- mono 和 dual PDF 都会复制内部目标坐标；跨页目标按目标页尺寸映射。

### 链接审计

`babeldoc/tools/agent/link_audit.py` 会逐条比较源 PDF 和译文 PDF，检查：

- 链接数量和动作是否保留。
- 内部目标页和坐标是否一致。
- 引文、图、表、公式、脚注编号是否覆盖正确文字。
- 外部 URI 是否保留。外部网站只检查地址，不访问网站。
- 区分 `missing`、`wrong_label`、`wrong_role`、`source_invalid` 和 `external_unchecked`。

### Markdown 翻译管线

`skills/document-translate/` 提供可恢复的工具管线：

```text
parse_document → translate_document → apply_translation
→ review_document → reconstruct_pdf → audit_links → layout_lint → report
```

翻译输入使用带段落 ID、样式锚点和公式锚点的 Markdown。写回阶段会检查 ID、锚点顺序、公式占位符和段落完整性。默认翻译模型是 `agy`，可指定模型和 thinking 档位。

### 解析和排版检查

- 解析后检查版面字符覆盖率、目录条目、保护内容和行内公式匹配率。
- 参考文献、作者信息、页脚、图内文字和表格内部文字默认保留原文。
- 参考文献后的 `Appendix`，以及有明确字母编号序列的附录标题可以恢复翻译；孤立标题不会自动恢复。
- XeLaTeX bbox 排版默认开启，失败的段落自动回退到普通排版，并记录回退原因。
- `layout_lint` 检查字号收缩、文本层兼容字形、溢出和重叠。

## 安装

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 MinerU API token。

```bash
git clone https://github.com/zcyisiee/ieeTranslater.git
cd ieeTranslater
uv sync
export MINERU_API_TOKEN="你的 token"
```

翻译模型 CLI 需要单独安装并登录。使用 `agy` 时确认命令可用：

```bash
agy models
```

## 使用方法

### 直接使用上游命令行

适合普通翻译：

```bash
uv run babeldoc \
  --files paper.pdf \
  --openai \
  --openai-model gpt-4o-mini \
  --openai-base-url https://api.openai.com/v1 \
  --openai-api-key "$OPENAI_API_KEY"
```

### 使用增强管线

在仓库根目录执行。每个阶段的结果写入指定工作目录；建议把工作目录放在 `tmp/`。

```bash
WD=tmp/my-paper
PDF="/absolute/path/paper.pdf"

# 1. 解析
a=('{"pdf":"'"$PDF"'","workdir":"'"$WD"'","layout":"mineru"}')
python -m babeldoc_tools call parse_document --args-json "$a"

# 2. 使用 agy 翻译
a=('{"workdir":"'"$WD"'","model":"gemini-3.8-flash-low","effort":"low","timeout":3600}')
python -m babeldoc_tools call translate_document --args-json "$a"

# 3. 写回、审查、重建
python -m babeldoc_tools call apply_translation --args-json '{"workdir":"'"$WD"'"}'
python -m babeldoc_tools call review_document --args-json '{"workdir":"'"$WD"'","skip_pdf_checks":true}'
python -m babeldoc_tools call reconstruct_pdf --args-json '{"workdir":"'"$WD"'","dual":true,"latex_bbox":true,"stats":true}'

# 4. 检查排版并导出报告
python -m babeldoc_tools call layout_lint --args-json '{"workdir":"'"$WD"'"}'
python -m babeldoc_tools call report --args-json '{"workdir":"'"$WD"'"}'
```

链接审计：

```bash
python - <<'PY'
from babeldoc.tools.agent.link_audit import audit_links

audit_links(
    "原文.pdf",
    "tmp/my-paper/output/译文.no_watermark.zh.mono.pdf",
    report_path="tmp/my-paper/agent/link_audit.json",
)
PY
```

也可以使用仓库内的稳定工具入口：

```bash
skills/document-translate/tools/bin/bdt list
skills/document-translate/tools/bin/bdt call parse_document --workdir tmp/my-paper ...
```

### 常用参数

- `layout=mineru`：使用 MinerU 解析。没有 token 时，可传 `mineru_json` 回放本地布局缓存。
- `model`：翻译模型，例如 `gemini-3.8-flash-low`。
- `effort`：`low`、`medium` 或 `high`。
- `dual=true`：同时生成 mono 和 dual PDF。
- `latex_bbox=true`：启用 bbox 内 LaTeX 两端对齐排版。
- `pages`：只处理指定页，例如 `1,2,5-7`。

## 输出文件

工作目录结构通常如下：

```text
agent/
  document.md          # 带段落 ID 的原文 Markdown
  translated.md        # 模型返回的译文
  translated.jsonl     # 写回后的段落
  apply_report.json    # 锚点和 ID 检查结果
  review_verdict.json  # 结构审查结果
  link_audit.json      # 链接逐条审计结果
  layout_geometry.json # 排版几何信息
  layout_lint.json     # 排版检查结果
  usage.json           # 模型用量
  FINAL_REPORT.md      # 汇总报告
output/
  *.zh.mono.pdf
  *.zh.dual.pdf
```

## 测试

运行链接相关测试：

```bash
python -m pytest tests/test_link_remap.py tests/test_link_audit.py tests/test_link_correspondence.py -q
```

运行全部测试：

```bash
python -m pytest -q
```

测试过程中的 PDF、PNG、JSON 和模型日志统一放在 `tmp/`，该目录已加入 `.gitignore`。

## 已知限制

- 外部 URI 只验证注释动作和地址是否保留，不验证网站当前是否可访问。
- `wrong_label` 或 `wrong_role` 表示对应关系需要人工复核；链接数量一致不能证明语义对应正确。
- 译文明显变长时，自动排版可能缩小字号；请查看 `layout_lint.json` 和渲染图片。
- 表格内部文字、图内文字和参考文献条目默认不翻译。
