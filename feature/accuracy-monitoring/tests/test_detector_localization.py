"""检测器检出位置收集单测（design_anomaly_localization.md P1）。

DetectionResult 扩展 hits（生僻字逐 token 命中）/ windows（乱码/重复触发窗口）后：
- 各 ill_type 路径的位置信息正确性；
- 纯 Python 类型（可 pickle 跨进程返回）；
- 容量上限防御。
"""
from __future__ import annotations

import pickle

import numpy as np
import pytest

from anomaly_middleware.detector import ILLDetector, DetectionResult
from anomaly_middleware.env import resolve_config_path


@pytest.fixture
def det():
    return ILLDetector(resolve_config_path())


def _arrays(rows):
    """list[{tid: logprob}] -> (logprobs_2d, token_ids_2d)，不足补 (-100, 0)。"""
    n = len(rows)
    k = max((len(r) for r in rows), default=0)
    lp = np.full((n, k), -100.0, dtype=np.float32)
    tid = np.zeros((n, k), dtype=np.int32)
    for i, d in enumerate(rows):
        for j, (t, v) in enumerate(d.items()):
            tid[i, j] = t
            lp[i, j] = v
    return lp, tid


def _vocab_tk2cat():
    """topk token id 映射多类别：混杂类别 > category_thresh(2)。"""
    return {str(i): ["chinese_cjk", "japanese_hiragana", "korean_hangul",
                     "whitespace", "punctuation", "english_latin"][i % 6]
            for i in range(2, 20)}


# --------------------------- 生僻字：词表模式逐 token 命中 --------------------------- #
def test_rare_with_vocab_hits(det):
    """词表模式：命中位置带 idx / token_id / cats / topk_ids。"""
    det.set_vocabulary(_vocab_tk2cat(), 20)
    lp, tid = _arrays([{3: -5.0, 4: -5.5, 5: -6.0, 6: -6.5, 7: -7.0} for _ in range(10)])
    res = det.detector(lp, tid, topk_n=5)
    assert res.is_ill is True and res.ill_type == 1
    assert len(res.hits) == 10
    assert [h["idx"] for h in res.hits] == list(range(10))
    h0 = res.hits[0]
    assert h0["token_id"] == 3  # top-1 token（logp 降序后首列）
    assert h0["logp_top1"] == pytest.approx(-5.0)
    assert isinstance(h0["cats"], list) and len(h0["cats"]) >= 3
    assert h0["topk_ids"] == [3, 4, 5, 6, 7]


def test_rare_no_vocab_hits_degraded(det):
    """无词表降级：命中位置带 idx / token_id / logp_top1，无 cats。"""
    lp, tid = _arrays([{1: -10.0, 2: -11.0, 3: -12.0} for _ in range(10)])
    res = det.detector(lp, tid, topk_n=3)
    assert res.is_ill is True and res.ill_type == 1
    assert [h["idx"] for h in res.hits] == list(range(10))
    assert res.hits[0]["logp_top1"] == pytest.approx(-10.0)
    assert "cats" not in res.hits[0]


def test_rare_short_sequence_hits(det):
    """短序列（< stride=64）仅生僻字路径：同样收集命中。"""
    lp, tid = _arrays([{3: -5.0, 4: -5.5, 5: -6.0, 6: -6.5, 7: -7.0} for _ in range(8)])
    det.set_vocabulary(_vocab_tk2cat(), 20)
    res = det.detector(lp, tid, topk_n=5)
    assert res.is_ill is True and res.ill_type == 1
    assert len(res.hits) == 8
    assert res.windows == []


def test_normal_no_hits(det):
    """正常序列：无命中、无窗口。"""
    lp, tid = _arrays([{1: -0.1, 2: -2.0} for _ in range(100)])
    res = det.detector(lp, tid, topk_n=2)
    assert res.is_ill is False
    assert res.hits == [] and res.windows == []


# --------------------------- 乱码：触发窗口 --------------------------- #
def test_garbled_windows_recorded(det):
    """无词表乱码：windows 记录触发窗口，同时保留逐位置命中。"""
    n = 140
    lp, tid = _arrays([{1000: -20.0, 1001: -21.0, 1002: -22.0}
                       for _ in range(n)])
    res = det.detector(lp, tid, topk_n=3)
    assert res.is_ill is True and res.ill_type == 2
    assert len(res.windows) >= 1
    w0 = res.windows[0]
    assert w0["kind"] == "garbled"
    assert w0["start"] == 0 and w0["end"] >= 64  # 至少覆盖第一个窗口
    # 降级生僻字命中（top1 < -6）逐位置保留
    assert len(res.hits) >= 1
    assert res.hits[0]["idx"] == 0


# --------------------------- 重复：触发窗口 --------------------------- #
def test_repetition_windows_recorded(det):
    """轨迹重复：触发窗口指向计数越阈的窗口区间。"""
    n = 1024
    lp, tid = _arrays([{1: -0.1, 2: -1.0, 3: -2.0} for _ in range(n)])
    res = det.detector(lp, tid, topk_n=3)
    assert res.is_ill is True and res.ill_type == 3
    assert len(res.windows) >= 1
    last = res.windows[-1]
    assert last["kind"] == "repet"
    assert last["end"] == n  # 重复持续到序列末尾，最后一个触发窗口触及结尾
    assert last["end"] - last["start"] >= 64  # 窗口长度 >= stride


def test_repetition_window_rolls_back_to_first_contributing_window(det):
    """重复窗口倒推：触发时按触发链回溯到首个贡献窗口（高亮近似重复起点）。

    构造：前 768 token 正常（50-token 轮换 → distinct_n 高 → 轨迹阴性；
    top1 logp 恒定 → std≈0 → ACF 阴性），尾部 256 token 周期重复
    （top1 logp 在 -0.05/-0.18 交替 → ACF+轨迹双阳性）。双检出链在第 3 个
    阳性窗口（start=896）触发 → 倒推 multi_window_thresh(2) 窗 → 首窗 start=768，
    恰为重复段起点。
    """
    n = 1024
    repeat_start = 768
    rows = []
    for i in range(n):
        if i < repeat_start:
            rows.append({5 + (i % 50): -0.1, 99: -5.0})       # 正常：distinct_n 高
        elif (i - repeat_start) % 2 == 0:
            rows.append({1: -0.05, 3: -3.0})                   # 重复：top1 交替
        else:
            rows.append({2: -0.18, 4: -3.0})
    lp, tid = _arrays(rows)
    res = det.detector(lp, tid, topk_n=2)
    assert res.is_ill is True and res.ill_type == 3
    assert len(res.windows) >= 1
    last = res.windows[-1]
    assert last["kind"] == "repet"
    assert last["end"] == n
    # 倒推后窗口起点 = 首个双阳性窗口（= 重复段起点），早于触发窗口起点（896）
    assert last["start"] == repeat_start


# --------------------------- NaN：无位置 --------------------------- #
def test_nan_no_positions(det):
    lp, tid = _arrays([{1: float("nan"), 2: -1.0} for _ in range(5)])
    res = det.detector(lp, tid, topk_n=2)
    assert res.is_ill is True and res.ill_type == 4
    assert res.hits == [] and res.windows == []


# --------------------------- 纯 Python 类型（可 pickle） --------------------------- #
def test_detection_result_picklable(det):
    """DetectionResult 含 hits/windows 后仍可 pickle（跨进程返回前提）。"""
    det.set_vocabulary(_vocab_tk2cat(), 20)
    lp, tid = _arrays([{3: -5.0, 4: -5.5, 5: -6.0, 6: -6.5, 7: -7.0} for _ in range(10)])
    res = det.detector(lp, tid, topk_n=5)
    data = pickle.dumps(res)
    loaded = pickle.loads(data)
    assert loaded == res
    # 全部为纯 Python 标量/容器
    assert all(isinstance(h["idx"], int) for h in loaded.hits)
    assert all(isinstance(h["logp_top1"], float) for h in loaded.hits)


# --------------------------- 容量上限防御 --------------------------- #
def test_hits_capped(det):
    """命中数超过 MAX_HITS(200) 时截断。

    构造：每 128-token 窗口含 25 个生僻命中（占比 0.195 < 乱码阈值 0.2，不触发
    乱码提前返回）；stride=64 重叠滑窗下 1024 token 累计 25×16=400 条原始命中 →
    截断到 200，且最终 ill_type=1（生僻字）。
    """
    det.set_vocabulary(_vocab_tk2cat(), 20)
    rare_row = {3: -5.0, 4: -5.5, 5: -6.0, 6: -6.5, 7: -7.0}
    normal_row = {2: -0.1, 3: -2.0, 8: -3.0}
    rows = []
    for _ in range(8):  # 8 × 128 = 1024
        rows.extend([dict(rare_row) for _ in range(25)])
        rows.extend([dict(normal_row) for _ in range(103)])
    lp, tid = _arrays(rows)
    res = det.detector(lp, tid, topk_n=5)
    assert res.is_ill is True and res.ill_type == 1
    assert res.windows == []  # 占比低于乱码阈值，无触发窗口
    assert len(res.hits) == 200


def test_detection_result_defaults():
    """向后兼容：默认构造 hits/windows 为空。"""
    r = DetectionResult(is_ill=True, ill_type=1)
    assert r.hits == [] and r.windows == []
