#!/bin/sh
# W15 e2e 用的 stub translator（**完全离线**）：读干提示词 → 小睡 $1 秒 → 把每条形如
# `<!-- id=... label=... -->` 的段落标记原样回显、正文换成固定文本。
#
# 为什么要有 sleep：集成用例需要在**运行中**断言实时链路（事件流有条目、时间线 live 段、
# ActiveJobCard 的运行中状态、job_update 推送比 5s 兜底轮询快），所以这个 stub 必须让
# translate 阶段停留几秒。不联网、不调模型。
#
# 先把 stdin 读干再睡：父进程（`translate.py` 的 `subprocess.run(input=prompt)`）一次写完
# 提示词就等输出，管道容量足够；但读干更稳妥（提示词大时也不会让父进程卡在写）。
set -eu

seconds="${1:-8}"
input=$(cat)
sleep "$seconds"
printf '%s\n' "$input" | awk '/^<!--/ { print; print "W15 e2e 译文（stub）"; }'
