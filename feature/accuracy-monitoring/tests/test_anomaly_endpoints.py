"""异常记录端点单元测试：GET /anomaly/anomalies（列表/增量/未开启保存/磁盘载入）
+ GET /anomaly/anomalies/{id}（详情/include/404/400/自定义路径）。
"""
from __future__ import annotations

import json
import pickle

import pytest

from anomaly_middleware import AnomalyMiddleware
from anomaly_middleware.anomaly_store import AnomalyStore
from anomaly_middleware.env import PluginConfig
from tests._helpers import FakeVLLM


def _make_mw(fake_app, store=None, **cfg):
    import os

    old = os.environ.get("VLLM_ANOMALY_ENABLED")
    os.environ["VLLM_ANOMALY_ENABLED"] = "0"
    try:
        mw = AnomalyMiddleware(fake_app)
    finally:
        if old is None:
            os.environ.pop("VLLM_ANOMALY_ENABLED", None)
        else:
            os.environ["VLLM_ANOMALY_ENABLED"] = old
    mw.config = PluginConfig(
        enabled=cfg.get("enabled", True),
        top_logprobs=cfg.get("top_logprobs", 20),
        monitor_rate=cfg.get("monitor_rate", 1.0),
        config_path=cfg.get("config_path", "/anomaly/config"),
        anomalies_path=cfg.get("anomalies_path", "/anomaly/anomalies"),
    )
    mw._monitor_rate = mw.config.monitor_rate
    mw.metrics.set_monitor_rate(mw._monitor_rate)
    mw._anomaly_store = store
    return mw


async def _drive(mw, method, path, query=b""):
    sent = []

    async def send(m):
        sent.append(m)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "headers": [[b"content-type", b"application/json"]],
    }
    msg = {"delivered": False}

    async def receive():
        if not msg["delivered"]:
            msg["delivered"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    await mw(scope, receive, send)
    return sent


def _body(sent) -> dict:
    return json.loads(sent[1]["body"])


def _store_with_records(n: int = 3) -> AnomalyStore:
    return _MemStore(n)


class _MemStore(AnomalyStore):
    """内存镜像注入版（_synced=True 防止端点触发磁盘加载覆盖）。"""

    def __init__(self, n: int) -> None:
        super().__init__(None)  # enabled=False，仅借用结构
        self.enabled = True
        self._synced = True
        for i in range(1, n + 1):
            self._data[i] = {
                "time": 1000.0 + i,
                "prompt": [{"role": "user", "content": f"q{i}"}],
                "ill_type": 2,
                "topk_logprobs": [[[0.1, 0.2]]],
                "tokens_ids": [[[1, 2]]],
                "text": f"answer {i}",
                "text_tokenid": [1],
                "reasoning_content": f"think {i}" if i % 2 else None,
                "model_name": "m",
            }
        self._counter = n


# --------------------------- 列表端点 --------------------------- #
@pytest.mark.asyncio
async def test_list_returns_records_ascending():
    mw = _make_mw(FakeVLLM(lambda *a: None), store=_store_with_records(3))
    sent = await _drive(mw, "GET", "/anomaly/anomalies")
    assert sent[0]["status"] == 200
    body = _body(sent)
    assert body["enabled"] is True
    assert body["latest_id"] == 3
    assert body["total"] == 3
    assert [r["id"] for r in body["records"]] == [1, 2, 3]
    r0 = body["records"][0]
    assert set(r0.keys()) == {"id", "time", "ill_type", "model_name", "hit_count"}
    assert r0["ill_type"] == 2 and r0["model_name"] == "m"
    assert r0["hit_count"] == 0  # 旧版记录无 localization → 0


@pytest.mark.asyncio
async def test_list_since_id_incremental():
    mw = _make_mw(FakeVLLM(lambda *a: None), store=_store_with_records(3))
    sent = await _drive(mw, "GET", "/anomaly/anomalies", query=b"since_id=2")
    body = _body(sent)
    assert [r["id"] for r in body["records"]] == [3]
    assert body["total"] == 1
    assert body["latest_id"] == 3


@pytest.mark.asyncio
async def test_list_limit_keeps_latest():
    mw = _make_mw(FakeVLLM(lambda *a: None), store=_store_with_records(5))
    sent = await _drive(mw, "GET", "/anomaly/anomalies", query=b"limit=2")
    body = _body(sent)
    assert [r["id"] for r in body["records"]] == [4, 5]
    assert body["total"] == 5  # 满足条件的总数不受 limit 截断影响


@pytest.mark.asyncio
async def test_list_limit_clamped():
    mw = _make_mw(FakeVLLM(lambda *a: None), store=_store_with_records(2))
    sent = await _drive(mw, "GET", "/anomaly/anomalies", query=b"limit=99999")
    body = _body(sent)
    assert len(body["records"]) == 2  # 夹取到上限内


@pytest.mark.asyncio
async def test_list_store_absent_reports_disabled():
    mw = _make_mw(FakeVLLM(lambda *a: None), store=None)
    sent = await _drive(mw, "GET", "/anomaly/anomalies")
    body = _body(sent)
    assert body == {"enabled": False, "latest_id": 0, "total": 0, "records": []}


@pytest.mark.asyncio
async def test_list_loads_from_disk_on_first_access(tmp_path):
    p = tmp_path / "anomalies.pkl"
    data = {
        1: {"time": 1.0, "ill_type": 1, "model_name": "m", "text": "x"},
        2: {"time": 2.0, "ill_type": 3, "model_name": "m"},
    }
    p.write_bytes(pickle.dumps(data))
    store = AnomalyStore(str(p))  # 文件存在但尚未同步（模拟重启后首次访问）
    mw = _make_mw(FakeVLLM(lambda *a: None), store=store)
    sent = await _drive(mw, "GET", "/anomaly/anomalies")
    body = _body(sent)
    assert body["enabled"] is True
    assert body["latest_id"] == 2
    assert [r["id"] for r in body["records"]] == [1, 2]


# --------------------------- 详情端点 --------------------------- #
@pytest.mark.asyncio
async def test_detail_returns_light_fields():
    mw = _make_mw(FakeVLLM(lambda *a: None), store=_store_with_records(3))
    sent = await _drive(mw, "GET", "/anomaly/anomalies/1")
    assert sent[0]["status"] == 200
    body = _body(sent)
    assert body["id"] == 1
    assert body["time"] == 1001.0
    assert body["ill_type"] == 2
    assert body["prompt"] == [{"role": "user", "content": "q1"}]
    assert body["text"] == "answer 1"
    assert body["reasoning_content"] == "think 1"
    # 大字段默认不返回
    assert "topk_logprobs" not in body
    assert "tokens_ids" not in body
    assert "text_tokenid" not in body


@pytest.mark.asyncio
async def test_detail_include_tokens_and_logprobs():
    mw = _make_mw(FakeVLLM(lambda *a: None), store=_store_with_records(2))
    sent = await _drive(mw, "GET", "/anomaly/anomalies/1",
                        query=b"include=tokens,logprobs")
    body = _body(sent)
    assert body["text_tokenid"] == [1]
    assert body["topk_logprobs"] == [[[0.1, 0.2]]]
    assert body["tokens_ids"] == [[[1, 2]]]


@pytest.mark.asyncio
async def test_detail_not_found():
    mw = _make_mw(FakeVLLM(lambda *a: None), store=_store_with_records(2))
    sent = await _drive(mw, "GET", "/anomaly/anomalies/99")
    assert sent[0]["status"] == 404
    assert "99" in _body(sent)["error"]


@pytest.mark.asyncio
async def test_detail_invalid_id_returns_400():
    mw = _make_mw(FakeVLLM(lambda *a: None), store=_store_with_records(2))
    sent = await _drive(mw, "GET", "/anomaly/anomalies/abc")
    assert sent[0]["status"] == 400


@pytest.mark.asyncio
async def test_detail_store_absent_returns_404():
    mw = _make_mw(FakeVLLM(lambda *a: None), store=None)
    sent = await _drive(mw, "GET", "/anomaly/anomalies/1")
    assert sent[0]["status"] == 404


# --------------------------- 自定义路径 --------------------------- #
@pytest.mark.asyncio
async def test_custom_anomalies_path():
    mw = _make_mw(FakeVLLM(lambda *a: ("json", {"ok": True})),
                  store=_store_with_records(1),
                  anomalies_path="/custom/anom")
    sent = await _drive(mw, "GET", "/custom/anom")
    assert sent[0]["status"] == 200
    assert _body(sent)["enabled"] is True
    # 默认路径不再拦截 → 透传下游
    sent2 = await _drive(mw, "GET", "/anomaly/anomalies")
    assert _body(sent2) == {"ok": True}
