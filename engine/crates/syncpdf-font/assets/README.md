# Bundled mathematical fallback

- `STIXTwoMath-Regular.otf`: STIX Fonts Project, **Version 2.12 b168**.
- Upstream: https://github.com/stipub/stixfonts
- Unmodified font copied from the installed TeX Live 2023 distribution; TeX is **not** a runtime dependency.
- SHA-256: `95bc2729e41faf93b0bcae9e96c4dc4da45855067fd0581e621e30734fe8d90b`.
- License and copyright notices: [`STIX-OFL.txt`](STIX-OFL.txt), SIL Open Font License 1.1.

The bytes are embedded in the Rust font crate and appended to the existing fallback chain. Exact Unicode is retained; mathematical italic letters are not normalized into different characters. Existing body fonts remain preferred.
