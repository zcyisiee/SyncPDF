#!/bin/sh
# 从 stdin 读审查提示词，调 agy，把结构化审查结果写到 stdout。
# 用法：bdt run --workdir W --from check --reviewer "scripts/agy-reviewer.sh"
# 协议：agy 的 JSON 输出里 response 字段本身是 JSON 文本
#       （{"verdict": ..., "findings": [...]}），解出后原样写到 stdout。
# 模型与档位通过环境变量覆盖：AGY_MODEL / AGY_EFFORT
# AGY_EFFORT 为空 / none / default / auto 时省略 --effort（claude 系模型不支持）。
#
# `--dangerously-skip-permissions` 是必需的：审查提示词要求 reviewer 亲自跑
# `python -c "import pymupdf; ..."` 复核文本层、跑 `uv run bdt ...` 取产物，而 agy
# 无头模式无法就地询问授权，会静默拒绝该工具调用并产出空响应，bdt 侧表现为
# reviewer_empty / reviewer_invalid_json。代价要认清：本次会话内所有工具调用都被
# 自动批准，而审查输入含论文正文，存在"提示词注入 → 命令执行"的风险。要收紧就在
# ~/.gemini/antigravity-cli/settings.json 里用 permissions.allow 只放行具体命令。
prompt=$(cat)

# 解 agy 的 JSON 信封，并把 response 归一成"恰好一个 JSON 对象"：
# - agy 偶尔把最终答案重复输出（agentic 循环里后台任务结束后又输出一次），
#   或前后带围栏/解释；bdt 的解析器要求整段 stdout 是一个 JSON 对象，多一份就报
#   "Extra data"。这里取首个可解析的 JSON 对象，保证契约成立。
# - 拿不到结果时把 agy 自己的错误写到 stderr 并以非零码退出——空 stdout 会把真实
#   原因（授权被拒 / 地区限制 / 登录失效）掩盖成一句"stdout 为空"，难以定位。
emit='
import json
import sys

raw = sys.stdin.read()
try:
    envelope = json.loads(raw)
except Exception as exc:
    sys.stderr.write(f"agy 输出不是合法 JSON: {exc}\n")
    raise SystemExit(1)

text = (envelope.get("response") or "")
if not text.strip():
    status = envelope.get("status") or "?"
    error = envelope.get("error") or "（agy 未给出 error 字段）"
    sys.stderr.write(f"agy 未产出审查结果: status={status} error={error}\n")
    raise SystemExit(1)

decoder = json.JSONDecoder()
payload = None
for index, char in enumerate(text):
    if char != "{":
        continue
    try:
        candidate, _end = decoder.raw_decode(text, index)
    except ValueError:
        continue
    if isinstance(candidate, dict):
        payload = candidate
        break

if payload is None:
    sys.stderr.write("agy 的 response 里找不到 JSON 对象\n")
    raise SystemExit(1)

sys.stdout.write(json.dumps(payload, ensure_ascii=False))
'

if [ -n "${AGY_EFFORT:-}" ] && [ "${AGY_EFFORT}" != "none" ] \
   && [ "${AGY_EFFORT}" != "default" ] && [ "${AGY_EFFORT}" != "auto" ]; then
  agy --model "${AGY_MODEL:-gemini-3.8-flash-low}" --effort "${AGY_EFFORT}" \
      --disable-slash-commands --print-timeout 30m --output-format json \
      --dangerously-skip-permissions --print "$prompt" \
    | python3 -c "$emit"
else
  agy --model "${AGY_MODEL:-gemini-3.8-flash-low}" \
      --disable-slash-commands --print-timeout 30m --output-format json \
      --dangerously-skip-permissions --print "$prompt" \
    | python3 -c "$emit"
fi
