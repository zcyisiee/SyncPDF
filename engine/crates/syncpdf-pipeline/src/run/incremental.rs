//! 单页重编：一次 run 结束后保留排版状态，之后的编辑只重编被编辑段所在的页。
//! 其余页的译文、版面与回写结果原样沿用：不重跑解析、版面分析、其它页的
//! 排版回写和全文校验，被编辑页的其它段走翻译缓存。
use super::*;
use lopdf::{Object, ObjectId};
use syncpdf_core::ir::Align;

/// 一次 run 结束时的排版状态。只对产生它的同一组 `configure` + `run` 有效。
#[allow(missing_debug_implementations)]
pub struct Retained {
    cfg: RunConfig,
    store: PathBuf,
    state: RunState,
    origin: descent::Origin,
    /// 本轮全部可译段（漏译兜底与翻译子集都按它）。
    translatable: Vec<ParagraphId>,
    selected: Vec<u32>,
    /// 源段落的对齐（撤销单块对齐覆盖时还原）。
    aligns: BTreeMap<ParagraphId, Align>,
    /// 由源文件决定、编辑改变不了的质量门禁（源区域冲突、覆盖缺口、模型身份错误）。
    blocked: bool,
}

impl Retained {
    #[allow(clippy::too_many_arguments)]
    pub(super) fn new(
        cfg: &RunConfig,
        store: PathBuf,
        state: RunState,
        origin: descent::Origin,
        translatable: Vec<ParagraphId>,
        selected: Vec<u32>,
        paras: &[Paragraph],
        blocked: bool,
    ) -> Self {
        Self {
            cfg: cfg.clone(),
            store,
            state,
            origin,
            translatable,
            selected,
            aligns: paras.iter().map(|p| (p.id.clone(), p.align)).collect(),
            blocked,
        }
    }

    /// `cfg` 与产生本状态的 run 相同，且 `edited` 全是本轮排过的可译段。
    pub fn covers(&self, cfg: &RunConfig, edited: &BTreeSet<ParagraphId>) -> bool {
        self.cfg.configure == cfg.configure
            && self.cfg.run == cfg.run
            && self.cfg.cache_only == cfg.cache_only
            && self.cfg.typography == cfg.typography
            && cfg.dual_output.is_none()
            && !edited.is_empty()
            && edited.iter().all(|id| self.translatable.contains(id))
    }

    /// 重编 `edited` 所在的页：还原这些页到源状态，按本篇库的最新编辑重新取译文
    /// （手改段不进模型，待重译段请求模型，其余走缓存），排版并只回写这些页。
    ///
    /// 出错时本状态已不一致，调用方必须丢弃它。取消照常以
    /// `run_finished{ok:false}` 收尾；其它错误不发事件，由调用方改走全篇 run。
    pub async fn recompile(
        &mut self,
        edited: &BTreeSet<ParagraphId>,
        sink: SharedSink,
        cancel: &CancellationToken,
    ) -> Result<RunSummary, PipelineError> {
        let started = Instant::now();
        let result = self
            .recompile_pages(edited, sink.clone(), cancel, started)
            .await;
        if let Err(PipelineError::Cancelled) = &result {
            fail(&mut sink.clone(), &PipelineError::Cancelled, started);
        }
        result
    }

    async fn recompile_pages(
        &mut self,
        edited: &BTreeSet<ParagraphId>,
        sink: SharedSink,
        cancel: &CancellationToken,
        started: Instant,
    ) -> Result<RunSummary, PipelineError> {
        let fields = self
            .cfg
            .run_fields()
            .ok_or_else(|| PipelineError::Protocol("缺少 run 请求".into()))?;
        let pages: BTreeSet<u32> = edited.iter().map(|id| id.page - 1).collect();
        let store = open_store(&self.store)?;
        let edits = load_block_edits(&store, fields.doc_id, &sink);
        let s = &mut self.state;
        s.styles = edits.styles;
        for (id, para) in s.pars.iter_mut() {
            if pages.contains(&(id.page - 1)) {
                para.align = match s.styles.get(id).and_then(|style| style.align) {
                    Some(align) => stages::typeset::align_of(align),
                    None => self.aligns.get(id).copied().unwrap_or(para.align),
                };
            }
        }
        for &page in &pages {
            sink.emit(Event::PageReopened { page: page + 1 });
            reopen(s, &self.origin, page)?;
        }

        let paras: Vec<Paragraph> = self
            .translatable
            .iter()
            .filter(|id| pages.contains(&(id.page - 1)))
            .map(|id| s.pars[id].clone())
            .collect();
        let kind = self
            .cfg
            .translator_kind()
            .cloned()
            .ok_or_else(|| PipelineError::Protocol("configure 里缺少 translator".into()))?;
        let translator = make_translator(&kind)?;
        let mut spec = syncpdf_translate::PromptSpec::new(fields.source_lang, fields.target_lang);
        if let Some(path) = fields.terminology {
            spec.terminology = load_terminology(path)?;
        }
        let mut cache = open_cache(self.cfg.cache_dir());
        if let Some(c) = cache.as_mut() {
            c.set_terminology(&spec.terminology);
        }
        let irs: Vec<PageIR> = pages.iter().map(|p| s.bound[p].ir.clone()).collect();
        let mut blocks = Vec::new();
        let translated = tokio::select! {
            biased;
            () = cancel_watch(cancel) => Err(PipelineError::Cancelled),
            r = stages::translate::translate_all_with_cache_policy(
                DynTranslator::new(translator),
                &spec,
                &paras,
                stages::glyph_text_lookup(&irs),
                cache.as_ref(),
                |block| blocks.push(block),
                self.cfg.cache_only,
                edits.overrides.clone(),
            ) => r,
        }?;
        for block in &translated.blocks {
            if edits.overrides.fresh.contains(&block.id)
                && block.status.is_ok()
                && !block.from_cache
            {
                if let Err(e) = store.clear_retranslate(fields.doc_id, &block.id) {
                    tracing::warn!(error = %e, para = %block.id, "清除重译标记失败");
                }
            }
        }
        sink.emit(Event::Issue {
            severity: Severity::Info,
            code: "translation_requests".into(),
            paragraph_id: None,
            page: None,
            message: format!(
                "单页重编（第 {} 页）：Markdown 主请求 {} 次，补救请求 {} 次，缓存命中 {} 段",
                pages
                    .iter()
                    .map(|p| (p + 1).to_string())
                    .collect::<Vec<_>>()
                    .join("、"),
                translated.stats.primary_prompts,
                translated.stats.retry_prompts,
                translated.stats.cache_hits
            ),
        });

        // 保留的块换成新译文：之后的重编与行距重放都用它。
        s.blocks.retain(|b| !pages.contains(&(b.id.page - 1)));
        s.blocks.extend(blocks.iter().cloned());
        let total = self.translatable.len() as u32;
        for block in blocks {
            check_cancelled(cancel)?;
            handle_block(s, &sink, block, total)?;
        }
        settle_rest(s, &self.translatable, &self.selected, &sink)?;

        let stats = s.stats();
        let ok = stats.fallbacks == 0 && !self.blocked;
        sink.emit(Event::DocumentFinished {
            output: fields.output.to_path_buf(),
            stats,
        });
        let elapsed_ms = started.elapsed().as_millis() as u64;
        sink.emit(Event::RunFinished { ok, elapsed_ms });
        Ok(RunSummary {
            ok,
            pages: pages.len() as u32,
            paragraphs: paras.len() as u32,
            elapsed_ms,
            stats,
        })
    }
}

/// 把一页的排版结果与主文档还原到源状态，等待重新落定。
fn reopen(s: &mut RunState, origin: &descent::Origin, page: u32) -> Result<(), PipelineError> {
    let ids: Vec<ParagraphId> = s
        .pars
        .keys()
        .filter(|id| id.page == page + 1)
        .cloned()
        .collect();
    for id in &ids {
        s.settled_ids.remove(id);
        s.echoed.remove(id);
        s.targets.remove(id);
        // 局部动态 bbox 改过的排版框回到源框。
        match origin.frames.get(id) {
            Some(frame) => s.frames.insert(id.clone(), frame.clone()),
            None => s.frames.remove(id),
        };
    }
    s.typeset_by_page.remove(&page);
    s.ready.retain(|p| *p != page);
    s.schedule.reopen(page + 1);
    revert_page(&mut s.doc, &origin.doc, page + 1)
        .map_err(|e| PipelineError::Protocol(format!("第 {} 页还原失败：{e}", page + 1)))
}

/// 页回写只新建对象、改页字典，唯独链接回写就地改注释字典：还原页字典和
/// 本页注释对象，补回先前清理掉的源对象，再丢掉旧回写留下的无引用对象
/// （否则每次重编都让输出变大）。
fn revert_page(
    doc: &mut lopdf::Document,
    origin: &lopdf::Document,
    page: u32,
) -> lopdf::Result<()> {
    let id = *origin
        .get_pages()
        .get(&page)
        .ok_or(lopdf::Error::PageNumberNotFound(page))?;
    let dict = origin.get_dictionary(id)?;
    let annots: Vec<ObjectId> = match dict.get(b"Annots") {
        Ok(Object::Reference(r)) => origin.get_object(*r)?.as_array()?.clone(),
        Ok(Object::Array(a)) => a.clone(),
        _ => Vec::new(),
    }
    .iter()
    .filter_map(|o| o.as_reference().ok())
    .collect();
    for annot in annots {
        doc.objects.insert(annot, origin.get_object(annot)?.clone());
    }
    doc.objects.insert(id, Object::Dictionary(dict.clone()));
    // 另一页的先前重编可能已把本页的源内容流当作无引用对象清掉。
    let mut stack = vec![id];
    let mut seen = BTreeSet::new();
    while let Some(next) = stack.pop() {
        if !seen.insert(next) {
            continue;
        }
        let Ok(object) = origin.get_object(next) else {
            continue;
        };
        doc.objects.entry(next).or_insert_with(|| object.clone());
        push_refs(object, &mut stack);
    }
    doc.prune_objects();
    Ok(())
}

fn push_refs(object: &Object, out: &mut Vec<ObjectId>) {
    match object {
        Object::Reference(id) => out.push(*id),
        Object::Array(items) => items.iter().for_each(|o| push_refs(o, out)),
        Object::Dictionary(d) => d.iter().for_each(|(_, o)| push_refs(o, out)),
        Object::Stream(s) => s.dict.iter().for_each(|(_, o)| push_refs(o, out)),
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::super::tests::{env_ready, pipeline, tmp_path, RunRecorder};
    use super::*;
    use syncpdf_core::require_fixture;

    type Log = Arc<Mutex<Vec<(u64, Event)>>>;

    fn recorder() -> (Log, SharedSink) {
        let log: Log = Arc::new(Mutex::new(Vec::new()));
        (log.clone(), SharedSink::new(RunRecorder::new(log)))
    }

    fn pixels(path: &Path, pages: u32) -> Vec<Vec<u8>> {
        let worker = syncpdf_pdf::pdfium::PdfiumWorker::spawn().unwrap();
        let doc = worker.open(path).unwrap();
        let out = (0..pages)
            .map(|p| worker.render_page(doc, p, 50.0).unwrap().data)
            .collect();
        worker.close(doc);
        out
    }

    /// 编辑一块只重编它所在的页：不发 `run_started`，只有该页 `page_reopened` /
    /// `page_ready`；结果与带同样编辑的全篇 run 逐像素一致，其它页不变。
    #[tokio::test]
    async fn edit_recompiles_only_its_page_and_matches_a_full_run() {
        if env_ready().is_none() {
            return;
        }
        let input = require_fixture!("up-vns.pdf");
        let p = pipeline(tmp_path("db"));
        let out = tmp_path("pdf");
        let mut cfg = RunConfig::with_fake("cjk", input.clone(), out.clone()).unwrap();
        if let Request::Run { pages, .. } = &mut cfg.run {
            *pages = Some(vec![0, 1]);
        }
        let (log, sink) = recorder();
        let mut retained = None;
        p.run_retaining(&cfg, sink, CancellationToken::new(), &mut retained)
            .await
            .unwrap();
        let mut retained = retained.expect("成功的 run 保留排版状态");
        let before = pixels(&out, 2);
        let (id, html) = log
            .lock()
            .unwrap()
            .iter()
            .rev()
            .find_map(|(_, e)| match e {
                Event::Paragraph {
                    paragraph_id,
                    status: ParagraphStatus::Typeset,
                    translated_html: Some(html),
                    ..
                } if paragraph_id.page == 2 => Some((paragraph_id.clone(), html.clone())),
                _ => None,
            })
            .expect("第 2 页有排上译文的段");
        // 事件译文是模型空间 HTML：原子保持 `{{KEEP_n}}`，编辑器据此回传仍能通过校验。
        assert!(html.contains("{{KEEP_"), "所编辑的段应含原子：{html}");
        let edited_html = html.replacen('>', ">改后甲乙丙丁", 1);
        p.save_edit(&Request::ApplyEdit {
            doc_id: "cli".into(),
            store: None,
            paragraph_id: id.clone(),
            translated_html: Some(edited_html.clone()),
            style: Default::default(),
        })
        .unwrap();

        let edited = BTreeSet::from([id.clone()]);
        // 反例：配置不同、或编辑了本轮没有的段，都不能走单页重编。
        let mut other = cfg.clone();
        if let Request::Run { pages, .. } = &mut other.run {
            *pages = Some(vec![0]);
        }
        assert!(!retained.covers(&other, &edited));
        assert!(!retained.covers(&cfg, &BTreeSet::from(["P09-001".parse().unwrap()])));
        assert!(retained.covers(&cfg, &edited));

        let (log, sink) = recorder();
        let summary = retained
            .recompile(&edited, sink, &CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(summary.pages, 1);
        let events = log.lock().unwrap().clone();
        assert!(!events
            .iter()
            .any(|(_, e)| matches!(e, Event::RunStarted { .. })));
        let pages_of = |want: fn(&Event) -> Option<u32>| -> Vec<u32> {
            events.iter().filter_map(|(_, e)| want(e)).collect()
        };
        assert_eq!(
            pages_of(|e| match e {
                Event::PageReopened { page } => Some(*page),
                _ => None,
            }),
            vec![2]
        );
        assert_eq!(
            pages_of(|e| match e {
                Event::PageReady { page, .. } => Some(*page),
                _ => None,
            }),
            vec![2]
        );
        assert!(events.iter().any(|(_, e)| matches!(e,
            Event::Paragraph { paragraph_id, status: ParagraphStatus::Typeset, translated_html: Some(h), .. }
                if *paragraph_id == id && h.contains("改后甲乙丙丁"))));
        assert!(matches!(
            events.last().unwrap().1,
            Event::RunFinished { .. }
        ));
        let recompiled = pixels(&out, 2);
        assert_eq!(recompiled[0], before[0], "未编辑的页不变");
        assert_ne!(recompiled[1], before[1], "编辑必须可见");

        // 同一编辑再编一次（还原过的页再还原）仍然成立。
        let (_, sink) = recorder();
        retained
            .recompile(&edited, sink, &CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(pixels(&out, 2), recompiled);

        let fresh = tmp_path("pdf");
        let mut full = cfg.clone();
        if let Request::Run { output, .. } = &mut full.run {
            *output = fresh.clone();
        }
        let (_, sink) = recorder();
        p.run(&full, sink, CancellationToken::new()).await.unwrap();
        assert_eq!(pixels(&fresh, 2), recompiled, "与全篇 run 逐像素一致");
    }
}
