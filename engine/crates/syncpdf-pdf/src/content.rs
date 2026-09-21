//! PDF 内容流的词法分析、语法分析与序列化（PDF 32000-1 §7.2、§7.3、§8.9.7）。
//!
//! 设计目标（见 03-整体计划与任务拆分.md M0-04）：
//! - 把任意内容流字节解析为带字节范围的 [`Op`] 序列；
//! - 未改动的 Op 按原字节写回（整流字节相等），改动过的 Op 走规范化序列化；
//! - 容错：未知操作符照收，多余操作数在操作符前清空并记 warning，
//!   单个畸形 token 不会让整页解析失败。
//!
//! 本模块只负责语法层，不解释图形状态语义（那是 `bind` 模块的职责）。
//!
//! # 往返保证
//!
//! [`write_content`] 依赖两件事：
//! 1. [`Op::span`] 是「首个被保留的操作数起点 .. 操作符终点」的紧凑范围；
//! 2. 相邻 Op 之间的字节（空白、注释、被丢弃的残留操作数）不属于任何 span，
//!    写回时原样拷贝。
//!
//! 因此只要所有 Op 都未标脏，`write_content(src, &parse_content(src)?) == src`。

use std::ops::Range;

/// 嵌套数组 / 字典的最大深度，超过即报错，防止病态输入递归爆栈。
const MAX_DEPTH: usize = 64;

/// 操作数栈上限。畸形流可能堆积海量数字，超过即丢弃最旧的部分。
const MAX_OPERANDS: usize = 512;

/// 实数序列化保留的小数位（PDF 实数不支持指数记法）。
const REAL_PRECISION: usize = 6;

const HEX_UPPER: &[u8; 16] = b"0123456789ABCDEF";

// ---------------------------------------------------------------------------
// 字符分类（§7.2.3 表 1、表 2）
// ---------------------------------------------------------------------------

/// PDF 空白字符：NUL、TAB、LF、FF、CR、SP。
#[inline]
const fn is_white(b: u8) -> bool {
    matches!(b, 0x00 | 0x09 | 0x0A | 0x0C | 0x0D | 0x20)
}

/// PDF 分隔符。
#[inline]
const fn is_delim(b: u8) -> bool {
    matches!(
        b,
        b'(' | b')' | b'<' | b'>' | b'[' | b']' | b'{' | b'}' | b'/' | b'%'
    )
}

/// PDF 正规字符：既非空白也非分隔符。
#[inline]
const fn is_regular(b: u8) -> bool {
    !is_white(b) && !is_delim(b)
}

#[inline]
const fn hex_val(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// 错误
// ---------------------------------------------------------------------------

/// 内容流解析错误。绝大多数畸形输入都被容错处理，只有下面两类会真正失败。
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ContentError {
    /// `BI … ID` 之后找不到合法的 `EI`。
    #[error("内联图像未终止：偏移 {offset} 处的 BI 找不到 EI")]
    UnterminatedInlineImage { offset: usize },
    /// 数组 / 字典嵌套过深。
    #[error("偏移 {offset} 处嵌套层级超过上限 {max}")]
    NestingTooDeep { offset: usize, max: usize },
}

// ---------------------------------------------------------------------------
// 操作数
// ---------------------------------------------------------------------------

/// 内容流操作数。`Name` 不含前导 `/`，`Str` 保存解码后的原始字节。
#[derive(Debug, Clone, PartialEq)]
pub enum Operand {
    Int(i64),
    Real(f64),
    Bool(bool),
    Null,
    Name(String),
    Str(Vec<u8>),
    Array(Vec<Operand>),
    Dict(Vec<(String, Operand)>),
}

impl Operand {
    /// 数字（Int 或 Real）取 `f64`。
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Self::Int(v) => Some(*v as f64),
            Self::Real(v) => Some(*v),
            _ => None,
        }
    }

    /// 整数取值；`Real` 仅在数值恰为整数时接受。
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Self::Int(v) => Some(*v),
            Self::Real(v) if v.is_finite() && v.fract() == 0.0 => Some(*v as i64),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Self::Bool(v) => Some(*v),
            _ => None,
        }
    }

    /// 名字（不含 `/`）。
    pub fn as_name(&self) -> Option<&str> {
        match self {
            Self::Name(n) => Some(n.as_str()),
            _ => None,
        }
    }

    /// 字符串原始字节。
    pub fn as_bytes(&self) -> Option<&[u8]> {
        match self {
            Self::Str(s) => Some(s.as_slice()),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Operand]> {
        match self {
            Self::Array(v) => Some(v.as_slice()),
            _ => None,
        }
    }

    pub fn as_dict(&self) -> Option<&[(String, Operand)]> {
        match self {
            Self::Dict(v) => Some(v.as_slice()),
            _ => None,
        }
    }

    pub fn is_number(&self) -> bool {
        matches!(self, Self::Int(_) | Self::Real(_))
    }
}

// ---------------------------------------------------------------------------
// 内联图像与操作
// ---------------------------------------------------------------------------

/// `BI … ID <data> EI` 的内联图像。`dict` 保持源顺序，键不含 `/`（缩写名原样保留）。
#[derive(Debug, Clone, PartialEq)]
pub struct InlineImage {
    pub dict: Vec<(String, Operand)>,
    /// `ID` 后的原始（可能已过滤编码的）图像字节，不含分隔空白与 `EI`。
    pub data: Vec<u8>,
}

impl InlineImage {
    /// 按长名或缩写名取字典项，例如 `get("Width", "W")`。
    pub fn get(&self, long: &str, short: &str) -> Option<&Operand> {
        self.dict
            .iter()
            .find(|(k, _)| k == long || k == short)
            .map(|(_, v)| v)
    }
}

/// 一条内容流操作：操作数 + 操作符 + 源字节范围。
///
/// `span` 是紧凑范围（首个保留操作数起点 .. 操作符终点），不含前导空白与注释。
/// 对内联图像，`operator` 为 `"BI"`，`span` 覆盖 `BI` 到 `EI` 的全部字节。
#[derive(Debug, Clone, PartialEq)]
pub struct Op {
    pub operator: String,
    pub operands: Vec<Operand>,
    pub span: Range<usize>,
    pub inline_image: Option<InlineImage>,
    /// 标脏后 [`write_content`] 不再拷贝源字节，改用 [`serialize_op`]。
    pub dirty: bool,
}

impl Op {
    /// 构造一条新插入的操作：`span` 为空且已标脏，写回时必然走序列化。
    pub fn new(operator: impl Into<String>, operands: Vec<Operand>) -> Self {
        Self {
            operator: operator.into(),
            operands,
            span: 0..0,
            inline_image: None,
            dirty: true,
        }
    }

    /// 标记为已修改。
    pub fn mark_dirty(&mut self) {
        self.dirty = true;
    }

    /// 是否为内联图像操作。
    pub fn is_inline_image(&self) -> bool {
        self.inline_image.is_some()
    }

    /// 取该操作在源缓冲里的原始字节；`span` 越界或为空时返回 `None`。
    pub fn source<'s>(&self, src: &'s [u8]) -> Option<&'s [u8]> {
        if self.span.is_empty() || self.span.end > src.len() {
            return None;
        }
        Some(&src[self.span.clone()])
    }

    /// 是否为文本显示操作：`Tj`、`TJ`、`'`、`"`。
    pub fn is_text_show(&self) -> bool {
        matches!(self.operator.as_str(), "Tj" | "TJ" | "'" | "\"")
    }

    /// 文本显示操作携带的字符串。`Tj` / `'` / `"` 各一个；`TJ` 取数组里的全部字符串（跳过数字）。
    pub fn text_strings(&self) -> Vec<&[u8]> {
        match self.operator.as_str() {
            "Tj" | "'" | "\"" => self
                .operands
                .iter()
                .rev()
                .find_map(Operand::as_bytes)
                .map(|s| vec![s])
                .unwrap_or_default(),
            "TJ" => self
                .operands
                .iter()
                .rev()
                .find_map(Operand::as_array)
                .map(|items| items.iter().filter_map(Operand::as_bytes).collect())
                .unwrap_or_default(),
            _ => Vec::new(),
        }
    }

    /// 是否为文本状态 / 定位操作。
    pub fn is_text_state(&self) -> bool {
        matches!(
            self.operator.as_str(),
            "Tf" | "Tm" | "Td" | "TD" | "T*" | "TL" | "Tc" | "Tw" | "Tz" | "Ts" | "Tr"
        )
    }
}

/// 常见操作符的固定操作数个数。返回 `None` 表示个数可变或未知（例如 `scn`、自定义操作符）。
fn operator_arity(op: &str) -> Option<usize> {
    let n = match op {
        // 图形状态
        "q" | "Q" | "BX" | "EX" => 0,
        "cm" => 6,
        "w" | "J" | "j" | "M" | "ri" | "i" | "gs" => 1,
        "d" => 2,
        // 路径构造
        "m" | "l" => 2,
        "c" => 6,
        "v" | "y" | "re" => 4,
        "h" => 0,
        // 路径绘制与裁剪
        "S" | "s" | "f" | "F" | "f*" | "B" | "B*" | "b" | "b*" | "n" | "W" | "W*" => 0,
        // 文本
        "BT" | "ET" | "T*" => 0,
        "Tc" | "Tw" | "Tz" | "TL" | "Ts" | "Tr" | "Tj" | "TJ" | "'" => 1,
        "Tf" | "Td" | "TD" => 2,
        "Tm" => 6,
        "\"" => 3,
        // 颜色
        "CS" | "cs" | "G" | "g" => 1,
        "RG" | "rg" => 3,
        "K" | "k" => 4,
        // 着色、XObject、标记内容
        "sh" | "Do" | "MP" | "BMC" => 1,
        "DP" | "BDC" => 2,
        "EMC" => 0,
        // Type 3 字形度量
        "d0" => 2,
        "d1" => 6,
        // SC/SCN/sc/scn 个数随色彩空间变化，不做裁剪
        _ => return None,
    };
    Some(n)
}

// ---------------------------------------------------------------------------
// 词法分析
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
enum Token {
    Int(i64),
    Real(f64),
    Name(String),
    Str(Vec<u8>),
    ArrayOpen,
    ArrayClose,
    DictOpen,
    DictClose,
    /// 关键字：操作符，或 `true` / `false` / `null`。
    Keyword(String),
    /// 无法归类的孤立字节（例如多余的 `)`），直接忽略。
    Junk,
}

#[derive(Debug)]
struct Lexer<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Lexer<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Self { buf, pos: 0 }
    }

    #[inline]
    fn peek(&self) -> Option<u8> {
        self.buf.get(self.pos).copied()
    }

    #[inline]
    fn at(&self, i: usize) -> Option<u8> {
        self.buf.get(i).copied()
    }

    /// 跳过空白与 `%` 注释（注释到行尾）。
    fn skip_white(&mut self) {
        while let Some(b) = self.peek() {
            if is_white(b) {
                self.pos += 1;
            } else if b == b'%' {
                while let Some(c) = self.peek() {
                    if c == b'\r' || c == b'\n' {
                        break;
                    }
                    self.pos += 1;
                }
            } else {
                break;
            }
        }
    }

    /// 读下一个 token；到流尾返回 `None`。调用前会自动跳过空白与注释。
    fn next_token(&mut self) -> Option<Token> {
        self.skip_white();
        let b = self.peek()?;
        let tok = match b {
            b'[' => {
                self.pos += 1;
                Token::ArrayOpen
            }
            b']' => {
                self.pos += 1;
                Token::ArrayClose
            }
            b'<' => {
                if self.at(self.pos + 1) == Some(b'<') {
                    self.pos += 2;
                    Token::DictOpen
                } else {
                    Token::Str(self.read_hex_string())
                }
            }
            b'>' => {
                if self.at(self.pos + 1) == Some(b'>') {
                    self.pos += 2;
                    Token::DictClose
                } else {
                    self.pos += 1;
                    Token::Junk
                }
            }
            b'(' => Token::Str(self.read_literal_string()),
            b')' => {
                self.pos += 1;
                Token::Junk
            }
            b'/' => Token::Name(self.read_name()),
            b'{' | b'}' => {
                self.pos += 1;
                Token::Keyword((b as char).to_string())
            }
            b'+' | b'-' | b'.' | b'0'..=b'9' => self.read_number(),
            _ => {
                let start = self.pos;
                while self.peek().is_some_and(is_regular) {
                    self.pos += 1;
                }
                if self.pos == start {
                    self.pos += 1;
                    Token::Junk
                } else {
                    Token::Keyword(String::from_utf8_lossy(&self.buf[start..self.pos]).into_owned())
                }
            }
        };
        Some(tok)
    }

    /// 名字对象：`/` 之后的正规字符，`#xx` 为十六进制转义。
    fn read_name(&mut self) -> String {
        self.pos += 1; // '/'
        let mut out: Vec<u8> = Vec::new();
        while let Some(c) = self.peek() {
            if !is_regular(c) {
                break;
            }
            if c == b'#' {
                let hi = self.at(self.pos + 1).and_then(hex_val);
                let lo = self.at(self.pos + 2).and_then(hex_val);
                if let (Some(h), Some(l)) = (hi, lo) {
                    out.push(h * 16 + l);
                    self.pos += 3;
                    continue;
                }
            }
            out.push(c);
            self.pos += 1;
        }
        String::from_utf8_lossy(&out).into_owned()
    }

    /// 字面串：括号可嵌套，`\` 转义，CR / CRLF 归一为 LF（§7.3.4.2）。
    fn read_literal_string(&mut self) -> Vec<u8> {
        self.pos += 1; // '('
        let mut out: Vec<u8> = Vec::new();
        let mut depth: usize = 1;
        while let Some(c) = self.peek() {
            self.pos += 1;
            match c {
                b'\\' => {
                    let Some(e) = self.peek() else { break };
                    self.pos += 1;
                    match e {
                        b'n' => out.push(b'\n'),
                        b'r' => out.push(b'\r'),
                        b't' => out.push(b'\t'),
                        b'b' => out.push(0x08),
                        b'f' => out.push(0x0C),
                        b'(' => out.push(b'('),
                        b')' => out.push(b')'),
                        b'\\' => out.push(b'\\'),
                        // 反斜杠 + 行结束符：续行，不产生字节
                        b'\r' => {
                            if self.peek() == Some(b'\n') {
                                self.pos += 1;
                            }
                        }
                        b'\n' => {}
                        b'0'..=b'7' => {
                            let mut v = u32::from(e - b'0');
                            for _ in 0..2 {
                                match self.peek() {
                                    Some(d @ b'0'..=b'7') => {
                                        v = v * 8 + u32::from(d - b'0');
                                        self.pos += 1;
                                    }
                                    _ => break,
                                }
                            }
                            out.push((v & 0xFF) as u8);
                        }
                        // 其余转义：反斜杠被忽略，字符保留
                        other => out.push(other),
                    }
                }
                b'(' => {
                    depth += 1;
                    out.push(b'(');
                }
                b')' => {
                    depth -= 1;
                    if depth == 0 {
                        return out;
                    }
                    out.push(b')');
                }
                b'\r' => {
                    if self.peek() == Some(b'\n') {
                        self.pos += 1;
                    }
                    out.push(b'\n');
                }
                _ => out.push(c),
            }
        }
        tracing::warn!("content: 字面串未闭合，按到流尾处理");
        out
    }

    /// 十六进制串：忽略空白与非法字符，奇数位末尾补 0。
    fn read_hex_string(&mut self) -> Vec<u8> {
        self.pos += 1; // '<'
        let mut out: Vec<u8> = Vec::new();
        let mut hi: Option<u8> = None;
        let mut closed = false;
        while let Some(c) = self.peek() {
            self.pos += 1;
            if c == b'>' {
                closed = true;
                break;
            }
            let Some(v) = hex_val(c) else { continue };
            match hi.take() {
                None => hi = Some(v),
                Some(h) => out.push(h * 16 + v),
            }
        }
        if let Some(h) = hi {
            out.push(h * 16);
        }
        if !closed {
            tracing::warn!("content: 十六进制串未闭合，按到流尾处理");
        }
        out
    }

    /// 数字：整数或实数，接受 `.5`、`-.5`、`6.`，容忍中途出现的非法字符。
    fn read_number(&mut self) -> Token {
        let start = self.pos;
        while self.peek().is_some_and(is_regular) {
            self.pos += 1;
        }
        parse_number(&self.buf[start..self.pos])
    }
}

fn parse_number(raw: &[u8]) -> Token {
    let mut neg = false;
    let mut seen_dot = false;
    let mut int_part = String::new();
    let mut frac_part = String::new();
    for (i, &c) in raw.iter().enumerate() {
        match c {
            b'-' if i == 0 => neg = true,
            b'+' | b'-' => {} // 中途符号按 §7.3.3 忽略
            b'.' => seen_dot = true,
            b'0'..=b'9' => {
                if seen_dot {
                    frac_part.push(char::from(c));
                } else {
                    int_part.push(char::from(c));
                }
            }
            _ => {} // 容错：忽略非法字符
        }
    }

    if !seen_dot {
        if int_part.is_empty() {
            return Token::Int(0);
        }
        return match int_part.parse::<i64>() {
            Ok(v) => Token::Int(if neg { -v } else { v }),
            Err(_) => {
                let f = int_part.parse::<f64>().unwrap_or(0.0);
                Token::Real(if neg { -f } else { f })
            }
        };
    }

    if int_part.is_empty() {
        int_part.push('0');
    }
    if frac_part.is_empty() {
        frac_part.push('0');
    }
    let f = format!("{int_part}.{frac_part}")
        .parse::<f64>()
        .unwrap_or(0.0);
    Token::Real(if neg { -f } else { f })
}

// ---------------------------------------------------------------------------
// 语法分析
// ---------------------------------------------------------------------------

/// 把内容流字节解析为 [`Op`] 序列。
///
/// 容错策略：
/// - 未知操作符照收，其全部前置操作数都归给它；
/// - 已知定长操作符若操作数过多，多余的旧操作数在操作符生效前被丢弃并记 warning；
/// - 孤立的 `)`、`]`、`>>` 等被忽略；
/// - 流尾未被消费的操作数被丢弃并记 warning。
pub fn parse_content(bytes: &[u8]) -> Result<Vec<Op>, ContentError> {
    let mut lex = Lexer::new(bytes);
    let mut ops: Vec<Op> = Vec::new();
    // 操作数连同它在源里的起点，起点用于算 span。
    let mut stack: Vec<(Operand, usize)> = Vec::new();

    loop {
        lex.skip_white();
        let start = lex.pos;
        let Some(tok) = lex.next_token() else { break };
        match tok {
            Token::Junk => {}
            Token::Int(v) => stack.push((Operand::Int(v), start)),
            Token::Real(v) => stack.push((Operand::Real(v), start)),
            Token::Name(n) => stack.push((Operand::Name(n), start)),
            Token::Str(s) => stack.push((Operand::Str(s), start)),
            Token::ArrayOpen => stack.push((parse_array(&mut lex, 1)?, start)),
            Token::DictOpen => stack.push((parse_dict(&mut lex, 1)?, start)),
            Token::ArrayClose | Token::DictClose => {
                tracing::warn!(offset = start, "content: 多余的闭合分隔符，已忽略");
            }
            Token::Keyword(k) => match k.as_str() {
                "true" => stack.push((Operand::Bool(true), start)),
                "false" => stack.push((Operand::Bool(false), start)),
                "null" => stack.push((Operand::Null, start)),
                "BI" => {
                    if !stack.is_empty() {
                        tracing::warn!(
                            offset = start,
                            count = stack.len(),
                            "content: BI 前存在操作数残留，已清空"
                        );
                        stack.clear();
                    }
                    let image = parse_inline_image(&mut lex, start)?;
                    ops.push(Op {
                        operator: "BI".to_string(),
                        operands: Vec::new(),
                        span: start..lex.pos,
                        inline_image: Some(image),
                        dirty: false,
                    });
                }
                _ => ops.push(finish_op(k, &mut stack, start, lex.pos)),
            },
        }

        if stack.len() > MAX_OPERANDS {
            let extra = stack.len() - MAX_OPERANDS;
            tracing::warn!(extra, "content: 操作数栈超限，已丢弃最旧的操作数");
            stack.drain(..extra);
        }
    }

    if !stack.is_empty() {
        tracing::warn!(
            count = stack.len(),
            "content: 流尾存在未被操作符消费的操作数，已丢弃"
        );
    }
    Ok(ops)
}

fn finish_op(
    operator: String,
    stack: &mut Vec<(Operand, usize)>,
    keyword_start: usize,
    end: usize,
) -> Op {
    if let Some(arity) = operator_arity(&operator) {
        if stack.len() > arity {
            let extra = stack.len() - arity;
            tracing::warn!(
                operator = %operator,
                extra,
                "content: 操作数残留，已在操作符生效前清空"
            );
            stack.drain(..extra);
        }
    }
    let start = stack.first().map_or(keyword_start, |(_, s)| *s);
    let operands: Vec<Operand> = stack.drain(..).map(|(o, _)| o).collect();
    Op {
        operator,
        operands,
        span: start..end,
        inline_image: None,
        dirty: false,
    }
}

fn parse_array(lex: &mut Lexer<'_>, depth: usize) -> Result<Operand, ContentError> {
    if depth > MAX_DEPTH {
        return Err(ContentError::NestingTooDeep {
            offset: lex.pos,
            max: MAX_DEPTH,
        });
    }
    let mut items: Vec<Operand> = Vec::new();
    loop {
        let start = lex.pos;
        let Some(tok) = lex.next_token() else {
            tracing::warn!(offset = start, "content: 数组未闭合，按流尾处理");
            break;
        };
        match tok {
            Token::ArrayClose => break,
            Token::ArrayOpen => items.push(parse_array(lex, depth + 1)?),
            Token::DictOpen => items.push(parse_dict(lex, depth + 1)?),
            Token::Int(v) => items.push(Operand::Int(v)),
            Token::Real(v) => items.push(Operand::Real(v)),
            Token::Name(n) => items.push(Operand::Name(n)),
            Token::Str(s) => items.push(Operand::Str(s)),
            Token::DictClose | Token::Junk => {
                tracing::warn!(offset = start, "content: 数组内出现无效 token，已忽略");
            }
            Token::Keyword(k) => match k.as_str() {
                "true" => items.push(Operand::Bool(true)),
                "false" => items.push(Operand::Bool(false)),
                "null" => items.push(Operand::Null),
                _ => tracing::warn!(keyword = %k, "content: 数组内出现关键字，已忽略"),
            },
        }
    }
    Ok(Operand::Array(items))
}

fn parse_dict(lex: &mut Lexer<'_>, depth: usize) -> Result<Operand, ContentError> {
    Ok(Operand::Dict(parse_dict_entries(lex, depth)?))
}

fn parse_dict_entries(
    lex: &mut Lexer<'_>,
    depth: usize,
) -> Result<Vec<(String, Operand)>, ContentError> {
    if depth > MAX_DEPTH {
        return Err(ContentError::NestingTooDeep {
            offset: lex.pos,
            max: MAX_DEPTH,
        });
    }
    let mut out: Vec<(String, Operand)> = Vec::new();
    loop {
        let start = lex.pos;
        let Some(tok) = lex.next_token() else {
            tracing::warn!(offset = start, "content: 字典未闭合，按流尾处理");
            break;
        };
        let key = match tok {
            Token::DictClose => break,
            Token::Name(n) => n,
            _ => {
                tracing::warn!(offset = start, "content: 字典键不是名字对象，已忽略");
                continue;
            }
        };
        let Some(value) = parse_value(lex, depth + 1)? else {
            tracing::warn!(key = %key, "content: 字典键缺少取值");
            break;
        };
        out.push((key, value));
    }
    Ok(out)
}

/// 读一个「值」位置的对象。返回 `Ok(None)` 表示遇到流尾或提前闭合。
fn parse_value(lex: &mut Lexer<'_>, depth: usize) -> Result<Option<Operand>, ContentError> {
    loop {
        let start = lex.pos;
        let Some(tok) = lex.next_token() else {
            return Ok(None);
        };
        let v = match tok {
            Token::ArrayOpen => parse_array(lex, depth + 1)?,
            Token::DictOpen => parse_dict(lex, depth + 1)?,
            Token::Int(x) => Operand::Int(x),
            Token::Real(x) => Operand::Real(x),
            Token::Name(n) => Operand::Name(n),
            Token::Str(s) => Operand::Str(s),
            Token::ArrayClose | Token::DictClose => return Ok(None),
            Token::Junk => {
                tracing::warn!(offset = start, "content: 取值位置出现无效字节，已忽略");
                continue;
            }
            Token::Keyword(k) => match k.as_str() {
                "true" => Operand::Bool(true),
                "false" => Operand::Bool(false),
                "null" => Operand::Null,
                _ => {
                    tracing::warn!(keyword = %k, "content: 取值位置出现关键字，已忽略");
                    continue;
                }
            },
        };
        return Ok(Some(v));
    }
}

// ---------------------------------------------------------------------------
// 内联图像（§8.9.7）
// ---------------------------------------------------------------------------

/// `BI` 已被消费；解析字典、`ID`、数据与 `EI`，返回时 `lex.pos` 停在 `EI` 之后。
fn parse_inline_image(lex: &mut Lexer<'_>, bi_start: usize) -> Result<InlineImage, ContentError> {
    let mut dict: Vec<(String, Operand)> = Vec::new();
    loop {
        let start = lex.pos;
        let Some(tok) = lex.next_token() else {
            return Err(ContentError::UnterminatedInlineImage { offset: bi_start });
        };
        match tok {
            Token::Keyword(ref k) if k == "ID" => break,
            Token::Keyword(ref k) if k == "EI" => {
                // 退化情形：没有 ID 段的空图像。
                tracing::warn!(offset = bi_start, "content: 内联图像缺少 ID 段");
                return Ok(InlineImage {
                    dict,
                    data: Vec::new(),
                });
            }
            Token::Name(key) => {
                let Some(value) = parse_value(lex, 1)? else {
                    return Err(ContentError::UnterminatedInlineImage { offset: bi_start });
                };
                dict.push((key, value));
            }
            _ => {
                tracing::warn!(
                    offset = start,
                    "content: 内联图像字典出现无效 token，已忽略"
                );
            }
        }
    }

    // §8.9.7：ID 后恰好一个空白字符，其后第一个字节即图像数据。
    // 部分生成器用 CRLF，这里两种起点都试。
    let after_id = lex.pos;
    let primary = if lex.at(after_id).is_some_and(is_white) {
        if lex.at(after_id) == Some(b'\r') && lex.at(after_id + 1) == Some(b'\n') {
            after_id + 2
        } else {
            after_id + 1
        }
    } else {
        after_id
    };

    let buf = lex.buf;
    let mut found: Option<(usize, usize, usize)> = None; // (数据起点, 数据终点, EI 终点)

    if let Some(len) = inline_data_len(&dict) {
        for ds in [primary, after_id + 1] {
            if ds > buf.len() {
                continue;
            }
            let Some(de) = ds.checked_add(len) else {
                continue;
            };
            if de > buf.len() {
                continue;
            }
            if let Some(ei_end) = ei_at(buf, de) {
                found = Some((ds, de, ei_end));
                break;
            }
        }
    }

    let (ds, de, ei_end) = match found {
        Some(t) => t,
        None => scan_for_ei(buf, primary)
            .ok_or(ContentError::UnterminatedInlineImage { offset: bi_start })?,
    };

    lex.pos = ei_end;
    Ok(InlineImage {
        dict,
        data: buf[ds..de].to_vec(),
    })
}

/// 从 `i` 起跳过空白后是否紧跟合法的 `EI`；是则返回 `EI` 之后的位置。
fn ei_at(buf: &[u8], mut i: usize) -> Option<usize> {
    while i < buf.len() && is_white(buf[i]) {
        i += 1;
    }
    if buf.len() < i + 2 || &buf[i..i + 2] != b"EI" {
        return None;
    }
    let after = i + 2;
    if after >= buf.len() || is_white(buf[after]) || is_delim(buf[after]) {
        Some(after)
    } else {
        None
    }
}

/// 无法推算长度时，按「空白 + EI + 边界 + 其后像正常内容」扫描。
fn scan_for_ei(buf: &[u8], data_start: usize) -> Option<(usize, usize, usize)> {
    let mut i = data_start;
    while i + 3 <= buf.len() {
        if is_white(buf[i]) && buf[i + 1] == b'E' && buf[i + 2] == b'I' {
            let after = i + 3;
            let boundary = after >= buf.len() || is_white(buf[after]) || is_delim(buf[after]);
            if boundary && looks_like_content(&buf[after..]) {
                return Some((data_start, i, after));
            }
        }
        i += 1;
    }
    None
}

/// `EI` 之后的字节看起来是否像正常内容流（全为可打印 ASCII 或空白）。
fn looks_like_content(rest: &[u8]) -> bool {
    let n = rest.len().min(16);
    rest[..n]
        .iter()
        .all(|&b| is_white(b) || (0x20..=0x7E).contains(&b))
}

/// 由内联图像字典推算数据字节数；无法确定时返回 `None`。
fn inline_data_len(dict: &[(String, Operand)]) -> Option<usize> {
    if let Some(v) = inline_dict_get(dict, "Length", "L").and_then(Operand::as_i64) {
        if v >= 0 {
            return usize::try_from(v).ok();
        }
    }
    // 有过滤器时数据已编码，长度不可由几何推算。
    if inline_dict_get(dict, "Filter", "F").is_some() {
        return None;
    }
    let w = u64::try_from(inline_dict_get(dict, "Width", "W")?.as_i64()?).ok()?;
    let h = u64::try_from(inline_dict_get(dict, "Height", "H")?.as_i64()?).ok()?;
    if w == 0 || h == 0 {
        return None;
    }
    let mask = inline_dict_get(dict, "ImageMask", "IM")
        .and_then(Operand::as_bool)
        .unwrap_or(false);
    let (bpc, ncomp) = if mask {
        (1u64, 1u64)
    } else {
        let bpc = inline_dict_get(dict, "BitsPerComponent", "BPC")
            .and_then(Operand::as_i64)
            .unwrap_or(8);
        let bpc = u64::try_from(bpc).ok()?;
        (
            bpc,
            color_components(inline_dict_get(dict, "ColorSpace", "CS"))?,
        )
    };
    if bpc == 0 || bpc > 32 {
        return None;
    }
    let bits = w.checked_mul(bpc)?.checked_mul(ncomp)?;
    let row = bits.div_ceil(8);
    usize::try_from(row.checked_mul(h)?).ok()
}

fn inline_dict_get<'d>(
    dict: &'d [(String, Operand)],
    long: &str,
    short: &str,
) -> Option<&'d Operand> {
    dict.iter()
        .find(|(k, _)| k == long || k == short)
        .map(|(_, v)| v)
}

/// 内联图像色彩空间的分量数；命名资源色彩空间无法确定，返回 `None`。
fn color_components(cs: Option<&Operand>) -> Option<u64> {
    match cs? {
        Operand::Name(n) => match n.as_str() {
            "G" | "DeviceGray" | "CalGray" => Some(1),
            "RGB" | "DeviceRGB" | "CalRGB" => Some(3),
            "CMYK" | "DeviceCMYK" => Some(4),
            "I" | "Indexed" => Some(1),
            _ => None,
        },
        Operand::Array(items) => match items.first() {
            Some(Operand::Name(n)) if n == "I" || n == "Indexed" => Some(1),
            _ => None,
        },
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// 序列化
// ---------------------------------------------------------------------------

/// 规范化序列化一条操作（不含前后分隔空白）。
///
/// 数字取最短合法写法：整数直出，实数最多保留 6 位小数并去掉尾随 0 与 `|x| < 1` 时的前导 0
/// （因此 `Real(12.0)` 会写成 `12`，数值等价）。字符串在全可打印 ASCII 时用 `(literal)`
/// 并转义 `\`、`(`、`)`，否则用 `<hex>`。
pub fn serialize_op(op: &Op) -> Vec<u8> {
    let mut out: Vec<u8> = Vec::with_capacity(32);
    if let Some(image) = &op.inline_image {
        write_inline_image(image, &mut out);
        return out;
    }
    for (i, operand) in op.operands.iter().enumerate() {
        if i > 0 {
            out.push(b' ');
        }
        write_operand(operand, &mut out);
    }
    if !op.operator.is_empty() {
        if !op.operands.is_empty() {
            out.push(b' ');
        }
        out.extend_from_slice(op.operator.as_bytes());
    }
    out
}

fn write_inline_image(image: &InlineImage, out: &mut Vec<u8>) {
    out.extend_from_slice(b"BI");
    for (k, v) in &image.dict {
        out.push(b' ');
        write_name(k, out);
        out.push(b' ');
        write_operand(v, out);
    }
    out.extend_from_slice(b"\nID ");
    out.extend_from_slice(&image.data);
    out.extend_from_slice(b"\nEI");
}

fn write_operand(v: &Operand, out: &mut Vec<u8>) {
    match v {
        Operand::Int(i) => out.extend_from_slice(i.to_string().as_bytes()),
        Operand::Real(f) => out.extend_from_slice(format_real(*f).as_bytes()),
        Operand::Bool(true) => out.extend_from_slice(b"true"),
        Operand::Bool(false) => out.extend_from_slice(b"false"),
        Operand::Null => out.extend_from_slice(b"null"),
        Operand::Name(n) => write_name(n, out),
        Operand::Str(s) => write_string(s, out),
        Operand::Array(items) => {
            out.push(b'[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(b' ');
                }
                write_operand(item, out);
            }
            out.push(b']');
        }
        Operand::Dict(entries) => {
            out.extend_from_slice(b"<<");
            for (i, (k, val)) in entries.iter().enumerate() {
                if i > 0 {
                    out.push(b' ');
                }
                write_name(k, out);
                out.push(b' ');
                write_operand(val, out);
            }
            out.extend_from_slice(b">>");
        }
    }
}

fn push_hex(b: u8, out: &mut Vec<u8>) {
    out.push(HEX_UPPER[usize::from(b >> 4)]);
    out.push(HEX_UPPER[usize::from(b & 0x0F)]);
}

fn write_name(n: &str, out: &mut Vec<u8>) {
    out.push(b'/');
    for &b in n.as_bytes() {
        if b != b'#' && (0x21..=0x7E).contains(&b) && is_regular(b) {
            out.push(b);
        } else {
            out.push(b'#');
            push_hex(b, out);
        }
    }
}

fn write_string(s: &[u8], out: &mut Vec<u8>) {
    if s.iter().all(|&b| (0x20..=0x7E).contains(&b)) {
        out.push(b'(');
        for &b in s {
            if matches!(b, b'(' | b')' | b'\\') {
                out.push(b'\\');
            }
            out.push(b);
        }
        out.push(b')');
    } else {
        out.push(b'<');
        for &b in s {
            push_hex(b, out);
        }
        out.push(b'>');
    }
}

/// PDF 实数的最短定点写法。非有限值与 `±0` 一律写成 `0`。
fn format_real(v: f64) -> String {
    if !v.is_finite() || v == 0.0 {
        return "0".to_string();
    }
    let mut s = format!("{v:.prec$}", prec = REAL_PRECISION);
    if s.contains('.') {
        while s.ends_with('0') {
            s.pop();
        }
        if s.ends_with('.') {
            s.pop();
        }
    }
    if s.is_empty() || s == "-" || s == "-0" || s == "0" {
        return "0".to_string();
    }
    if s.starts_with("0.") {
        s.remove(0);
    } else if s.starts_with("-0.") {
        s.remove(1);
    }
    s
}

// ---------------------------------------------------------------------------
// 写回
// ---------------------------------------------------------------------------

/// 若 `out` 末尾不是空白，补一个换行，避免序列化结果与相邻字节粘连。
fn ensure_separator(out: &mut Vec<u8>) {
    if out.last().is_some_and(|b| !is_white(*b)) {
        out.push(b'\n');
    }
}

/// 把 [`Op`] 序列写回内容流字节。
///
/// - `span` 非空、落在 `src` 内且未标脏 → 直接拷贝 `src[span]`；
/// - `span` 有效但已标脏 → 保留原位置前的空白/注释，写 [`serialize_op`] 结果；
/// - `span` 为空或越界（新插入的 Op）→ 补分隔后写序列化结果。
///
/// 两个 Op 之间未被任何 span 覆盖的字节（空白、注释、被丢弃的残留操作数）原样保留，
/// 因此 `ops` 全部未标脏时输出与 `src` 字节相等。
pub fn write_content(src: &[u8], ops: &[Op]) -> Vec<u8> {
    let mut out: Vec<u8> = Vec::with_capacity(src.len() + 64);
    let mut cursor: usize = 0;
    for op in ops {
        let usable = !op.span.is_empty() && op.span.end <= src.len() && op.span.start >= cursor;
        if usable {
            if op.dirty {
                out.extend_from_slice(&src[cursor..op.span.start]);
                ensure_separator(&mut out);
                out.extend_from_slice(&serialize_op(op));
            } else {
                out.extend_from_slice(&src[cursor..op.span.end]);
            }
            cursor = op.span.end;
        } else {
            ensure_separator(&mut out);
            out.extend_from_slice(&serialize_op(op));
            out.push(b'\n');
        }
    }
    if cursor < src.len() {
        out.extend_from_slice(&src[cursor..]);
    }
    out
}

// ---------------------------------------------------------------------------
// 单元测试
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn ops(src: &str) -> Vec<Op> {
        parse_content(src.as_bytes()).expect("解析应成功")
    }

    fn only(src: &str) -> Op {
        let mut v = ops(src);
        assert_eq!(v.len(), 1, "应只有一条操作：{v:?}");
        v.pop().unwrap()
    }

    fn ser(op: &Op) -> String {
        String::from_utf8_lossy(&serialize_op(op)).into_owned()
    }

    // --- 词法：数字 ---

    #[test]
    fn lex_number_forms() {
        let op = only("1 -2 +3 .5 -.5 6. 0 34.5 -3.62 +123.6 -.002 zz");
        assert_eq!(
            op.operands,
            vec![
                Operand::Int(1),
                Operand::Int(-2),
                Operand::Int(3),
                Operand::Real(0.5),
                Operand::Real(-0.5),
                Operand::Real(6.0),
                Operand::Int(0),
                Operand::Real(34.5),
                Operand::Real(-3.62),
                Operand::Real(123.6),
                Operand::Real(-0.002),
            ]
        );
    }

    #[test]
    fn lex_number_tolerates_garbage_and_overflow() {
        // 中途的符号被忽略；超出 i64 的整数退化为实数。
        let op = only("4-5 1.2.3 99999999999999999999 zz");
        assert_eq!(op.operands[0], Operand::Int(45));
        assert_eq!(op.operands[1], Operand::Real(1.23));
        assert!(matches!(op.operands[2], Operand::Real(_)));
    }

    // --- 词法：名字 ---

    #[test]
    fn lex_name_hash_escape() {
        let op = only("/A#20B /Lime#20Green /paired#28#29parentheses zz");
        assert_eq!(op.operands[0], Operand::Name("A B".into()));
        assert_eq!(op.operands[1], Operand::Name("Lime Green".into()));
        assert_eq!(op.operands[2], Operand::Name("paired()parentheses".into()));
    }

    #[test]
    fn lex_name_incomplete_hash_kept_literally() {
        // `#` 后不是两位十六进制时保留原字符。
        let op = only("/A#ZB /empty#4 zz");
        assert_eq!(op.operands[0], Operand::Name("A#ZB".into()));
        assert_eq!(op.operands[1], Operand::Name("empty#4".into()));
    }

    #[test]
    fn lex_empty_name() {
        let op = only("/ zz");
        assert_eq!(op.operands, vec![Operand::Name(String::new())]);
    }

    // --- 词法：字面串 ---

    #[test]
    fn lex_literal_string_nested_parens() {
        let op = only("((a)(b)c) zz");
        assert_eq!(op.operands[0], Operand::Str(b"(a)(b)c".to_vec()));
    }

    #[test]
    fn lex_literal_string_backslash_escapes() {
        let op = only(r"(a\nb\rc\td\be\f\(\)\\z) zz");
        assert_eq!(
            op.operands[0],
            Operand::Str(b"a\nb\rc\td\x08e\x0C()\\z".to_vec())
        );
    }

    #[test]
    fn lex_literal_string_octal() {
        let op = only(r"(\101\102\1\53\0053) zz");
        // \101=A \102=B \1=0x01 \53=0x2B('+') \005=0x05 然后字面 '3'
        assert_eq!(op.operands[0], Operand::Str(b"AB\x01+\x053".to_vec()));
    }

    #[test]
    fn lex_literal_string_line_continuation() {
        let op = only("(a\\\nb\\\r\nc) zz");
        assert_eq!(op.operands[0], Operand::Str(b"abc".to_vec()));
    }

    #[test]
    fn lex_literal_string_eol_normalised_to_lf() {
        let op = only("(a\rb\r\nc\nd) zz");
        assert_eq!(op.operands[0], Operand::Str(b"a\nb\nc\nd".to_vec()));
    }

    #[test]
    fn lex_literal_string_unknown_escape_drops_backslash() {
        let op = only(r"(\q\/) zz");
        assert_eq!(op.operands[0], Operand::Str(b"q/".to_vec()));
    }

    #[test]
    fn lex_unterminated_strings_are_tolerated() {
        // 未闭合的串吞到流尾：没有后继操作符，因此不产出 Op，但也不报错。
        let v = ops("q (abc");
        assert_eq!(v.len(), 1);
        assert_eq!(v[0].operator, "q");
        let v2 = ops("q <4865");
        assert_eq!(v2.len(), 1);
        assert_eq!(v2[0].operator, "q");
    }

    // --- 词法：十六进制串 ---

    #[test]
    fn lex_hex_string_basic() {
        let op = only("<48656C6C6F> zz");
        assert_eq!(op.operands[0], Operand::Str(b"Hello".to_vec()));
    }

    #[test]
    fn lex_hex_string_odd_digit_padded() {
        let op = only("<901FA> <4> zz");
        assert_eq!(op.operands[0], Operand::Str(vec![0x90, 0x1F, 0xA0]));
        assert_eq!(op.operands[1], Operand::Str(vec![0x40]));
    }

    #[test]
    fn lex_hex_string_ignores_whitespace_and_junk() {
        let op = only("<48 65\n6C\t6C-6F> zz");
        assert_eq!(op.operands[0], Operand::Str(b"Hello".to_vec()));
    }

    #[test]
    fn lex_empty_hex_and_literal_strings() {
        let op = only("<> () zz");
        assert_eq!(op.operands[0], Operand::Str(Vec::new()));
        assert_eq!(op.operands[1], Operand::Str(Vec::new()));
    }

    // --- 词法：注释与空白 ---

    #[test]
    fn lex_comments_are_skipped() {
        let op = only("% 整行注释\n1 % 行尾注释\r\n2 zz");
        assert_eq!(op.operands, vec![Operand::Int(1), Operand::Int(2)]);
    }

    #[test]
    fn lex_all_whitespace_bytes() {
        let op = only("1\x002\t3\n4\x0C5\r6 zz");
        assert_eq!(op.operands.len(), 6);
    }

    #[test]
    fn lex_empty_input() {
        assert!(parse_content(b"").unwrap().is_empty());
        assert!(parse_content("   \n\t % 只有注释".as_bytes())
            .unwrap()
            .is_empty());
    }

    // --- 语法：复合对象 ---

    #[test]
    fn parse_array_operand() {
        let op = only("[(A) -100 (B) 3.5] TJ");
        assert_eq!(op.operator, "TJ");
        assert_eq!(
            op.operands,
            vec![Operand::Array(vec![
                Operand::Str(b"A".to_vec()),
                Operand::Int(-100),
                Operand::Str(b"B".to_vec()),
                Operand::Real(3.5),
            ])]
        );
    }

    #[test]
    fn parse_nested_array_and_dict() {
        let op = only("[[1 2] [<</K (v)>>]] zz");
        let outer = op.operands[0].as_array().unwrap();
        assert_eq!(outer.len(), 2);
        let inner = outer[1].as_array().unwrap();
        assert_eq!(
            inner[0].as_dict().unwrap(),
            &[("K".to_string(), Operand::Str(b"v".to_vec()))]
        );
    }

    #[test]
    fn parse_inline_dict_operand() {
        let op = only("/P <</MCID 0 /Lang (en-US) /A [1 2]>> BDC");
        assert_eq!(op.operator, "BDC");
        assert_eq!(op.operands[0], Operand::Name("P".into()));
        let d = op.operands[1].as_dict().unwrap();
        assert_eq!(d[0], ("MCID".to_string(), Operand::Int(0)));
        assert_eq!(d[1].0, "Lang");
        assert_eq!(d[2].0, "A");
    }

    #[test]
    fn parse_bool_and_null_operands() {
        let op = only("true false null zz");
        assert_eq!(
            op.operands,
            vec![Operand::Bool(true), Operand::Bool(false), Operand::Null]
        );
    }

    #[test]
    fn parse_nesting_depth_limit() {
        let deep = format!(
            "{}{} zz",
            "[".repeat(MAX_DEPTH + 5),
            "]".repeat(MAX_DEPTH + 5)
        );
        assert!(matches!(
            parse_content(deep.as_bytes()),
            Err(ContentError::NestingTooDeep { .. })
        ));
    }

    // --- 语法：容错 ---

    #[test]
    fn parse_unknown_operator_keeps_all_operands() {
        let op = only("1 2 3 4 5 6 7 zzQQ");
        assert_eq!(op.operator, "zzQQ");
        assert_eq!(op.operands.len(), 7);
    }

    #[test]
    fn parse_operand_residue_cleared_before_operator() {
        let op = only("9 9 9 /F1 12 Tf");
        assert_eq!(op.operator, "Tf");
        assert_eq!(
            op.operands,
            vec![Operand::Name("F1".into()), Operand::Int(12)]
        );
        // span 从被保留的首个操作数开始。
        assert_eq!(&"9 9 9 /F1 12 Tf"[op.span.clone()], "/F1 12 Tf");
    }

    #[test]
    fn parse_stray_delimiters_ignored() {
        let op = only(") ] >> 1 2 zz");
        assert_eq!(op.operands, vec![Operand::Int(1), Operand::Int(2)]);
    }

    #[test]
    fn parse_trailing_operands_dropped() {
        let v = ops("q Q 1 2 3");
        assert_eq!(v.len(), 2);
        assert_eq!(v[0].operator, "q");
        assert_eq!(v[1].operator, "Q");
    }

    // --- span ---

    #[test]
    fn spans_are_tight_and_monotonic() {
        let src = "BT\n  /F1 12 Tf\n  (hi) Tj\nET";
        let v = ops(src);
        assert_eq!(v.len(), 4);
        assert_eq!(&src[v[0].span.clone()], "BT");
        assert_eq!(&src[v[1].span.clone()], "/F1 12 Tf");
        assert_eq!(&src[v[2].span.clone()], "(hi) Tj");
        assert_eq!(&src[v[3].span.clone()], "ET");
        for w in v.windows(2) {
            assert!(w[0].span.end <= w[1].span.start);
        }
        assert!(v.iter().all(|o| !o.dirty));
    }

    // --- 内联图像 ---

    #[test]
    fn inline_image_length_from_geometry_survives_ei_in_data() {
        // W=4 H=2 BPC=8 CS=/G → 8 字节数据，数据里故意含 " EI "。
        let mut src: Vec<u8> = Vec::new();
        src.extend_from_slice(b"BI /W 4 /H 2 /BPC 8 /CS /G ID ");
        src.extend_from_slice(b" EI \x00\xffab");
        src.extend_from_slice(b"\nEI\nQ");
        let v = parse_content(&src).unwrap();
        assert_eq!(v.len(), 2);
        let img = v[0].inline_image.as_ref().unwrap();
        assert_eq!(img.data, b" EI \x00\xffab".to_vec());
        assert_eq!(img.get("Width", "W").unwrap().as_i64(), Some(4));
        assert_eq!(v[1].operator, "Q");
        assert!(v[0].is_inline_image());
    }

    #[test]
    fn inline_image_image_mask_length() {
        // IM true, W=16 H=2 → 每行 2 字节，共 4 字节。
        let mut src: Vec<u8> = Vec::new();
        src.extend_from_slice(b"BI /IM true /W 16 /H 2 /D [1 0] ID ");
        src.extend_from_slice(b"\x01\x02\x03\x04");
        src.extend_from_slice(b" EI Q");
        let v = parse_content(&src).unwrap();
        let img = v[0].inline_image.as_ref().unwrap();
        assert_eq!(img.data, vec![1, 2, 3, 4]);
        assert_eq!(v[1].operator, "Q");
    }

    #[test]
    fn inline_image_with_filter_falls_back_to_scan() {
        let src = b"q BI /W 4 /H 4 /F /AHx ID 0011223344>\nEI\nQ";
        let v = parse_content(src).unwrap();
        assert_eq!(v.len(), 3);
        assert_eq!(v[0].operator, "q");
        assert_eq!(v[1].operator, "BI");
        assert_eq!(
            v[1].inline_image.as_ref().unwrap().data,
            b"0011223344>".to_vec()
        );
        assert_eq!(v[2].operator, "Q");
    }

    #[test]
    fn inline_image_missing_ei_is_error() {
        let src = b"BI /W 4 /H 4 /F /AHx ID 00112233";
        assert!(matches!(
            parse_content(src),
            Err(ContentError::UnterminatedInlineImage { .. })
        ));
    }

    #[test]
    fn inline_image_roundtrips_byte_for_byte() {
        let mut src: Vec<u8> = Vec::new();
        src.extend_from_slice(b"q 1 0 0 1 0 0 cm\nBI /W 2 /H 2 /BPC 8 /CS /RGB ID ");
        src.extend_from_slice(&[0u8, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
        src.extend_from_slice(b"\nEI\nQ\n");
        let v = parse_content(&src).unwrap();
        assert_eq!(write_content(&src, &v), src);
    }

    #[test]
    fn inline_image_serializes_to_parsable_form() {
        let src = b"BI /W 2 /H 1 /BPC 8 /CS /G ID \x01\x02 EI";
        let v = parse_content(src).unwrap();
        let bytes = serialize_op(&v[0]);
        let again = parse_content(&bytes).unwrap();
        assert_eq!(again.len(), 1);
        assert_eq!(
            again[0].inline_image.as_ref().unwrap().data,
            v[0].inline_image.as_ref().unwrap().data
        );
    }

    // --- Op 辅助 ---

    #[test]
    fn text_show_helpers() {
        let tj = only("(hello) Tj");
        assert!(tj.is_text_show());
        assert_eq!(tj.text_strings(), vec![b"hello".as_slice()]);

        let tj_array = only("[(A) -250 (B) 10 (C)] TJ");
        assert!(tj_array.is_text_show());
        assert_eq!(
            tj_array.text_strings(),
            vec![b"A".as_slice(), b"B".as_slice(), b"C".as_slice()]
        );

        let quote = only("(x) '");
        assert!(quote.is_text_show());
        assert_eq!(quote.text_strings(), vec![b"x".as_slice()]);

        let dquote = only("1 2 (y) \"");
        assert!(dquote.is_text_show());
        assert_eq!(dquote.text_strings(), vec![b"y".as_slice()]);

        let other = only("/F1 12 Tf");
        assert!(!other.is_text_show());
        assert!(other.text_strings().is_empty());
    }

    #[test]
    fn text_state_helpers() {
        for src in [
            "/F1 12 Tf",
            "1 0 0 1 0 0 Tm",
            "1 2 Td",
            "1 2 TD",
            "T*",
            "14 TL",
            "0 Tc",
            "0 Tw",
            "100 Tz",
            "0 Ts",
            "0 Tr",
        ] {
            assert!(only(src).is_text_state(), "{src} 应是文本状态操作");
        }
        assert!(!only("(a) Tj").is_text_state());
        assert!(!only("q").is_text_state());
    }

    #[test]
    fn op_source_slice() {
        let src = b"BT (a) Tj ET";
        let v = parse_content(src).unwrap();
        assert_eq!(v[1].source(src), Some(b"(a) Tj".as_slice()));
        assert_eq!(Op::new("Tj", Vec::new()).source(src), None);
    }

    // --- 序列化 ---

    #[test]
    fn serialize_numbers_shortest() {
        assert_eq!(
            ser(&Op::new(
                "Td",
                vec![Operand::Real(0.5), Operand::Real(-0.5)]
            )),
            ".5 -.5 Td"
        );
        assert_eq!(
            ser(&Op::new(
                "zz",
                vec![
                    Operand::Real(12.0),
                    Operand::Real(-3.625),
                    Operand::Int(-7),
                    Operand::Real(0.0),
                    Operand::Real(f64::NAN),
                ]
            )),
            "12 -3.625 -7 0 0 zz"
        );
    }

    #[test]
    fn serialize_strings_and_names() {
        assert_eq!(
            ser(&Op::new("Tj", vec![Operand::Str(b"a(b)c\\d".to_vec())])),
            r"(a\(b\)c\\d) Tj"
        );
        assert_eq!(
            ser(&Op::new("Tj", vec![Operand::Str(vec![0x00, 0xFF])])),
            "<00FF> Tj"
        );
        assert_eq!(
            ser(&Op::new("zz", vec![Operand::Name("A B#C".into())])),
            "/A#20B#23C zz"
        );
    }

    #[test]
    fn serialize_composites_and_bare_operator() {
        assert_eq!(
            ser(&Op::new(
                "TJ",
                vec![Operand::Array(vec![
                    Operand::Str(b"A".to_vec()),
                    Operand::Int(-250),
                    Operand::Array(vec![Operand::Bool(true), Operand::Null]),
                ])]
            )),
            "[(A) -250 [true null]] TJ"
        );
        assert_eq!(
            ser(&Op::new(
                "BDC",
                vec![
                    Operand::Name("P".into()),
                    Operand::Dict(vec![
                        ("MCID".into(), Operand::Int(0)),
                        ("Lang".into(), Operand::Str(b"en".to_vec())),
                    ]),
                ]
            )),
            "/P <</MCID 0 /Lang (en)>> BDC"
        );
        assert_eq!(ser(&Op::new("q", Vec::new())), "q");
    }

    #[test]
    fn serialize_then_parse_is_stable() {
        let src = "BT /F1 12 Tf 1 0 0 1 72 720 Tm [(A) -250 (B)] TJ ET";
        let v = ops(src);
        let rendered: Vec<u8> = v
            .iter()
            .flat_map(|o| {
                let mut b = serialize_op(o);
                b.push(b'\n');
                b
            })
            .collect();
        let again = parse_content(&rendered).unwrap();
        assert_eq!(v.len(), again.len());
        for (a, b) in v.iter().zip(&again) {
            assert_eq!(a.operator, b.operator);
            assert_eq!(a.operands, b.operands);
        }
    }

    // --- write_content ---

    #[test]
    fn write_content_is_identity_when_clean() {
        let src = concat!(
            "%PDF content\r\n",
            "q 1 0 0 1 0 0 cm\n",
            "BT/F1 12 Tf 72 720 Td[(Hello)-250(World)]TJ ET\r",
            "0.5 .25 0 rg <48656C> Tj\n",
            "/Fm0 Do Q\n",
            "  % 尾部注释\n"
        );
        let v = ops(src);
        assert!(v.len() > 8);
        assert_eq!(write_content(src.as_bytes(), &v), src.as_bytes());
    }

    #[test]
    fn write_content_reserializes_dirty_op_only() {
        let src = "BT /F1 12 Tf (old) Tj ET";
        let mut v = ops(src);
        let idx = v.iter().position(Op::is_text_show).unwrap();
        v[idx].operands = vec![Operand::Str(b"new".to_vec())];
        v[idx].mark_dirty();
        let out = write_content(src.as_bytes(), &v);
        assert_eq!(String::from_utf8_lossy(&out), "BT /F1 12 Tf (new) Tj ET");
    }

    #[test]
    fn write_content_separates_dirty_op_with_no_leading_space() {
        let src = "BT/F1 12 Tf ET";
        let mut v = ops(src);
        v[0].mark_dirty();
        let out = write_content(src.as_bytes(), &v);
        assert_eq!(String::from_utf8_lossy(&out), "BT/F1 12 Tf ET");
        // 让第二条脏，它前面没有空白，应自动补换行
        let mut v2 = ops(src);
        v2[1].mark_dirty();
        let out2 = write_content(src.as_bytes(), &v2);
        assert_eq!(String::from_utf8_lossy(&out2), "BT\n/F1 12 Tf ET");
    }

    #[test]
    fn write_content_inserts_new_ops() {
        let src = "BT ET";
        let mut v = ops(src);
        v.insert(
            1,
            Op::new("Tf", vec![Operand::Name("F1".into()), Operand::Int(9)]),
        );
        let out = write_content(src.as_bytes(), &v);
        assert_eq!(String::from_utf8_lossy(&out), "BT\n/F1 9 Tf\n ET");
        // 空源 + 全新 Op
        let fresh = write_content(b"", &[Op::new("q", Vec::new()), Op::new("Q", Vec::new())]);
        assert_eq!(String::from_utf8_lossy(&fresh), "q\nQ\n");
    }

    #[test]
    fn write_content_preserves_trailing_bytes_and_empty_ops() {
        let src = b"q Q\n% trailing\n";
        assert_eq!(write_content(src, &[]), src.to_vec());
        let v = parse_content(src).unwrap();
        assert_eq!(write_content(src, &v), src.to_vec());
    }

    #[test]
    fn write_content_ignores_out_of_range_spans() {
        let src = b"q";
        let bogus = Op {
            operator: "Q".to_string(),
            operands: Vec::new(),
            span: 100..200,
            inline_image: None,
            dirty: false,
        };
        let out = write_content(src, &[bogus]);
        assert_eq!(String::from_utf8_lossy(&out), "Q\nq");
    }

    #[test]
    fn format_real_edge_cases() {
        assert_eq!(format_real(0.0), "0");
        assert_eq!(format_real(-0.0), "0");
        assert_eq!(format_real(f64::INFINITY), "0");
        assert_eq!(format_real(1e-9), "0");
        assert_eq!(format_real(-0.125), "-.125");
        assert_eq!(format_real(100.0), "100");
        assert_eq!(format_real(1.5), "1.5");
    }

    #[test]
    fn operand_accessors() {
        assert_eq!(Operand::Int(3).as_f64(), Some(3.0));
        assert_eq!(Operand::Real(3.0).as_i64(), Some(3));
        assert_eq!(Operand::Real(3.5).as_i64(), None);
        assert_eq!(Operand::Bool(true).as_bool(), Some(true));
        assert_eq!(Operand::Name("X".into()).as_name(), Some("X"));
        assert_eq!(
            Operand::Str(b"x".to_vec()).as_bytes(),
            Some(b"x".as_slice())
        );
        assert!(Operand::Null.as_f64().is_none());
        assert!(Operand::Int(1).is_number());
        assert!(!Operand::Null.is_number());
    }
}
