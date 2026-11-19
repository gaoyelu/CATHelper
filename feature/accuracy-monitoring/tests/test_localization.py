"""localization 构建单测（design_anomaly_localization.md §5）。

build_localization：token → 文本对齐（char_offsets/align_text）、思维链边界
（reasoning_end）、区块高亮区间换算（text_spans/reasoning_spans）、
对齐失败降级（aligned_* 标记）、NaN 最小契约。
"""
from __future__ import annotations

import pickle

from anomaly_middleware.localization import (
    MAX_HITS,
    build_localization,
)


class FakeResolver:
    """token_id -> 文本（dict 驱动；未注册 id 返回 None）。

    groups 可选：{tuple(ids): text} 模拟字节级 BPE tokenizer 的组解码
    （碎片单解为 U+FFFD，组解还原原字符）；未命中组键时：
    单 id 查 mapping，多 id 逐 token 拼接（模拟整体解码正确的 tokenizer）。
    eos 可选：EOS token id 集合（模拟 tokenizer.eos_token_id）。
    """

    def __init__(self, mapping, groups=None, eos=()):
        self._m = mapping
        self._g = groups or {}
        self._eos = set(eos)

    def resolve(self, token_id):
        return self._m.get(int(token_id))

    def eos_token_ids(self):
        return self._eos

    def decode_ids(self, ids):
        key = tuple(int(t) for t in ids)
        if key in self._g:
            return self._g[key]
        if len(key) == 1:
            return self._m.get(key[0])
        parts = [self._m.get(t) for t in key]
        if any(p is None for p in parts):
            return None
        return "".join(parts)


# --------------------------- 精确对齐（无思维链） --------------------------- #
def test_exact_alignment_no_reasoning():
    resolver = FakeResolver({10: "你", 20: "好", 30: "鿊", 40: "。"})
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 2, "token_id": 30, "logp_top1": -8.2, "cats": ["chinese_cjk", "greek"]}],
        windows=[],
        tokenids=[10, 20, 30, 40],
        text="你好鿊。",
        reasoning_content=None,
        resolver=resolver,
        seq_len=4,
    )
    assert loc["detector_ver"] == 2
    assert loc["sequence_ref"] == "text_tokenid"
    assert loc["char_offsets"] == [0, 1, 2, 3]
    assert loc["align_text"] == "你好鿊。"
    assert loc["reasoning_end"] is None
    assert loc["aligned_text"] is True
    assert loc["text_spans"] == [{
        "start": 2, "end": 3, "kind": "hit", "ref": 2,
        "title": loc["hits"][0]["title"],
    }]
    assert loc["reasoning_spans"] is None


def test_hit_span_covers_multi_char_token():
    """多字符 token：高亮区间覆盖整个 token 文本。"""
    resolver = FakeResolver({1: "你好", 2: "世界鿊", 3: "。"})
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 1, "token_id": 2, "logp_top1": -9.0}],
        windows=[],
        tokenids=[1, 2, 3],
        text="你好世界鿊。",
        reasoning_content=None,
        resolver=resolver,
        seq_len=3,
    )
    assert loc["char_offsets"] == [0, 2, 5]
    assert loc["text_spans"] == [{"start": 2, "end": 5, "kind": "hit", "ref": 1,
                                  "title": loc["hits"][0]["title"]}]


# --------------------------- 思维链边界（reasoning_content 定位） --------------------------- #
def test_reasoning_boundary_and_span_split():
    """reasoning_content 在 align_text 中定位成功：边界/双区块区间正确。"""
    resolver = FakeResolver({1: "<think>", 2: "思考", 3: "中", 4: "</think>", 5: "答", 6: "案"})
    loc = build_localization(
        ill_type=1,
        hits=[
            {"idx": 1, "token_id": 2, "logp_top1": -7.0},   # 思维链内
            {"idx": 5, "token_id": 6, "logp_top1": -7.5},   # 正文内
        ],
        windows=[],
        tokenids=[1, 2, 3, 4, 5, 6],
        text="答案",
        reasoning_content="思考中",
        resolver=resolver,
        seq_len=6,
    )
    # align_text = "<think>思考中</think>答案"
    assert loc["align_text"] == "<think>思考中</think>答案"
    assert loc["reasoning_end"] == 4  # 正文"答"token 索引（跳过 </think> 标记）
    assert loc["aligned_reasoning"] is True
    assert loc["aligned_text"] is True
    assert loc["reasoning_spans"] == [{"start": 0, "end": 2, "kind": "hit", "ref": 1,
                                       "title": loc["hits"][0]["title"]}]
    assert loc["text_spans"] == [{"start": 1, "end": 2, "kind": "hit", "ref": 5,
                                  "title": loc["hits"][1]["title"]}]


def test_think_marker_fallback_without_reasoning_content():
    """reasoning_content 缺失/未匹配：凭 </think> 标记切分，思维链从 token 流提取。"""
    resolver = FakeResolver({1: "<think>", 2: "abc", 3: "</think>", 4: "答"})
    loc = build_localization(
        ill_type=2,
        hits=[{"idx": 1, "token_id": 2, "logp_top1": -3.0}],
        windows=[{"start": 1, "end": 2, "kind": "garbled"}],
        tokenids=[1, 2, 3, 4],
        text="答",
        reasoning_content=None,
        resolver=resolver,
        seq_len=4,
    )
    assert loc["reasoning_end"] == 3
    assert loc["reasoning_text"] == "abc"       # 从 token 解码流提取
    assert loc["aligned_reasoning"] is True     # 提取文本自带精确坐标
    assert [(s["start"], s["end"], s["kind"]) for s in loc["reasoning_spans"]] == [
        (0, 3, "hit"), (0, 3, "garbled"),
    ]
    assert loc["text_spans"] is None            # 命中/窗口均在思维链侧
    assert loc["aligned_text"] is True


# --------------------------- 对齐失败降级 --------------------------- #
def test_unaligned_text_no_spans():
    """align_text 与 text 不一致（如 parser 剥离标记）：不产出区间，仅保留命中。"""
    resolver = FakeResolver({1: "你", 2: "好", 3: "鿊"})
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 2, "token_id": 3, "logp_top1": -8.0}],
        windows=[],
        tokenids=[1, 2, 3],
        text="不同文本",
        reasoning_content=None,
        resolver=resolver,
        seq_len=3,
    )
    assert loc["aligned_text"] is False
    assert loc["text_spans"] is None
    assert loc["char_offsets"] == [0, 1, 2]  # offsets 仍可用于降级展示
    assert len(loc["hits"]) == 1


def test_no_resolver_degrades():
    """resolver 缺失：无字符对齐，命中按原样保留。"""
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 3, "token_id": 99, "logp_top1": -9.0}],
        windows=[],
        tokenids=[1, 2, 3, 4],
        text=None,
        reasoning_content=None,
        resolver=None,
        seq_len=4,
    )
    assert loc["char_offsets"] is None
    assert loc["align_text"] is None
    assert loc["text_spans"] is None
    assert loc["hits"][0]["idx"] == 3


def test_too_many_parse_failures_degrades():
    """token 文本解析失败占比 > 20% → 放弃字符对齐（命中清洗与对齐无关，仍保留）。"""
    resolver = FakeResolver({1: "a"})  # 仅 id=1 可解析
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 0, "token_id": 1, "logp_top1": -5.0}],
        windows=[],
        tokenids=[1, -1, -1, 7],
        text=None,
        reasoning_content=None,
        resolver=resolver,
        seq_len=4,
    )
    assert loc["char_offsets"] is None
    assert loc["hits"][0]["idx"] == 0


def test_no_tokenids_keeps_hits():
    """tokenids 缺失（旧管线）：命中保留，sequence_ref 回退。"""
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 1, "token_id": 5, "logp_top1": -6.5}],
        windows=[],
        tokenids=[],
        text="abc",
        reasoning_content=None,
        resolver=FakeResolver({}),
        seq_len=3,
    )
    assert loc["sequence_ref"] == "tokens_ids_top1"
    assert loc["char_offsets"] is None
    assert len(loc["hits"]) == 1


# --------------------------- 窗口区间换算 --------------------------- #
def test_window_span_text_block():
    resolver = FakeResolver({1: "你", 2: "好", 3: "鿊", 4: "呀", 5: "。"})
    loc = build_localization(
        ill_type=2,
        hits=[],
        windows=[{"start": 1, "end": 4, "kind": "garbled"}],
        tokenids=[1, 2, 3, 4, 5],
        text="你好鿊呀。",
        reasoning_content=None,
        resolver=resolver,
        seq_len=5,
    )
    assert loc["text_spans"] == [{"start": 1, "end": 4, "kind": "garbled",
                                  "ref": "1-4", "title": "乱码触发窗口 [1,4)"}]


def test_repet_window_title():
    resolver = FakeResolver({1: "哈"})
    loc = build_localization(
        ill_type=3,
        hits=[],
        windows=[{"start": 0, "end": 3, "kind": "repet"}],
        tokenids=[1, 1, 1],
        text="哈哈哈",
        reasoning_content=None,
        resolver=resolver,
        seq_len=3,
    )
    assert loc["text_spans"][0]["title"] == "重复触发窗口 [0,3)"


# --------------------------- NaN 最小契约 --------------------------- #
def test_nan_minimal_contract():
    loc = build_localization(
        ill_type=4,
        hits=[{"idx": 0, "token_id": 1, "logp_top1": float("nan")}],
        windows=[],
        tokenids=[1],
        text="x",
        reasoning_content=None,
        resolver=FakeResolver({1: "x"}),
        seq_len=1,
    )
    assert loc["hits"] == []
    assert loc["windows"] == []
    assert loc["char_offsets"] is None


# --------------------------- 清洗与上限 --------------------------- #
def test_hits_sanitized_and_capped():
    hits = [{"idx": i, "token_id": i, "logp_top1": -1.0} for i in range(300)]
    hits.append({"idx": "bad"})  # 非法条目跳过
    hits.append({"idx": 10})     # 重复 idx 去重
    loc = build_localization(
        ill_type=1,
        hits=hits,
        windows=[{"start": 0, "end": 999, "kind": "garbled"}],
        tokenids=None,
        text=None,
        reasoning_content=None,
        resolver=None,
        seq_len=500,
    )
    assert len(loc["hits"]) == MAX_HITS
    assert loc["windows"][0]["end"] == 500  # end 截到 seq_len


def test_localization_picklable():
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 0, "token_id": 1, "logp_top1": -5.0}],
        windows=[],
        tokenids=[1, 2],
        text="ab",
        reasoning_content=None,
        resolver=FakeResolver({1: "a", 2: "b"}),
        seq_len=2,
    )
    loaded = pickle.loads(pickle.dumps(loc))
    assert loaded == loc


def test_build_never_raises():
    """全捕获：非法输入返回降级块而不抛。"""
    loc = build_localization(
        ill_type=1,
        hits="garbage",
        windows=None,
        tokenids="garbage",
        text=123,
        reasoning_content=456,
        resolver="not-a-resolver",
        seq_len="bad",
    )
    assert loc["detector_ver"] == 2
    assert loc["hits"] == []
    assert loc["char_offsets"] is None


# --------------------------- 字节碎片单元合并（字节级 BPE） --------------------------- #
# 模拟 Qwen 类字节级 tokenizer：生僻字"鿊"拆成 2 个字节 token，单解为 U+FFFD，
# 组解（整体解码）还原原字符
BYTE_GROUPS = {(82912,): "\ufffd", (99,): "\ufffd", (82912, 99): "鿊"}


def _byte_fragment_resolver():
    return FakeResolver(
        {10: "你", 82912: "\ufffd", 99: "\ufffd", 40: "。"},
        groups=BYTE_GROUPS,
    )


def test_byte_fragment_unit_merge_restores_char():
    """生僻字拆成字节 token：合并为单单元后还原原字符，align_text 与原文一致。"""
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 1, "token_id": 82912, "logp_top1": -8.2}],
        windows=[],
        tokenids=[10, 82912, 99, 40],
        text="你鿊。",
        reasoning_content=None,
        resolver=_byte_fragment_resolver(),
        seq_len=4,
    )
    assert loc["char_offsets"] == [0, 1, 2]  # 3 个显示单元（2 个碎片 token 合并为 1）
    assert loc["align_text"] == "你鿊。"
    assert loc["aligned_text"] is True       # 字节合并后对齐成立（旧口径为 False）
    # 命中落在碎片的任一 token → 高亮整个合并单元
    assert loc["text_spans"] == [{"start": 1, "end": 2, "kind": "hit", "ref": 1,
                                  "title": loc["hits"][0]["title"]}]


def test_byte_fragment_hit_on_second_token_covers_unit():
    """命中落在碎片的第二个 token：同样高亮整个合并单元。"""
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 2, "token_id": 99, "logp_top1": -8.5}],
        windows=[],
        tokenids=[10, 82912, 99, 40],
        text="你鿊。",
        reasoning_content=None,
        resolver=_byte_fragment_resolver(),
        seq_len=4,
    )
    assert loc["text_spans"][0]["start"] == 1
    assert loc["text_spans"][0]["end"] == 2


def test_window_span_over_merged_unit():
    """触发窗口覆盖碎片 token 区间：换算到合并单元的完整字符区间。"""
    loc = build_localization(
        ill_type=3,
        hits=[],
        windows=[{"start": 1, "end": 3, "kind": "repet"}],
        tokenids=[10, 82912, 99, 40],
        text="你鿊。",
        reasoning_content=None,
        resolver=_byte_fragment_resolver(),
        seq_len=4,
    )
    assert loc["text_spans"] == [{"start": 1, "end": 2, "kind": "repet",
                                  "ref": "1-3", "title": "重复触发窗口 [1,3)"}]


def test_unfixable_trailing_fragment_keeps_unit_boundary():
    """序列末尾无法补齐的碎片：不吞并后续完整 token，独立单元显示 U+FFFD。"""
    resolver = FakeResolver(
        {10: "你", 82912: "\ufffd", 40: "。"},
        groups={(82912,): "\ufffd"},
    )
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 1, "token_id": 82912, "logp_top1": -8.0}],
        windows=[],
        tokenids=[10, 82912, 40],
        text="你\ufffd。",
        reasoning_content=None,
        resolver=resolver,
        seq_len=3,
    )
    # 碎片独立成单元（'。' 未被吞并），与原文（同样含替换符）对齐成立
    assert loc["char_offsets"] == [0, 1, 2]
    assert loc["align_text"] == "你\ufffd。"
    assert loc["aligned_text"] is True
    assert loc["text_spans"][0]["start"] == 1
    assert loc["text_spans"][0]["end"] == 2


# --------------------------- 近似对齐（保存文本被后处理） --------------------------- #
_CHARS = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"  # 20 个单字单元


def _seq_resolver():
    return FakeResolver({i + 1: ch for i, ch in enumerate(_CHARS)})


def test_approx_text_alignment_maps_spans():
    """text 有单字符差异（相似度 0.95 >= 0.9）：近似对齐，区间只映射匹配块。"""
    align = _CHARS
    saved = _CHARS[:9] + "X" + _CHARS[10:]  # 仅下标 9 一个字符被替换
    loc = build_localization(
        ill_type=3,
        hits=[
            {"idx": 5, "token_id": 6, "logp_top1": -1.0},   # 匹配区内
            {"idx": 9, "token_id": 10, "logp_top1": -1.0},  # 差异点上 → 应跳过
        ],
        windows=[{"start": 8, "end": 12, "kind": "repet"}],  # 跨差异点 → 截取映射
        tokenids=list(range(1, 21)),
        text=saved,
        reasoning_content=None,
        resolver=_seq_resolver(),
        seq_len=20,
    )
    assert loc["aligned_text"] is True
    assert loc["align_mode"] == "approx"
    spans = loc["text_spans"]
    assert [(s["start"], s["end"], s["kind"]) for s in spans] == [
        (5, 6, "hit"),      # 匹配块内：原样映射
        (8, 9, "repet"),    # 窗口左半（差异点前）
        (10, 12, "repet"),  # 窗口右半（差异点后）
    ]
    assert all(s["ref"] == "8-12" for s in spans if s["kind"] == "repet")


def test_low_similarity_still_degrades():
    """text 与重建完全不同（相似度 < 0.9）：维持降级，不产出区间。"""
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 2, "token_id": 3, "logp_top1": -8.0}],
        windows=[],
        tokenids=list(range(1, 21)),
        text="ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",  # 完全不同
        reasoning_content=None,
        resolver=_seq_resolver(),
        seq_len=20,
    )
    assert loc["aligned_text"] is False
    assert loc["text_spans"] is None
    assert len(loc["hits"]) == 1


def test_approx_reasoning_located_and_mapped():
    """reasoning_content 有单字符差异：模糊定位思维链区间并映射命中。"""
    rc_actual = _CHARS[:10]                      # 甲..癸（实际生成）
    rc_saved = rc_actual[:8] + "X" + rc_actual[9:]  # '壬' 被替换（覆盖率 0.9）
    resolver = FakeResolver(
        {1: "<think>", 12: "</think>", 13: "答", 14: "案",
         **{i + 2: ch for i, ch in enumerate(rc_actual)}}
    )
    loc = build_localization(
        ill_type=1,
        hits=[
            {"idx": 4, "token_id": 5, "logp_top1": -7.0},   # '丁'（匹配区内）
            {"idx": 9, "token_id": 10, "logp_top1": -7.0},  # '壬'（差异点上 → 跳过）
        ],
        windows=[],
        tokenids=[1] + list(range(2, 12)) + [12, 13, 14],
        text="答案",
        reasoning_content=rc_saved,
        resolver=resolver,
        seq_len=14,
    )
    assert loc["aligned_reasoning"] is True
    assert loc["aligned_text"] is True   # text_start 推进正确，正文精确对齐
    assert loc["align_mode"] == "approx"
    assert loc["reasoning_end"] == 12    # 正文'答'的 token 索引
    spans = loc["reasoning_spans"]
    assert [(s["start"], s["end"]) for s in spans] == [(3, 4)]  # 差异点命中被跳过


# --------------------------- 思维链提取 + EOS 截尾 --------------------------- #
def test_reasoning_extracted_with_end_marker():
    """reasoning_content 缺失：凭 </think> 提取思维链（含开始标记剥离）。"""
    resolver = FakeResolver(
        {1: "<think>", 2: "思", 3: "考", 4: "</think>", 5: "答", 6: "案"}
    )
    loc = build_localization(
        ill_type=1,
        hits=[
            {"idx": 1, "token_id": 2, "logp_top1": -7.0},  # 思维链内
            {"idx": 4, "token_id": 5, "logp_top1": -7.5},  # 正文内
        ],
        windows=[],
        tokenids=[1, 2, 3, 4, 5, 6],
        text="答案",
        reasoning_content=None,
        resolver=resolver,
        seq_len=6,
    )
    assert loc["reasoning_text"] == "思考"
    assert loc["aligned_reasoning"] is True
    assert loc["aligned_text"] is True
    assert loc["reasoning_end"] == 4
    assert [(s["start"], s["end"], s["kind"]) for s in loc["reasoning_spans"]] == [
        (0, 1, "hit"),
    ]
    assert [(s["start"], s["end"], s["kind"]) for s in loc["text_spans"]] == [
        (0, 1, "hit"),
    ]


def test_reasoning_extracted_without_start_marker():
    """无 <think> 开始标记、仅 </think>：同样切分提取（口径放宽）。"""
    resolver = FakeResolver({2: "思", 3: "考", 4: "</think>", 5: "答", 6: "案"})
    loc = build_localization(
        ill_type=1,
        hits=[
            {"idx": 0, "token_id": 2, "logp_top1": -7.0},  # 思维链内
            {"idx": 3, "token_id": 5, "logp_top1": -7.5},  # 正文内
        ],
        windows=[],
        tokenids=[2, 3, 4, 5, 6],
        text="答案",
        reasoning_content=None,
        resolver=resolver,
        seq_len=5,
    )
    assert loc["reasoning_text"] == "思考"
    assert loc["aligned_reasoning"] is True
    assert loc["aligned_text"] is True
    assert loc["reasoning_spans"][0]["start"] == 0
    assert loc["text_spans"][0]["start"] == 0


def test_reasoning_extracted_start_marker_only():
    """仅有 <think>（截断生成兜底）：其后全部视为思维链。"""
    resolver = FakeResolver({1: "<think>", 2: "思", 3: "考"})
    loc = build_localization(
        ill_type=3,
        hits=[{"idx": 1, "token_id": 2, "logp_top1": -1.0}],
        windows=[],
        tokenids=[1, 2, 3],
        text=None,
        reasoning_content=None,
        resolver=resolver,
        seq_len=3,
    )
    assert loc["reasoning_text"] == "思考"
    assert loc["aligned_reasoning"] is True
    # 提取体 = align_text[7:]，'思' 映射到提取体相对坐标 (0,1)
    assert loc["reasoning_spans"][0]["start"] == 0
    assert loc["reasoning_spans"][0]["end"] == 1


def test_eos_trailing_trimmed_for_alignment():
    """解码重建尾部带 EOS：截掉后再与保存 text 对齐（短输出相似度不再被拉低）。"""
    resolver = FakeResolver(
        {1: "你", 2: "好", 9: "<|im_end|>"},
        eos={9},
    )
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 0, "token_id": 1, "logp_top1": -8.0}],
        windows=[],
        tokenids=[1, 2, 9],
        text="你好",
        reasoning_content=None,
        resolver=resolver,
        seq_len=3,
    )
    assert loc["align_text"] == "你好"       # EOS 已截掉
    assert loc["aligned_text"] is True
    assert loc["align_mode"] == "exact"
    assert loc["text_spans"][0]["start"] == 0


def test_text_prefix_with_extra_suffix_aligned():
    """EOS 未被识别（无 eos 信息）：正文为重建的精确前缀 → 精确对齐。"""
    resolver = FakeResolver({1: "你", 2: "好", 3: "<|end|>"})
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 1, "token_id": 2, "logp_top1": -8.0}],
        windows=[],
        tokenids=[1, 2, 3],
        text="你好",
        reasoning_content=None,
        resolver=resolver,
        seq_len=3,
    )
    assert loc["align_text"] == "你好<|end|>"
    assert loc["aligned_text"] is True
    assert loc["align_mode"] == "exact"
    assert loc["text_spans"][0]["start"] == 1


# --------------------------- 全量 text（含思维链原文）的包含定位 --------------------------- #
def test_full_text_containing_reasoning_answer_segment():
    """text 为全量输出（思维链+正文，无标记）：先切出正文段再比，offset 随 loc 下发。

    场景：reasoning_content='思考'、text='思考答案'（旧中间件未拆分）。
    命中在思维链 → reasoning_spans（保存 reasoning 坐标）；
    命中在正文 → text_spans 为切出正文段坐标，text_answer_offset=2 供前端补偿。
    """
    resolver = FakeResolver({1: "思", 2: "考", 3: "答", 4: "案"})
    loc = build_localization(
        ill_type=1,
        hits=[
            {"idx": 0, "token_id": 1, "logp_top1": -7.0},  # 思维链内
            {"idx": 3, "token_id": 4, "logp_top1": -7.5},  # 正文内
        ],
        windows=[],
        tokenids=[1, 2, 3, 4],
        text="思考答案",
        reasoning_content="思考",
        resolver=resolver,
        seq_len=4,
    )
    assert loc["aligned_text"] is True
    assert loc["aligned_reasoning"] is True
    assert loc["reasoning_spans"][0]["start"] == 0       # 相对 reasoning_content
    # 正文命中为切出正文段内坐标（'案' 在正文段[1]），偏移由 text_answer_offset 下发
    assert loc["text_spans"][0]["start"] == 1
    assert loc["text_spans"][0]["end"] == 2
    assert loc["text_answer_offset"] == 2


def test_full_text_with_markers_containing_reasoning():
    """text 含 <think> 标记原文：按 </think> 切出正文段，offset=17。"""
    resolver = FakeResolver(
        {1: "<think>", 2: "思", 3: "考", 4: "</think>", 5: "答", 6: "案"}
    )
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 4, "token_id": 5, "logp_top1": -7.0}],
        windows=[],
        tokenids=[1, 2, 3, 4, 5, 6],
        text="<think>思考</think>答案",
        reasoning_content="思考",
        resolver=resolver,
        seq_len=6,
    )
    assert loc["aligned_text"] is True
    assert loc["text_spans"][0]["start"] == 0            # 切出正文段内坐标
    assert loc["text_spans"][0]["end"] == 1
    assert loc["text_answer_offset"] == 17               # '答' 在全量 text[17]


def test_approx_threshold_lowered_to_0_7():
    """相似度 0.85（介于旧阈值 0.9 与新阈值 0.7 之间）：近似对齐可用。"""
    align = _CHARS
    # 3 个字符被替换：ratio = 2*17/40 = 0.85
    saved = (_CHARS[:3] + "XYZ" + _CHARS[6:12] + "KQV" + _CHARS[15:])
    assert len(saved) == len(align)
    loc = build_localization(
        ill_type=1,
        hits=[{"idx": 1, "token_id": 2, "logp_top1": -7.0}],
        windows=[],
        tokenids=list(range(1, 21)),
        text=saved,
        reasoning_content=None,
        resolver=_seq_resolver(),
        seq_len=20,
    )
    assert loc["aligned_text"] is True
    assert loc["align_mode"] == "approx"
    assert loc["text_answer_offset"] == 0
    assert loc["text_spans"][0]["start"] == 1
