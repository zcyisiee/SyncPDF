#!/bin/sh
# W13 e2e 用的 stub translator（**完全离线**）：读干提示词（stdin，含服务端注入的术语约束段），
# 把 $1 指向的原文当译文吐回去。不联网、不调模型。
#
# 用途：验证「服务端把词表注入翻译提示词」这条链路 —— 提示词会落在 workdir 的
# agent/prompt.md（可经 GET /artifacts/agent/prompt.md 取回），e2e 断言它含「术语约束」段。
cat > /dev/null
cat "$1"
