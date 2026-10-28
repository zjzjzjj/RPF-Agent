# -*- coding: utf-8 -*-
"""校准参数机制（data/calibration/<name>.json，PR-4）。

缺失/坏文件 → 空 profile 不阻断；version 落 ai_raw_json 供审计还原。
"""
from __future__ import annotations

from app import config


def test_calibration_profile_default_file_loads(app_env):  # noqa: ANN001
    # app_env 将 data_dir 重定向到临时目录——把校准文件写进当前 data_dir
    import json

    calib = config.data_dir() / "calibration"
    calib.mkdir(parents=True, exist_ok=True)
    (calib / "default.json").write_text(
        json.dumps({"version": "2026-10-27.test1.v1",
                    "scale_text": "【档位校准参考（测试1=浙江中烟门户运维人工分布校准）】…"},
                   ensure_ascii=False),
        encoding="utf-8")
    p = config.calibration_profile()
    assert p["version"] == "2026-10-27.test1.v1"
    assert "档位校准参考" in p["scale_text"]


def test_calibration_profile_missing_file_returns_empty(app_env, monkeypatch):  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_CALIBRATION_PROFILE", "nonexistent-profile")
    p = config.calibration_profile()
    assert p == {"version": "", "scale_text": ""}


def test_calibration_profile_bad_json_returns_empty(app_env, tmp_path, monkeypatch):  # noqa: ANN001
    import json

    calib = tmp_path / "calibration"
    calib.mkdir()
    (calib / "bad.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BID_EVAL_CALIBRATION_PROFILE", "bad")
    p = config.calibration_profile()
    assert p == {"version": "", "scale_text": ""}
