//! 开发辅助任务：`cargo xtask <cmd>`。
use clap::{Parser, Subcommand};
use std::process::Command as Proc;

#[derive(Parser)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// 把本机 ~/.sp 与 examples/ci 的真实 PDF 复制到 engine/fixtures/。
    Fixtures,
    /// fmt --check + clippy -D warnings + test（CI 同款）。
    Ci,
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Fixtures => {
            let status = Proc::new("sh").arg("fixtures/sync.sh").status()?;
            anyhow::ensure!(status.success(), "fixtures/sync.sh failed");
        }
        Cmd::Ci => {
            for args in [
                vec!["fmt", "--all", "--check"],
                vec![
                    "clippy",
                    "--workspace",
                    "--all-targets",
                    "--",
                    "-D",
                    "warnings",
                ],
                vec!["test", "--workspace"],
            ] {
                let status = Proc::new("cargo").args(&args).status()?;
                anyhow::ensure!(status.success(), "cargo {} failed", args.join(" "));
            }
        }
    }
    Ok(())
}
