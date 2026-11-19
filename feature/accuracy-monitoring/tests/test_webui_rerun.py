"""方案 B 离线重跑定位单测（design_anomaly_localization.md §4.1B / §6.2 / §11.4）。

覆盖：topk 数组构建 / 序列回退 / 启发式兜底 / 检测器重跑（stub 注入）/
models 配置解析 / 详情端点按需重跑（单飞 + 负缓存 + 回写缓存）/
collector with_data 大字段追加拉取。
"""
from __future__ import annotations

import types
from pathlib import Path

import numpy as np
import pytest

from webui.collector import Collector
from webui.config import ConfigError, PollConfig, StoreConfig, WebUIConfig
from webui.events import EventSynthesizer
from webui.rerun import (
    RerunManager,
    build_topk_arrays,
    find_garbled_window,
    find_repetition_window,
    get_seq_ids,
    rare_top_positions,
)
from webui.store import Store
from tests._webui_helpers import build_webui_config_dict, write_yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECTOR_YAML = str(REPO_ROOT / "configs" / "detector.yaml")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def make_record(**over):
    rec = {
        "id": 1, "ts": 1.0, "ill_type": "repetition", "model": "m1",
        "prompt": "p", "text": "abcabc", "reasoning_content": None,
        "topk_logprobs": None, "tokens_ids": None, "text_tokenid": None,
        "localization": None,
    }
    rec.update(over)
    return rec


class StubDet:
    """ILLDetector 替身：固定返回检出结果（monkeypatch _new_detector 注入）。"""

    def __init__(self, ill, hits=(), windows=()):
        self._ill = ill
        self._hits = list(hits)
        self._windows = list(windows)
        self.calls = 0

    def set_vocabulary(self, tk2cat, vocab_size):  # pragma: no cover - 有词表模式才触发
        pass

    def detector(self, lp, ti, topk_n=None):
        self.calls += 1
        return types.SimpleNamespace(
            is_ill=True, ill_type=self._ill,
            hits=list(self._hits), windows=list(self._windows),
        )


# --------------------------------------------------------------------------- #
# build_topk_arrays / get_seq_ids
# --------------------------------------------------------------------------- #

def test_build_topk_arrays_dict_rows_sorted_desc():
    lp, ti = build_topk_arrays(
        [{"9": -0.5, "5": -0.1}, {"3": -2.0, "1": -0.2}], None
    )
    assert lp.shape == (2, 2)
    assert ti.shape == (2, 2)
    # 行内按 logprob 降序
    assert lp[0].tolist() == pytest.approx([-0.1, -0.5])
    assert ti[0].tolist() == [5, 9]
    assert lp[1].tolist() == pytest.approx([-0.2, -2.0])
    assert ti[1].tolist() == [1, 3]


def test_build_topk_arrays_rows_with_tokens_ids():
    lp, ti = build_topk_arrays(
        [[-1.0, -2.0], [-5.0, -0.5]], [[10, 11], [12, 13]]
    )
    # 每行按 logprob 降序稳定排序：第 2 行 [-5,-0.5] -> [-0.5,-5]
    assert lp[1].tolist() == pytest.approx([-0.5, -5.0])
    assert ti[1].tolist() == [13, 12]


def test_build_topk_arrays_invalid():
    assert build_topk_arrays(None, None) == (None, None)
    assert build_topk_arrays([], []) == (None, None)
    assert build_topk_arrays([None], None) == (None, None)


def test_get_seq_ids_prefers_text_tokenid():
    assert get_seq_ids([1, 2, 3], [[9, 8], [9, 8], [9, 8]]) == [1, 2, 3]


def test_get_seq_ids_fallback_top1_column():
    assert get_seq_ids(None, [[9, 8], [7, 6]]) == [9, 7]


def test_get_seq_ids_flat_rows():
    assert get_seq_ids(None, [5, 6, 7]) == [5, 6, 7]


def test_get_seq_ids_empty():
    assert get_seq_ids(None, None) == []


# --------------------------------------------------------------------------- #
# 启发式兜底
# --------------------------------------------------------------------------- #

def test_find_repetition_window():
    seq = [1, 2, 3] * 4  # 周期 3，共 12 token
    assert find_repetition_window(seq) == (0, 12)
    assert find_repetition_window([1, 2, 3, 4, 5]) is None


def test_find_garbled_window():
    texts = ["你", "好", "\uffff", "\ufffe", "\ufffd", "世"]
    assert find_garbled_window(texts) == (2, 5)
    assert find_garbled_window(["你", "好"]) is None


def test_rare_top_positions_sorted_by_top1_logp():
    lp = np.array([[-1.0, -2.0], [-5.0, -0.5], [-0.2, -9.0]], dtype=np.float32)
    ti = np.array([[10, 11], [12, 13], [14, 15]], dtype=np.int32)
    hits = rare_top_positions(lp, ti)
    # top1 = 第一列，升序：-5.0(idx1) < -1.0(idx0) < -0.2(idx2)
    assert [h["idx"] for h in hits] == [1, 0, 2]
    assert hits[0]["token_id"] == 12
    assert hits[0]["logp_top1"] == pytest.approx(-5.0)


# --------------------------------------------------------------------------- #
# 检测器重跑（stub 注入）
# --------------------------------------------------------------------------- #

def test_rerun_with_stub_detector_consistent(monkeypatch):
    mgr = RerunManager({}, default_detector_config=DETECTOR_YAML)
    monkeypatch.setattr(
        "webui.rerun._new_detector",
        lambda *a, **k: StubDet(3, windows=[{"start": 4, "end": 10, "kind": "repet"}]),
    )
    lp = np.full((12, 2), -0.1, dtype=np.float32)
    ti = np.tile(np.array([[7, 99]], dtype=np.int32), (12, 1))
    rec = make_record(
        topk_logprobs=lp.tolist(), tokens_ids=ti.tolist(), text_tokenid=[7] * 12
    )
    loc = mgr.rerun_localization(rec)
    assert loc["source"] == "rerun"
    assert loc["consistent"] is True
    assert loc["rerun_mode"] == "无词表"
    assert loc["windows"] == [{"start": 4, "end": 10, "kind": "repet"}]
    assert loc["hits"] == []
    # 无 tokenizer → 无字符对齐（§6.2 降级口径）
    assert loc["char_offsets"] is None
    assert loc["aligned_text"] is False


def test_rerun_with_stub_detector_inconsistent(monkeypatch):
    mgr = RerunManager({}, default_detector_config=DETECTOR_YAML)
    monkeypatch.setattr(
        "webui.rerun._new_detector",
        lambda *a, **k: StubDet(1, hits=[{"idx": 2, "token_id": 7, "logp_top1": -8.0}]),
    )
    lp = np.full((6, 2), -0.1, dtype=np.float32)
    ti = np.tile(np.array([[7, 99]], dtype=np.int32), (6, 1))
    rec = make_record(topk_logprobs=lp.tolist(), tokens_ids=ti.tolist())
    loc = mgr.rerun_localization(rec)
    assert loc["consistent"] is False
    assert len(loc["hits"]) == 1
    assert loc["hits"][0]["idx"] == 2


def test_rerun_heuristic_fallback_repetition(tmp_path):
    # 检测器配置不存在 → 检测器重跑不可用，回退启发式（token 序列即可）
    mgr = RerunManager({}, default_detector_config=str(tmp_path / "nope.yaml"))
    rec = make_record(text_tokenid=[1, 2, 3, 1, 2, 3, 1, 2, 3])
    loc = mgr.rerun_localization(rec)
    assert loc is not None
    assert loc["windows"] == [{"start": 0, "end": 9, "kind": "repet"}]
    assert loc["consistent"] is None  # 检测器未参与，无从比对
    assert "启发式" in loc["rerun_mode"]
    assert "启发式兜底" in loc["rerun_note"]


def test_rerun_heuristic_fallback_rare_with_topk(tmp_path):
    mgr = RerunManager({}, default_detector_config=str(tmp_path / "nope.yaml"))
    rec = make_record(
        ill_type="rare_character",
        topk_logprobs=[[-1.0, -2.0], [-5.0, -0.5], [-0.2, -9.0]],
        tokens_ids=[[10, 11], [12, 13], [14, 15]],
    )
    loc = mgr.rerun_localization(rec)
    assert loc is not None
    assert [h["idx"] for h in loc["hits"]] == [0, 1, 2]
    assert loc["windows"] == []


def test_rerun_returns_none_for_nan_or_insufficient():
    mgr = RerunManager({}, default_detector_config="")
    assert mgr.rerun_localization(make_record(ill_type="nan_value")) is None
    assert mgr.rerun_localization(make_record(ill_type="unknown")) is None
    # 无 topk / 无序列 / 非重复类型 → 启发式也无产出
    assert mgr.rerun_localization(make_record(ill_type="garbled")) is None
    assert mgr.rerun_localization({}) is None


def test_rerun_repetition_no_real_repeat_returns_none(tmp_path):
    mgr = RerunManager({}, default_detector_config=str(tmp_path / "nope.yaml"))
    rec = make_record(text_tokenid=[1, 2, 3, 4, 5, 6])
    assert mgr.rerun_localization(rec) is None


# --------------------------------------------------------------------------- #
# models 配置解析
# --------------------------------------------------------------------------- #

def _base_config_dict(**over):
    data = {
        "auth": {"username": "admin", "password": "x"},
        "poll": {},
        "store": {},
        "instances": [],
        "webhooks": {},
        "email": {},
        "alerts": [],
    }
    data.update(over)
    return data


def test_models_config_parse():
    cfg = WebUIConfig.from_dict(_base_config_dict(
        models={"Qwen3-0.6B": {"tokenizer_path": "/models/q", "token2category_path": "/t/tk2cat.json"}}
    ))
    models = dict(cfg.models)
    assert models["Qwen3-0.6B"].tokenizer_path == "/models/q"
    assert models["Qwen3-0.6B"].detector_config == ""


def test_models_config_empty_ok():
    cfg = WebUIConfig.from_dict(_base_config_dict())
    assert cfg.models == ()


def test_models_config_invalid():
    with pytest.raises(ConfigError):
        WebUIConfig.from_dict(_base_config_dict(models={"": {}}))
    with pytest.raises(ConfigError):
        WebUIConfig.from_dict(_base_config_dict(models={"Q": {"tokenizer_path": "  "}}))
    with pytest.raises(ConfigError):
        WebUIConfig.from_dict(_base_config_dict(models="not-a-map"))


# --------------------------------------------------------------------------- #
# 详情端点按需重跑（单飞 / 负缓存 / 回写缓存）
# --------------------------------------------------------------------------- #

async def _login(client):
    r = await client.post("/api/login", json={"username": "admin", "password": "test123"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _fake_loc():
    return {
        "detector_ver": 2, "sequence_ref": "text_tokenid", "reasoning_end": None,
        "hits": [], "windows": [{"start": 0, "end": 2, "kind": "repet"}],
        "char_offsets": None, "align_text": None, "text_spans": None,
        "reasoning_spans": None, "aligned_text": False, "aligned_reasoning": False,
        "source": "rerun", "consistent": None, "rerun_mode": "启发式", "rerun_note": "",
    }


async def test_detail_rerun_on_cached_imported(monkeypatch, tmp_path):
    import httpx

    from webui.main import create_app

    cfg_path = tmp_path / "webui.yaml"
    write_yaml(str(cfg_path), build_webui_config_dict())
    app = create_app(str(cfg_path))
    ctx = app.state.ctx
    await ctx.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            h = await _login(client)
            await client.post("/api/instances", json={"name": "a", "url": "http://127.0.0.1:1"}, headers=h)
            ctx.store.add_anomaly_meta(
                "a", {"id": 5, "ts": 1.0, "ill_type": "repetition", "model": "m", "source": "imported"}
            )
            ctx.store.put_anomaly_detail("a", 5, make_record(id=5))
            calls = []
            monkeypatch.setattr(
                ctx.rerun, "rerun_localization",
                lambda rec: (calls.append(rec.get("id")), _fake_loc())[1],
            )
            r = await client.get("/api/instances/a/anomalies/5", headers=h)
            assert r.status_code == 200
            body = r.json()
            assert body["localization"]["source"] == "rerun"
            assert calls == [5]
            # 结果已回写缓存：第二次点击不再重跑
            r2 = await client.get("/api/instances/a/anomalies/5", headers=h)
            assert r2.json()["localization"]["source"] == "rerun"
            assert calls == [5]
            assert ctx.store.get_anomaly_detail("a", 5)["localization"]["source"] == "rerun"
    finally:
        await ctx.stop()


async def test_detail_rerun_negative_cache(monkeypatch, tmp_path):
    import httpx

    from webui.main import create_app

    cfg_path = tmp_path / "webui.yaml"
    write_yaml(str(cfg_path), build_webui_config_dict())
    app = create_app(str(cfg_path))
    ctx = app.state.ctx
    await ctx.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            h = await _login(client)
            await client.post("/api/instances", json={"name": "a", "url": "http://127.0.0.1:1"}, headers=h)
            ctx.store.put_anomaly_detail("a", 5, make_record(id=5))
            calls = []
            monkeypatch.setattr(
                ctx.rerun, "rerun_localization",
                lambda rec: (calls.append(rec.get("id")), None)[1],
            )
            r = await client.get("/api/instances/a/anomalies/5", headers=h)
            assert r.status_code == 200
            assert r.json()["localization"] is None
            r2 = await client.get("/api/instances/a/anomalies/5", headers=h)
            assert r2.status_code == 200
            assert calls == [5]  # 负缓存：失败后不重复重试
    finally:
        await ctx.stop()


# --------------------------------------------------------------------------- #
# 导入联动：同 id 覆盖（含 live 记录），表格立即可见
# --------------------------------------------------------------------------- #

async def test_import_upsert_replaces_colliding_live_meta(tmp_path):
    """导入 pkl 的 id 与实时轮询已拉到的记录撞车时，导入元数据覆盖旧记录。

    场景：webui 重启后 since_id 归零 → 历史异常以 source=live 进表格；
    再导入同一份 pkl → 同 id 记录被 source=imported 覆盖（而非被去重拒绝）。
    """
    import pickle as _pkl

    import httpx

    from webui.main import create_app

    cfg_path = tmp_path / "webui.yaml"
    write_yaml(str(cfg_path), build_webui_config_dict())
    app = create_app(str(cfg_path))
    ctx = app.state.ctx
    await ctx.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            h = await _login(client)
            await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
            # 模拟实时轮询已拉到 id 1（live）
            ctx.store.add_anomaly_meta(
                "x", {"id": 1, "ts": 100.0, "ill_type": "repetition", "model": "model-a", "source": "live"}
            )
            body = _pkl.dumps({
                1: {"time": 200.0, "prompt": "p", "ill_type": 3,
                    "text": "abc", "model_name": "model-a"},
            })
            r = await client.post("/api/import?instance=x", headers=h, content=body)
            assert r.status_code == 200
            assert r.json()["anomalies_linked"] == 1
            # 表格中 id=1 已被导入记录覆盖（不再有 live 版本）
            recs = ctx.store.recent_anomalies("x", 100)
            assert len(recs) == 1
            assert recs[0]["source"] == "imported"
            assert recs[0]["ts"] == 200.0
    finally:
        await ctx.stop()


async def test_import_meta_carries_hit_count(tmp_path):
    """导入元数据带 hit_count（localization 命中 + 窗口数），供表格位置数列展示。"""
    import pickle as _pkl

    import httpx

    from webui.main import create_app

    cfg_path = tmp_path / "webui.yaml"
    write_yaml(str(cfg_path), build_webui_config_dict())
    app = create_app(str(cfg_path))
    ctx = app.state.ctx
    await ctx.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            h = await _login(client)
            await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
            loc = {"detector_ver": 2, "hits": [{"idx": 1}], "windows": [{"start": 0, "end": 4}]}
            body = _pkl.dumps({
                9: {"time": 100.0, "prompt": "p", "ill_type": 2,
                    "text": "t", "model_name": "model-a", "localization": loc},
            })
            r = await client.post("/api/import?instance=x", headers=h, content=body)
            assert r.status_code == 200
            recs = ctx.store.recent_anomalies("x", 100)
            assert recs[0]["hit_count"] == 2
    finally:
        await ctx.stop()


# --------------------------------------------------------------------------- #
# collector：with_data 追加重跑输入大字段
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _StubClient:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.urls = []

    async def get(self, url):
        self.urls.append(url)
        return _Resp(self._payloads.pop(0))

    async def aclose(self):
        pass


def _make_collector(payloads):
    from webui.alerts import AlertEngine

    store = Store(StoreConfig())
    coll = Collector(
        store=store,
        synthesizer=EventSynthesizer(),
        alerts=AlertEngine(store.alerts),
        poll_cfg=PollConfig(),
        client_factory=lambda t: _StubClient(payloads),
    )
    coll.start()
    return coll


async def test_fetch_anomaly_detail_with_data_old_record():
    light = {"time": 1.0, "ill_type": 3, "model_name": "m", "prompt": "p",
             "text": "t", "reasoning_content": None}
    full = dict(light, topk_logprobs=[[0.0]], tokens_ids=[[1]], text_tokenid=[1])
    coll = _make_collector([light, full])
    inst = types.SimpleNamespace(name="a", url="http://x:1")
    detail = await coll.fetch_anomaly_detail(inst, 3, with_data=True)
    assert detail["localization"] is None
    assert detail["topk_logprobs"] == [[0.0]]
    assert detail["tokens_ids"] == [[1]]
    assert detail["text_tokenid"] == [1]
    assert coll._client.urls[-1].endswith("include=tokens,logprobs")


async def test_fetch_anomaly_detail_with_data_new_record_skips_refetch():
    light = {"time": 1.0, "ill_type": 3, "model_name": "m", "prompt": "p",
             "text": "t", "reasoning_content": None,
             "localization": {"detector_ver": 2}}
    coll = _make_collector([light])
    inst = types.SimpleNamespace(name="a", url="http://x:1")
    detail = await coll.fetch_anomaly_detail(inst, 3, with_data=True)
    assert detail["localization"] == {"detector_ver": 2}
    assert "topk_logprobs" not in detail
    assert len(coll._client.urls) == 1  # 新版记录带定位 → 不追加 include 拉取
