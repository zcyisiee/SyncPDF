#!/bin/sh
# 从 stdin 读提示词，调 agy，把译文写到 stdout。
# 用法：bdt translate --workdir W --translator "scripts/agy-translator.sh"
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
