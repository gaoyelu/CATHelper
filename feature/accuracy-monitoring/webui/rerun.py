"""方案 B：读取时重跑定位（design_anomaly_localization.md §4.1 / §6.2 / §11.4）。

旧版 pkl / 旧版中间件记录无 ``localization`` 字段 → webui 详情端点首次命中时
按需重跑：按保存的 ``topk_logprobs``/``tokens_ids`` 重跑检测器（有词表 → 无词表
依序尝试），取回命中/窗口，复用 ``anomaly_middleware.localization.build_localization``
换算 token→字符对齐与高亮区间；结果写回详情 LRU 缓存（调用方负责），每条只算一次。

口径与降级（与报告工具 tools/anomaly_report.py 一致）：

- 检测器重跑结果带 ``consistent`` 标记（与线上检出 ill_type 比对，R2 风险对策），
  两种模式均不一致时仍采用首个可用结果，前端明确标注「重跑口径」；
- 检测器不可用（缺配置/缺 numpy/构造失败）或重跑无位置产出 → 启发式兜底：
  重复 find_repetition_window（token 序列，无需 tokenizer）、
  乱码 find_garbled_window（需逐 token 文本，即 tokenizer）、
  生僻字 top1 logp 最可疑位置（需 topk 数据）；
- ``models`` 配置提供 tokenizer_path 时逐 token 解码 → char_offsets/align_text/
  text_spans/reasoning_spans 全量定位；未提供 → 仅 token 索引级数据（hits/windows），
  语义视图无高亮（§6.2 降级口径「纯数据展示，无 token 文本」）。

重跑为 CPU 密集操作（numpy + tokenizer），调用方应放线程池执行（asyncio.to_thread）；
本模块全部方法为纯同步、无 IO 网络操作，失败不抛异常（返回 None / 降级块）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ModelRerunConfig

logger = logging.getLogger("webui.rerun")

try:
    import numpy as np
except ImportError:  # pragma: no cover - webui 最小部署时才可能缺失
    np = None  # type: ignore[assignment]

from anomaly_middleware.localization import build_localization
from anomaly_middleware.token_resolver import TokenTextResolver

# 异常类型名 → 检测器编号（与 events.ill_type_name 互逆）
ILL_NAME_TO_INT = {
    "rare_character": 1,
    "garbled": 2,
    "repetition": 3,
    "nan_value": 4,
}

ILL_INT_NAMES = {1: "生僻字", 2: "乱码", 3: "重复", 4: "NaN"}

# 检测序列长度上限（防御异常形态数据；与 localization.MAX_ALIGN_TOKENS 对齐）
MAX_SEQ_TOKENS = 20000

# 重跑操作数上限（find_repetition_window 防御）
RERUN_OP_CAP = 2_000_000

# 生僻字启发式兜底的最大命中数（top1 logp 最可疑位置）
RARE_MAX_HITS = 20

# 仓库默认检测器配置（webui 包上一级 configs/detector.yaml）
DEFAULT_DETECTOR_CONFIG = str(
    Path(__file__).resolve().parent.parent / "configs" / "detector.yaml"
)


def _repo_default_detector_config() -> str:
    """返回默认检测器配置路径；文件不存在返回空串（检测器重跑不可用）。"""
    return DEFAULT_DETECTOR_CONFIG if os.path.isfile(DEFAULT_DETECTOR_CONFIG) else ""


# --------------------------------------------------------------------------- #
# 数据归一化（口径同 tools/anomaly_report.py）
# --------------------------------------------------------------------------- #

def _as_float_list(row: Any) -> Tuple[Optional[List[float]], Optional[List[int]]]:
    """topk_logprobs 行归一化：dict{token_id:logp} 或 list -> (logps, token_ids)。"""
    if isinstance(row, dict):
        items: List[Tuple[int, float]] = []
        for k, v in row.items():
            try:
                items.append((int(k), float(v)))
            except (TypeError, ValueError):
                continue
        items.sort(key=lambda x: -x[1])
        return [v for _, v in items], [k for k, _ in items]
    if isinstance(row, (list, tuple)):
        try:
            return [float(v) for v in row], None
        except (TypeError, ValueError):
            return None, None
    return None, None


def build_topk_arrays(
    topk_logprobs: Any, tokens_ids: Any
) -> Tuple[Optional["Any"], Optional["Any"]]:
    """构造检测器输入 (logprobs, token_ids) ndarray，按 logprob 降序排列。

    任一行非法/为空 → (None, None)；行内 token_ids 缺失时以 -1 填充占位。
    """
    if np is None:
        return None, None
    if not isinstance(topk_logprobs, (list, tuple)) or not topk_logprobs:
        return None, None
    lp_rows: List[List[float]] = []
    ti_rows: List[List[int]] = []
    for i, prow in enumerate(topk_logprobs):
        ps, ts = _as_float_list(prow)
        if ps is None:
            return None, None
        trow = tokens_ids[i] if isinstance(tokens_ids, (list, tuple)) and i < len(tokens_ids) else None
        if ts is None:
            if isinstance(trow, (list, tuple)):
                try:
                    ts = [int(v) for v in trow]
                except (TypeError, ValueError):
                    ts = [-1] * len(ps)
            else:
                ts = [-1] * len(ps)
        elif trow is not None and isinstance(trow, (list, tuple)):
            try:
                ts = [int(v) for v in trow]
            except (TypeError, ValueError):
                pass
        n = min(len(ps), len(ts))
        if n <= 0:
            return None, None
        lp_rows.append([float(v) for v in ps[:n]])
        ti_rows.append([int(v) for v in ts[:n]])
    k = min(len(r) for r in lp_rows)
    if k <= 0:
        return None, None
    lp = np.array([r[:k] for r in lp_rows], dtype=np.float32)
    ti = np.array([r[:k] for r in ti_rows], dtype=np.int32)
    order = np.argsort(-lp, axis=1, kind="stable")
    lp = np.take_along_axis(lp, order, axis=1)
    ti = np.take_along_axis(ti, order, axis=1)
    return lp, ti


def get_seq_ids(text_tokenid: Any, tokens_ids: Any) -> List[int]:
    """展示序列 token id：优先 text_tokenid（实际生成序列），回退 tokens_ids top-1 列。"""
    tt = text_tokenid
    if isinstance(tt, (list, tuple)) and tt:
        out: List[int] = []
        for v in tt[:MAX_SEQ_TOKENS]:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                out.append(-1)
        return out
    rows = tokens_ids
    if isinstance(rows, (list, tuple)) and rows:
        first = rows[0]
        if isinstance(first, (list, tuple)):
            col: List[int] = []
            for r in rows[:MAX_SEQ_TOKENS]:
                if isinstance(r, (list, tuple)) and r:
                    try:
                        col.append(int(r[0]))
                    except (TypeError, ValueError):
                        col.append(-1)
                else:
                    col.append(-1)
            return col
        if isinstance(first, int) and not isinstance(first, bool):
            out = []
            for v in rows[:MAX_SEQ_TOKENS]:
                try:
                    out.append(int(v))
                except (TypeError, ValueError):
                    out.append(-1)
            return out
    return []


# --------------------------------------------------------------------------- #
# 启发式兜底定位（口径同 tools/anomaly_report.py）
# --------------------------------------------------------------------------- #

def find_repetition_window(
    seq: List[int], max_period: int = 32, op_cap: int = RERUN_OP_CAP
) -> Optional[Tuple[int, int]]:
    """启发式：最大周期重复窗口（窗口覆盖 token 数最多）。返回 (start, end) 或 None。"""
    T = len(seq)
    best: Optional[Tuple[int, int, int]] = None  # (span, start, end)
    ops = 0
    for p in range(1, max_period + 1):
        if T < 2 * p:
            break
        i = 0
        while i + 2 * p <= T:
            if seq[i : i + p] != seq[i + p : i + 2 * p]:
                i += 1
                ops += 1
                continue
            blocks = 2
            j = i + 2 * p
            while j + p <= T and seq[i + (blocks - 1) * p : i + blocks * p] == seq[j : j + p]:
                blocks += 1
                j += p
                ops += 1
            span = p * blocks
            if best is None or span > best[0]:
                best = (span, i, i + span)
            i += max(1, p)
            if ops > op_cap:
                return (best[1], best[2]) if best else None
    return (best[1], best[2]) if best else None


def _is_unusual(text: str) -> bool:
    if not text:
        return False
    odd = sum(
        1
        for ch in text
        if not (
            "\u4e00" <= ch <= "\u9fff"
            or ch.isascii() and ch.isalnum()
            or ch in " ，。、；：！？,.!?;:'\"()[]{}<>-_=+/\\|@#$%^&*~`…《》"
        )
    )
    return odd / len(text) > 0.5


def find_garbled_window(texts: List[str]) -> Optional[Tuple[int, int]]:
    """启发式：最长连续异常 token 段（>=3）。返回 (start, end) 或 None。"""
    best: Optional[Tuple[int, int]] = None
    i, T = 0, len(texts)
    while i < T:
        if _is_unusual(texts[i]):
            j = i
            while j < T and _is_unusual(texts[j]):
                j += 1
            if best is None or (j - i) > (best[1] - best[0]):
                best = (i, j)
            i = j
        else:
            i += 1
    return best if (best and best[1] - best[0] >= 3) else None


def rare_top_positions(
    lp: Any, ti: Any, max_hits: int = RARE_MAX_HITS
) -> List[Dict[str, Any]]:
    """生僻字启发式兜底：top1 logp 最低（最不可能）的前 max_hits 个位置。"""
    if lp is None or ti is None:
        return []
    n = lp.shape[0]
    top1 = lp[:, 0]
    order = list(range(n))
    order.sort(key=lambda i: float(top1[i]))
    out: List[Dict[str, Any]] = []
    for i in order[: max(0, max_hits)]:
        out.append(
            {
                "idx": int(i),
                "token_id": int(ti[i, 0]),
                "logp_top1": float(top1[i]),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 检测器构建（lazy import，供测试 monkeypatch）
# --------------------------------------------------------------------------- #

def _new_detector(config_path: str, tk2cat: Any = None, vocab_size: Any = None) -> Any:
    """构建 ILLDetector（可注入词表）；失败抛异常，由调用方捕获降级。"""
    from anomaly_middleware.detector import ILLDetector

    det = ILLDetector(config_path)
    if tk2cat is not None and vocab_size is not None and hasattr(det, "set_vocabulary"):
        det.set_vocabulary(tk2cat, vocab_size)
    return det


def _load_token2category_file(path: str) -> Tuple[Optional[Dict[str, str]], Optional[int]]:
    """加载预生成词表类别映射 JSON `{str(token_id): category}`（口径同 proxy_integration）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, None
    if not isinstance(data, dict) or not data:
        return None, None
    tk2cat: Dict[str, str] = {}
    max_id = -1
    for k, v in data.items():
        try:
            tid = int(k)
        except (TypeError, ValueError):
            continue
        tk2cat[str(tid)] = str(v)
        if tid > max_id:
            max_id = tid
    if not tk2cat:
        return None, None
    return tk2cat, max_id + 1


class _StubResult:
    """检测器返回形态的轻量结构（仅本模块内部使用）。"""

    __slots__ = ("is_ill", "ill_type", "hits", "windows")

    def __init__(
        self,
        is_ill: bool,
        ill_type: int,
        hits: List[Dict[str, Any]],
        windows: List[Dict[str, Any]],
    ) -> None:
        self.is_ill = is_ill
        self.ill_type = ill_type
        self.hits = hits
        self.windows = windows


# --------------------------------------------------------------------------- #
# ModelRerunner：单模型的惰性运行时（检测器 + tokenizer + tk2cat）
# --------------------------------------------------------------------------- #

class ModelRerunner:
    """单个模型的重跑运行时；全部资源惰性构建（首次重跑时），线程锁保护。"""

    def __init__(self, model: str, cfg: Optional[ModelRerunConfig],
                 default_detector_config: str) -> None:
        self.model = model
        self._cfg = cfg or ModelRerunConfig()
        self._detector_config = (self._cfg.detector_config or default_detector_config)
        self._lock = threading.Lock()
        self._loaded = False
        self._tokenizer: Any = None          # transformers tokenizer 或 None
        self._tok_note = ""                  # tokenizer 加载失败原因（记日志/降级说明）
        self._tk2cat: Optional[Dict[str, str]] = None
        self._vocab_size: Optional[int] = None
        self._tk2cat_note = ""
        self._resolver: Optional[TokenTextResolver] = None
        self._det_novocab: Any = None
        self._det_vocab: Any = None
        self._load_error = ""

    # ---- 资源加载（首次调用时同步构建） ---- #
    def ensure_loaded(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            self._load_resources()

    def _load_resources(self) -> None:
        # tokenizer（供 token 文本解码 + 有词表模式）
        tok_path = self._cfg.tokenizer_path
        if tok_path:
            try:
                from transformers import AutoTokenizer

                self._tokenizer = AutoTokenizer.from_pretrained(
                    str(tok_path), local_files_only=True, trust_remote_code=True
                )
                self._resolver = TokenTextResolver(self._tokenizer)
                logger.info(
                    "模型 %s tokenizer 已加载（无字符对齐问题时可对照检查此行）: %s",
                    self.model, tok_path,
                )
            except Exception as exc:
                self._tok_note = f"tokenizer 加载失败（无 token 文本对齐）: {exc}"
                logger.warning("模型 %s tokenizer 加载失败: %s", self.model, exc)
        else:
            logger.warning(
                "模型 %s 未配置 tokenizer_path（models 段缺该键或键名不匹配）→ 无字符对齐",
                self.model,
            )
        # tk2cat：文件优先，回退由 tokenizer 现场生成
        if self._cfg.token2category_path:
            tk2cat, vocab = _load_token2category_file(self._cfg.token2category_path)
            if tk2cat is not None:
                self._tk2cat, self._vocab_size = tk2cat, vocab
            else:
                self._tk2cat_note = "token2category 文件加载失败"
                logger.warning(
                    "模型 %s token2category 文件加载失败: %s",
                    self.model, self._cfg.token2category_path,
                )
        if self._tk2cat is None and self._tokenizer is not None:
            try:
                from anomaly_middleware.token_categorizer import generate_tk2cat

                tk2cat, vocab = generate_tk2cat(self._tokenizer)
                if tk2cat:
                    self._tk2cat, self._vocab_size = tk2cat, vocab
            except Exception as exc:
                self._tk2cat_note = self._tk2cat_note or "由 tokenizer 生成词表类别失败"
                logger.warning("模型 %s 由 tokenizer 生成词表类别失败: %s", self.model, exc)
        # 检测器
        if not self._detector_config or not os.path.isfile(self._detector_config):
            self._load_error = self._load_error or "检测器配置不存在，重跑不可用"
            logger.warning("模型 %s 检测器配置不存在: %s", self.model, self._detector_config)
            return
        if np is None:
            self._load_error = self._load_error or "缺少 numpy，重跑不可用"
            logger.warning("模型 %s 缺少 numpy，检测器重跑不可用", self.model)
            return
        try:
            self._det_novocab = _new_detector(self._detector_config)
        except Exception as exc:
            self._load_error = f"检测器构造失败: {exc}"
            logger.warning("模型 %s 检测器构造失败: %s", self.model, exc)

    # ---- 检测 ---- #
    @staticmethod
    def _run_once(det: Any, lp: Any, ti: Any) -> Optional[_StubResult]:
        if det is None:
            return None
        try:
            try:
                det._garbled_count = 0  # 对齐线上语义：每请求复位
            except Exception:
                pass
            r = det.detector(lp, ti, topk_n=lp.shape[1])
            return _StubResult(
                bool(getattr(r, "is_ill", False)),
                int(getattr(r, "ill_type", 0)),
                list(getattr(r, "hits", []) or []),
                list(getattr(r, "windows", []) or []),
            )
        except Exception:
            return None

    def _vocab_detector(self) -> Any:
        if self._det_vocab is not None:
            return self._det_vocab
        if self._tk2cat is None:
            return None
        try:
            self._det_vocab = _new_detector(
                self._detector_config, self._tk2cat, self._vocab_size
            )
            return self._det_vocab
        except Exception:
            return None

    def detect(self, lp: Any, ti: Any, online_ill: int) -> Tuple[Optional[_StubResult], str, Optional[bool]]:
        """重跑检测：有词表 → 无词表依序尝试，与线上一致者优先。

        返回 (result|None, mode, consistent)；检测器不可用返回 (None, "", None)。
        """
        self.ensure_loaded()
        if self._det_novocab is None:
            return None, "", None
        results: List[Tuple[str, Optional[_StubResult]]] = []
        dv = self._vocab_detector()
        if dv is not None:
            results.append(("有词表", self._run_once(dv, lp, ti)))
        results.append(("无词表", self._run_once(self._det_novocab, lp, ti)))
        first: Optional[Tuple[str, _StubResult]] = None
        for mode, res in results:
            if res is None:
                continue
            if res.ill_type == online_ill:
                return res, mode, True
            if first is None:
                first = (mode, res)
        if first is not None:
            return first[1], first[0], False
        return None, "", None

    def resolver(self) -> Optional[TokenTextResolver]:
        self.ensure_loaded()
        return self._resolver

    def token_texts(self, seq: List[int]) -> List[str]:
        """逐 token 解码文本（无 tokenizer 返回空列表）。"""
        resolver = self.resolver()
        if resolver is None or not seq:
            return []
        return [resolver.resolve(tid) or "" for tid in seq]

    def notes(self) -> List[str]:
        """资源加载降级说明（拼入 rerun_note）。"""
        self.ensure_loaded()
        out = []
        if self._load_error:
            out.append(self._load_error)
        if self._tok_note:
            out.append(self._tok_note)
        if self._tk2cat_note:
            out.append(self._tk2cat_note)
        return out


# --------------------------------------------------------------------------- #
# RerunManager：按模型名惰性复用 ModelRerunner
# --------------------------------------------------------------------------- #

class RerunManager:
    """按模型名管理 ModelRerunner（惰性创建、复用），并对外暴露重跑入口。"""

    def __init__(
        self,
        models: Optional[Dict[str, ModelRerunConfig]] = None,
        default_detector_config: str = "",
    ) -> None:
        self._models = dict(models or {})
        self._default_detector_config = (
            default_detector_config or _repo_default_detector_config()
        )
        self._lock = threading.Lock()
        self._runners: Dict[str, ModelRerunner] = {}

    def get(self, model: str) -> ModelRerunner:
        key = str(model or "unknown")
        with self._lock:
            r = self._runners.get(key)
            if r is None:
                r = ModelRerunner(key, self._models.get(key), self._default_detector_config)
                self._runners[key] = r
            return r

    # ------------------------------------------------------------------ #
    # 主入口：重跑一条旧记录 → localization 块（失败/无位置产出返回 None）
    # ------------------------------------------------------------------ #
    def rerun_localization(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """对一条无 localization 的详情记录离线重跑定位。

        record 为 webui 详情 dict（含 ill_type/model/text/reasoning_content 与
        可选 topk_logprobs/tokens_ids/text_tokenid）。返回与实时契约同形构的
        localization dict，附加 ``source="rerun"`` / ``consistent`` / ``rerun_mode``
        / ``rerun_note`` 供前端标注「重跑口径」。
        """
        if not isinstance(record, dict):
            return None
        ill = ILL_NAME_TO_INT.get(str(record.get("ill_type") or ""))
        if ill is None or ill == 4:  # NaN 值级判定，无文本位置
            return None

        model = str(record.get("model") or "unknown")
        runner = self.get(model)
        lp, ti = build_topk_arrays(record.get("topk_logprobs"), record.get("tokens_ids"))
        seq = get_seq_ids(record.get("text_tokenid"), record.get("tokens_ids"))
        seq_len = int(lp.shape[0]) if lp is not None else len(seq)

        notes: List[str] = []
        hits: List[Dict[str, Any]] = []
        windows: List[Dict[str, Any]] = []
        mode = ""
        consistent: Optional[bool] = None

        if lp is not None:
            res, mode, consistent = runner.detect(lp, ti, ill)
            if res is not None:
                hits, windows = list(res.hits), list(res.windows)
            else:
                notes.append("检测器重跑不可用")
        else:
            notes.append("topk 数据缺失，检测器重跑不可用")
        notes.extend(runner.notes())

        # 启发式兜底：检测器重跑不可用或无位置产出
        if not hits and not windows:
            fb_mode, fb_hits, fb_windows = self._heuristic_fallback(
                runner, ill, seq, lp, ti
            )
            hits, windows = fb_hits, fb_windows
            if fb_mode:
                mode = mode + ("+" if mode else "") + fb_mode
                notes.append("采用启发式兜底定位")

        if not hits and not windows:
            logger.warning(
                "离线重跑未产出定位（进负缓存）model=%s ill_type=%s "
                "有topk=%s 有序列=%s 降级说明=[%s]",
                model, ill, lp is not None, bool(seq),
                "；".join(n for n in notes if n),
            )
            return None

        try:
            loc = build_localization(
                ill_type=ill,
                hits=hits,
                windows=windows,
                tokenids=seq or None,
                text=record.get("text"),
                reasoning_content=record.get("reasoning_content"),
                resolver=runner.resolver(),
                seq_len=seq_len,
            )
        except Exception as exc:
            logger.warning("离线重跑 build_localization 异常 model=%s: %s", model, exc)
            return None
        if loc.get("char_offsets") is None:
            logger.warning(
                "离线重跑无字符对齐（仅 token 级命中/窗口，前端会提示未对齐）"
                "model=%s ill_type=%s 降级说明=[%s]",
                model, ill, "；".join(n for n in notes if n),
            )
        loc["source"] = "rerun"
        loc["consistent"] = consistent
        loc["rerun_mode"] = mode
        loc["rerun_note"] = "；".join(n for n in notes if n)
        return loc

    def _heuristic_fallback(
        self,
        runner: ModelRerunner,
        ill: int,
        seq: List[int],
        lp: Any,
        ti: Any,
    ) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
        """按异常类型的启发式兜底（检测器重跑无产出时）。"""
        if ill == 3:  # repetition：token 序列即可，无需 tokenizer
            w = find_repetition_window(seq)
            if w is not None:
                return "启发式", [], [{"start": int(w[0]), "end": int(w[1]), "kind": "repet"}]
            return "", [], []
        if ill == 2:  # garbled：需要逐 token 文本
            texts = runner.token_texts(seq)
            w = find_garbled_window(texts) if texts else None
            if w is not None:
                return "启发式", [], [{"start": int(w[0]), "end": int(w[1]), "kind": "garbled"}]
            return "", [], []
        if ill == 1:  # rare_character：top1 logp 最可疑位置
            hits = rare_top_positions(lp, ti)
            if hits:
                return "启发式", hits, []
            return "", [], []
        return "", [], []
