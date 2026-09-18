#!/bin/sh
# W11 e2e 用的 stub translator（**完全离线**）：读干重译提示词（stdin），把每条
# `<!-- id=... -->` 原样回显一遍，正文换成 $1（profile 的命令字符串里带的"候选正文"）。
# 不联网、不调模型、不写盘（真写盘的是服务端的隔离副本，用完即删）。
set -eu
text="$1"
cat | awk -v text="$text" '/^<!-- id=/ { print; print text; }'
