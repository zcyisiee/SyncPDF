//! Paragraph-wide Knuth–Plass line breaking over measured, indivisible boxes.
//!
//! A `Box` is atomic. A `Glue` contributes its natural width and adjustment only
//! when it lies between drawn boxes; a break at glue discards that glue. An
//! unchosen `Penalty` contributes no width. At a chosen penalty its width is
//! appended to the line (for example, a discretionary hyphen), but the penalty
//! itself is outside `Line::start..Line::end`. Renderers must inspect `break_at`
//! to draw that appended item. The line range trims leading/trailing glue and
//! penalties; internal penalties in the range are nondrawing nodes.
//!
//! `widths[0]` is the first-line measure and can encode first-line indentation.
//! Later lines use `widths[min(line_index, widths.len() - 1)]`. `tolerance` is the
//! maximum absolute glue adjustment ratio; shrinking is additionally bounded
//! by -1. A nonfinal, nonmandatory line with no adjustable glue is feasible only
//! at its exact natural width. Final and mandatory lines are ragged left with
//! ratio zero and must fit at natural width, without shrinking boxes or glue.
//!
//! Badness is `100 * abs(ratio)^3`. A line costs `(10 + badness + penalty)^2`
//! for nonnegative penalties, or `(10 + badness)^2 - penalty^2` for negative
//! penalties. Forced penalties (cost <= -10000) have zero penalty cost; forbidden
//! penalties (cost >= 10000) are never breaks. Adjacent fitness classes differing
//! by more than one add 100 demerits, as do consecutive flagged penalties.
//! Fitness classes are tight (`ratio < -0.5`), decent (`<= 0.5`), loose
//! (`<= 1`), and very loose. Ties retain the first predecessor in node order.
//! Complexity is O(breaks² × min(widths.len(), boxes) × 4), with linear
//! storage in the number of breaks and retained width slots.

use thiserror::Error;

/// A measured, indivisible item or a legal break opportunity.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Node {
    Box {
        width: f32,
    },
    Glue {
        width: f32,
        stretch: f32,
        shrink: f32,
    },
    Penalty {
        width: f32,
        cost: i32,
        flagged: bool,
    },
}

/// A line's drawable half-open node range and selected break.
#[derive(Clone, Debug, PartialEq)]
pub struct Line {
    pub start: usize,
    pub end: usize,
    /// Index of the selected glue/penalty, or `nodes.len()` at implicit end.
    pub break_at: usize,
    pub ratio: f32,
}

/// Minimum-demerit sequence for the complete paragraph.
#[derive(Clone, Debug, PartialEq)]
pub struct Solution {
    pub lines: Vec<Line>,
    pub demerits: f64,
}

#[derive(Clone, Copy, Debug, Error, PartialEq, Eq)]
pub enum BreakError {
    #[error("invalid Knuth–Plass input: {0}")]
    InvalidInput(&'static str),
    #[error("paragraph has no feasible line-break sequence")]
    NoSolution,
}

#[derive(Clone, Copy, Debug)]
struct Breakpoint {
    at: usize,
    next: usize,
    penalty: i32,
    append_width: f64,
    flagged: bool,
    mandatory: bool,
}

#[derive(Clone, Debug)]
struct State {
    demerits: f64,
    prev: Option<(usize, usize, usize)>,
    line: Line,
}

/// Find the paragraph-wide minimum-demerit break sequence.
///
/// Empty nodes return an empty solution after validating widths and tolerance.
/// All dimensions must be finite and nonnegative; line widths must be positive.
/// A nonempty paragraph with no drawable box, or one that cannot fit without
/// violating its fixed box widths, returns [`BreakError::NoSolution`].
pub fn solve(nodes: &[Node], widths: &[f32], tolerance: f32) -> Result<Solution, BreakError> {
    validate(nodes, widths, tolerance)?;
    if nodes.is_empty() {
        return Ok(Solution {
            lines: Vec::new(),
            demerits: 0.0,
        });
    }

    let n = nodes.len();
    let mut natural = vec![0.0_f64; n + 1];
    let mut stretch = vec![0.0_f64; n + 1];
    let mut shrink = vec![0.0_f64; n + 1];
    let mut box_count = vec![0_usize; n + 1];
    let mut forced_count = vec![0_usize; n + 1];
    let mut next_box = vec![n; n + 1];
    let mut last_box = vec![None; n + 1];
    let mut breaks = Vec::new();
    for (i, node) in nodes.iter().enumerate() {
        natural[i + 1] = natural[i];
        stretch[i + 1] = stretch[i];
        shrink[i + 1] = shrink[i];
        box_count[i + 1] = box_count[i];
        forced_count[i + 1] = forced_count[i];
        last_box[i + 1] = last_box[i];
        match *node {
            Node::Box { width } => {
                natural[i + 1] += f64::from(width);
                box_count[i + 1] += 1;
                last_box[i + 1] = Some(i);
            }
            Node::Glue {
                width,
                stretch: plus,
                shrink: minus,
            } => {
                natural[i + 1] += f64::from(width);
                stretch[i + 1] += f64::from(plus);
                shrink[i + 1] += f64::from(minus);
                breaks.push(Breakpoint {
                    at: i,
                    next: i + 1,
                    penalty: 0,
                    append_width: 0.0,
                    flagged: false,
                    mandatory: false,
                });
            }
            Node::Penalty {
                width,
                cost,
                flagged,
            } => {
                let mandatory = cost <= -10_000;
                forced_count[i + 1] += usize::from(mandatory);
                if cost < 10_000 {
                    breaks.push(Breakpoint {
                        at: i,
                        next: i + 1,
                        penalty: cost,
                        append_width: f64::from(width),
                        flagged,
                        mandatory,
                    });
                }
            }
        }
    }
    if box_count[n] == 0 {
        return Err(BreakError::NoSolution);
    }
    for i in (0..n).rev() {
        next_box[i] = if matches!(nodes[i], Node::Box { .. }) {
            i
        } else {
            next_box[i + 1]
        };
    }
    breaks.push(Breakpoint {
        at: n,
        next: n,
        penalty: 0,
        append_width: 0.0,
        flagged: false,
        mandatory: false,
    });

    // Different line counts at one breakpoint can see different future widths.
    // Once the last width is reached, all later counts have the same measure.
    let slots = widths.len().min(box_count[n] + 1);
    let mut states: Vec<Vec<[Option<State>; 4]>> = (0..breaks.len())
        .map(|_| (0..slots).map(|_| std::array::from_fn(|_| None)).collect())
        .collect();
    let mut terminal: Option<(f64, usize, usize, usize)> = None;
    for (bi, point) in breaks.iter().enumerate() {
        let (previous_states, current_and_later) = states.split_at_mut(bi);
        let current = &mut current_and_later[0];
        let breakpoints = &breaks;
        let candidates = std::iter::once((None, 0, 0.0, None, false, 0)).chain(
            previous_states
                .iter()
                .enumerate()
                .flat_map(|(pi, by_slot)| {
                    by_slot.iter().enumerate().flat_map(move |(slot, classes)| {
                        classes
                            .iter()
                            .enumerate()
                            .filter_map(move |(fitness, state)| {
                                state.as_ref().map(|state| {
                                    (
                                        Some((pi, slot, fitness)),
                                        breakpoints[pi].next,
                                        state.demerits,
                                        Some(fitness),
                                        breakpoints[pi].flagged,
                                        slot,
                                    )
                                })
                            })
                    })
                }),
        );
        for (prev, from, previous_demerits, previous_fitness, previous_flagged, slot) in candidates
        {
            if from >= point.at && point.at != n {
                continue;
            }
            if forced_count[point.at] != forced_count[from] {
                continue;
            }
            let start = next_box[from];
            let Some(last) = last_box[point.at] else {
                continue;
            };
            if start > last {
                continue;
            }
            let end = last + 1;
            let target = f64::from(widths[slot]);
            let body_width = natural[end] - natural[start] + point.append_width;
            let is_terminal = box_count[n] == box_count[point.next]
                && forced_count[n] == forced_count[point.next];
            let ragged = point.mandatory || is_terminal;
            let ratio = if ragged {
                if body_width > target {
                    continue;
                }
                0.0
            } else {
                let delta = target - body_width;
                let adjust = if delta >= 0.0 {
                    stretch[end] - stretch[start]
                } else {
                    shrink[end] - shrink[start]
                };
                if adjust == 0.0 {
                    if delta != 0.0 {
                        continue;
                    }
                    0.0
                } else {
                    delta / adjust
                }
            };
            if !ratio.is_finite() || ratio.abs() > f64::from(tolerance) || ratio < -1.0 {
                continue;
            }
            let fitness = fitness_class(ratio);
            let badness = 100.0 * ratio.abs().powi(3);
            let base = 10.0 + badness;
            let penalty = if point.mandatory {
                0.0
            } else {
                f64::from(point.penalty)
            };
            let line_demerits = if penalty >= 0.0 {
                (base + penalty).powi(2)
            } else {
                base.powi(2) - penalty.powi(2)
            };
            let fitness_demerits = previous_fitness.map_or(0.0, |old| {
                if old.abs_diff(fitness) > 1 {
                    100.0
                } else {
                    0.0
                }
            });
            let flagged_demerits = if previous_flagged && point.flagged {
                100.0
            } else {
                0.0
            };
            let score = previous_demerits + line_demerits + fitness_demerits + flagged_demerits;
            if !score.is_finite() {
                continue;
            }
            let line = Line {
                start,
                end,
                break_at: point.at,
                ratio: ratio as f32,
            };
            let next_slot = (slot + 1).min(slots - 1);
            if current[next_slot][fitness]
                .as_ref()
                .is_none_or(|existing| score < existing.demerits)
            {
                current[next_slot][fitness] = Some(State {
                    demerits: score,
                    prev,
                    line,
                });
            }
        }
        if box_count[n] == box_count[point.next] && forced_count[n] == forced_count[point.next] {
            for (slot, classes) in current.iter().enumerate() {
                for (fitness, state) in classes.iter().enumerate() {
                    if let Some(state) = state {
                        if terminal.is_none_or(|(best, _, _, _)| state.demerits < best) {
                            terminal = Some((state.demerits, bi, slot, fitness));
                        }
                    }
                }
            }
        }
    }
    let Some((demerits, mut bi, mut slot, mut fitness)) = terminal else {
        return Err(BreakError::NoSolution);
    };
    let mut lines = Vec::new();
    loop {
        let state = states[bi][slot][fitness]
            .as_ref()
            .expect("selected DP state exists");
        lines.push(state.line.clone());
        if let Some((pi, next_slot, fi)) = state.prev {
            bi = pi;
            slot = next_slot;
            fitness = fi;
        } else {
            break;
        }
    }
    lines.reverse();
    Ok(Solution { lines, demerits })
}

fn fitness_class(ratio: f64) -> usize {
    if ratio < -0.5 {
        0
    } else if ratio <= 0.5 {
        1
    } else if ratio <= 1.0 {
        2
    } else {
        3
    }
}

fn validate(nodes: &[Node], widths: &[f32], tolerance: f32) -> Result<(), BreakError> {
    if widths.is_empty()
        || widths
            .iter()
            .any(|width| !width.is_finite() || *width <= 0.0)
    {
        return Err(BreakError::InvalidInput(
            "widths must be finite and positive",
        ));
    }
    if !tolerance.is_finite() || tolerance < 0.0 {
        return Err(BreakError::InvalidInput(
            "tolerance must be finite and nonnegative",
        ));
    }
    for node in nodes {
        let valid = match *node {
            Node::Box { width } | Node::Penalty { width, .. } => width.is_finite() && width >= 0.0,
            Node::Glue {
                width,
                stretch,
                shrink,
            } => [width, stretch, shrink]
                .into_iter()
                .all(|value| value.is_finite() && value >= 0.0),
        };
        if !valid {
            return Err(BreakError::InvalidInput(
                "node dimensions must be finite and nonnegative",
            ));
        }
    }
    Ok(())
}
