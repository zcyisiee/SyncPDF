//! 取消令牌：自实现（不引入 `tokio-util`）。
//!
//! 语义与 `tokio_util::sync::CancellationToken` 的最小子集一致：
//! 单向、幂等、可克隆共享；**不支持**取消后重置。
//! 阶段边界（每阶段开始前）与流式回调（每段排版前）都应检查它。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

/// 可克隆的取消令牌。任意克隆收到 `cancel()` 后，所有克隆都可见。
#[derive(Clone, Default, Debug)]
pub struct CancellationToken(Arc<AtomicBool>);

impl CancellationToken {
    /// 新建未取消的令牌。
    pub fn new() -> Self {
        Self(Arc::new(AtomicBool::new(false)))
    }

    /// 请求取消（幂等）。
    pub fn cancel(&self) {
        self.0.store(true, Ordering::SeqCst);
    }

    /// 是否已请求取消。
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn new_token_is_not_cancelled() {
        let t = CancellationToken::new();
        assert!(!t.is_cancelled());
        assert!(!CancellationToken::default().is_cancelled());
    }

    #[test]
    fn cancel_is_idempotent_and_visible_to_clones() {
        let t = CancellationToken::new();
        let a = t.clone();
        let b = t.clone();
        assert!(!a.is_cancelled() && !b.is_cancelled());
        a.cancel();
        assert!(t.is_cancelled());
        assert!(b.is_cancelled());
        // 再取消一次不改变语义。
        b.cancel();
        t.cancel();
        assert!(a.is_cancelled() && b.is_cancelled() && t.is_cancelled());
    }

    #[test]
    fn token_is_send_sync_and_crosses_threads() {
        let t = CancellationToken::new();
        let child = t.clone();
        let h = std::thread::spawn(move || {
            assert!(!child.is_cancelled());
            child.cancel();
        });
        h.join().unwrap();
        assert!(t.is_cancelled());
    }
}
