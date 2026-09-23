use syncpdf_typeset::knuth_plass::{solve, BreakError, Line, Node};

fn b(width: f32) -> Node {
    Node::Box { width }
}

fn g(width: f32, stretch: f32, shrink: f32) -> Node {
    Node::Glue {
        width,
        stretch,
        shrink,
    }
}

fn p(width: f32, cost: i32, flagged: bool) -> Node {
    Node::Penalty {
        width,
        cost,
        flagged,
    }
}

// Intentionally exhaustive and independent of the production DP: enumerate
// every short break sequence, measure its boxes and glue directly, then score it.
fn exhaustive_oracle(nodes: &[Node], widths: &[f32], tolerance: f32) -> Option<(f64, Vec<usize>)> {
    let candidates: Vec<usize> = (0..nodes.len())
        .filter(|&i| match nodes[i] {
            Node::Glue { .. } => true,
            Node::Penalty { cost, .. } => cost < 10_000,
            Node::Box { .. } => false,
        })
        .chain(std::iter::once(nodes.len()))
        .collect();
    struct Oracle<'a> {
        nodes: &'a [Node],
        widths: &'a [f32],
        tolerance: f32,
        candidates: &'a [usize],
    }
    impl Oracle<'_> {
        fn visit(
            &self,
            from: usize,
            line_number: usize,
            prior_fitness: Option<usize>,
            prior_flagged: bool,
            score: f64,
            path: &mut Vec<usize>,
            best: &mut Option<(f64, Vec<usize>)>,
        ) {
            let Self {
                nodes,
                widths,
                tolerance,
                candidates,
            } = *self;
            for &at in candidates {
                if at < from || (at == from && at != nodes.len()) {
                    continue;
                }
                if nodes[from..at]
                    .iter()
                    .any(|n| matches!(n, Node::Penalty { cost, .. } if *cost <= -10_000))
                {
                    continue;
                }
                let box_positions: Vec<usize> = (from..at)
                    .filter(|&i| matches!(nodes[i], Node::Box { .. }))
                    .collect();
                let (Some(&start), Some(&last)) = (box_positions.first(), box_positions.last())
                else {
                    continue;
                };
                let mut natural = 0.0;
                let mut stretch = 0.0;
                let mut shrink = 0.0;
                for node in &nodes[start..=last] {
                    match *node {
                        Node::Box { width } => natural += f64::from(width),
                        Node::Glue {
                            width,
                            stretch: plus,
                            shrink: minus,
                        } => {
                            natural += f64::from(width);
                            stretch += f64::from(plus);
                            shrink += f64::from(minus);
                        }
                        Node::Penalty { .. } => {}
                    }
                }
                let (penalty, append_width, flagged, mandatory) = match nodes.get(at) {
                    Some(Node::Penalty {
                        width,
                        cost,
                        flagged,
                    }) => (*cost, f64::from(*width), *flagged, *cost <= -10_000),
                    _ => (0, 0.0, false, false),
                };
                natural += append_width;
                let later = &nodes[at.min(nodes.len())..];
                let terminal = !later
                    .iter()
                    .skip(usize::from(at < nodes.len()))
                    .any(|node| {
                        matches!(node, Node::Box { .. })
                            || matches!(node, Node::Penalty { cost, .. } if *cost <= -10_000)
                    });
                let width = f64::from(widths[line_number.min(widths.len() - 1)]);
                let single_word_ragged = !mandatory
                    && !terminal
                    && matches!(nodes.get(at), Some(Node::Penalty { .. }))
                    && stretch == 0.0
                    && shrink == 0.0
                    && natural <= width;
                let ratio = if mandatory || terminal || single_word_ragged {
                    if natural > width {
                        continue;
                    }
                    0.0
                } else if natural < width {
                    if stretch == 0.0 {
                        continue;
                    }
                    (width - natural) / stretch
                } else if natural > width {
                    if shrink == 0.0 {
                        continue;
                    }
                    (width - natural) / shrink
                } else {
                    0.0
                };
                if ratio.abs() > f64::from(tolerance) || ratio < -1.0 {
                    continue;
                }
                let fitness = if ratio < -0.5 {
                    0
                } else if ratio <= 0.5 {
                    1
                } else if ratio <= 1.0 {
                    2
                } else {
                    3
                };
                let badness_ratio = if single_word_ragged {
                    (width - natural) / width
                } else {
                    ratio.abs()
                };
                let base = 10.0 + 100.0 * badness_ratio.powi(3);
                let cost = if mandatory { 0.0 } else { f64::from(penalty) };
                let mut extra = if cost >= 0.0 {
                    base.powi(2) + cost.powi(2)
                } else {
                    base.powi(2) - cost.powi(2)
                };
                if prior_fitness.is_some_and(|old| old.abs_diff(fitness) > 1) {
                    extra += 100.0;
                }
                if prior_flagged && flagged {
                    extra += 100.0;
                }
                path.push(at);
                if terminal {
                    let total = score + extra;
                    if best.as_ref().is_none_or(|(old, _)| total < *old) {
                        *best = Some((total, path.clone()));
                    }
                } else if at < nodes.len() {
                    self.visit(
                        at + 1,
                        line_number + 1,
                        Some(fitness),
                        flagged,
                        score + extra,
                        path,
                        best,
                    );
                }
                path.pop();
            }
        }
    }
    let mut best: Option<(f64, Vec<usize>)> = None;
    Oracle {
        nodes,
        widths,
        tolerance,
        candidates: &candidates,
    }
    .visit(0, 0, None, false, 0.0, &mut Vec::new(), &mut best);
    best
}

#[test]
fn empty_and_invalid_inputs() {
    assert_eq!(solve(&[], &[20.0], 1.0).unwrap().lines, Vec::<Line>::new());
    for widths in [&[][..], &[0.0][..], &[f32::NAN][..], &[f32::INFINITY][..]] {
        assert!(matches!(
            solve(&[], widths, 1.0),
            Err(BreakError::InvalidInput(_))
        ));
    }
    assert!(matches!(
        solve(&[b(1.0)], &[2.0], f32::NAN),
        Err(BreakError::InvalidInput(_))
    ));
    assert!(matches!(
        solve(&[g(1.0, -1.0, 0.0)], &[2.0], 1.0),
        Err(BreakError::InvalidInput(_))
    ));
    assert_eq!(solve(&[b(30.0)], &[20.0], 2.0), Err(BreakError::NoSolution));
    assert_eq!(
        solve(&[g(1.0, 1.0, 1.0)], &[20.0], 2.0),
        Err(BreakError::NoSolution)
    );
}

#[test]
fn mandatory_forbidden_and_penalty_width() {
    let nodes = [
        b(2.0),
        g(1.0, 2.0, 1.0),
        b(2.0),
        p(1.0, -10_000, false),
        b(2.0),
        p(0.0, 10_000, false),
        g(1.0, 2.0, 1.0),
        b(2.0),
    ];
    let result = solve(&nodes, &[6.0, 6.0], 2.0).unwrap();
    assert_eq!(result.lines.len(), 2);
    assert_eq!(result.lines[0].break_at, 3);
    assert_eq!((result.lines[0].start, result.lines[0].end), (0, 3));
    assert_eq!(result.lines[0].ratio, 0.0);
    assert_ne!(result.lines[1].break_at, 5);
    assert_eq!(solve(&nodes, &[5.0], 2.0), Err(BreakError::NoSolution));
}

#[test]
fn first_width_and_final_ragged() {
    let nodes = [b(4.0), g(1.0, 2.0, 1.0), b(4.0)];
    let solution = solve(&nodes, &[4.0, 5.0], 2.0).unwrap();
    assert_eq!(
        solution
            .lines
            .iter()
            .map(|l| l.break_at)
            .collect::<Vec<_>>(),
        vec![1, 3]
    );
    assert_eq!(solution.lines[0].ratio, 0.0);
    assert_eq!(solution.lines[1].ratio, 0.0);
    assert_eq!(solve(&nodes, &[3.0, 5.0], 2.0), Err(BreakError::NoSolution));
}

#[test]
fn widths_advance_by_line_then_repeat_the_last_value() {
    let nodes = [
        b(2.0),
        p(0.0, -10_000, false),
        b(3.0),
        p(0.0, -10_000, false),
        b(4.0),
        p(0.0, -10_000, false),
        b(4.0),
    ];
    let solution = solve(&nodes, &[2.0, 3.0, 4.0], 1.0).unwrap();
    assert_eq!(solution.lines.len(), 4);
    assert!(solution.lines.iter().all(|line| line.ratio == 0.0));
    assert_eq!(solve(&nodes, &[2.0, 3.0], 1.0), Err(BreakError::NoSolution));
}

#[test]
fn stretch_shrink_and_trimmed_edges() {
    let stretch_nodes = [
        g(9.0, 0.0, 0.0),
        b(3.0),
        g(1.0, 2.0, 1.0),
        b(3.0),
        g(9.0, 0.0, 0.0),
        b(2.0),
    ];
    let stretched = solve(&stretch_nodes, &[8.0, 8.0], 2.0).unwrap();
    assert_eq!((stretched.lines[0].start, stretched.lines[0].end), (1, 4));
    assert_eq!(stretched.lines[0].break_at, 4);
    assert!((stretched.lines[0].ratio - 0.5).abs() < 1e-6);
    let shrunk = solve(&stretch_nodes, &[6.5, 8.0], 2.0).unwrap();
    assert!((shrunk.lines[0].ratio + 0.5).abs() < 1e-6);
    assert_eq!(
        solve(&stretch_nodes, &[8.0, 8.0], 0.25),
        Err(BreakError::NoSolution)
    );
}

#[test]
fn equal_demerit_paths_choose_earlier_break() {
    let nodes = [b(1.0), p(0.0, 0, false), p(0.0, 0, false), b(1.0)];
    let result = solve(&nodes, &[1.0], 0.0).unwrap();
    assert_eq!(result.lines[0].break_at, 1);
}

#[test]
fn paragraph_optimum_beats_longest_fit_greedy_break() {
    // At width 5 the longest natural-width prefix is boxes 1+1. That first
    // line stretches by 100%, costing 12100 demerits. Breaking after 1+1+2
    // shrinks by 50% and makes the entire paragraph much cheaper.
    let nodes = [
        b(1.0),
        g(1.0, 2.0, 1.0),
        b(1.0),
        g(1.0, 2.0, 1.0),
        b(2.0),
        g(1.0, 2.0, 1.0),
        b(1.0),
    ];
    let result = solve(&nodes, &[5.0], 2.0).unwrap();
    assert_eq!(
        result.lines.iter().map(|l| l.break_at).collect::<Vec<_>>(),
        vec![5, 7]
    );
    assert_eq!(result.lines[0].ratio, -0.5);
    assert!((result.demerits - 606.25).abs() < 1e-8);
    assert!(result.demerits < 12_200.0);
}

#[test]
fn consecutive_flagged_breaks_are_penalized() {
    let flagged = [b(1.0), p(0.0, -10_000, true), b(1.0), p(0.0, -10_000, true)];
    let unflagged = [
        b(1.0),
        p(0.0, -10_000, false),
        b(1.0),
        p(0.0, -10_000, false),
    ];
    let a = solve(&flagged, &[2.0], 1.0).unwrap();
    let b = solve(&unflagged, &[2.0], 1.0).unwrap();
    assert_eq!(a.demerits - b.demerits, 100.0);
}

#[test]
fn exhaustive_small_paragraphs_match_global_optimum() {
    let mut checked = 0;
    for a in 1..=3 {
        for b_width in 1..=3 {
            for c in 1..=3 {
                for first in 3..=7 {
                    let nodes = [
                        b(a as f32),
                        g(1.0, 2.0, 1.0),
                        b(b_width as f32),
                        p(0.5, 20, false),
                        g(1.0, 2.0, 1.0),
                        b(c as f32),
                    ];
                    let widths = [first as f32, 5.0];
                    let oracle = exhaustive_oracle(&nodes, &widths, 2.0);
                    let actual = solve(&nodes, &widths, 2.0);
                    match (oracle, actual) {
                        (None, Err(BreakError::NoSolution)) => {}
                        (Some((expected, _)), Ok(solution)) => {
                            assert!((solution.demerits - expected).abs() < 1e-7);
                        }
                        pair => panic!("oracle/solver disagreement: {pair:?}, nodes={nodes:?}, widths={widths:?}"),
                    }
                    checked += 1;
                }
            }
        }
    }
    assert_eq!(checked, 135);
}

#[test]
fn penalty_cost_is_additive_and_glue_cannot_shrink_below_zero() {
    let solution = solve(&[b(1.0), p(0.0, 20, false), b(1.0)], &[1.0], 0.0).unwrap();
    assert_eq!(solution.demerits, 600.0); // (10^2 + 20^2) + 10^2
    assert!(matches!(
        solve(&[b(1.0), g(1.0, 2.0, 2.0), b(1.0)], &[2.0], 1.0),
        Err(BreakError::InvalidInput(_))
    ));
}

#[test]
fn discretionary_hyphen_allows_ragged_single_word_and_counts_width() {
    let nodes = [b(3.0), p(1.0, 50, true), b(4.0)];
    let result = solve(&nodes, &[5.0], 0.0).unwrap();
    assert_eq!(
        result.lines.iter().map(|l| l.break_at).collect::<Vec<_>>(),
        [1, 3]
    );
    assert_eq!(result.lines[0].ratio, 0.0);
    assert!((result.demerits - (10.8_f64.powi(2) + 2500.0 + 100.0)).abs() < 1e-7);
    assert_eq!(solve(&nodes, &[3.5], 1.0), Err(BreakError::NoSolution));
}

#[test]
fn long_cjk_pruning_keeps_exact_breaks_without_quadratic_scan() {
    let mut nodes = Vec::new();
    for _ in 0..350 {
        nodes.extend([b(10.0), g(0.0, 5.0, 0.0)]);
    }
    let start = std::time::Instant::now();
    for _ in 0..100 {
        let out = solve(&nodes, &[300.0], 3.0).unwrap();
        assert_eq!(out.lines.len(), 12);
        assert_eq!(out.lines[0].break_at, 59);
    }
    eprintln!("100 CJK solves: {:?}", start.elapsed());
}
