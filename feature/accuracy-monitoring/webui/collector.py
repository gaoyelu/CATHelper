"""collector：轮询各实例 `/anomaly/metrics` + 解析 Prometheus 文本 + 快照 → 事件流。

- 每个活跃实例持有独立 asyncio 轮询 task；新增/恢复创建（建基线），暂停取消
  （保留数据），删除取消并清除数据；单实例慢不阻塞整体。
- 解析复用 prometheus_client.parser；个别序列损坏跳过，其余照常。
- 失败/超时/非 200 → 实例标记离线，快照保留上次值，不影响其他实例。
- 计数器回绕/实例重启（值变小）→ 事件合成忽略负增量。
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import events as events_mod
from .alerts import AlertEngine
from .config import ILL_TYPES, PollConfig
from .events import DeltaSummary, EventSynthesizer, Snapshot
from .store import Store, TrendPoint

logger = logging.getLogger("webui.collector")

REQUESTS_METRIC = "vllm_anomaly_requests_total"
DETECTED_METRIC = "vllm_anomaly_detected_total"
ERRORS_METRIC = "vllm_anomaly_detection_errors_total"
DURATION_METRIC = "vllm_anomaly_detection_duration_seconds"

ANOMALIES_PATH = "/anomaly/anomalies"
# 单次增量拉取的异常元数据条数上限（首轮补齐最多拉这么多，其余靠后续轮询）
ANOMALIES_PULL_LIMIT = 500

# gauge 指标名 → 异常类型名
GAUGE_NAMES = {
    "vllm_anomaly_last_rare_character": "rare_character",
    "vllm_anomaly_last_garbled": "garbled",
    "vllm_anomaly_last_repetition": "repetition",
    "vllm_anomaly_last_nan_value": "nan_value",
}


class MetricsParseError(Exception):
    """Prometheus 文本整体不可解析。"""


class PollError(Exception):
    """实例轮询失败（超时/非 200 等）。"""


def _canonical_family_name(full_name: str) -> str:
    """prometheus_client 解析器对 counter 族返回去掉 `_total` 后缀的规范名。

    较新版本（>=0.16）family.name 为规范名（如 `vllm_anomaly_requests`），
    而样本 s.name 仍为完整名（`vllm_anomaly_requests_total`）。
    统一按「完整名 或 规范名」匹配，兼容两代解析器。
    """
    return full_name[:-6] if full_name.endswith("_total") else full_name


def _family_matches(family_name: str, full_name: str) -> bool:
    return family_name == full_name or family_name == _canonical_family_name(full_name)


def _histogram_quantile(buckets: List[Tuple[float, float]], q: float, count: int) -> float:
    """基于累积桶线性插值求分位数（Prometheus histogram_quantile 近似）。"""
    if count <= 0 or not buckets:
        return 0.0
    rank = q * count
    last_finite: Optional[float] = None
    prev_le: Optional[float] = None
    prev_cum = 0.0
    for le, c in buckets:
        if math.isinf(le):
            continue
        last_finite = le
        if c >= rank:
            if prev_le is None or c == prev_cum:
                return le
            return prev_le + (le - prev_le) * ((rank - prev_cum) / (c - prev_cum))
        prev_le, prev_cum = le, c
    return last_finite if last_finite is not None else 0.0


def _histogram_stats(samples: Iterable[Any]) -> Dict[str, float]:
    """从直方图样本计算 count/sum/mean/p50/p95。"""
    buckets: List[Tuple[float, float]] = []
    total = 0.0
    total_sum = 0.0
    for s in samples:
        nm = s.name
        le = s.labels.get("le") if s.labels else None
        try:
            if nm.endswith("_bucket") and le is not None:
                le_val = float("inf") if le == "+Inf" else float(le)
                buckets.append((le_val, float(s.value)))
            elif nm.endswith("_sum"):
                total_sum = float(s.value)
            elif nm.endswith("_count"):
                total = float(s.value)
        except (TypeError, ValueError):
            continue
    buckets.sort(key=lambda x: x[0])
    count = int(total)
    mean = total_sum / count if count else 0.0
    return {
        "count": count,
        "sum": total_sum,
        "mean": mean,
        "p50": _histogram_quantile(buckets, 0.50, count),
        "p95": _histogram_quantile(buckets, 0.95, count),
    }


def parse_metrics_text(text: str) -> Dict[str, Any]:
    """解析 Prometheus 文本为计数器/仪表盘/直方图快照字典。

    返回 {requests, errors, detected{(ill,model,choice):n}, gauges{(gauge,model):0/1},
    hist_stats{count,sum,mean,p50,p95} | None}。
    个别序列损坏 → 跳过该序列记日志；整体不可解析 → 抛 MetricsParseError。
    """
    from prometheus_client.parser import text_string_to_metric_families

    requests = 0
    errors = 0
    detected: Dict[Tuple[int, str, str], int] = {}
    gauges: Dict[Tuple[str, str], int] = {}
    hist_samples: List[Any] = []

    try:
        families = text_string_to_metric_families(text)
        for family in families:
            try:
                name = family.name
                if _family_matches(name, REQUESTS_METRIC):
                    for s in family.samples:
                        requests = int(float(s.value))
                        break
                elif _family_matches(name, ERRORS_METRIC):
                    for s in family.samples:
                        errors = int(float(s.value))
                        break
                elif _family_matches(name, DETECTED_METRIC):
                    for s in family.samples:
                        try:
                            ill = int(s.labels["ill_type"])
                            model = s.labels["model"]
                            choice = s.labels["choice_index"]
                            detected[(ill, model, choice)] = int(float(s.value))
                        except (KeyError, TypeError, ValueError) as exc:
                            logger.warning("跳过损坏的 detected 序列 %s: %s", s.name, exc)
                elif name in GAUGE_NAMES:
                    for s in family.samples:
                        try:
                            model = s.labels["model"]
                            gauges[(GAUGE_NAMES[name], model)] = int(float(s.value))
                        except (KeyError, TypeError, ValueError) as exc:
                            logger.warning("跳过损坏的 gauge 序列 %s: %s", s.name, exc)
                elif name == DURATION_METRIC:
                    hist_samples.extend(family.samples)
            except Exception as exc:  # noqa: BLE001 —— 个别 family 损坏不拖垮整体
                logger.warning("跳过指标 family %s: %s", getattr(family, "name", "?"), exc)
    except Exception as exc:  # noqa: BLE001 —— 整体文本不可解析
        raise MetricsParseError(f"Prometheus 文本解析失败: {exc}") from exc

    hist_stats = _histogram_stats(hist_samples) if hist_samples else None
    return {
        "requests": requests,
        "errors": errors,
        "detected": detected,
        "gauges": gauges,
        "hist_stats": hist_stats,
    }


class Collector:
    """轮询调度 + 快照解析 + 事件/趋势/告警落盘 + 轮询 task 生命周期管理。"""

    def __init__(
        self,
        *,
        store: Store,
        synthesizer: EventSynthesizer,
        alerts: AlertEngine,
        poll_cfg: PollConfig,
        client_factory=None,
    ) -> None:
        self._store = store
        self._synth = synthesizer
        self._alerts = alerts
        self._poll = poll_cfg
        self._client_factory = client_factory or _default_client_factory
        self._client = None
        self._tasks: Dict[str, asyncio.Task] = {}
        self._instances: Dict[str, Any] = {}  # name -> InstanceConfig
        self._anomaly_since: Dict[str, int] = {}  # name -> 已拉取的最新异常编号

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._client is None:
            self._client = self._client_factory(self._poll.http_timeout_seconds)

    async def shutdown(self) -> None:
        for name, t in list(self._tasks.items()):
            t.cancel()
            self._tasks.pop(name, None)
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ #
    # 实例 apply（统一入口；幂等 no-op）
    # ------------------------------------------------------------------ #
    def apply_instances(self, instances: List[Any]) -> None:
        """desired-state 收敛：增/删/暂停/恢复/URL 变更。重复应用相同列表为幂等 no-op。"""
        if self._client is None:
            self.start()
        new_map = {i.name: i for i in instances}

        # 删除：取消 task + 清除数据
        for name in [n for n in self._instances if n not in new_map]:
            self._teardown_task(name, purge=True)

        for name, inst in new_map.items():
            prev = self._instances.get(name)
            if inst.paused:
                if name in self._tasks:
                    self._teardown_task(name, purge=False)
                self._store.set_state(name, "paused")
            else:
                reconfigured = prev is not None and prev.url != inst.url
                if name not in self._tasks or reconfigured:
                    if name in self._tasks:
                        self._teardown_task(name, purge=False)
                    self._synth.reset(name)  # 首次/恢复/重配：下一份快照仅作基线
                    self._store.set_state(name, "offline")
                    self._tasks[name] = asyncio.create_task(self._poll_loop(inst))
                self._store.set_url(name, inst.url)
        self._instances = new_map

    def _teardown_task(self, name: str, purge: bool) -> None:
        t = self._tasks.pop(name, None)
        if t is not None:
            t.cancel()
        self._synth.reset(name)
        if purge:
            self._store.purge_instance(name)
        elif name in self._instances:
            pass

    # ------------------------------------------------------------------ #
    # 轮询
    # ------------------------------------------------------------------ #
    async def poll_once(self, name: str) -> Optional[DeltaSummary]:
        """同步执行一次轮询（供测试）。返回 DeltaSummary 或 None（基线轮）。"""
        inst = self._instances.get(name)
        if inst is None:
            return None
        return await self._poll_once(inst)

    async def _poll_loop(self, inst) -> None:
        while True:
            try:
                await self._poll_once(inst)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 轮询失败绝不抛到主流程
                logger.warning("实例 %s 轮询失败: %s", inst.name, exc)
                self._store.set_state(inst.name, "offline")
            await asyncio.sleep(self._poll.interval_seconds)

    async def _poll_once(self, inst) -> Optional[DeltaSummary]:
        url = inst.url.rstrip("/") + "/anomaly/metrics"
        resp = await self._client.get(url)
        if resp.status_code != 200:
            raise PollError(f"实例 {inst.name} 返回 HTTP {resp.status_code}")
        try:
            parsed = parse_metrics_text(resp.text)
        except MetricsParseError as exc:
            raise PollError(f"实例 {inst.name} 指标解析失败: {exc}") from exc

        snapshot = Snapshot(
            instance=inst.name,
            requests=parsed["requests"],
            errors=parsed["errors"],
            detected=parsed["detected"],
            gauges=parsed["gauges"],
            duration=parsed["hist_stats"],
        )
        self._store.set_state(inst.name, "online")
        # 记录 metrics 观测到的模型名（含正常请求；供导入模型对应校验使用）
        self._store.observe_models(inst.name, snapshot.models)

        delta = self._synth.process(snapshot)
        # 异常元数据增量拉取（基线轮也执行；失败只影响 feed 状态，不影响指标链路）
        await self._poll_anomalies(inst)
        if delta is None:
            return None
        self._store.record_delta(inst.name, delta)
        self._record_trends(snapshot, delta)
        for e in delta.events:
            self._alerts.ingest(e)
        return delta

    # ------------------------------------------------------------------ #
    # 异常记录增量拉取（/anomaly/anomalies）
    # ------------------------------------------------------------------ #
    async def _poll_anomalies(self, inst) -> None:
        """按 since_id 增量拉取实例异常元数据并入环形缓冲。

        feed 状态机：ok / disabled（实例未开启保存）/ unavailable（端点缺失、
        非 200、解析失败、网络异常）。任何失败不影响实例 online 状态。
        """
        name = inst.name
        url = inst.url.rstrip("/") + ANOMALIES_PATH
        since = self._anomaly_since.get(name, 0)
        url += f"?since_id={since}&limit={ANOMALIES_PULL_LIMIT}"
        try:
            resp = await self._client.get(url)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._store.set_anomaly_feed(name, "unavailable")
            return
        if resp.status_code == 404:
            self._store.set_anomaly_feed(name, "unavailable")  # 旧版中间件无此端点
            return
        if resp.status_code != 200:
            self._store.set_anomaly_feed(name, "unavailable")
            return
        try:
            data = resp.json()
        except Exception:
            self._store.set_anomaly_feed(name, "unavailable")
            return
        if not isinstance(data, dict) or data.get("enabled") is not True:
            self._store.set_anomaly_feed(name, "disabled")
            return
        records = data.get("records")
        if not isinstance(records, list):
            self._store.set_anomaly_feed(name, "unavailable")
            return
        latest = int(data.get("latest_id") or 0)
        if latest < since:
            # 实例侧编号回退（pkl 被清理/重建）→ 重置游标，接受重新拉取
            since = latest
        max_id = since
        for r in records:
            if not isinstance(r, dict):
                continue
            try:
                meta = {
                    "id": int(r["id"]),
                    "ts": float(r["time"]),
                    "ill_type": events_mod.ill_type_name(int(r["ill_type"])),
                    "model": str(r.get("model_name") or "unknown"),
                    "source": "live",
                }
            except (KeyError, TypeError, ValueError):
                continue
            hc = r.get("hit_count")
            if hc is not None:
                try:
                    meta["hit_count"] = int(hc)
                except (TypeError, ValueError):
                    pass
            self._store.add_anomaly_meta(name, meta)
            if meta["id"] > max_id:
                max_id = meta["id"]
        self._anomaly_since[name] = max_id
        self._store.set_anomaly_feed(name, "ok")

    async def _get_anomaly_json(self, inst, anomaly_id: int, include: str = "") -> Any:
        """拉取单条异常记录 JSON；失败抛 PollError。include 附加查询参数。"""
        url = inst.url.rstrip("/") + f"{ANOMALIES_PATH}/{int(anomaly_id)}"
        if include:
            url += f"?include={include}"
        try:
            resp = await self._client.get(url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PollError(f"实例 {inst.name} 异常详情拉取失败: {exc}") from exc
        if resp.status_code == 404:
            raise PollError(f"异常 {anomaly_id} 不存在（实例可能已清理历史数据）")
        if resp.status_code != 200:
            raise PollError(f"实例 {inst.name} 返回 HTTP {resp.status_code}")
        try:
            rec = resp.json()
        except Exception as exc:
            raise PollError(f"实例 {inst.name} 异常详情解析失败: {exc}") from exc
        if not isinstance(rec, dict):
            raise PollError("异常详情响应不是 JSON 对象")
        return rec

    async def fetch_anomaly_detail(
        self, inst, anomaly_id: int, with_data: bool = False
    ) -> Dict[str, Any]:
        """按需拉取单条异常完整记录。

        常规返回轻量字段（不含 tokens/logprobs 大字段）；``with_data=True`` 且记录
        无 localization（旧版中间件）时追加拉取 `?include=tokens,logprobs` 的重跑
        输入大字段（topk_logprobs/tokens_ids/text_tokenid），供 webui 方案 B 离线
        重跑定位（design_anomaly_localization.md §6.2）。

        返回归一化 dict：{id, ts, ill_type(字符串名), model, prompt, text,
        reasoning_content, localization[, topk_logprobs, tokens_ids, text_tokenid]}。
        失败抛 PollError。
        """
        rec = await self._get_anomaly_json(inst, anomaly_id)
        try:
            ill_name = events_mod.ill_type_name(int(rec.get("ill_type")))
        except (TypeError, ValueError):
            ill_name = "unknown"
        loc = rec.get("localization")
        detail: Dict[str, Any] = {
            "id": int(anomaly_id),
            "ts": rec.get("time"),
            "ill_type": ill_name,
            "model": str(rec.get("model_name") or "unknown"),
            "prompt": rec.get("prompt"),
            "text": rec.get("text"),
            "reasoning_content": rec.get("reasoning_content"),
            # 检出位置定位数据（旧版中间件记录无此字段 → None，前端降级展示）
            "localization": loc if isinstance(loc, dict) else None,
        }
        if with_data and detail["localization"] is None:
            rec2 = await self._get_anomaly_json(
                inst, anomaly_id, include="tokens,logprobs"
            )
            detail["topk_logprobs"] = rec2.get("topk_logprobs")
            detail["tokens_ids"] = rec2.get("tokens_ids")
            detail["text_tokenid"] = rec2.get("text_tokenid")
        return detail

    def _record_trends(self, snapshot: Snapshot, delta: DeltaSummary) -> None:
        models = snapshot.models
        if not models:
            models = ["unknown"]
        for m in models:
            pm = delta.per_model.get(m)
            point = TrendPoint(
                ts=snapshot.ts,
                requests=float(delta.requests),
                errors=float(delta.errors),
                anomalies=float(sum(pm.values())) if pm else 0.0,
                by_type={t: float(pm.get(t, 0)) if pm else 0.0 for t in ILL_TYPES},
            )
            self._store.record_trend(snapshot.instance, m, point)


def _default_client_factory(timeout: float):
    import httpx

    return httpx.AsyncClient(timeout=timeout)