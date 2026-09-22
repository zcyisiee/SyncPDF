//! 页就绪判定：哪些页的段落已全部落定，可以立刻回写并发 `page_ready`。
//!
//! 设计基准：02-技术路径与架构.md §9.2/§6（流式编译）。
//! 翻译引擎是流式的：每交付一个已校验段落就调 [`PageSchedule::mark`]，
//! 返回 `Some(page)` 时上游立即对该页做 PatchSet + Writer + 快照。
//!
//! 三种「页就绪」：
//! 1. 页内全部段落都已到达（`mark` / `mark_page` 触发）；
//! 2. 该页本来就没有段落（构造时就知道，见 [`PageSchedule::pages_without_paragraphs`]）；
//! 3. 翻译结束仍有缺口（失败/漏译），由 `force_ready_rest` 兜底转 ready。

use std::collections::{BTreeMap, BTreeSet};

use syncpdf_core::ParagraphId;

/// 逐页的「应到段落集合 / 已到段落集合 / 是否已 ready」。
#[derive(Debug, Clone, Default)]
pub struct PageSchedule {
    /// 页号（1 基，与 `ParagraphId::page` 一致）→ 该页应到的段。
    expected: BTreeMap<u32, BTreeSet<ParagraphId>>,
    /// 页号 → 已到达的段。
    arrived: BTreeMap<u32, BTreeSet<ParagraphId>>,
    /// 已发过就绪信号的页。
    ready: BTreeSet<u32>,
}

impl PageSchedule {
    /// 用「(页号, 段 id)」序列构造。
    ///
    /// `paras` 里的页即使没有段也会建表（构造后 `pages_without_paragraphs`
    /// 能报出来）；调用方还应把全部页号经 [`Self::add_page`] 补进来。
    pub fn new(paras: impl Iterator<Item = (u32, ParagraphId)>) -> Self {
        let mut s = Self::default();
        for (page, id) in paras {
            s.expected.entry(page).or_default().insert(id);
        }
        s
    }

    /// 登记一个「本页无段落」的空页（构造后即可就绪）。
    pub fn add_page(&mut self, page: u32) {
        self.expected.entry(page).or_default();
    }

    /// 登记 `[1, pages]` 的全部页号。
    pub fn add_all_pages(&mut self, pages: u32) {
        for p in 1..=pages {
            self.add_page(p);
        }
    }

    /// 某页应到的段数（页未登记时 0）。
    pub fn expected_count(&self, page: u32) -> usize {
        self.expected.get(&page).map_or(0, BTreeSet::len)
    }

    /// 某页已到的段数。
    pub fn arrived_count(&self, page: u32) -> usize {
        self.arrived.get(&page).map_or(0, BTreeSet::len)
    }

    /// 该页是否已经 ready 过。
    pub fn is_ready(&self, page: u32) -> bool {
        self.ready.contains(&page)
    }

    /// 已 ready 的页（升序）。
    pub fn ready_pages(&self) -> Vec<u32> {
        self.ready.iter().copied().collect()
    }

    /// 标记一个段落到达。若该段所在页全部到齐且尚未 ready，
    /// 返回 `Some(页号)` 并记 ready；否则 `None`。
    ///
    /// 重复 `mark` 同一个段是幂等的（仍返回 `None`）。
    pub fn mark(&mut self, id: &ParagraphId) -> Option<u32> {
        let page = id.page;
        self.arrived.entry(page).or_default().insert(id.clone());
        self.try_ready(page)
    }

    /// 标记整页到达（该页所有应到段都在别处落定过时用）。
    ///
    /// 用于「页内最后一段以 fallback 收尾」等没法逐段 `mark` 的路径。
    pub fn mark_page(&mut self, page: u32) -> Option<u32> {
        for id in self.expected.get(&page).cloned().unwrap_or_default() {
            self.arrived.entry(page).or_default().insert(id);
        }
        self.try_ready(page)
    }

    /// 页号已知、应到集合已满时转 ready（幂等）。
    fn try_ready(&mut self, page: u32) -> Option<u32> {
        if self.ready.contains(&page) {
            return None;
        }
        let expected = self.expected.get(&page)?;
        let arrived = self.arrived.get(&page);
        let all = expected.iter().all(|id| {
            arrived
                .map(|a| a.contains(id))
                .unwrap_or_else(|| expected.is_empty())
        });
        if all {
            self.ready.insert(page);
            Some(page)
        } else {
            None
        }
    }

    /// 翻译结束后兜底：未凑齐的页全部转 ready，返回新就绪页（升序）。
    ///
    /// 已 ready 的页不在返回值里。
    pub fn force_ready_rest(&mut self) -> Vec<u32> {
        let rest: Vec<u32> = self
            .expected
            .keys()
            .copied()
            .filter(|p| !self.ready.contains(p))
            .collect();
        for p in &rest {
            self.ready.insert(*p);
        }
        rest
    }

    /// 尚未就绪的页（升序），供 `force_ready_rest` 后核对。
    pub fn pending_pages(&self) -> Vec<u32> {
        self.expected
            .keys()
            .copied()
            .filter(|p| !self.ready.contains(p))
            .collect()
    }

    /// 登记过的页里「没有段落」的那些（升序）。
    ///
    /// 这些页永远不会被 `mark` 触发，调用方应在开始前就把它们转 ready
    /// （否则 `pending_pages` 会长期挂着它们）。
    pub fn pages_without_paragraphs(&self, all_pages: &[u32]) -> Vec<u32> {
        let mut v: Vec<u32> = all_pages
            .iter()
            .copied()
            .filter(|p| self.expected_count(*p) == 0)
            .collect();
        v.sort_unstable();
        v.dedup();
        v
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pid(page: u32, seq: u32) -> ParagraphId {
        ParagraphId { page, seq }
    }

    #[test]
    fn empty_page_with_no_paragraphs_is_reported_not_ready() {
        let mut s = PageSchedule::new(std::iter::empty());
        s.add_all_pages(3);
        // 没有段落的页不会自动 ready（免得在还没开跑时就发页事件）。
        assert_eq!(s.pages_without_paragraphs(&[1, 2, 3]), vec![1, 2, 3]);
        assert_eq!(s.pending_pages(), vec![1, 2, 3]);
        // 显式转 ready。
        assert_eq!(s.mark_page(2), Some(2));
        assert!(s.is_ready(2));
        assert_eq!(s.pending_pages(), vec![1, 3]);
        // 幂等。
        assert_eq!(s.mark_page(2), None);
    }

    #[test]
    fn page_readies_only_after_all_its_paragraphs_arrive() {
        let mut s = PageSchedule::new(
            vec![(1, pid(1, 1)), (1, pid(1, 2)), (2, pid(2, 1))].into_iter(),
        );
        assert_eq!(s.mark(&pid(1, 1)), None);
        assert_eq!(s.mark(&pid(2, 1)), Some(2));
        assert_eq!(s.mark(&pid(1, 2)), Some(1));
        // 第二次标记同一个段不重复就绪。
        assert_eq!(s.mark(&pid(1, 2)), None);
        assert_eq!(s.ready_pages(), vec![1, 2]);
        assert!(s.pending_pages().is_empty());
    }

    #[test]
    fn single_paragraph_page_readies_immediately() {
        let mut s = PageSchedule::new(vec![(7, pid(7, 1))].into_iter());
        assert_eq!(s.mark(&pid(7, 1)), Some(7));
        assert_eq!(s.arrived_count(7), 1);
        assert_eq!(s.expected_count(7), 1);
    }

    #[test]
    fn unknown_page_mark_does_not_ready_anything() {
        // 段 id 指向没登记过的页：不该凭空 ready。
        let mut s = PageSchedule::new(vec![(1, pid(1, 1))].into_iter());
        assert_eq!(s.mark(&pid(9, 1)), None);
        assert_eq!(s.ready_pages(), Vec::<u32>::new());
        assert_eq!(s.arrived_count(9), 1);
        assert_eq!(s.expected_count(9), 0);
    }

    #[test]
    fn force_ready_rest_drains_in_ascending_order() {
        let mut s = PageSchedule::new(
            vec![(5, pid(5, 1)), (3, pid(3, 1)), (9, pid(9, 1))].into_iter(),
        );
        assert_eq!(s.mark(&pid(3, 1)), Some(3));
        assert_eq!(s.force_ready_rest(), vec![5, 9]);
        assert_eq!(s.force_ready_rest(), Vec::<u32>::new());
        assert!(s.pending_pages().is_empty());
        assert_eq!(s.ready_pages(), vec![3, 5, 9]);
    }

    #[test]
    fn mark_page_completes_a_gapped_page() {
        // 页 2 有 2 段，只到了 1 段；翻译结束后整页转 ready 不再等第 2 段。
        let mut s = PageSchedule::new(vec![(2, pid(2, 1)), (2, pid(2, 2))].into_iter());
        assert_eq!(s.mark(&pid(2, 1)), None);
        assert_eq!(s.pending_pages(), vec![2]);
        assert_eq!(s.mark_page(2), Some(2));
        assert_eq!(s.pending_pages(), Vec::<u32>::new());
    }

    #[test]
    fn mixed_pages_with_and_without_paragraphs() {
        let mut s = PageSchedule::new(vec![(1, pid(1, 1)), (3, pid(3, 1))].into_iter());
        s.add_all_pages(4);
        assert_eq!(s.pages_without_paragraphs(&[1, 2, 3, 4]), vec![2, 4]);
        assert_eq!(s.mark(&pid(1, 1)), Some(1));
        assert_eq!(s.mark(&pid(3, 1)), Some(3));
        assert_eq!(s.pending_pages(), vec![2, 4]);
        assert_eq!(s.force_ready_rest(), vec![2, 4]);
    }

    #[test]
    fn multiple_paragraphs_same_page_ready_once() {
        let mut s = PageSchedule::new(
            (1..=4).map(|i| (1u32, pid(1, i))).collect::<Vec<_>>().into_iter(),
        );
        let mut readied = Vec::new();
        for i in 1..=4 {
            if let Some(p) = s.mark(&pid(1, i)) {
                readied.push(p);
            }
        }
        assert_eq!(readied, vec![1], "每页只 ready 一次");
    }
}
