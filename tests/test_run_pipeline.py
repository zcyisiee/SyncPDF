"""``bdt run`` 编排契约：阶段前置校验 / run_state.json 哈希失效 / 续跑语义。

不依赖 PDF、网络或真实模型：用 ``tmp_path`` 造最小 workdir，并用替身
``_run_stage`` 观察阶段顺序与状态记录（真实阶段实现已有各自测试覆盖）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from babeldoc_tools import run as run_tool

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 显式 reviewer 命令：给了它 review 阶段才会执行并进入 report（U4 起无 --reviewer
#: 时 run 以 ``waiting_for_reviewer`` / exit 1 结束——质量门禁不把"没人审查"当成功）。
REVIEWER = "stub-reviewer"

DOC_MD = (
    "<!-- BabelDOC markdown v1 -->\n\n"
    "<!-- id=P01-001 label=text -->\nHello world.\n\n"
    "<!-- id=P01-002 label=text -->\nSecond paragraph.\n"
)


def _cli(*argv: str) -> subprocess.CompletedProcess:
    # argv 全部由本测试构造，非外部输入拼接
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "babeldoc_tools", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


# --------------------------------------------------------------------------- #
# 替身阶段：按需落盘最小 artifact，避免真实解析/重建/模型调用
# --------------------------------------------------------------------------- #
@pytest.fixture
def stub_stages(monkeypatch):
    """把 ``run._run_stage`` 换成替身，返回被调用的阶段名列表。"""
    calls: list[str] = []
    behavior: dict = {"check_verdict": "pass", "review": {"verdict": "pass", "findings": []}}

    def fake_stage(stage, workdir_path, _cfg):
        calls.append(stage)
        agent = workdir_path / "agent"
        agent.mkdir(parents=True, exist_ok=True)
        if stage == "parse":
            (agent / "document.md").write_text(DOC_MD, encoding="utf-8")
            return {"ok": True, "data": {"document_md": str(agent / "document.md")}}
        if stage == "translate":
            (agent / "translated.md").write_text(DOC_MD, encoding="utf-8")
            return {"ok": True, "data": {"translated_md": str(agent / "translated.md")}}
        if stage == "apply":
            (agent / "apply_report.json").write_text(
                json.dumps({"ok": True, "applied": 2}), encoding="utf-8"
            )
            return {"ok": True, "data": {"ok": True, "applied": 2}}
        if stage == "build":
            out = workdir_path / "output"
            out.mkdir(parents=True, exist_ok=True)
            mono = out / "paper.no_watermark.zh.mono.pdf"
            mono.write_bytes(b"%PDF-1.4 stub\n")
            return {"ok": True, "data": {"mono_pdf": str(mono)}}
        if stage == "check":
            (agent / "review_verdict.json").write_text(
                json.dumps({"verdict": behavior["check_verdict"]}), encoding="utf-8"
            )
            return {
                "ok": True,
                "data": {
                    "verdict": behavior["check_verdict"],
                    "blockers": [],
                    "warnings": [],
                    "metrics": {},
                    "layout": {"status": "ok", "summary": {}},
                    "links": {"status": "ok", "missing": 0, "wrong_label": 0, "wrong_role": 0},
                    "reasons": [] if behavior["check_verdict"] == "pass" else ["demo blocker"],
                    "report": str(agent / "review_verdict.json"),
                },
            }
        if stage == "review":
            review = dict(behavior["review"])
            (agent / "agent_review.json").write_text(
                json.dumps(review), encoding="utf-8"
            )
            return {"ok": True, "data": review}
        if stage == "report":
            final = workdir_path / "FINAL_REPORT.md"
            final.write_text("# report\n", encoding="utf-8")
            return {"ok": True, "data": {"report": str(final)}}
        raise AssertionError(f"unexpected stage {stage}")  # pragma: no cover

    monkeypatch.setattr(run_tool, "_run_stage", fake_stage)
    return {"calls": calls, "behavior": behavior}


def _make_workdir(tmp_path: Path) -> Path:
    workdir = tmp_path / "wd"
    (workdir / "agent").mkdir(parents=True)
    return workdir


def _make_pdf(tmp_path: Path) -> Path:
    """最小占位 PDF（只用于前置校验的存在性检查，替身阶段不解析它）。"""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub\n")
    return pdf


# --------------------------------------------------------------------------- #
# 阶段前置校验：缺 artifact 必须报结构错误，不能靠跳过制造成功
# --------------------------------------------------------------------------- #
def test_missing_artifact_apply_requires_translated_md(tmp_path, stub_stages):  # noqa: ARG001
    workdir = _make_workdir(tmp_path)
    (workdir / "agent" / "document.md").write_text(DOC_MD, encoding="utf-8")

    result = run_tool.run_pipeline(str(workdir), from_stage="apply")

    assert result["ok"] is False
    error = result["error"]
    assert error["code"] == "missing_artifact"
    assert error["artifact"] == "agent/translated.md"
    assert error["stage"] == "apply"
    assert "translate" in error["message"]
    assert stub_stages["calls"] == []  # 一个阶段都没跑


def test_missing_artifact_build_requires_apply_report(tmp_path, stub_stages):  # noqa: ARG001
    workdir = _make_workdir(tmp_path)
    (workdir / "agent" / "translated.md").write_text(DOC_MD, encoding="utf-8")

    result = run_tool.run_pipeline(str(workdir), from_stage="build")

    assert result["ok"] is False
    assert result["error"]["code"] == "missing_artifact"
    assert result["error"]["artifact"] == "agent/apply_report.json"
    assert stub_stages["calls"] == []


def test_missing_artifact_check_requires_build_output(tmp_path, stub_stages):  # noqa: ARG001
    workdir = _make_workdir(tmp_path)
    for name in ("document.md", "translated.md", "apply_report.json"):
        (workdir / "agent" / name).write_text("{}", encoding="utf-8")

    result = run_tool.run_pipeline(str(workdir), from_stage="check")

    assert result["ok"] is False
    assert result["error"]["code"] == "missing_artifact"
    assert result["error"]["stage"] == "check"
    assert stub_stages["calls"] == []


def test_preflight_error_keeps_completed_stages(tmp_path, stub_stages):  # noqa: ARG001
    """前置校验失败要报告已完成阶段（产物保留供续跑）。"""
    workdir = _make_workdir(tmp_path)
    (workdir / "agent" / "document.md").write_text(DOC_MD, encoding="utf-8")

    result = run_tool.run_pipeline(str(workdir), from_stage="apply")

    stages = result["data"]["stages"]
    assert [(item["stage"], item["status"]) for item in stages] == [
        ("parse", "skipped"),
        ("translate", "skipped"),
    ]
    assert result["error"]["completed_stages"] == []


def test_bad_from_stage_is_reported(tmp_path):
    result = run_tool.run_pipeline(str(_make_workdir(tmp_path)), from_stage="nope")
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_from_stage"


def test_parse_requires_pdf(tmp_path, stub_stages):  # noqa: ARG001
    result = run_tool.run_pipeline(str(_make_workdir(tmp_path)), from_stage="parse")
    assert result["ok"] is False
    assert result["error"]["code"] in {"missing_pdf", "pdf_missing"}


# --------------------------------------------------------------------------- #
# 成功路径与 run_state.json
# --------------------------------------------------------------------------- #
def test_full_run_records_state_and_summary(tmp_path, stub_stages):
    workdir = _make_workdir(tmp_path)

    result = run_tool.run_pipeline(
        str(workdir),
        str(_make_pdf(tmp_path)),
        from_stage="parse",
        markdown="self",
        reviewer=REVIEWER,
    )

    assert result["ok"] is True
    assert stub_stages["calls"] == [
        "parse",
        "translate",
        "apply",
        "build",
        "check",
        "review",
        "report",
    ]
    stages = result["data"]["stages"]
    assert [item["stage"] for item in stages] == list(run_tool.STAGES)
    statuses = {item["stage"]: item["status"] for item in stages}
    assert statuses["review"] == "ok"
    assert statuses["parse"] == "ok"
    assert result["data"]["verdict"] == "pass"
    assert result["data"]["outputs"]["mono_pdf"].endswith(".mono.pdf")

    state = json.loads((workdir / "agent" / "run_state.json").read_text(encoding="utf-8"))
    assert state["version"] == run_tool.STATE_VERSION
    assert set(state["stages"]) == set(run_tool.STAGES)
    translate_entry = state["stages"]["translate"]
    assert translate_entry["ok"] is True
    assert translate_entry["inputs"]["document_md"]
    assert translate_entry["artifacts"]["translated_md"] == "agent/translated.md"
    # 哈希是 64 位十六进制
    for value in state["inputs"].values():
        if value is not None:
            assert len(value) == 64
    assert state["inputs"]["translated_md"] == state["inputs"]["document_md"]  # self 自译


def test_run_adds_run_state_without_touching_stage_artifacts(tmp_path, stub_stages):  # noqa: ARG001
    """run 的私有状态只新增 agent/run_state.json，不改各阶段产物路径。"""
    workdir = _make_workdir(tmp_path)
    result = run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown="self", reviewer=REVIEWER
    )
    assert result["ok"] is True
    agent_files = {path.name for path in (workdir / "agent").iterdir()}
    # 阶段产物（替身写的）都在原路径，run 只多一个 run_state.json
    assert agent_files == {
        "document.md",
        "translated.md",
        "apply_report.json",
        "review_verdict.json",
        "agent_review.json",
        "run_state.json",
    }


# --------------------------------------------------------------------------- #
# --from 续跑与哈希失效
# --------------------------------------------------------------------------- #
def test_from_build_unchanged_is_ok_and_skips_upstream(tmp_path, stub_stages):
    workdir = _make_workdir(tmp_path)
    assert run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown="self", reviewer=REVIEWER
    )["ok"] is True
    stub_stages["calls"].clear()

    result = run_tool.run_pipeline(
        str(workdir), from_stage="build", markdown="self", reviewer=REVIEWER
    )

    assert result["ok"] is True
    assert stub_stages["calls"] == ["build", "check", "review", "report"]
    statuses = {item["stage"]: item["status"] for item in result["data"]["stages"]}
    assert statuses["translate"] == "skipped"
    assert statuses["build"] == "ok"


def test_hash_mismatch_invalidates_downstream(tmp_path, stub_stages):  # noqa: ARG001
    """改 document.md 一个字节后 --from build 必须报 stale_upstream（不沿用旧产物）。"""
    workdir = _make_workdir(tmp_path)
    assert run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown="self", reviewer=REVIEWER
    )["ok"] is True
    stub_stages["calls"].clear()

    document = workdir / "agent" / "document.md"
    document.write_text(document.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    result = run_tool.run_pipeline(str(workdir), from_stage="build", markdown="self")

    assert result["ok"] is False
    error = result["error"]
    assert error["code"] == "stale_upstream"
    assert error["stage"] == "translate"
    assert error["input"] == "document_md"
    assert error["suggested_from"] == "translate"
    assert error["recorded_sha256"] != error["current_sha256"]
    assert stub_stages["calls"] == []  # 不静默沿用旧产物


def test_hash_mismatch_after_layout_overrides_change(tmp_path, stub_stages):  # noqa: ARG001
    """layout_overrides.json 变化使 build/check/report 失效（--from check 报错）。"""
    workdir = _make_workdir(tmp_path)
    assert run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown="self", reviewer=REVIEWER
    )["ok"] is True
    stub_stages["calls"].clear()

    (workdir / "agent" / "layout_overrides.json").write_text(
        json.dumps({"paragraphs": {"P01-001": {"scale_cap": 0.9}}}), encoding="utf-8"
    )

    result = run_tool.run_pipeline(
        str(workdir), from_stage="check", markdown="self", reviewer=REVIEWER
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "stale_upstream"
    assert result["error"]["stage"] == "build"
    assert result["error"]["input"] == "layout_overrides"
    assert result["error"]["suggested_from"] == "build"
    assert stub_stages["calls"] == []


def test_stale_detection_picks_earliest_affected_stage(tmp_path, stub_stages):  # noqa: ARG001
    workdir = _make_workdir(tmp_path)
    assert run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown="self", reviewer=REVIEWER
    )["ok"] is True

    (workdir / "agent" / "document.md").write_text(DOC_MD + "\n", encoding="utf-8")
    result = run_tool.run_pipeline(
        str(workdir), from_stage="report", markdown="self", reviewer=REVIEWER
    )

    assert result["ok"] is False
    # translate 是受 document_md 影响的最早阶段
    assert result["error"]["stage"] == "translate"


def test_resume_after_following_suggestion_succeeds(tmp_path, stub_stages):
    """按错误里的 suggested_from 重跑即可恢复。"""
    workdir = _make_workdir(tmp_path)
    assert run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown="self", reviewer=REVIEWER
    )["ok"] is True

    (workdir / "agent" / "document.md").write_text(DOC_MD + "\n", encoding="utf-8")
    stale = run_tool.run_pipeline(str(workdir), from_stage="build", markdown="self")
    assert stale["ok"] is False
    stub_stages["calls"].clear()

    result = run_tool.run_pipeline(
        str(workdir), from_stage=stale["error"]["suggested_from"], markdown="self",
        reviewer=REVIEWER,
    )

    assert result["ok"] is True
    assert stub_stages["calls"] == ["translate", "apply", "build", "check", "review", "report"]


def test_first_run_from_build_has_no_state_and_reports_missing(tmp_path, stub_stages):  # noqa: ARG001
    """没有 run_state.json 时不误报 stale，而是按 artifact 缺失报错。"""
    workdir = _make_workdir(tmp_path)
    result = run_tool.run_pipeline(str(workdir), from_stage="build")
    assert result["ok"] is False
    assert result["error"]["code"] == "missing_artifact"


# --------------------------------------------------------------------------- #
# 失败与 verdict 语义
# --------------------------------------------------------------------------- #
def test_stage_failure_returns_that_stage_error(tmp_path, stub_stages, monkeypatch):  # noqa: ARG001
    workdir = _make_workdir(tmp_path)
    original = run_tool._run_stage

    def failing_build(stage, workdir_path, cfg):
        if stage == "build":
            return {"ok": False, "error": {"code": "link_uri_set_mismatch", "message": "链接集合不一致"}}
        return original(stage, workdir_path, cfg)

    monkeypatch.setattr(run_tool, "_run_stage", failing_build)

    result = run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown="self", reviewer=REVIEWER
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "link_uri_set_mismatch"
    assert result["error"]["stage"] == "build"
    completed = [item["stage"] for item in result["data"]["stages"] if item["status"] == "ok"]
    assert completed == ["parse", "translate", "apply"]


def test_needs_fix_still_runs_report_but_exits_failed(tmp_path, stub_stages):  # noqa: ARG001
    workdir = _make_workdir(tmp_path)
    stub_stages["behavior"]["check_verdict"] = "needs_fix"

    result = run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown="self", reviewer=REVIEWER
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "check_needs_fix"
    assert result["error"]["verdict"] == "needs_fix"
    assert result["data"]["verdict"] == "needs_fix"
    # report 仍在 check 之后跑完
    statuses = [item["stage"] for item in result["data"]["stages"] if item["status"] == "ok"]
    assert statuses[-1] == "report"
    assert (workdir / "FINAL_REPORT.md").exists()


def test_prompt_only_stops_after_translate(tmp_path, stub_stages, monkeypatch):  # noqa: ARG001
    workdir = _make_workdir(tmp_path)
    original = run_tool._run_stage

    def prompt_only_translate(stage, workdir_path, cfg):
        if stage == "translate":
            agent = workdir_path / "agent"
            agent.mkdir(parents=True, exist_ok=True)
            return {
                "ok": True,
                "data": {
                    "dry_run": True,
                    "prompt_only": True,
                    "prompt": str(agent / "prompt.md"),
                    "translated_md": None,
                },
            }
        return original(stage, workdir_path, cfg)

    monkeypatch.setattr(run_tool, "_run_stage", prompt_only_translate)

    result = run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown=None, prompt_only=True
    )

    assert result["ok"] is True
    assert result["data"]["stopped"]["reason"] == "prompt_only"
    assert result["data"]["stopped_at"] == "translate"
    statuses = {item["stage"]: item["status"] for item in result["data"]["stages"]}
    assert statuses["translate"] == "ok"
    assert statuses["apply"] == "skipped"


def test_run_without_reviewer_fails_the_gate(tmp_path, stub_stages):  # noqa: ARG001
    """U4 收紧：没有 --reviewer 时停在 review 并**失败**（exit 1），不跑 report。

    U3 曾把它当"等待"返回 exit 0；质量门禁不能把"没人审查"当交付成功。
    """
    workdir = _make_workdir(tmp_path)

    result = run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown="self"
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "waiting_for_reviewer"
    assert result["error"]["stage"] == "review"
    assert "--reviewer" in result["error"]["reviewer_usage"]
    assert result["data"]["stopped_at"] == "review"
    assert "report" not in stub_stages["calls"]
    statuses = {item["stage"]: item["status"] for item in result["data"]["stages"]}
    assert statuses["review"] == "waiting"
    assert statuses["report"] == "skipped"
    assert not (workdir / "FINAL_REPORT.md").exists()


def test_reviewer_pass_cannot_override_check_blocker(tmp_path, stub_stages):  # noqa: ARG001
    """check 的确定性 blocker 优先：reviewer 给 pass 也不能把整体判成成功。"""
    workdir = _make_workdir(tmp_path)
    stub_stages["behavior"]["check_verdict"] = "needs_fix"

    result = run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown="self", reviewer=REVIEWER
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "check_needs_fix"
    assert result["error"]["reviewer_verdict"] == "pass"
    assert result["error"]["verdict"] == "needs_fix"


def test_resume_from_review_keeps_check_gate(tmp_path, stub_stages):
    """`--from review` 续跑不能丢掉已记录的 check needs_fix（reviewer pass 不可绕过）。"""
    workdir = _make_workdir(tmp_path)
    pdf = str(_make_pdf(tmp_path))
    stub_stages["behavior"]["check_verdict"] = "needs_fix"

    first = run_tool.run_pipeline(str(workdir), pdf, markdown="self", reviewer=REVIEWER)
    assert first["ok"] is False
    assert first["error"]["code"] == "check_needs_fix"

    resumed = run_tool.run_pipeline(
        str(workdir), from_stage="review", reviewer=REVIEWER
    )

    assert resumed["ok"] is False
    assert resumed["error"]["code"] == "check_needs_fix"
    assert resumed["error"]["reviewer_verdict"] == "pass"
    assert resumed["error"]["verdict"] == "needs_fix"


def test_reviewer_needs_fix_returns_actions_and_counts_a_round(tmp_path, stub_stages):  # noqa: ARG001
    """needs_fix 的 findings 映射为 actions；run 不自动修复，只记一轮。"""
    workdir = _make_workdir(tmp_path)
    stub_stages["behavior"]["review"] = {
        "verdict": "needs_fix",
        "findings": [
            {
                "id": "P01-001",
                "kind": "retranslate",
                "evidence": "demo",
                "action": "translate --ids P01-001",
            },
            {
                "id": "P02-003",
                "kind": "layout",
                "evidence": "overflow",
                "action": "layout-set scale_cap 0.9",
            },
        ],
    }

    result = run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown="self", reviewer=REVIEWER
    )

    assert result["ok"] is False
    error = result["error"]
    assert error["code"] == "reviewer_needs_fix"
    assert error["actions"]["retranslate"]["ids"] == ["P01-001"]
    assert error["actions"]["layout"]["items"][0]["id"] == "P02-003"
    assert error["fix_rounds"] == {"retranslate": 1, "layout": 1}
    # report 仍跑完（check 之后）；review 没有隐藏的自动修复循环
    assert result["data"]["stages"][-1]["stage"] == "report"
    assert stub_stages["calls"].count("translate") == 1

    state = json.loads((workdir / "agent" / "run_state.json").read_text(encoding="utf-8"))
    assert state["quality"]["fix_rounds"] == {"retranslate": 1, "layout": 1}
    assert state["quality"]["reviewer"]["verdict"] == "needs_fix"


def test_reviewer_fix_rounds_are_capped(tmp_path, stub_stages):  # noqa: ARG001
    """超上限停在 needs_human_review，不再调用 reviewer。"""
    workdir = _make_workdir(tmp_path)
    finding = {
        "id": "P01-001",
        "kind": "retranslate",
        "evidence": "demo",
        "action": "translate --ids P01-001",
    }
    stub_stages["behavior"]["review"] = {"verdict": "needs_fix", "findings": [finding]}

    for expected_round in (1, 2):
        result = run_tool.run_pipeline(
            str(workdir), str(_make_pdf(tmp_path)), markdown="self", reviewer=REVIEWER
        )
        assert result["error"]["code"] == "reviewer_needs_fix"
        assert result["error"]["fix_rounds"]["retranslate"] == expected_round

    stub_stages["calls"].clear()
    result = run_tool.run_pipeline(
        str(workdir), str(_make_pdf(tmp_path)), markdown="self", reviewer=REVIEWER
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "needs_human_review"
    assert result["error"]["exhausted"] == ["retranslate"]
    # 已超限：review 阶段的模型调用不再发生（check 仍跑了）
    assert "review" not in stub_stages["calls"]


# --------------------------------------------------------------------------- #
# CLI 契约：stdout 单行 JSON
# --------------------------------------------------------------------------- #
def test_cli_missing_artifact_is_single_line_json(tmp_path):
    workdir = tmp_path / "wd"
    (workdir / "agent").mkdir(parents=True)

    out = _cli("run", "--workdir", str(workdir), "--from", "apply")

    assert out.returncode == 1
    payload = json.loads(out.stdout)  # 单次 loads 成功 => stdout 只有一行 JSON
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_artifact"


def test_cli_run_requires_workdir():
    out = _cli("run", "paper.pdf")
    assert out.returncode == 1
    payload = json.loads(out.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "workdir_missing"


def test_cli_run_help_exposes_from_choices():
    out = _cli("run", "--help")
    assert out.returncode == 0, out.stderr
    for stage in run_tool.STAGES:
        assert stage in out.stdout
    for flag in ("--translator", "--reviewer", "--markdown", "--prompt-only", "--mineru-json", "--dual"):
        assert flag in out.stdout
    # U3 删除了 agy 专属旗标
    for flag in ("--model", "--effort"):
        assert flag not in out.stdout
