//! SyncPDF 引擎命令行入口（sidecar）。
#![forbid(unsafe_code)]

use clap::{Parser, Subcommand};

#[derive(Parser, Debug)]
#[command(name = "syncpdf-cli", version, about = "SyncPDF engine")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// 打印版本与构建信息。
    Version,
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Version => {
            println!("syncpdf-cli {}", env!("CARGO_PKG_VERSION"));
        }
    }
    Ok(())
}
