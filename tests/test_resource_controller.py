"""Telemetry records hardware use without throttling or a utilization gate."""

from types import SimpleNamespace

import pytest

from babeldoc.docvision import resource_controller as rc


def test_gpu_driver_sensor_is_read_without_privilege(monkeypatch):
    monkeypatch.setattr(rc.platform, "system", lambda: "Darwin")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            stdout='"Device Utilization %"=32\n"Device Utilization %"=47'
        )

    monkeypatch.setattr(rc.subprocess, "run", run)
    assert rc.apple_gpu_percent() == 47
    assert calls[0][0] == "/usr/sbin/ioreg"


def test_full_utilization_does_not_block_or_sleep(monkeypatch):
    ctl = rc.InferenceResourceController(cpu_sensor=lambda: 100, gpu_sensor=lambda: 100)

    def forbidden(_):
        raise AssertionError("Resource telemetry must not sleep")

    monkeypatch.setattr(rc.time, "sleep", forbidden)
    assert ctl.admit("mlx-gpu").gpu_percent == 100
    report = ctl.report()
    assert report["status"] == "measured"
    assert report["gpu_percent"]["peak"] == 100
    assert report["limit_percent"] is None
    assert not report["throttling"]


def test_missing_sensor_is_reported_without_rejecting_inference():
    ctl = rc.InferenceResourceController(cpu_sensor=lambda: 10, gpu_sensor=lambda: None)
    ctl.admit("mlx-gpu")
    assert ctl.report()["status"] == "incomplete"
    assert ctl.report()["gpu_percent"] is None


def test_failed_inference_records_device_duration_and_failure():
    ctl = rc.InferenceResourceController(cpu_sensor=lambda: 10, gpu_sensor=lambda: 20)
    with pytest.raises(ValueError):
        with ctl.measure("cpu", job="page-3"):
            raise ValueError("model failed")
    job = ctl.report()["jobs"][0]
    assert job["status"] == "failed"
    assert job["job"] == "page-3"
    assert job["elapsed_s"] >= 0
    assert job["device"] == "cpu"


def test_cpu_threads_not_limited_to_seventy_percent(monkeypatch):
    monkeypatch.setattr(rc.psutil, "cpu_count", lambda: 18)
    assert rc.InferenceResourceController().cpu_threads == 18
