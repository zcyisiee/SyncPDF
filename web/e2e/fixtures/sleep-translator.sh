#!/bin/sh
# W08 e2e stub translator：读干提示词后长睡（不发任何网络请求）。
# 用法：serve 的 profile 指向它（绝对路径），job 从 translate 起跑 → 状态停在 running，
# 好让 e2e 断言"运行中 → 取消 → canceled"这条链路。
#
# 取消时它会被 SIGTERM 收掉（job 取消杀的是整个进程组），所以这里不需要自己退出。
cat > /dev/null
sleep 300
