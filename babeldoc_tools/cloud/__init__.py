"""``bdt cloud``：多用户云端翻译服务（邀请码登录、单篇排队、共享译文缓存）。

与 ``bdt serve``（本地单机工作台）互不依赖：这里只驱动 Rust 引擎
（:mod:`babeldoc_tools.rust_backend`），数据全部落在 ``--root``（默认 ``~/.bdt-cloud``）。
"""
