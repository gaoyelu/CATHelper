# 报告工具使用指南（C5 / W4）

`tools/anomaly_report.py`（位于仓库根，解析顺序见 SKILL.md「路径约定」）从 pkl 落盘数据生成《推理异常数据分析
报告》——支持单日/最近 N 天/任意日期区间、多实例聚合、离线重跑检测器还原异常定位细节，
输出 **Markdown + HTML 双格式**。

**执行位置（标准 = 节点侧生成 + 控制节点合并）**：报告在 **pkl 所在节点的运行环境内**生成
（执行位跟随档案 `runtime`：docker/venv/conda/bare——容器/venv 自带依赖时在运行环境内执行；
环境、tokenizer、pkl 现成，pkl 零传输；工具代码查缺顺序：节点侧检出根 `repo_checkout` →
控制节点副本补投缺失文件 → 双侧皆缺请用户检出仓库），产出 `<报告>.html + <报告>.stats.json` 回传控制节点，
由 `tools/merge_reports.py` 合并为一份总报告（见下节）。pkl 集中同步到控制节点本机生成
为遗留备选路径，仅单机自查/离线深挖、或孤儿 pkl 降级通道（档案对不上，见下文「孤儿 pkl」节）时使用。

## 分布式生成与合并（多实例舰队标准流程）

```
控制节点（唯一调度者）：
 1. 跨会话档案（instances.yaml，固定落点，查无即无档案）→ 解析目标实例 → 按节点分组
 2. 相对时间在控制节点换算为绝对窗口 --from/--to 下发（免疫节点时钟偏差）
 3. 执行环境定点验证（执行位 = 档案 runtime，不限容器）：docker → 容器内验证；venv/conda →
    该解释器验证；bare → 宿主机；档案缺 runtime 记录先补档。验证三项：
    ① pkl/tokenizer 可见性 ② numpy/PyYAML/transformers ③ 工具代码（import anomaly_middleware
    定位检出根 → tools/anomaly_report.py + configs/detector.yaml 存在性与版本一致性）
 4. 渲染每节点最小 instances.yaml（仅该节点实例：pkl_file/tokenizer_path/detector_config；
    server 用节点内网可达地址——EIP 从节点自身/容器内不可达，探活会误判离线）
 5. 按查缺顺序备料后执行：检出根已有全套工具代码 → 只投最小 yaml 执行
    anomaly_report.py --config <临时yaml> --from X --to Y --instance <名>；
    旧版/缺文件 → 从控制节点副本补投缺失文件（KB 级）；双侧皆缺 → 停止，请用户检出仓库
 6. fetch 回 <报告>.html + <报告>.stats.json（KB~MB 级；容器内产出先 docker cp 出宿主机）
 7. merge_reports.py 合并 → 一份总报告呈现用户
```

工具改造约定（anomaly_report.py 已内置）：
- **章节埋点**：每实例章节被 `<!-- ar:instance:<名>:begin -->` / `:end -->` 包裹（错误/
  零异常节同样有完整标记对）——合并器据此拼接；
- **stats.json 侧车**：与报告同基名输出（`stats_version` / window / data_window /
  overview_columns / rows[]（总览行结构化孪生，列名=键）/ errors[] / type_counts）——
  合并器按列名对齐取值，不刮 HTML、不重算求和。

merge_reports.py 用法：

```bash
python tools/merge_reports.py --output <合并报告.html> \
    [--title 标题] [--error 实例名=原因 ...]   # 整节点失败（无报告回传）用 --error 声明
    <报告1.html> <报告2.html> ...
# 语义：1.总览 = 表头一行 + 每实例一行（列名对齐，"详细分析"锚点跳转合并后章节）；
#       2+. 各实例章节原样拼接（SVG/明细随章节整体搬运）；尾部错误汇总（铁律 8：单实例
#       失败不阻塞，失败进"实例状态与错误"表）；stats_version 不兼容明确报错。
```

## 前置条件

1. Python 依赖：`numpy`、`PyYAML`（必需）；`transformers`（tokenizer 解码；缺失时报告仍生成
   分布表，明细无法解码）。依赖在**运行环境内**验证（执行位跟随档案 runtime）：容器/venv 自带
   依赖时在运行环境内执行报告（容器内产出经 docker cp 出宿主机再 fetch 回）——不在被测节点
   宿主机 pip 装依赖；
2. `configs/instances.yaml`（跨会话服务档案，见下节）已配置实例；
3. 各实例 pkl 位于配置 `pkl_dir` 指向的**本地**目录（本工具不做远程拉取）。标准流程 = 节点侧生成：
   pkl_dir 即 pkl 所在节点上的原地目录（每节点渲染最小 instances.yaml，pkl 零传输）；仅遗留备选
   路径（控制节点本机生成/离线深挖/孤儿 pkl 降级）才需先同步 pkl（ansible fetch / rsync；持续追加中的大 pkl
   建议先压缩再传输，同步慢且易不一致）。**容器部署链路**：pkl 在容器层（`VLLM_ANOMALY_SAVE_PATH`
   为容器内路径）→ 先 `docker cp <容器>:<save_path> <宿主机临时目录>` 拷出到宿主机 → 就地作为
   该节点 pkl_dir（宿主机挂载目录内的 pkl 宿主机直达，无需 docker cp；执行位同理——runtime
   记录为容器时报告在容器内执行，容器自带 transformers/numpy）；节点侧生成即止，仅遗留
   备选路径再 fetch 到控制节点；
4. 离线重跑定位使用仓库内 `ILLDetector` + `configs/detector.yaml`（与线上一致的单一事实来源），
   重跑阈值/词表须与线上一致，不一致时报告显著标注；
5. 工具代码查缺顺序（拉起过监控 ⇒ 运行环境内必有 anomaly_middleware；editable 安装 ⇒ 检出根
   多半有全套）：节点侧检出根 `repo_checkout`（import anomaly_middleware 定位）→ 控制节点
   仓库副本补投缺失文件（KB 级）→ 双侧皆缺 → 停止，请用户检出仓库 `pip install -e .`；
   旧检出须核对存在性与版本（stats_version 与控制节点副本一致），不盲信"能 import = 有 tools/"。

## CLI 契约

```
python tools/anomaly_report.py
    [时间窗，四选一，互斥]
    --date YYYY-MM-DD|today|yesterday   # 单日（缺省 = today）
    --days N                            # 最近 N 天（含今天），N≥1
    --from YYYY-MM-DD --to YYYY-MM-DD   # 任意闭区间
    --all                               # 数据中出现的全部日期
    [可选]
    --config PATH                       # 默认 configs/instances.yaml
    --report-dir PATH                   # 缺省取配置 report_dir，再缺省 ./reports
    --instance NAME [NAME ...]          # 过滤实例（缺省全部）
    --ill-type TYPE [TYPE ...]          # 过滤类型：rare_character|garbled|repetition|nan_value
    --no-detector                       # 跳过离线重跑定位，仅统计分布（大窗口快速出报告）
    --max-detail N                      # 每实例每日每类型明细上限（默认 5）
    --format html|markdown|both         # 输出格式（默认 both：同名 .html 与 .md 各一份）
```

**语义规则**：

- 时间窗参数互斥，同时出现 → exit 2 并提示用法；`--days N` 等价 `--from (today-(N-1)) --to today`；
- 报告命名：单日 `推理异常数据分析报告_{date}.html`；区间 `推理异常数据分析报告_{from}_to_{to}.html`；
  同名 `.md` 同目录各一份；
- 幂等：同参数重跑覆盖同文件；窗口内某日无记录 → 计入趋势表为 0，不视为错误；
- exit code：0 成功；1 运行错误（配置缺失/pkl_dir 不存在）；2 参数互斥/非法；3 全部实例失败；
- stdout 输出摘要（各实例分类型数量/失败实例原因），最后一行 `报告已生成: <html绝对路径>`。

## 跨会话服务档案（configs/instances.yaml）

报告数据源 + 部署事实缓存（Agent 维护：读档优先、缺则询问+回写、探后回写；
落点仅 SKILL.md「路径约定」两处（`<仓库根>/configs/` 或 `<SKILL_DIR>/.archive/`），
查无即无档案，禁止全盘搜索补找（SKILL.md 铁律 11）；
`*` 标注字段为部署档案可选字段，报告工具容忍未知字段）：

```yaml
report_dir: ./reports              # 报告输出目录（可被 --report-dir 覆盖）
pkl_dir: /data/pkl_sync            # pkl 所在本地目录（标准 = 节点侧 pkl 原地目录；遗留备选 = 控制节点集中同步目录）
detector_config: configs/detector.yaml   # 离线重跑阈值（与线上同口径）

instances:
  - name: vllm-prod-1              # 实例唯一名（档案主键，按 name/model/host 匹配）
    server: 10.0.0.1               # 计算节点 IP/主机名（节点内网可达地址——EIP 从节点自身/容器内不可达，报告状态列探活用）
    pkl_file: qwen3-0.6b.pkl       # 相对 pkl_dir 或绝对路径
    tokenizer_path: /models/Qwen3-0.6B   # 本地 HF 格式 tokenizer 目录（离线重跑 tk2cat 用）
    reasoning_end_markers: ["</think>"]  # 可选，思维链结束标记，默认 ["</think>"]
    # ---- 以下为部署档案可选字段（probe/deploy/verify 回写）----
    form: single                   # * single | pd_proxy
    deploy_mode: bare              # * bare | systemd | docker
    runtime: bare                  # * 运行环境标识：bare | docker:<容器名> | venv:<解释器路径> | conda:<env名>
                                   #   ——报告执行位/验证位依据（W4.3 定点验证在此环境内做）
    repo_checkout: /opt/accuracy-monitoring  # * 节点侧部署检出根（middleware 安装源目录）——W4 工具代码查缺第一落点
    launch_cmd: "vllm serve ..."   # * 完整启动命令
    launch_script: /opt/start.sh   # * 启动脚本路径（如有）
    model: /models/qwen3           # * 模型路径/名称
    tp: 4                          # * tensor parallel 大小
    port: 8000                     # * 服务端口
    metrics_path: /anomaly/metrics # * 指标端点路径
    save_path: /data/anomaly/      # * 计算节点上 pkl 落盘目录
    hardware: NPU                  # * NPU | GPU | "-"（附属获取物，probe 顺带判定）
    integrated_at: "2026-10-20"    # * 接入日期
    last_probed: "2026-10-20 15:00"      # * 最后一次 probe 时间（新鲜度提示，过期重探）
    # ---- 连接凭证字段（铁律 10：获取后必须显式记录，下次会话直接复用、不再重问）----
    ssh_user: root                       # * ansible 连接用户（必记）
    ssh_password: "xxxxxx"               # * 明文密码（用户直接给出时必记；与密码文件可并存）
    connection_password_file: ~/.ansible_cpass   # * 密码文件路径（运行时载体；选项 b 用户自建或 Agent 物化）
    key_path: ~/.ssh/id_ed25519          # * SSH 私钥路径（密钥模式）
    stopped:                             # * 停服标记（结构化；停服后条目保留，重新拉起后删除本字段）
      result: "停服成功（定向停服，无残留）"  #   * 停服结果摘要
      at: "2026-10-29 08:15"             #   * ISO 时间
      note: "用户要求；卡已释放"           #   * 原因/影响（可选）
```

**档案使用守则**：静态身份信息（模型路径/tokenizer/启动脚本/pkl 文件名/部署方式/runtime/
repo_checkout/**连接凭证**（ssh_user/ssh_password/connection_password_file/key_path——铁律 10
显式入档，命中直接复用不重问，仅认证失败时复核））可信复用；
动态状态（进程存活/端口/空闲卡/monitor_rate 当前值）执行前**必须实测**（铁律 2）——
档案只免"问用户"，不免"实测"。档案为明文敏感文件（含密码字段），建议 `chmod 600`。
**停服双轨**：本档案条目默认保留 + 结构化 stopped 标记
（webui.yaml 登记项默认注销，见 webui-guide.md）。**拉起请求先查档案**：用户给出被测 IP
即按 host 过滤该机实例，running/stopped 分列展示（running 状态顺手 HTTP 复核标注），有
历史实例：**单命中自动复用**（复用仍 probe 实测再拉起，不问用户），多命中/参数冲突才让用户选
——档案命中把"问参数"坍缩为零交互或一次复用。**落点**：仓库可用 → `<仓库根>/configs/instances.yaml`；
repo-less（仓库解析失败）→ `<SKILL_DIR>/.archive/instances.yaml`。

## 孤儿 pkl（档案对不上）的报告路径

适用：需要为某个 pkl 出报告，但 instances.yaml 查无对应实例（按 文件名/路径/目录 过滤无命中）。

**纪律：对不上 → 立刻停止探寻**——不读 pkl 猜身份、不漫游找 tokenizer/模型、不全盘搜索
（铁律 11）；第一步就是把降级后果讲明白，让用户二选一。

**先告知（必做）**：不知道该 pkl 的执行环境与模型路径 ⇒ tokenizer 无法正确加载 ⇒
**明细/异常用例解码失败，报告只剩类型分布/趋势/计数，异常用例展示不完备**。

- **用户接受降级**：
  1. **优先使用档案里已有的执行环境**——借用其他已登记实例的 runtime（容器 exec /
     venv·conda 解释器；仅需 numpy/PyYAML，pkl 对该环境只读可见即可执行；tokenizer 同样降级）；
     借用环境定点验证不过（pkl 不可见/缺依赖）→ 落到下一条，不擅自换第三个环境；
  2. 档案里也没有可用执行环境记录 → **优先使用控制节点生成**（孤儿 pkl 豁免通道）：
     pkl fetch 回控制节点本机执行 anomaly_report.py（需 numpy/PyYAML，缺失按铁律 9 给命令
     由用户装），**tokenizer 降级**——分布表正常、明细无解码，报告显著标注；
  3. 实例命名 `orphan-<pkl文件名>`，标注"来源未匹配 + 降级范围"，产出照常 fetch/merge；
- **用户不接受 → 用户提供最少要素**（pkl 路径已知）：**执行环境**（docker:<容器名> /
  venv:<解释器路径> / conda:<env名> / bare）+ **模型路径**（tokenizer 加载源）；
  **先验证后生成**——W4.3 三项定点验证在该执行环境内做（pkl/tokenizer 可见性、
  numpy/PyYAML/transformers、检出根工具代码），验证不过把缺失项原样反馈，不擅自换环境；
  通过 → 渲染最小 instances.yaml → 执行环境内生成 → fetch 回 → merge；
- **收尾必做：信息回写档案**——新登记实例条目（pkl_file/server/model/tokenizer_path/runtime/
  覆盖窗口 + note：来源未匹配、用户已确认，执行环境/模型来源注明"用户提供"或"降级借用"），
  下次出报告免重查；全程对 pkl 零写操作（不改名/不删/不移动）。

## 处理流水线

```
解析 CLI/档案 → 确定时间窗 [d1, d2]
 → Phase 1 加载过滤：逐实例加载 pkl → 按 time 过滤窗口 → 按 instance/ill_type 过滤
 → Phase 2 统计聚合：日×实例×类型计数矩阵；逐日计数序列（趋势图数据源）；
    窗口首日/末日类型集合（类型变化行数据源）
 → Phase 3 明细定位（未 --no-detector）：离线重跑 ILLDetector（有词表→无词表依序尝试，
    任一与线上 ill_type 一致即采用）；生僻字逐位置明细；乱码/重复触发窗口定位；
    思维链结束标记定位（未找到或首 token 即标记 → 提示无法区分思维链与输出）
 → Phase 4 渲染：Markdown + HTML 双格式 → report_dir/ → stdout 摘要
```

## 报告结构（HTML 为准，Markdown 同构简化）

```
报告头（meta 小字，仅三项）：标题 + 数据日期（窗口起止）+ 报告生成时间 + 实例数
1. 总览
   - 实例总览表：服务实例|模型|部署服务器|硬件|状态|部署方式|检出异常数|详细分析[锚点]
     · 硬件：档案 hardware（附属获取物，无则 "-"）；部署方式：档案 form（PD(proxy) 硬件列 "-"）
     · 状态：生成时刻能否 HTTP 访问该实例 /anomaly/metrics（可达=在线，不可达=离线）
   - 实例状态与错误表（pkl 缺失/tokenizer 缺失/解析错误 + 补救动作；仅存在错误时展示）
逐实例详细解析（每实例一节 h2，锚点对应总览"详细分析"）
   - 异常类型分布表：异常类型 | 数量 | pkl 文件数据位置序号（可回查）；NaN 仅计数不成节
   - 类型变化（窗口首日 vs 末日）：新出现/消失的异常类型 note 行（有变化才展示）
    - 检出趋势：自适应粒度（窗口 ≤2 天按小时分桶、x 轴铺满整个固定窗口、空档计 0；
      更长窗口按天）——仅 HTML 内联 SVG 折线图，x 轴标注按标签宽度自适应稀疏化
      （相邻日期标签不重叠，末标签 end 锚点防出界），悬停看精确时段与
      数值；粒度记入 stats.json（trend.granularity）；Markdown 无图不铺数值，直接省略本节
   - 异常实例具体分析（每类型 ≤max-detail 条）：HTML 中每条为 <details> 折叠块（可下拉查看），
     Markdown 平铺；每条含：
     生僻字=逐位置明细表（位置/token_id/token/上下文/top20 候选，≤20 行）；
     乱码与重复=触发窗口 + ≤200 token 展示文本块；
     思维链：未找到结束标记（含序列首 token 即标记的情形）→ "当前序列未找到思维链结束标记，
     无法区分思维链与输出"，否则标注结束位置
     尾注（有截断时）："其余 N 条请按 pkl 序号回查源文件内容"
```

> 报告即最终交付物，无附录/口径说明/不一致清单/数据清单等附加章节；pkl 序号在分布表与
> 每条明细中直接标注，可回查源文件。

## 降级与容错

| 场景 | 行为 |
|---|---|
| 某实例 pkl 缺失 | 该实例章节保留 + 状态"pkl 缺失" + 补救动作；其余实例正常（铁律 8） |
| tokenizer 缺失/加载失败 | 分布表正常；明细无解码，标注原因 |
| 离线重跑单条异常 | 该记录退化为输出文本展示 + 备注各模式失败原因 |
| 重跑模式均与线上不一致 | 采用线上 ill_type 展示（检出事实以 pkl 为准，不另列清单） |
| `--no-detector` | 跳过 Phase 3，明细降级为记录摘要（时间/类型/输出前 200 字符） |
| 全部实例失败 | exit 3，不生成残缺报告 |
| 窗口无记录 | 生成"零异常"报告，总览显著标注 |
