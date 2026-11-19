"""API 集成测试：认证、查询端点结构、实例管理端点、trends 白名单、yaml 写回。"""
from __future__ import annotations

import time

import yaml
import pytest
import httpx
from fastapi import FastAPI

from webui.main import create_app
from tests._webui_helpers import build_webui_config_dict, write_yaml


@pytest.fixture
async def app_client(tmp_path):
    cfg_path = str(tmp_path / "webui.yaml")
    write_yaml(cfg_path, build_webui_config_dict())
    app = create_app(cfg_path)
    ctx = app.state.ctx
    await ctx.start()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, ctx, cfg_path
    await ctx.stop()


async def _login(client):
    r = await client.post("/api/login", json={"username": "admin", "password": "test123"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ------------------------------------------------------------------ #
# 认证
# ------------------------------------------------------------------ #
async def test_apis_require_auth(app_client):
    client, _, _ = app_client
    for path in ("/api/summary", "/api/instances", "/api/events", "/api/alerts",
                 "/api/trends?window=1day"):
        r = await client.get(path)
        assert r.status_code == 401, path


async def test_login_wrong_credentials(app_client):
    client, _, _ = app_client
    r = await client.post("/api/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


async def test_login_success(app_client):
    client, _, _ = app_client
    r = await client.post("/api/login", json={"username": "admin", "password": "test123"})
    assert r.status_code == 200
    assert "token" in r.json()


async def test_login_does_not_leak_user_existence(app_client):
    client, _, _ = app_client
    r1 = await client.post("/api/login", json={"username": "ghost", "password": "x"})
    r2 = await client.post("/api/login", json={"username": "admin", "password": "x"})
    assert r1.status_code == r2.status_code == 401


# ------------------------------------------------------------------ #
# 查询端点结构
# ------------------------------------------------------------------ #
async def test_summary_structure(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    r = await client.get("/api/summary", headers=h)
    assert r.status_code == 200
    data = r.json()
    for k in ("requests", "anomalies", "anomaly_rate", "errors",
              "by_type", "by_instance", "instances", "updated_at"):
        assert k in data
    assert set(data["by_type"].keys()) == {"rare_character", "garbled", "repetition", "nan_value"}


async def test_summary_reflects_store(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    from webui.events import AnomalyEvent, DeltaSummary

    d = DeltaSummary(requests=5)
    for i in range(2):
        d.events.append(AnomalyEvent(id=i + 1, ts=1.0, instance="a", model="m",
                                     ill_type="garbled", choice_index="0"))
    d.anomalies_total = 2
    d.by_type["garbled"] = 2
    d.by_model["m"] = 2
    ctx.store.record_delta("a", d)
    r = await client.get("/api/summary", headers=h)
    data = r.json()
    assert data["requests"] == 5
    assert data["anomalies"] == 2
    assert data["by_type"]["garbled"] == 2
    assert data["by_instance"] == {"a": 2}


async def test_events_and_alerts_require_auth_then_return_lists(app_client):
    client, ctx, _ = app_client
    r = await client.get("/api/events")
    assert r.status_code == 401
    h = await _login(client)
    r = await client.get("/api/events", headers=h)
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    r = await client.get("/api/alerts", headers=h)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_events_limit_clamped(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    from webui.events import AnomalyEvent

    for i in range(30):
        ctx.store.add_event(AnomalyEvent(id=i + 1, ts=float(i), instance="a", model="m",
                                         ill_type="garbled", choice_index="0"))
    r = await client.get("/api/events?limit=10", headers=h)
    assert len(r.json()) == 10
    r = await client.get("/api/events?limit=99999", headers=h)
    assert len(r.json()) == 30  # 上限 1000 以内


async def test_trends_window_whitelist(app_client):
    client, _, _ = app_client
    h = await _login(client)
    for w in ("1day", "7day", "30day"):
        r = await client.get(f"/api/trends?window={w}", headers=h)
        assert r.status_code == 200, w
    r = await client.get("/api/trends?window=3h", headers=h)
    assert r.status_code == 400
    r = await client.get("/api/trends?window=bogus", headers=h)
    assert r.status_code == 400


async def test_instances_structure(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    ctx.cm.last_config  # ensure loaded
    ctx.store.set_state("x", "online")
    r = await client.get("/api/instances", headers=h)
    assert r.status_code == 200
    for item in r.json():
        for k in ("name", "url", "paused", "state", "requests", "anomalies", "last_event"):
            assert k in item


# ------------------------------------------------------------------ #
# 实例详情端点（summary / trends / events）
# ------------------------------------------------------------------ #
async def test_instance_summary_requires_auth_then_returns_detail(app_client):
    client, ctx, _ = app_client
    r = await client.get("/api/instances/x/summary")
    assert r.status_code == 401
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    from webui.events import AnomalyEvent, DeltaSummary

    d = DeltaSummary(requests=7)
    d.events.append(AnomalyEvent(id=1, ts=1.0, instance="x", model="m",
                                 ill_type="garbled", choice_index="0"))
    d.anomalies_total = 1
    d.by_type["garbled"] = 1
    d.by_model["m"] = 1
    ctx.store.record_delta("x", d)
    ctx.store.set_state("x", "online")
    r = await client.get("/api/instances/x/summary", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "x"
    assert data["requests"] == 7
    assert data["anomalies"] == 1
    assert data["by_type"]["garbled"] == 1
    assert data["by_model"] == {"m": 1}
    assert data["models"] == ["m"]
    r = await client.get("/api/instances/nope/summary", headers=h)
    assert r.status_code == 404


async def test_instance_trends(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    import time
    from webui.events import AnomalyEvent, DeltaSummary

    ts = time.time() - 10  # 落在 1day 窗口内的最近事件
    d = DeltaSummary(requests=1)
    d.events.append(AnomalyEvent(id=1, ts=ts, instance="x", model="m",
                                 ill_type="garbled", choice_index="0"))
    ctx.store.record_delta("x", d)
    r = await client.get("/api/instances/x/trends?window=1day", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["window"] == "1day"
    assert data["points"][0]["cumulative"] == 1
    assert data["points"][0]["ill_type"] == "garbled"
    assert data["points"][0]["model"] == "m"
    # 其他实例的数据不被混入
    d2 = DeltaSummary(requests=1)
    d2.events.append(AnomalyEvent(id=2, ts=ts, instance="other", model="m",
                                  ill_type="nan_value", choice_index="0"))
    ctx.store.record_delta("other", d2)
    r = await client.get("/api/instances/x/trends?window=1day", headers=h)
    pts = r.json()["points"]
    assert len(pts) == 1
    assert pts[0]["cumulative"] == 1
    # window 白名单 + 实例不存在
    r = await client.get("/api/instances/x/trends?window=3h", headers=h)
    assert r.status_code == 400
    r = await client.get("/api/instances/nope/trends?window=1day", headers=h)
    assert r.status_code == 404


async def test_instance_events_filtered(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    from webui.events import AnomalyEvent

    ctx.store.add_event(AnomalyEvent(id=1, ts=1.0, instance="x", model="m",
                                     ill_type="garbled", choice_index="0"))
    ctx.store.add_event(AnomalyEvent(id=2, ts=2.0, instance="y", model="m",
                                     ill_type="nan_value", choice_index="0"))
    r = await client.get("/api/instances/x/events?limit=10", headers=h)
    assert r.status_code == 200
    items = r.json()
    assert [e["instance"] for e in items] == ["x"]
    r = await client.get("/api/instances/nope/events", headers=h)
    assert r.status_code == 404


# ------------------------------------------------------------------ #
# 异常实例端点（元数据列表 + 详情）
# ------------------------------------------------------------------ #
async def test_instance_anomalies_requires_auth(app_client):
    client, _, _ = app_client
    r = await client.get("/api/instances/a/anomalies")
    assert r.status_code == 401
    r = await client.get("/api/instances/a/anomalies/1")
    assert r.status_code == 401


async def test_instance_anomalies_list_and_detail(app_client):
    client, ctx, _ = app_client
    h = await _login(client)

    # 未认证外的 404：实例不存在
    r = await client.get("/api/instances/nope/anomalies", headers=h)
    assert r.status_code == 404
    r = await client.get("/api/instances/nope/anomalies/1", headers=h)
    assert r.status_code == 404

    await client.post("/api/instances", json={"name": "a", "url": "http://127.0.0.1:1"}, headers=h)

    # 元数据列表（初始 feed=unknown）
    ctx.store.add_anomaly_meta("a", {"id": 1, "ts": 1.0, "ill_type": "garbled", "model": "m"})
    ctx.store.add_anomaly_meta("a", {"id": 2, "ts": 2.0, "ill_type": "nan_value", "model": "m"})
    r = await client.get("/api/instances/a/anomalies", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["instance"] == "a"
    assert data["feed"] == "unknown"
    assert [x["id"] for x in data["records"]] == [2, 1]  # id 倒序
    assert data["records"][0]["ill_type"] == "nan_value"

    # limit 裁剪
    r = await client.get("/api/instances/a/anomalies?limit=1", headers=h)
    assert len(r.json()["records"]) == 1

    # 详情：缓存命中
    ctx.store.put_anomaly_detail("a", 2, {
        "id": 2, "ts": 2.0, "ill_type": "nan_value", "model": "m",
        "prompt": "q", "text": "t", "reasoning_content": None,
    })
    r = await client.get("/api/instances/a/anomalies/2", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["prompt"] == "q" and body["text"] == "t"

    # 详情：未缓存 + 实例不可达 → 502（fetch_anomaly_detail 抛 PollError）
    r = await client.get("/api/instances/a/anomalies/99", headers=h)
    assert r.status_code == 502

    # 非法编号 → 422（FastAPI int 转换）
    r = await client.get("/api/instances/a/anomalies/abc", headers=h)
    assert r.status_code == 422


async def test_instance_anomalies_detail_disabled_feed_404(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "a", "url": "http://127.0.0.1:1"}, headers=h)
    ctx.store.set_anomaly_feed("a", "disabled")
    r = await client.get("/api/instances/a/anomalies/1", headers=h)
    assert r.status_code == 404
    assert "未开启异常保存" in r.json()["detail"]


async def test_instance_response_contains_anomaly_feed(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "a", "url": "http://127.0.0.1:1"}, headers=h)
    r = await client.get("/api/instances/a/summary", headers=h)
    assert r.status_code == 200
    assert r.json()["anomaly_feed"] == "unknown"
    ctx.store.set_anomaly_feed("a", "ok")
    r = await client.get("/api/instances", headers=h)
    feeds = {i["name"]: i["anomaly_feed"] for i in r.json()}
    assert feeds["a"] == "ok"


# ------------------------------------------------------------------ #
# 实例管理端点
# ------------------------------------------------------------------ #
async def test_add_instance(app_client):
    client, ctx, cfg_path = app_client
    h = await _login(client)
    r = await client.post("/api/instances", json={"name": "prod-1", "url": "http://10.0.0.5:8000"}, headers=h)
    assert r.status_code == 201
    assert r.json()["name"] == "prod-1"
    # yaml 已写回
    with open(cfg_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert [i["name"] for i in data["instances"]] == ["prod-1"]
    # 内存配置同步
    assert "prod-1" in ctx.current_instances()


async def test_add_instance_duplicate_409(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "p", "url": "http://x:1"}, headers=h)
    r = await client.post("/api/instances", json={"name": "p", "url": "http://y:2"}, headers=h)
    assert r.status_code == 409


async def test_add_instance_invalid_400(app_client):
    client, _, _ = app_client
    h = await _login(client)
    r = await client.post("/api/instances", json={"name": "", "url": "http://x:1"}, headers=h)
    assert r.status_code == 400
    r = await client.post("/api/instances", json={"name": "q", "url": "not-a-url"}, headers=h)
    assert r.status_code == 400


async def test_delete_instance(app_client):
    client, ctx, cfg_path = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "del-me", "url": "http://x:1"}, headers=h)
    r = await client.delete("/api/instances/del-me", headers=h)
    assert r.status_code == 204
    assert "del-me" not in ctx.current_instances()
    r = await client.get("/api/instances", headers=h)
    assert all(i["name"] != "del-me" for i in r.json())


async def test_delete_instance_404(app_client):
    client, _, _ = app_client
    h = await _login(client)
    r = await client.delete("/api/instances/missing", headers=h)
    assert r.status_code == 404


async def test_pause_resume_flow(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "p", "url": "http://x:1"}, headers=h)
    r = await client.post("/api/instances/p/pause", headers=h)
    assert r.status_code == 200
    assert r.json()["paused"] is True
    r = await client.post("/api/instances/p/pause", headers=h)
    assert r.status_code == 409  # 已暂停
    r = await client.post("/api/instances/p/resume", headers=h)
    assert r.status_code == 200
    assert r.json()["paused"] is False
    r = await client.post("/api/instances/p/resume", headers=h)
    assert r.status_code == 409  # 未暂停


async def test_pause_missing_404(app_client):
    client, _, _ = app_client
    h = await _login(client)
    r = await client.post("/api/instances/nope/pause", headers=h)
    assert r.status_code == 404


async def test_delete_purges_instance_data(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "p", "url": "http://x:1"}, headers=h)
    from webui.events import AnomalyEvent, DeltaSummary

    d = DeltaSummary(requests=1)
    d.events.append(AnomalyEvent(id=1, ts=1.0, instance="p", model="m",
                                 ill_type="garbled", choice_index="0"))
    d.anomalies_total = 1
    d.by_type["garbled"] = 1
    d.by_model["m"] = 1
    ctx.store.record_delta("p", d)
    assert ctx.store.summary()["anomalies"] == 1

    await client.delete("/api/instances/p", headers=h)
    assert ctx.store.instance_stats("p") is None
    assert all(e.instance != "p" for e in ctx.store.recent_events(10))
    assert ctx.store.summary()["anomalies"] == 0
    assert ctx.store.summary()["by_instance"] == {}


async def test_add_then_instance_visible_in_list(app_client):
    client, _, _ = app_client
    h = await _login(client)
    r = await client.get("/api/instances", headers=h)
    assert r.json() == []
    await client.post("/api/instances", json={"name": "n", "url": "http://x:1"}, headers=h)
    r = await client.get("/api/instances", headers=h)
    assert [i["name"] for i in r.json()] == ["n"]


async def test_static_pages_public(app_client):
    client, _, _ = app_client
    r = await client.get("/")
    assert r.status_code == 200
    assert "推理精度异常监控" in r.text
    r = await client.get("/js/app.js")
    assert r.status_code == 200
    r = await client.get("/css/style.css")
    assert r.status_code == 200


# ------------------------------------------------------------------ #
# 趋势自定义区间（custom + start/end）与窗口分类统计
# ------------------------------------------------------------------ #
def _seed_trend_events(store, now):
    from webui.store import TrendEvent

    store.append_trend_events(
        [
            TrendEvent(ts=now - 100, instance="x", model="m",
                       ill_type="garbled", source="live"),
            TrendEvent(ts=now - 50, instance="x", model="m",
                       ill_type="garbled", source="live"),
            TrendEvent(ts=now - 40, instance="other", model="m",
                       ill_type="nan_value", source="live"),
        ],
        now=now,
    )


async def test_trends_custom_range_returns_window_stats(app_client):
    import time

    client, ctx, _ = app_client
    h = await _login(client)
    now = time.time()
    _seed_trend_events(ctx.store, now)
    r = await client.get(
        f"/api/trends?window=custom&start={now - 100}&end={now - 10}",
        headers=h,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["window"] == "custom"
    assert data["window_seconds"] is None
    assert abs(data["start"] - (now - 100)) < 1
    assert abs(data["end"] - (now - 10)) < 1
    assert data["total"] == 3
    assert data["by_type"]["garbled"] == 2
    assert data["by_type"]["nan_value"] == 1
    assert data["by_type"]["repetition"] == 0
    # start == data_start（完全覆盖）→ 不置 partial
    assert data["partial"] is False
    assert data["data_start"] is not None
    # points 结构向后兼容
    assert data["points"][-1]["cumulative"] == 3


async def test_trends_custom_range_validation_400(app_client):
    import time

    client, _, _ = app_client
    h = await _login(client)
    now = time.time()
    base = "/api/trends?window=custom"
    # 缺 start/end
    r = await client.get(base, headers=h)
    assert r.status_code == 400
    r = await client.get(base + f"&start={now - 100}", headers=h)
    assert r.status_code == 400
    # 非数值
    r = await client.get(base + "&start=abc&end=123", headers=h)
    assert r.status_code == 400
    # 负数
    r = await client.get(base + "&start=-5&end=100", headers=h)
    assert r.status_code == 400
    # start >= end
    r = await client.get(base + f"&start={now}&end={now}", headers=h)
    assert r.status_code == 400
    r = await client.get(base + f"&start={now}&end={now - 100}", headers=h)
    assert r.status_code == 400
    # 结束晚于当前时间（容差 60s 之外）
    r = await client.get(base + f"&start={now}&end={now + 3600}", headers=h)
    assert r.status_code == 400


async def test_trends_partial_coverage_when_range_precedes_data(app_client):
    import time

    client, ctx, _ = app_client
    h = await _login(client)
    now = time.time()
    _seed_trend_events(ctx.store, now)
    data_start = ctx.store.query_trend_window(now - 3600, now)["data_start"]
    # 区间起点早于实际数据：部分交集 → partial=True，仍返回区间内实际数据
    r = await client.get(
        f"/api/trends?window=custom&start={data_start - 86400}&end={now - 10}",
        headers=h,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["partial"] is True
    assert abs(data["data_start"] - data_start) < 1
    assert data["total"] == 3
    # 完全早于数据：空结果 + partial
    r2 = await client.get(
        f"/api/trends?window=custom&start={data_start - 90000}&end={data_start - 100}",
        headers=h,
    )
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["partial"] is True
    assert data2["total"] == 0
    assert data2["points"] == []


async def test_trends_preset_includes_window_stats(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    _seed_trend_events(ctx.store, time.time())
    r = await client.get("/api/trends?window=1day", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["window"] == "1day"
    assert data["window_seconds"] == 86400
    assert data["total"] == 3
    assert data["by_type"]["garbled"] == 2
    assert "partial" in data and "data_start" in data


async def test_instance_trends_custom_scope_by_type(app_client):
    import time

    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    now = time.time()
    _seed_trend_events(ctx.store, now)
    r = await client.get(
        f"/api/instances/x/trends?window=custom&start={now - 200}&end={now - 10}",
        headers=h,
    )
    assert r.status_code == 200
    data = r.json()
    # 仅统计该实例
    assert data["total"] == 2
    assert data["by_type"]["garbled"] == 2
    assert data["by_type"]["nan_value"] == 0
    # 其他实例不可越权查询不存在的实例
    r2 = await client.get(
        f"/api/instances/nope/trends?window=custom&start={now - 200}&end={now - 10}",
        headers=h,
    )
    assert r2.status_code == 404


# ------------------------------------------------------------------ #
# 历史导入模型对应校验（严格匹配，失败不合入）
# ------------------------------------------------------------------ #
def _import_body(models, n=2):
    import io
    import pickle

    d = {}
    for i in range(n):
        d[i + 1] = {
            "time": time.time() - 60,
            "prompt": "p",
            "ill_type": (i % 4) + 1,
            "topk_logprobs": [],
            "tokens_ids": [],
            "text": "t",
            "model_name": models[i % len(models)],
        }
    return pickle.dumps(d)


def _seed_observed_model(ctx, instance, model):
    from webui.events import AnomalyEvent, DeltaSummary

    d = DeltaSummary(requests=1)
    d.events.append(AnomalyEvent(id=1, ts=time.time() - 30, instance=instance, model=model,
                                 ill_type="garbled", choice_index="0"))
    d.anomalies_total = 1
    d.by_type["garbled"] = 1
    d.by_model[model] = 1
    ctx.store.record_delta(instance, d)


async def test_import_nonexistent_instance_404_no_write(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    before = ctx.store.summary()["anomalies"]
    r = await client.post("/api/import?instance=ghost", headers=h,
                          content=_import_body(["model-a"]))
    assert r.status_code == 404
    assert ctx.store.summary()["anomalies"] == before


async def test_import_model_mismatch_rejected_no_write(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    _seed_observed_model(ctx, "x", "model-a")
    before = ctx.store.summary()

    # 完全无交集
    r = await client.post("/api/import?instance=x", headers=h,
                          content=_import_body(["model-b"]))
    assert r.status_code == 400
    assert "模型不匹配" in r.json()["detail"]
    assert "model-b" in r.json()["detail"]
    # 部分交集也整体拒绝（严格子集）
    r2 = await client.post("/api/import?instance=x", headers=h,
                           content=_import_body(["model-a", "model-b"], n=4))
    assert r2.status_code == 400
    assert "model-b" in r2.json()["detail"]

    # 两次失败导入均零写入：累计/事件/趋势不变
    after = ctx.store.summary()
    assert after["anomalies"] == before["anomalies"]
    assert after["by_type"] == before["by_type"]
    assert all(e.instance != "x" or getattr(e, "source", "live") != "imported"
               for e in ctx.store.recent_events(100))
    assert not [p for p in ctx.store.query_trend_window(0, time.time() + 60)["points"]
                if p["source"] == "imported"]


async def test_import_model_match_success(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    _seed_observed_model(ctx, "x", "model-a")
    r = await client.post("/api/import?instance=x", headers=h,
                          content=_import_body(["model-a"], n=3))
    assert r.status_code == 200
    data = r.json()
    assert data["imported"] == 3
    assert data["warning"] is None
    assert ctx.store.summary()["anomalies"] == 4  # 1 观测 + 3 导入
    assert ctx.store.instance_stats("x").by_model == {"model-a": 4}


async def test_import_preserves_localization(app_client):
    """新版 pkl（记录带 localization）：导入后详情原样保留，供前端位置高亮。"""
    import pickle as _pkl

    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    _seed_observed_model(ctx, "x", "model-a")
    loc = {
        "detector_ver": 2, "sequence_ref": "text_tokenid", "reasoning_end": None,
        "hits": [{"idx": 1, "token_id": 30, "logp_top1": -8.2,
                  "cats": ["chinese_cjk"], "topk_ids": [30], "title": "t"}],
        "windows": [], "char_offsets": [0, 1], "align_text": "ab",
        "text_spans": [{"start": 1, "end": 2, "kind": "hit", "ref": 1, "title": "t"}],
        "reasoning_spans": None, "aligned_text": True, "aligned_reasoning": False,
    }
    body = _pkl.dumps({7: {
        "time": time.time() - 60, "prompt": "p", "ill_type": 1,
        "text": "ab", "model_name": "model-a", "localization": loc,
    }})
    r = await client.post("/api/import?instance=x", headers=h, content=body)
    assert r.status_code == 200
    assert r.json()["anomalies_linked"] == 1
    # 详情联动：localization 原样保留
    r2 = await client.get("/api/instances/x/anomalies/7", headers=h)
    assert r2.status_code == 200
    d = r2.json()
    assert d["localization"] == loc
    assert d["ill_type"] == "rare_character"


async def test_import_legacy_pkl_without_localization(app_client):
    """旧版 pkl（无 localization 字段）：导入成功，详情 localization 为 None。"""
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    _seed_observed_model(ctx, "x", "model-a")
    r = await client.post("/api/import?instance=x", headers=h,
                          content=_import_body(["model-a"]))
    assert r.status_code == 200
    r2 = await client.get("/api/instances/x/anomalies/1", headers=h)
    assert r2.status_code == 200
    assert r2.json()["localization"] is None


async def test_import_legacy_pkl_preserves_rerun_inputs(app_client, monkeypatch):
    """旧版 pkl（无 localization）导入：重跑输入字段保留进详情，供方案 B
    首次点击详情时离线重跑定位（models 配置生效路径）。"""
    import pickle as _pkl

    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    _seed_observed_model(ctx, "x", "model-a")
    body = _pkl.dumps({9: {
        "time": time.time() - 60, "prompt": "p", "ill_type": 3,
        "text": "abcabc", "model_name": "model-a",
        "topk_logprobs": [[-0.1, -0.9]], "tokens_ids": [[5, 6]],
        "text_tokenid": [5, 6],
    }})
    r = await client.post("/api/import?instance=x", headers=h, content=body)
    assert r.status_code == 200
    assert r.json()["anomalies_linked"] == 1
    captured = []
    monkeypatch.setattr(
        ctx.rerun, "rerun_localization",
        lambda rec: (captured.append(rec), None)[1],
    )
    r2 = await client.get("/api/instances/x/anomalies/9", headers=h)
    assert r2.status_code == 200
    d = r2.json()
    assert d["localization"] is None
    assert d["topk_logprobs"] == [[-0.1, -0.9]]
    assert d["tokens_ids"] == [[5, 6]]
    assert d["text_tokenid"] == [5, 6]
    # 方案 B 重跑被触发，且重跑输入完整可用
    assert len(captured) == 1
    assert captured[0]["topk_logprobs"] == [[-0.1, -0.9]]
    assert captured[0]["tokens_ids"] == [[5, 6]]
    assert captured[0]["text_tokenid"] == [5, 6]


async def test_import_instance_without_observed_models_allowed(app_client):
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "fresh", "url": "http://y:1"}, headers=h)
    r = await client.post("/api/import?instance=fresh", headers=h,
                          content=_import_body(["model-a"]))
    assert r.status_code == 200
    data = r.json()
    assert data["imported"] == 2
    assert data["warning"]
    assert "未做模型对应校验" in data["warning"]


async def test_import_validates_online_instance_without_anomalies(app_client):
    """在线但零异常的实例：观测模型来自 metrics（models_seen），导入同样严格校验。"""
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    # 模拟轮询观测：实例在线、有 served-model-name、但从未检出异常
    ctx.store.set_state("x", "online")
    ctx.store.observe_models("x", ["served-model-1"])
    before = ctx.store.summary()["anomalies"]

    # 模型不匹配 → 拒绝且零写入
    r = await client.post("/api/import?instance=x", headers=h,
                          content=_import_body(["other-model"]))
    assert r.status_code == 400
    assert "served-model-1" in r.json()["detail"]
    assert ctx.store.summary()["anomalies"] == before

    # 模型匹配 → 正常导入，无 warning
    r2 = await client.post("/api/import?instance=x", headers=h,
                           content=_import_body(["served-model-1"], n=2))
    assert r2.status_code == 200
    assert r2.json()["warning"] is None
    assert ctx.store.summary()["anomalies"] == before + 2


async def test_instance_models_includes_metrics_observed(app_client):
    """实例列表的模型名列：零异常但 metrics 观测到模型时也能显示。"""
    client, ctx, _ = app_client
    h = await _login(client)
    await client.post("/api/instances", json={"name": "x", "url": "http://x:1"}, headers=h)
    ctx.store.set_state("x", "online")
    ctx.store.observe_models("x", ["served-model-1"])
    r = await client.get("/api/instances", headers=h)
    item = [i for i in r.json() if i["name"] == "x"][0]
    assert item["models"] == ["served-model-1"]
    assert item["anomalies"] == 0