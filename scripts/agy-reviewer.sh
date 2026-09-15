#!/bin/sh
# 从 stdin 读审查提示词，调 agy，把结构化审查结果写到 stdout。
# 用法：bdt run --workdir W --from check --reviewer "scripts/agy-reviewer.sh"
# 协议：agy 的 JSON 输出里 response 字段本身是 JSON 文本
#       （{"verdict": ..., "findings": [...]}），解出后原样写到 stdout。
# 模型与档位通过环境变量覆盖：AGY_MODEL / AGY_EFFORT
# AGY_EFFORT 为空 / none / default / auto 时省略 --effort（claude 系模型不支持）。
prompt=$(cat)
if [ -n "${AGY_EFFORT:-}" ] && [ "${AGY_EFFORT}" != "none" ] \
   && [ "${AGY_EFFORT}" != "default" ] && [ "${AGY_EFFORT}" != "auto" ]; then
  agy --model "${AGY_MODEL:-gemini-3.8-flash-low}" --effort "${AGY_EFFORT}" \
      --disable-slash-commands --print-timeout 30m --output-format json --print "$prompt" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("response",""))'
else
  agy --model "${AGY_MODEL:-gemini-3.8-flash-low}" \
      --disable-slash-commands --print-timeout 30m --output-format json --print "$prompt" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("response",""))'
fi
