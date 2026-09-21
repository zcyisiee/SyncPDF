#!/bin/sh
# 把真实测试夹具复制到 engine/fixtures/。PDF 被根 .gitignore 忽略，不入库。
set -e
cd "$(dirname "$0")"
cp -f "$HOME/.sp/up-vns-20260921-022426/source.pdf" up-vns.pdf
cp -f "$HOME/.sp/up-trc-20260919-091110/source.pdf" up-trc.pdf
cp -f "$HOME/.sp/up-2602-02908v2-20260920-155426/source.pdf" up-2602.pdf
cp -f ../../examples/ci/test.pdf ci-test.pdf
ls -la *.pdf
