#!/bin/sh
# 从 stdin 读提示词，调 pi，把译文写到 stdout。
# 用法：bdt translate --workdir W --translator "scripts/pi-translator.sh"
# 模型与档位通过环境变量覆盖：PI_MODEL（默认 deepseek/deepseek-v4-pro:low）。
# pi 的 stdout 是 JSONL 事件流，取 turn_end 事件的 assistant 文本拼接为译文。
pi -p --no-session --no-tools --mode json \
   --model "${PI_MODEL:-deepseek/deepseek-v4-pro:low}" \
  | python3 -c '
import json
import sys

texts = []
for line in sys.stdin:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
    if event.get("type") != "turn_end":
        continue
    message = event.get("message") or {}
    for part in message.get("content") or []:
        if part.get("type") == "text" and part.get("text"):
            texts.append(part["text"])
print("".join(texts))
'
