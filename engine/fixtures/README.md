# 测试夹具

| 文件 | 来源 | 规模 |
|---|---|---|
| ci-test.pdf | examples/ci/test.pdf | 小 |
| up-vns.pdf | ~/.sp/up-vns-20260921-022426/source.pdf | 810KB，12 页，169 段 |
| up-trc.pdf | ~/.sp/up-trc-20260919-091110/source.pdf | 6.3MB，27 页 |
| up-2602.pdf | ~/.sp/up-2602-02908v2-20260920-155426/source.pdf | 18.7MB，58 页 |

PDF 不入库（根 `.gitignore` 的 `*.pdf`）。运行 `cargo xtask fixtures` 或 `sh fixtures/sync.sh` 复制。
测试通过 `syncpdf_core::fixtures::path("up-vns.pdf")` 定位；文件缺失时测试 skip 并打印原因。
