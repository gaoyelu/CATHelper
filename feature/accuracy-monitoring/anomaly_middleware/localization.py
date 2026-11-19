"""检出位置定位数据构建（design_anomaly_localization.md §5）。

在异常落盘时构建 localization 块，统一数据契约：

- ``hits`` / ``windows``：检测器返回的命中位置（token 索引基于检测序列，
  即排序后 top-1 序列；与 ``text_tokenid`` 实际生成序列**按索引对齐**）；
- ``char_offsets`` / ``align_text``：按 UTF-8 字节边界把 token 序列合并为
  "显示单元"（字节级 BPE 拆出的多字节字符碎片合并成完整字符）后逐单元
  解码拼接得到，供前端语义视图精确高亮；命中/窗口按 token 索引映射到
  所属单元换算区间；
- ``reasoning_end``：思维链结束 token 边界（优先按 reasoning_content 在 align_text
  中定位，回退 ``</think>`` 标记）；
- ``text_spans`` / ``reasoning_spans``：换算到 ``text`` / ``reasoning_content``
  字符坐标的高亮区间。对齐判定分两档：重建文本与保存文本**完全一致**为
  精确对齐（``align_mode="exact"``）；不一致但相似度足够（``APPROX_ALIGN_RATIO``）
  时按 difflib 匹配块做**近似对齐**（``align_mode="approx"``），区间只映射到
  匹配块内（差异点附近的命中跳过、窗口截取映射），相似度过低仍降级
  （``aligned_*`` 为 False，前端提示）。

构建失败不抛异常（返回降级块），保证异常保存主流程不受影响。
"""
from __future__ import annotations

import difflib
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("anomaly_middleware.localization")

LOCALIZATION_VER = 2

# 容量上限（防御异常形态数据，保证 pkl / 详情响应体积可控）
MAX_HITS = 200
MAX_WINDOWS = 50
MAX_ALIGN_TOKENS = 20000

# 命中详情携带的 topk 候选数（供前端 tooltip）
MAX_HIT_CANDIDATES = 5

# token 文本解析失败占比超过该值 → 放弃字符对齐（char_offsets=None）
ALIGN_FAIL_RATIO = 0.2

# 近似对齐：字符相似度低于该值视为无法对齐；文本超过该长度不做模糊映射（性能兜底）
APPROX_ALIGN_RATIO = 0.9
APPROX_ALIGN_MAX_CHARS = 20000

# 单元合并的 token 数上限（防御原文自带 U+FFFD/坏数据导致组无限延伸；
# UTF-8 单字符最多 4 字节，字节级 BPE 每 token 1 字节 → 8 足够覆盖）
MAX_UNIT_GROUP = 8

THINK_START = "<think>"
THINK_END = "</think>"

ILL_NAN = 4


def _first_unit_at_or_after(offsets: List[int], boundary: int) -> Optional[int]:
    """返回 offsets 中第一个 >= boundary 的单元索引；不存在返回 None。"""
    for i, off in enumerate(offsets):
        if off >= boundary:
            return i
    return None


def _approx_blocks(src: str, dst: str) -> Optional[List[Tuple[int, int, int]]]:
    """src→dst 字符级模糊对齐（difflib 匹配块）。

    返回 [(src_start, dst_start, size), ...]（升序、不重叠）；
    相似度不足 / 文本超长 / 空串返回 None（调用方维持降级）。
    """
    if not src or not dst:
        return None
    if len(src) > APPROX_ALIGN_MAX_CHARS or len(dst) > APPROX_ALIGN_MAX_CHARS:
        logger.info(
            "近似对齐跳过（文本超长）src=%d dst=%d 上限=%d", len(src), len(dst),
            APPROX_ALIGN_MAX_CHARS,
        )
        return None
    sm = difflib.SequenceMatcher(None, src, dst, autojunk=False)
    if sm.ratio() < APPROX_ALIGN_RATIO:
        logger.info(
            "近似对齐失败：相似度 %.2f < %.2f（src=%d 字符 dst=%d 字符）",
            sm.ratio(), APPROX_ALIGN_RATIO, len(src), len(dst),
        )
        return None
    return [(b.a, b.b, b.size) for b in sm.get_matching_blocks() if b.size > 0]


def _map_range(
    blocks: List[Tuple[int, int, int]], cs: int, ce: int
) -> List[Tuple[int, int]]:
    """src 坐标区间 (cs, ce) → dst 坐标区间列表（仅匹配块内的部分）。

    区间落在差异点（替换/增删处）→ 返回空列表（调用方跳过该高亮）；
    区间跨多个匹配块 → 逐块产出片段。
    """
    out: List[Tuple[int, int]] = []
    for a, b, size in blocks:
        s = max(cs, a)
        e = min(ce, a + size)
        if e > s:
            out.append((b + (s - a), b + (e - a)))
    return out


def _locate_approx(
    hay: str, needle: str
) -> Optional[Tuple[List[Tuple[int, int, int]], int, int]]:
    """在 hay 中模糊定位 needle（用于思维链文本被后处理的场景）。

    覆盖率 = needle 被匹配的字符占比，低于阈值返回 None；
    返回 (blocks, start, end)：blocks 为 (hay_pos, needle_pos, size)，
    start/end 为 needle 在 hay 中的近似起止（首块起点 ~ 末块终点）。
    """
    if not hay or not needle:
        return None
    if len(hay) > APPROX_ALIGN_MAX_CHARS or len(needle) > APPROX_ALIGN_MAX_CHARS:
        return None
    sm = difflib.SequenceMatcher(None, hay, needle, autojunk=False)
    blocks = [(b.a, b.b, b.size) for b in sm.get_matching_blocks() if b.size > 0]
    if not blocks:
        return None
    covered = sum(size for _, _, size in blocks)
    if covered / len(needle) < APPROX_ALIGN_RATIO:
        logger.info(
            "思维链模糊定位失败：覆盖率 %.2f < %.2f（hay=%d needle=%d）",
            covered / len(needle), APPROX_ALIGN_RATIO, len(hay), len(needle),
        )
        return None
    return blocks, blocks[0][0], blocks[-1][0] + blocks[-1][2]


def _sanitize_hits(hits: Any, seq_len: int) -> List[Dict[str, Any]]:
    """清洗检测器命中：合法 idx、排序、去重、截断；全部转为纯 Python 类型。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(hits, list):
        return out
    seen = set()
    for h in hits:
        if not isinstance(h, dict):
            continue
        try:
            idx = int(h.get("idx"))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= seq_len or idx in seen:
            continue
        seen.add(idx)
        item: Dict[str, Any] = {"idx": idx}
        try:
            item["token_id"] = int(h.get("token_id"))
        except (TypeError, ValueError):
            item["token_id"] = -1
        try:
            item["logp_top1"] = float(h.get("logp_top1"))
        except (TypeError, ValueError):
            item["logp_top1"] = None
        cats = h.get("cats")
        if isinstance(cats, list):
            item["cats"] = [str(c) for c in cats][:8]
        cands = h.get("topk_ids")
        if isinstance(cands, list):
            ids: List[int] = []
            for c in cands[:MAX_HIT_CANDIDATES]:
                try:
                    ids.append(int(c))
                except (TypeError, ValueError):
                    continue
            if ids:
                item["topk_ids"] = ids
        out.append(item)
        if len(out) >= MAX_HITS:
            break
    return out


def _sanitize_windows(windows: Any, seq_len: int) -> List[Dict[str, Any]]:
    """清洗触发窗口：范围合法、截断。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(windows, list):
        return out
    for w in windows:
        if not isinstance(w, dict):
            continue
        try:
            start = int(w.get("start"))
            end = int(w.get("end"))
        except (TypeError, ValueError):
            continue
        kind = str(w.get("kind") or "window")
        if start < 0 or end <= start:
            continue
        if end > seq_len:
            end = seq_len
        if start >= end:
            continue
        out.append({"start": start, "end": end, "kind": kind})
        if len(out) >= MAX_WINDOWS:
            break
    return out


def _is_incomplete(text: Optional[str]) -> bool:
    """组解码结果是否为不完整字节序列（解码失败或含 U+FFFD 替换符）。"""
    return text is None or "\ufffd" in text


def _decode_unit(resolver: Any, group: List[Any]) -> Optional[str]:
    """解码一个候选显示单元（组解码优先；旧式 resolver 回退单 id 解码）。"""
    try:
        if hasattr(resolver, "decode_ids"):
            return resolver.decode_ids(group)
        if len(group) == 1:
            return resolver.resolve(group[0])
    except Exception:
        return None
    return None


def _build_units(
    tokenids: Any, seq_len: int, resolver: Any
) -> Optional[Tuple[List[str], List[int], List[int], int]]:
    """按 UTF-8 字节边界把 token 序列重组为"显示单元"。

    字节级 BPE tokenizer 会把多字节字符（如生僻字）拆成多个字节 token，
    单 token 解码只得到 U+FFFD 碎片；把相邻碎片合并到字节完整边界后
    整体解码（resolver.decode_ids）可还原原字符，且 align_text 与原文一致。
    resolver 无组解码能力（旧式仅 resolve）时退化为逐 token 单元（原口径）。

    返回 (units_text, unit_first_token, token_unit, fails) 或 None
    （tokenids 缺失/非法/超长/单元解析失败占比过高时）：
    - units_text[k]：单元 k 的解码文本（可能为空串）；
    - unit_first_token[k]：单元 k 的首个 token 索引；
    - token_unit[i]：token i 所属单元索引（长度 = 对齐的 token 数）；
    - fails：解码失败（异常/空串）单元数。
    """
    if not isinstance(tokenids, list) or not tokenids:
        return None
    if seq_len > 0:
        n = min(len(tokenids), seq_len)
    else:
        n = len(tokenids)
    if n <= 0 or n > MAX_ALIGN_TOKENS:
        return None

    def _valid(tid: Any) -> bool:
        return (
            resolver is not None
            and isinstance(tid, int)
            and not isinstance(tid, bool)
            and tid >= 0
        )

    can_group = resolver is not None and hasattr(resolver, "decode_ids")
    units_text: List[str] = []
    unit_first_token: List[int] = []
    token_unit: List[int] = []
    fails = 0
    i = 0
    while i < n:
        tid = tokenids[i]
        if not _valid(tid):
            # 非法 id：独立失败单元，不与相邻 token 合并
            units_text.append("")
            unit_first_token.append(i)
            token_unit.append(len(units_text) - 1)
            fails += 1
            i += 1
            continue
        j = i + 1
        group = [tid]
        text = _decode_unit(resolver, group)
        # 字节碎片/解码失败 → 向后并入相邻合法 token，直到字节完整或达上限；
        # 候选 token 自身字节完整时终止扩展（碎片已无法由其补齐，避免吞并后续完整 token）
        while (
            can_group
            and j < n
            and (j - i) < MAX_UNIT_GROUP
            and _is_incomplete(text)
            and _valid(tokenids[j])
            and _is_incomplete(_decode_unit(resolver, [tokenids[j]]))
        ):
            group.append(tokenids[j])
            j += 1
            text = _decode_unit(resolver, group)
        if text is None or text == "":
            fails += 1
        units_text.append(text or "")
        unit_first_token.append(i)
        for k in range(i, j):
            token_unit.append(len(units_text) - 1)
        i = j
    if fails / len(units_text) > ALIGN_FAIL_RATIO:
        logger.info(
            "显示单元解码失败占比 %d/%d（%.0f%%）超阈值 %d%% → 放弃字符对齐",
            fails, len(units_text), fails / len(units_text) * 100,
            ALIGN_FAIL_RATIO * 100,
        )
        return None
    return units_text, unit_first_token, token_unit, fails


def _eos_token_ids(resolver: Any) -> set:
    """从 resolver 获取 EOS token id 集合（无该能力/异常 → 空集）。"""
    getter = getattr(resolver, "eos_token_ids", None)
    if getter is None:
        return set()
    try:
        ids = getter()
        return {int(x) for x in ids} if ids else set()
    except Exception:
        return set()


def _hit_tooltip_fields(hit: Dict[str, Any]) -> str:
    """命中 tooltip 摘要（token_id / top1 logp / 候选 / 类别）。"""
    parts: List[str] = [f"token_id={hit.get('token_id', -1)}"]
    lp = hit.get("logp_top1")
    if lp is not None:
        parts.append(f"top1_logp={lp:.3f}")
    cands = hit.get("topk_ids")
    if cands:
        parts.append("topk=" + ",".join(str(c) for c in cands))
    cats = hit.get("cats")
    if cats:
        parts.append("cats=" + "|".join(cats))
    return " · ".join(parts)


def build_localization(
    *,
    ill_type: int,
    hits: Any,
    windows: Any,
    tokenids: Any,
    text: Any,
    reasoning_content: Any,
    resolver: Any,
    seq_len: int,
) -> Dict[str, Any]:
    """构建 localization 块（全捕获，失败返回降级块）。

    Parameters
    ----------
    ill_type : 异常类型（1=生僻字 2=乱码 3=重复 4=NaN）
    hits / windows : 检测器返回的命中位置 / 触发窗口
    tokenids : 实际生成 token id 序列（text_tokenid），缺失则无法字符对齐
    text / reasoning_content : 输出文本 / 思维链文本
    resolver : TokenTextResolver（token_id -> 单 token 文本），可为 None
    seq_len : 检测序列长度（hits idx 的合法上界）
    """
    loc: Dict[str, Any] = {
        "detector_ver": LOCALIZATION_VER,
        "sequence_ref": "text_tokenid" if isinstance(tokenids, list) and tokenids else "tokens_ids_top1",
        "reasoning_end": None,
        "hits": [],
        "windows": [],
        "char_offsets": None,
        "align_text": None,
        "text_spans": None,
        "reasoning_spans": None,
        "aligned_text": False,
        "aligned_reasoning": False,
        "align_mode": "exact",  # exact=完全一致 | approx=difflib 近似映射
        "reasoning_text": None,  # reasoning_content 缺失时从 token 流提取的思维链文本
    }
    try:
        clean_hits = _sanitize_hits(hits, seq_len)
        clean_windows = _sanitize_windows(windows, seq_len)
        loc["hits"] = [
            dict(h, title=_hit_tooltip_fields(h)) for h in clean_hits
        ]
        loc["windows"] = clean_windows

        # NaN 为值级判定，无文本位置
        if ill_type == ILL_NAN:
            loc["hits"] = []
            loc["windows"] = []
            return loc
        # 无命中数据时无需字符对齐
        if not clean_hits and not clean_windows:
            return loc

        # 截掉尾部 EOS：保存的 text 不含 eos，解码重建带上会拉低相似度
        #（短输出时一个长 eos 标记即可把相似度拉到阈值之下）
        ids_src: Any = tokenids
        eos_ids = _eos_token_ids(resolver)
        if eos_ids and isinstance(tokenids, list) and tokenids:
            trimmed = list(tokenids)
            while (
                trimmed
                and isinstance(trimmed[-1], int)
                and not isinstance(trimmed[-1], bool)
                and trimmed[-1] in eos_ids
            ):
                trimmed.pop()
            if len(trimmed) < len(tokenids):
                logger.info("截掉尾部 EOS token %d 个 → 重建文本对齐", len(tokenids) - len(trimmed))
            ids_src = trimmed

        units = _build_units(ids_src, seq_len, resolver)
        if units is None:
            logger.info(
                "无法构建显示单元 → 无字符对齐（tokenids=%s resolver=%s）",
                "缺失/非法" if not isinstance(tokenids, list) or not tokenids else f"len={len(tokenids)}",
                "有" if resolver is not None else "无",
            )
            return loc
        units_text, unit_first_token, token_unit, _fails = units
        n_units = len(units_text)
        n_tokens = len(token_unit)
        offsets: List[int] = []
        total = 0
        for t in units_text:
            offsets.append(total)
            total += len(t)
        align_text = "".join(units_text)
        loc["char_offsets"] = offsets
        loc["align_text"] = align_text

        # ---- 思维链边界 ---- #
        # Qwen3 类模型：align_text = "<think>" + reasoning_content + "</think>" + text，
        # 标记 token 夹在两区块之间。reasoning_end = 正文首个 token 索引。
        # 精确匹配失败时尝试模糊定位（保存文本被后处理/细微差异时仍可归属区间）。
        rc = reasoning_content if isinstance(reasoning_content, str) and reasoning_content else None
        rc_start: Optional[int] = None   # reasoning_content 在 align_text 中的起点
        rc_end: Optional[int] = None     # reasoning_content 结束（字符坐标）
        rc_blocks: Optional[List[Tuple[int, int, int]]] = None  # align_text→rc 映射块（模糊时）
        reasoning_end: Optional[int] = None
        text_start = 0                   # 正文区块字符起点

        def _locate_boundary_from(end_char: int) -> None:
            """按思维链结束字符坐标推进 text_start 并解析 reasoning_end（闭包写回）。"""
            nonlocal text_start, reasoning_end
            boundary = end_char
            if align_text.startswith(THINK_END, boundary):
                boundary += len(THINK_END)  # 跳过紧随的结束标记
            text_start = boundary
            u = _first_unit_at_or_after(offsets, boundary)
            reasoning_end = unit_first_token[u] if u is not None else n_tokens

        if rc:
            p = align_text.find(rc)
            if p >= 0:
                rc_start, rc_end = p, p + len(rc)
                _locate_boundary_from(rc_end)
            else:
                located = _locate_approx(align_text, rc)
                if located:
                    rc_blocks, rc_start, rc_end = located
                    _locate_boundary_from(rc_end)
        # 仅凭结束标记切分（不强求 <think> 开始标记）：
        # - reasoning_content 缺失/未匹配时，结束标记前的内容即思维链，
        #   直接从 token 解码流提取展示（其自带精确字符坐标，天然对齐）；
        # - 连结束标记都没有但存在开始标记：其后全部视为思维链（截断生成的兜底）。
        reasoning_text_extracted: Optional[str] = None
        if rc_start is None and THINK_END in align_text:
            idx = align_text.find(THINK_END)
            text_start = idx + len(THINK_END)
            u = _first_unit_at_or_after(offsets, text_start)
            reasoning_end = unit_first_token[u] if u is not None else n_tokens
            if rc is None:
                body = align_text[:idx]
                rc_start = 0
                if body.startswith(THINK_START):
                    body = body[len(THINK_START):]
                    rc_start = len(THINK_START)
                if body:
                    reasoning_text_extracted = body
                    rc_end = idx
        elif rc_start is None and THINK_START in align_text:
            idx = align_text.find(THINK_START) + len(THINK_START)
            body = align_text[idx:]
            if body:
                reasoning_text_extracted = body
                rc_start = idx
                rc_end = len(align_text)
                text_start = len(align_text)
                reasoning_end = n_tokens
        if reasoning_text_extracted is not None:
            loc["reasoning_text"] = reasoning_text_extracted
        loc["reasoning_end"] = reasoning_end

        # ---- 区块对齐判定（精确一致 → 近似映射 → 降级） ---- #
        # text 可能为全量输出（含思维链原文，旧中间件/未拆分场景）：
        # - 精确/前缀：正文段与 text 完全一致或为 text 前缀；
        # - 全量一致：align_text 与 text 完全一致（text 含思维链原文）→ 绝对坐标映射；
        # - 包含定位：正文段内嵌于 text（text = 思维链 + 正文）→ difflib 覆盖率定位，
        #   text_spans 直接映射到全量 text 坐标（前端无需再偏移）。
        text_str = text if isinstance(text, str) else None
        text_blocks: Optional[List[Tuple[int, int, int]]] = None  # 正文段→全量 text 映射块
        aligned_text = False
        if text_str is not None:
            src_seg = align_text[text_start:]
            if src_seg == text_str:
                aligned_text = True
            elif text_str and src_seg.startswith(text_str):
                # 重建尾部多出内容（未被识别的 EOS/结束标记）：正文为精确前缀
                aligned_text = True
            elif align_text == text_str:
                # text 为全量输出（含思维链原文）：正文段对应 text[text_start:]，绝对坐标映射
                aligned_text = True
                if text_start:
                    text_blocks = [(0, text_start, len(src_seg))]
            elif src_seg:
                located = _locate_approx(text_str, src_seg)
                if located:
                    blocks, _s, _e = located
                    # (hay=text 坐标, needle=正文段坐标, size) → 反转为 (src, dst, size)
                    text_blocks = [(b[1], b[0], b[2]) for b in blocks]
                    aligned_text = True
                    loc["align_mode"] = "approx"
        if rc_blocks is not None:
            loc["align_mode"] = "approx"
        aligned_reasoning = rc_start is not None
        loc["aligned_text"] = aligned_text
        loc["aligned_reasoning"] = aligned_reasoning

        # ---- 高亮区间换算（仅对齐区块；命中/窗口按 token 索引映射到单元） ---- #
        def _char_range(u_start: int, u_end: int) -> Optional[tuple]:
            """单元区间 [u_start, u_end) -> align_text 字符区间 (cs, ce)。"""
            if u_start < 0 or u_start >= n_units:
                return None
            last = min(u_end, n_units) - 1
            if last < u_start:
                return None
            cs = offsets[u_start]
            ce = offsets[last] + len(units_text[last])
            return (cs, max(ce, cs))

        def _hit_span(idx: int) -> List[Tuple[int, int, str]]:
            """单 token 命中 → [(start, end, 区块)]。

            映射到所属单元（字节碎片合并后高亮整个字符）；近似对齐时落在
            差异点的命中返回空列表（跳过，不高亮错位）。
            """
            if idx < 0 or idx >= n_tokens:
                return []
            u = token_unit[idx]
            cr = _char_range(u, u + 1)
            if cr is None:
                return []
            cs, ce = cr
            if aligned_reasoning:
                if rc_blocks is not None:
                    return [(s, e, "reasoning") for s, e in _map_range(rc_blocks, cs, ce)]
                if rc_start <= cs and ce <= rc_end:
                    return [(cs - rc_start, ce - rc_start, "reasoning")]
            if aligned_text:
                if text_blocks is not None:
                    return [(s, e, "text") for s, e in _map_range(text_blocks, cs - text_start, ce - text_start)]
                if cs >= text_start:
                    return [(cs - text_start, ce - text_start, "text")]
            return []

        def _window_span(start_idx: int, end_idx: int) -> List[Tuple[int, int, str]]:
            """触发窗口 → [(start, end, 区块)]（跨匹配块时产出多个片段）。"""
            if start_idx < 0 or start_idx >= n_tokens:
                return []
            last_idx = min(end_idx, n_tokens) - 1
            if last_idx < start_idx:
                return []
            cr = _char_range(token_unit[start_idx], token_unit[last_idx] + 1)
            if cr is None:
                return []
            cs, ce = cr
            if aligned_text and ce > text_start:
                if text_blocks is not None:
                    return [(s, e, "text") for s, e in _map_range(text_blocks, cs - text_start, ce - text_start)]
                s2 = max(cs, text_start)
                return [(s2 - text_start, ce - text_start, "text")]
            if aligned_reasoning and cs < rc_end and ce > rc_start:
                if rc_blocks is not None:
                    return [(s, e, "reasoning") for s, e in _map_range(rc_blocks, cs, ce)]
                s2, e2 = max(cs, rc_start), min(ce, rc_end)
                if e2 > s2:
                    return [(s2 - rc_start, e2 - rc_start, "reasoning")]
            return []

        text_spans: List[Dict[str, Any]] = []
        reasoning_spans: List[Dict[str, Any]] = []
        for h in clean_hits:
            for r in _hit_span(h["idx"]):
                span = {
                    "start": r[0], "end": r[1], "kind": "hit",
                    "ref": h["idx"], "title": _hit_tooltip_fields(h),
                }
                (reasoning_spans if r[2] == "reasoning" else text_spans).append(span)
        for w in clean_windows:
            for r in _window_span(w["start"], w["end"]):
                span = {
                    "start": r[0], "end": r[1], "kind": w["kind"],
                    "ref": f"{w['start']}-{w['end']}",
                    "title": ("重复" if w["kind"] == "repet" else "乱码") + f"触发窗口 [{w['start']},{w['end']})",
                }
                (reasoning_spans if r[2] == "reasoning" else text_spans).append(span)
        if text_spans:
            text_spans.sort(key=lambda s: s["start"])
            loc["text_spans"] = text_spans
        if reasoning_spans:
            reasoning_spans.sort(key=lambda s: s["start"])
            loc["reasoning_spans"] = reasoning_spans
        return loc
    except Exception:
        return loc
