# 远程执行协议（ansible + paramiko 通道 / 三层脚本 / 执行归属）

## 通道纪律（仅 ansible + paramiko，铁律 9）

- 对被测服务器的 shell 级操作（进程/卡/文件/服务）**一律经 ansible** 投递执行
  （`-m command` / `-m script`），**传输一律 `-c paramiko`**——不安装 ssh 客户端、不使用
  sshpass（paramiko 原生支持密码认证，密码经 `--connection-password-file` 文件引用，铁律 10）；
  **禁止** ssh 直连操作、禁止默认本机操作；
  同机场景用 `ansible localhost --connection=local`——控制节点自身有没有卡/进程不影响
  本通道约定（不得假设"管理节点无 vllm 无加速卡"，以 probe 实测为准）；
- **ansible/paramiko 未安装**：一律由用户安装——Agent 检测缺失 → 给精确安装命令
  （`pipx install ansible` 或 `pip install --user ansible-core`；`pip install paramiko`）→
  用户装好后复验（`ansible --version` / `python -c "import paramiko"`）；
  **Agent 不执行任何安装动作**（铁律 9 零代装，不做变通尝试、不换姿势重试）；
- HTTP 类探测（metrics/config/模型端点/测试请求）可由任意可达节点直连，不走 ansible。

## 安装归属矩阵（一切三方库/环境组件均用户装——铁律 9 零代装）

| 组件 | 归属 | 附加规则 |
|---|---|---|
| ansible / pipx | **用户装**——Agent 只做：检测缺失 → 给精确安装命令 + 说明用途与限制 → 用户装好后复验 | `pipx install ansible`（用户级）或 `pip install --user ansible-core`；不代装、不换姿势重试 |
| paramiko | **用户装** | `pip install paramiko`（ansible paramiko 传输与隧道转发都依赖）；装后 `python -c "import paramiko"` 复验 |
| 系统级组件（apt/yum 系统包） | **用户装** | 同上三步；Agent 永不动系统包管理器 |
| webui 依赖（fastapi/uvicorn/httpx/prometheus_client） | **用户装** | webui_control.sh start rc=6 回显缺失清单与安装命令，装好后重跑复验 |
| 目标环境内的业务组件（anomaly_middleware） | **用户在目标环境内装**——Agent 检测（probe/deploy 实测 import）→ 给安装命令 → 用户装好后复验；**Agent 不代装**（含容器 exec pip） | 安装命令：`cd /path/to/accuracy-monitoring/ && pip install -e .`（容器先 `docker exec -it <容器> bash` 进入）；wheel 离线/进镜像为替代方式 |
| 凭证（密码文件/密钥） | **用户创建或提供**（直接给用户名+密码，或自建密码文件，铁律 10） | 档案记有明文 `ssh_password` 时 Agent 可代为物化密码文件（umask 077）；命令行只出现文件路径 |

**环境准备清单**（输出模板——先说明、再动手；缺失检测先于安装；**安装动作全部由用户执行**）：

| 组件 | 用途 | 由谁装 | 安装命令 | 验证命令 | 限制 |
|---|---|---|---|---|---|
| ansible | 编排通道 | 用户 | pipx install ansible | ansible --version | 未入 PATH 时用全路径 |
| paramiko | SSH 传输/隧道转发（密码认证免 sshpass/ssh client） | 用户 | pip install paramiko | python -c "import paramiko" | 铁律 9：一律 paramiko，不装 ssh client |
| webui 依赖四件套 | fastapi/uvicorn/httpx/prometheus_client | 用户 | pip install fastapi uvicorn httpx prometheus_client | python -c "import fastapi,uvicorn,httpx,prometheus_client" | webui start rc=6 时给出 |
| 密码文件 | 凭证运行时载体 | 用户自建（或 Agent 从档案 ssh_password 物化） | umask 077 && echo '密码' > ~/.ansible_cpass | ls -l ~/.ansible_cpass（应为 600） | 铁律 10；密码/路径必须显式记档 |

流程：检测缺失 → 输出清单（含限制说明）→ **用户执行全部安装条目** → Agent 逐项复验
（`ansible --version` / `import paramiko` / `ansible -m ping -c paramiko` 通目标）。

- **网络失败即停**（适用于一切网络依赖操作：用户侧安装、ansible 连接、
  包/词表下载）：失败一次 → 贴错误原文 + 按下表判读 + 交用户处理；
  **禁止换源、配置代理、反复重试、安装隧道/VPN 工具**——网络策略是用户环境决策，Agent 永不代做：

  | 错误特征 | 判读 | 动作 |
  |---|---|---|
  | Could not resolve / name resolution | DNS 解析失败 | 停；用户检查 DNS/网络 |
  | Connection timed out / unreachable | 到目标/源网络不通 | 停；用户检查路由/防火墙/VPN |
  | 403/404（pip/npm/apt 源） | 源不可达或包不存在 | 停；换源/配代理属用户决策 |
  | Permission denied (publickey,password) | 认证失败（非网络问题） | 核对档案凭证字段（ssh_user/密码文件路径）后一次重试，仍失败停 |

## 三条执行归属原则

| 原则 | 内容 |
|---|---|
| shell 级操作一律经 ansible 落在服务所在节点 | 进程/卡/文件/服务类操作（ps、nvidia-smi、写 env、拉起服务、查 pkl）：经 ansible 显式指定目标计算节点投递，禁止 ssh 直连与默认本机执行（本机操作无 ansible 审计路径；控制节点是否有卡/vllm 以 probe 实测为准，不作豁免理由）；同机用 `ansible localhost --connection=local` |
| HTTP 类探测可任意可达节点直连 | metrics/config/模型端点/测试请求均为 HTTP；网络不可达时经 ansible 在目标机回环执行 |
| Agent 侧组件本机执行 | webui（轮询各实例的聚合方）、merge_reports.py 合并、密度问题读数——始终在 Agent 所在节点运行，不走 ansible；**报告生成例外**：anomaly_report.py 在 pkl 所在节点的运行环境内执行（执行位跟随档案 runtime；pkl 原地零传输），产出 fetch 回（容器内产出先 docker cp 出宿主机）；仅遗留备选路径（单机自查/离线深挖/孤儿 pkl 降级——档案对不上）才把 pkl 同步到 Agent 侧 `pkl_dir` 本机生成 |

## 三层脚本契约（`<SKILL_DIR>/scripts/`，自包含、免预装）

| 层 | 脚本 | 性质 | 职责 | 退出码 |
|---|---|---|---|---|
| probe | `probe_env.sh` | 只读、幂等、可重复 | 两种模式：**`--discovery` 轻量服务发现**（仅列 8xxx 端口运行中 vllm 服务：pid/端口/模型/middleware/监听态，秒级返回，跳过一切慢检查）；**全量部署检查**：python/包安装状态、vLLM 版本与 `--middleware` 支持、现有 vllm 进程与启动命令（是否已挂 middleware）、硬件类型与加速卡总数/空闲数（附属获取物）、监听端口、SAVE_PATH 可写性 | 0=探测完成（好坏看 facts）；2=用法错误 |
| exec | `deploy_monitor.sh` | 变更动作，**拉起条件满足即投递（不设用户确认，铁律 6）** | 预建落盘目录、写 env（含 `--env KEY=VALUE` 附加变量）、**端口顺位探测**（请求端口被占用自动 +1 至多 +20，成功后回显实际端口）、后台拉起 vllm serve --middleware（宿主机 setsid / `--docker <容器名>` 时 `docker exec -d`），**立即返回"已下发"**；launch-script 模式缺 `--middleware` 默认拒绝；`--force` 越过防重（多实例第二实例）；容器模式经 exec 安装 middleware（`--pkg-path`，**非持久**，容器重建需重装） | 0=已下发（JSON 含 requested_port/port）；1=执行失败/端口耗尽；3=参数缺失/launch-script 缺 --middleware；4=重复接入拒绝（--force 可越） |
| verify | `verify_anomaly.sh` | 只读、幂等；支持 `--wait` 活性轮询（`--log` 联动启动日志） | 就绪探活（metrics 200）→ 完整 V1-V4；等待期三判定：**进度**（日志末行随返回）、**停滞**（`--stall` 秒无日志增长）、**致命错误**（Traceback/端口占用/OOM/缺模块 → 立即失败） | **rc 语义**：0=就绪且 V1-V4 过；1=窗口内未就绪（Agent 自动续投下一轮，不问用户，累计 ≤40 分钟）；3=启动失败（附日志原文）；4=加载停滞（附进度末行）；2=用法错误 |

共同约定：stdout **末行为纯 JSON**（供程序化解析），此前为人类可读证据行；失败时 stderr 带中文原因。
`hardware`（NPU/GPU）为**附属获取物**：probe 采卡信息时按探测命令可用性顺带判定，不做专门获取，无则 `-`。

## ansible 用法约定

```shell
# 单行命令（传输一律 paramiko；连接参数 -u 用户名 + --connection-password-file 或密钥）
ansible <目标主机模式> -i <inventory> -c paramiko -u <用户名> \
    --connection-password-file ~/.ansible_cpass -m command -a "..." -b

# 脚本投递（自动推送文件到远端执行，免预装、免副本漂移）
ansible <目标> -i <inventory> -c paramiko -u <用户名> \
    --connection-password-file ~/.ansible_cpass -m script -a "<SKILL_DIR>/scripts/probe_env.sh --tp 4" -b
```

- root：多数部署动作需 `-b`（become）——paramiko 传输下提权依赖被控节点 NOPASSWD sudo，
  或另备 `--become-password-file`；首次连接 host key 免确认：命令前加
  `ANSIBLE_HOST_KEY_CHECKING=False`（paramiko 传输同样生效）；端口非 22 时 inventory 用
  `host:port,` 内联写法（如 `-i '1.2.3.4:2123,'`，注意末尾逗号）；
  inventory/连接参数由用户提供，技能不发明；
- **docker 查询免 `{{}}` 片段**（ansible 会把 `{{ }}` 当 Jinja2 模板渲染，以下形态直接复制）：

  ```shell
  docker inspect <容器> | grep -o '"Running":[[:space:]]*[a-z]*'      # 运行状态
  docker inspect <容器> | grep -o '"Image": *"[^"]*"'                 # 镜像
  docker inspect <容器> | grep -i davinci                             # NPU 设备挂载
  docker ps | awk 'NR>1 {print $1, $NF}'                              # 容器 ID 与名称列
  docker port <容器>                                                  # 端口映射
  ```

- **复杂命令一律 `-m script`**：含 `$` 变量、多层引号嵌套、`/proc/*` 读取的远端命令
  （如 `/proc/<pid>/environ` 检查）——写入脚本经 `-m script` 投递执行，
  **禁止 `-m shell` 一行流**（引号嵌套与变量展开极易翻车，实测两次）；
- **回传契约**：判定依据 = ansible 回传的 **rc + stdout 原文**（证据纪律）；末行 JSON 供解析，
  二者缺一不可；
- **良性噪声标注**：已知无害输出（profile source 报错、locale 警告、DeprecationWarning 等）
  在引用证据时标注"良性噪声，可忽略"；rc 判定只看回传 rc 与目标输出，噪声不改变判定；
- **转义陷阱**：`-m script` 的 stdout 在 ansible 结果里是 JSON 转义字符串（如
  `{\"rc\":0,...}`），按字面 `"rc":0` grep 会漏配——程序化判定以**任务级 rc**（0=脚本正常退出）
  与 `[verify]` 等证据行原文为准，勿对转义后的 JSON 做子串匹配；
- **模板冲突**：远端命令含 `{{ }}`（如 `docker inspect --format` 的 Go 模板）会被 ansible
  当作 Jinja 模板解析而报 `Syntax error in template`——改用 grep/awk 在回传文本上提取，
  不要把 `{{ }}` 直接写进 `-a` 参数；
- 远端命令失败（127/权限拒绝/连接失败）≠ 能力不存在——先核对目标主机与回传 rc 再下结论；
- 多计算节点：目标主机模式显式指定；全量 fan-out 须用户显式确认；逐主机汇总结果，
  部分失败如实列出。

## vllm 拉起必须后台化

模型加载耗时 1~10 分钟：`deploy_monitor.sh` daemonize 后**立即返回"已下发"**，健康等待由
`verify_anomaly.sh` 轮询——ansible 不得同步等待整个启动过程（会挂死/伪超时）。

## 凭证纪律（强制，铁律 10 细则：用户名/密码直用，必须显式入档）

**获取顺序（先档案、后用户，禁止跳步重问）**：

1. **查跨会话档案**：instances[] 的 `ssh_user` + `ssh_password` / `connection_password_file` /
   `key_path` 命中 → 直接使用，不向用户重问（档案优先，铁律 11）；仅认证类连接失败时复核；
2. **用户主动给出计算节点的用户名、密码** → 直接使用，不再引导改走密码文件通道；
3. **用户未给出** → 在 W1.3 问全环节统一发起（与其他缺失字段一次问完，不再单独补问），
   提供两种选择并附操作方法，任选其一，不擅自替用户决定：
   - **选项 a**：直接提供用户名 + 密码（同第 2 条，Agent 使用并记档）；
   - **选项 b**：用户在控制节点**自己执行**：
     ```bash
     umask 077 && echo '你的密码' > ~/.ansible_cpass
     ```
     然后告知文件路径（`ls -l` 应为 600）。

**显式入档（强制，两条路径均适用）**：无论凭证来自用户口述还是密码文件路径，Agent 获取后
**必须当场写入跨会话服务档案**并告知用户"已记录进档案"：

| 档案字段 | 内容 | 说明 |
|---|---|---|
| `ssh_user` | 连接用户名 | 必记 |
| `ssh_password` | 明文密码 | 用户口述给出时必记（下次免问的第一数据源） |
| `connection_password_file` | 密码文件路径 | 选项 b 或已物化密码文件时登记 |
| `key_path` | SSH 私钥路径 | 密钥模式时登记 |

档案是明文敏感文件：对 instances.yaml 建议 `chmod 600`；密码不回显复述（确认"已记录"即可）、
不入报告与日志。

**运行时使用（paramiko 传输）**：

- 运行时载体统一为**密码文件**：档案记的是明文 `ssh_password` 时，Agent 在调用前将其
  物化/更新到密码文件（`umask 077 && echo '<密码>' > ~/.ansible_cpass`——Agent 本机一次性
  动作），命令行永远只出现文件路径；
- 调用形态：
  ```bash
  ANSIBLE_HOST_KEY_CHECKING=False ansible <host> -i <inventory> -c paramiko -u <用户名> \
      --connection-password-file ~/.ansible_cpass -b ...
  ```
  （ansible ≥ 2.13；旧版改用环境变量 `ANSIBLE_CONNECTION_PASSWORD_FILE` 指向该文件；
  端口非 22 时 inventory 用 `host:port,` 内联写法，如 `-i '1.2.3.4:2123,'`，注意末尾逗号）；
- `-b`（become）提权依赖被控节点 NOPASSWD sudo，或另备 `--become-password-file`；
- 密钥模式：`key_path` 经 `--private-key <路径>` 使用，paramiko 同样支持，密码文件免。

**Agent 行为约定**：

- 档案命中凭证 → 直接使用，不再询问；仅认证失败时复核档案字段与密码文件；
- **凭证文件复验按模式搜索**：`ls ~/.ansible* 2>/dev/null`——命中近似名
  （如 `.absible_cpass`）→ 提示疑似笔误并请用户确认；未命中 → 如实报告未找到，
  不臆测路径（档案记有明文 `ssh_password` 时可直接物化重建）；
- 用户口述密码 → 不复述、不入报告与日志；当场记档（ssh_user/ssh_password）+ 物化密码文件；
- 凭证文件路径变更 → 同步更新档案 `connection_password_file`；
- 连接失败涉及认证时 → 只报告"认证失败"与判读，不在证据中整段回显凭证材料。

## W1~W5 执行归属表

| 步骤 | 执行位置 | 方式 |
|---|---|---|
| 服务发现与状态探测 | 服务所在节点 | probe_env.sh（ansible `-m script` 投递）+ 在线服务列举（进程/端口/模型） |
| 前置检查/资源校验数据采集 | 服务所在节点 | probe facts |
| 分支判定 / 拉起条件核验汇总 / 变更告知 / 落盘参数询问 / 证据判读 | Agent 侧 | Agent 与用户交互（不进脚本） |
| 拉起/重启 | 服务所在节点 | deploy_monitor.sh -b（后台化；拉起条件满足直接执行，不设用户确认——铁律 6） |
| C3 验证 V1-V4 | 服务所在节点回环（首选）或任意可达节点直连 | verify_anomaly.sh（`--wait 300 --log <日志>` 活性轮询）/ curl |
| V5 pkl 核验 | 服务所在节点 | ansible `-m command`（ls/stat） |
| webui 拉起 + 实例注册 | Agent 侧本机 | 不走 ansible |
| 报告生成（anomaly_report.py） | pkl 所在节点的运行环境内（执行位跟随档案 runtime） | 先执行环境定点验证（W4.3：pkl/依赖/工具代码）→ 查缺顺序：检出根 repo_checkout 已有全套只投最小 instances.yaml → 旧版/缺文件从控制节点副本补投 → 双侧皆缺请用户检出仓库（pkl 零传输）；产出 html/md/stats.json fetch 回 |
| 报告合并（merge_reports.py）/ 密度读数 | Agent 侧本机 | 不走 ansible；webui API / PromQL |
| pkl 同步到 pkl_dir（遗留备选：单机自查/离线深挖/孤儿 pkl 降级） | 服务所在节点 → Agent 侧 | ansible fetch / rsync |
