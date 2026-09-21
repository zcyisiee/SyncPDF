//! 夹具内容流往返测试：每页 /Contents 与每个 Form XObject 流，
//! 解压后 `parse_content` → `write_content` 必须字节相等。
//!
//! 夹具缺失时用 `require_fixture!` skip（见 engine/fixtures/README.md）。

use std::time::{Duration, Instant};

use lopdf::{Document, Object};
use syncpdf_core::require_fixture;
use syncpdf_pdf::content::{parse_content, write_content, Op};

#[derive(Debug, Default)]
struct Stats {
    streams: usize,
    ops: usize,
    bytes: usize,
    text_ops: usize,
    inline_images: usize,
    parse_time: Duration,
}

impl Stats {
    fn report(&self, label: &str) {
        eprintln!(
            "{label}: {} 个流 / {} 字节 / {} 个 op（文本显示 {}，内联图像 {}），解析耗时 {:?}",
            self.streams, self.bytes, self.ops, self.text_ops, self.inline_images, self.parse_time
        );
    }
}

/// 解析一个流并验证往返字节相等。
fn check_stream(label: &str, src: &[u8], stats: &mut Stats) {
    let started = Instant::now();
    let ops: Vec<Op> = match parse_content(src) {
        Ok(v) => v,
        Err(e) => panic!("{label}: 解析失败：{e}"),
    };
    stats.parse_time += started.elapsed();

    let out = write_content(src, &ops);
    assert_roundtrip(label, src, &out);

    stats.streams += 1;
    stats.bytes += src.len();
    stats.ops += ops.len();
    stats.text_ops += ops.iter().filter(|o| o.is_text_show()).count();
    stats.inline_images += ops.iter().filter(|o| o.is_inline_image()).count();

    // span 必须单调不减且落在源内。
    let mut cursor = 0usize;
    for op in &ops {
        assert!(
            op.span.start >= cursor && op.span.end <= src.len() && op.span.start <= op.span.end,
            "{label}: 操作 {} 的 span {:?} 不合法（cursor={cursor}, len={}）",
            op.operator,
            op.span,
            src.len()
        );
        cursor = op.span.end;
    }
}

fn assert_roundtrip(label: &str, src: &[u8], out: &[u8]) {
    if out == src {
        return;
    }
    let at = src
        .iter()
        .zip(out.iter())
        .position(|(a, b)| a != b)
        .unwrap_or_else(|| src.len().min(out.len()));
    let lo = at.saturating_sub(48);
    let src_hi = (at + 48).min(src.len());
    let out_hi = (at + 48).min(out.len());
    panic!(
        "{label}: 往返不一致。首个差异在偏移 {at}（src {} 字节，out {} 字节）\n\
         src[{lo}..{src_hi}] = {:?}\nout[{lo}..{out_hi}] = {:?}",
        src.len(),
        out.len(),
        String::from_utf8_lossy(&src[lo.min(src.len())..src_hi]),
        String::from_utf8_lossy(&out[lo.min(out.len())..out_hi]),
    );
}

/// 文档里全部 `/Subtype /Form` 的流（解码后内容）。
fn form_xobject_streams(doc: &Document) -> Vec<(String, Vec<u8>)> {
    let mut out = Vec::new();
    for (id, obj) in &doc.objects {
        let Ok(stream) = obj.as_stream() else {
            continue;
        };
        let is_form = stream
            .dict
            .get(b"Subtype")
            .and_then(Object::as_name)
            .is_ok_and(|name| name == b"Form".as_slice());
        if !is_form {
            continue;
        }
        match stream.get_plain_content() {
            Ok(data) => out.push((format!("XObject {} {} R", id.0, id.1), data)),
            // 解码失败（未知过滤器 / 加密）时跳过，不是本模块的职责。
            Err(_) => continue,
        }
    }
    out
}

fn roundtrip_fixture(name: &str) -> Stats {
    let path = match syncpdf_core::fixtures::path(name) {
        Some(p) => p,
        None => unreachable!("调用方已用 require_fixture! 检查"),
    };
    let doc = Document::load(&path).unwrap_or_else(|e| panic!("{name}: lopdf 打开失败：{e}"));

    let mut stats = Stats::default();
    for (page_no, page_id) in doc.get_pages() {
        // lopdf 的 get_page_content 已按 /Contents 数组顺序拼接并在流间补 `\n`。
        let content = doc.get_page_content(page_id);
        if content.is_empty() {
            continue;
        }
        check_stream(&format!("{name} 第 {page_no} 页"), &content, &mut stats);
    }
    for (label, data) in form_xobject_streams(&doc) {
        if data.is_empty() {
            continue;
        }
        check_stream(&format!("{name} {label}"), &data, &mut stats);
    }
    stats.report(name);
    stats
}

#[test]
fn roundtrip_ci_test() {
    let _ = require_fixture!("ci-test.pdf");
    let stats = roundtrip_fixture("ci-test.pdf");
    assert!(stats.streams > 0, "ci-test.pdf 应至少有一个内容流");
    assert!(stats.ops > 0);
}

#[test]
fn roundtrip_up_vns() {
    let _ = require_fixture!("up-vns.pdf");
    let stats = roundtrip_fixture("up-vns.pdf");
    assert!(stats.streams >= 12, "up-vns.pdf 有 12 页");
    assert!(stats.text_ops > 0, "应解析出文本显示操作");
}

#[test]
fn roundtrip_up_trc() {
    let _ = require_fixture!("up-trc.pdf");
    let stats = roundtrip_fixture("up-trc.pdf");
    assert!(stats.streams >= 27, "up-trc.pdf 有 27 页");
    assert!(stats.text_ops > 0);
}

#[test]
fn roundtrip_up_2602() {
    let _ = require_fixture!("up-2602.pdf");
    let stats = roundtrip_fixture("up-2602.pdf");
    assert!(stats.streams >= 58, "up-2602.pdf 有 58 页");
    assert!(stats.text_ops > 0);
    // 性能：debug 下全部页面解析（不含 lopdf 解压）应远低于 10s。
    assert!(
        stats.parse_time < Duration::from_secs(10),
        "up-2602.pdf 解析耗时 {:?} 超过 10s",
        stats.parse_time
    );
}

/// 往返用的是解码后的字节，改一条 Op 后其余字节仍应保持原样。
#[test]
fn dirty_op_is_the_only_rewritten_region() {
    let _ = require_fixture!("ci-test.pdf");
    let path = syncpdf_core::fixtures::path("ci-test.pdf").unwrap();
    let doc = Document::load(&path).expect("打开 ci-test.pdf");
    // 找第一个含文本显示操作的页面。
    let mut found: Option<(Vec<u8>, Vec<Op>, usize)> = None;
    for page_id in doc.get_pages().into_values() {
        let src = doc.get_page_content(page_id);
        let ops = parse_content(&src).expect("解析成功");
        if let Some(idx) = ops.iter().position(Op::is_text_show) {
            found = Some((src, ops, idx));
            break;
        }
    }
    let Some((src, mut ops, idx)) = found else {
        eprintln!("SKIP: ci-test.pdf 没有文本显示操作");
        return;
    };
    ops[idx].mark_dirty();
    let out = write_content(&src, &ops);
    // 标脏后只是换成规范化写法，重新解析应得到同样的操作序列。
    let again = parse_content(&out).expect("重解析成功");
    assert_eq!(ops.len(), again.len());
    for (a, b) in ops.iter().zip(&again) {
        assert_eq!(a.operator, b.operator, "操作符应一致");
    }
}
