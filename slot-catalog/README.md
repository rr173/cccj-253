# stable-slot-catalog 槽位目录

公示系统的**可局部维护槽位目录**与**两种读视图**，外加一条面向公示镜像的
**离线同步通道**。不生成纸质文档；纸版只留"各版曾正式列出的槽与身份"的
不可变表。纯 Python 3 标准库，无第三方依赖，无权限系统、无远程接入。

## 快速开始

```sh
cd slot-catalog
python3 -m unittest discover -s tests -t . -v   # 单元测试（117 个）
python3 acceptance/run_acceptance.py            # 验收：跑场景并比对 golden
python3 acceptance/merge_example/run_merge_example.py  # 离线合稿 CLI 验证
python3 acceptance/sync_example/run_sync_example.py    # 离线同步通道 CLI 验证
./run_all.sh                                    # 上面四条一起
```

CLI（从仓库根目录运行，或设 `PYTHONPATH` 指向仓库根目录）：

```sh
python3 -m slotcatalog init   [--out STATE.json]             # 新建空目录状态
python3 -m slotcatalog run    SCENARIO.json                  # 执行场景文件
python3 -m slotcatalog apply  STATE.json BATCH.json --out NEW.json  # 局部提交一个批次
python3 -m slotcatalog merge  BASE.json BRANCH1.json BRANCH2.json [...] --out MERGED.json  # 离线合稿
python3 -m slotcatalog query  STATE.json V1 B1 0007          # stable-slot 查询
python3 -m slotcatalog export STATE.json --booklet B1        # 搜索导出

# 离线同步通道（见下文）
python3 -m slotcatalog sync-open       STATE.json --out SRC.json        # 开通同步源
python3 -m slotcatalog sync-apply      SRC.json BATCH.json --out SRC2.json  # 提交更正批次
python3 -m slotcatalog sync-checkpoint SRC.json                        # 查看源检查点
python3 -m slotcatalog sync-package    SRC.json [--from-seq N] --out PKG.json  # 首包/增量包
python3 -m slotcatalog mirror-install  PKG.json [--mirror M.json] --out M2.json # 装包
python3 -m slotcatalog mirror-query    M.json V1 B1 0007               # 镜像纸册索引查询
python3 -m slotcatalog mirror-export   M.json [--booklet B1]           # 镜像搜索视图
python3 -m slotcatalog mirror-checkpoint M.json                        # 查看镜像检查点
```

所有输出均为确定性 JSON（键排序、UTF-8、无时间戳、无随机数），可逐字节复跑。

## 概念模型

| 概念 | 说明 |
|---|---|
| 册 booklet | 唯一身份字符串。每册独立的非负整数槽空间。 |
| 槽长 slot_width | 排版固定槽长：槽号的定宽印刷宽度（宽 4 时槽 7 印作 `0007`），同时限定册容量为 `10^slot_width` 个槽。 |
| 槽 slot | 三态：`reserved`（预留占位，无件）、`occupied`（有件）、`tombstone`（撤行后的墓碑；件号仍属原槽，槽永不复用）。 |
| 件 part | 件号在册内唯一，一生只占一个稳定槽。正文 = 登记 body + 补写 attachments。 |
| 批次 batch | 编目批次：一组操作的原子提交；替代图不变量按**整批终态**校验。 |
| 版次 version | 纸版表：发布时各册"正式列出的 槽→件身份"快照，不可变，不复制正文。 |
| 替代边 edge | `old → new`：new 法律上替代 old。双方须已占槽、同册、出度 ≤1、无环。 |

## 规则与不变式

1. **槽分配**：隐式登记取最小未用槽号（不占用预留池）；显式选槽必须选
   **之前预留**且空闲的槽（`slot_not_reserved`）；不得填别件名下已用槽
   （`slot_occupied`，含墓碑槽）。
2. **登记 / 补写 / 撤行**：已占槽件可登记正文、以同件新附件补写（槽不变）、
   撤行（槽变墓碑，件号仍属原槽，槽永不复用，件号不得再登记）。
3. **替代**：双方都须已占槽（预留无件的槽没有件号，永远进不了替代链，
   报 `part_not_found`）；替代方保有自己的稳定槽；被替代方从当前可见目录
   退出；同批可撤旧边设新边，按整批终态解释；环（含自环）整批不生效
   （`replacement_cycle`，已应用操作全部回滚）。
4. **发布资格**：终点件当前**未撤行且已审核**才公开正文；上游旧件不得
   绕开新终点的不可见状态——终点不可见时只回终点身份与原因，不回链上
   任何旧件正文。
5. **局部维护**：替代终点永远在读时经边索引**惰性解析**（带备忘），改一条
   边只失效该册备忘，不重写任何历史槽或版次表；更正经由批次提交，不存在
   "整册重生成"的提交路径。搜索导出只在显式发起时遍历当前可见终点。

## 两种读视图

### 1. stable-slot 查询 `query_slot(version, booklet, slot)`

读者拿某版纸质目录的页内索引（版次、册、槽号/印刷槽标）查询：

```json
{
  "ok": true,
  "version": "V1", "booklet": "B1", "slot": 0, "slot_label": "0000",
  "printed": {"part": "A", "title": "旧件甲"},
  "current": {
    "replaced": true,
    "endpoint": {"part": "C", "slot": 2, "slot_label": "0002", "title": "现行件丙"},
    "status": "public",
    "value": {"body": "丙的正文", "attachments": ["补写附件一"]}
  }
}
```

- `printed`：该版当时在此槽正式列出的件号与收录身份（来自不可变纸版表）。
- `current.endpoint`：**当前**替代链终点身份（只给终点，不给整条链）。
- `status: "public"` 时带 `value`（终点当前正文与全部补写附件）；
  `status: "not_public"` 时带 `reason: "withdrawn" | "not_approved"`，无 `value`。

校验错误（`{"ok": false, "error": {...}}`）：`unknown_version`（未知版次）、
`slot_not_printed`（预留但未印 / 从未分配 / 该版时已被替代或撤下）、
`slot_out_of_range`、`unknown_booklet`。

### 2. 搜索导出 `search_export([booklet])`

当前可见目录：每个**已通过审核且未撤行的可见终点**只出现一次，标记其拥有
的稳定槽；被替代件与墓碑不出现。按 (册, 槽) 排序：

```json
{"ok": true, "type": "search_export", "count": 1,
 "entries": [{"booklet": "B1", "slot": 2, "slot_label": "0002", "part": "C",
              "title": "现行件丙",
              "value": {"body": "丙的正文", "attachments": []}}]}
```

## 数据格式

### 批次文件 / 场景中的 batch 步

```json
{"batch_id": "SETUP", "ops": [
  {"op": "add_booklet", "booklet": "B1", "slot_width": 4},
  {"op": "reserve",  "booklet": "B1", "count": 10},
  {"op": "reserve",  "booklet": "B1", "slots": [20, 21]},
  {"op": "register", "booklet": "B1", "part": "A", "slot": 20,
   "title": "…", "body": "…", "attachments": ["…"], "approved": true},
  {"op": "amend",    "booklet": "B1", "part": "A", "attachment": "补写附件"},
  {"op": "withdraw", "booklet": "B1", "part": "A"},
  {"op": "set_review", "booklet": "B1", "part": "A", "approved": false},
  {"op": "replace",  "booklet": "B1", "old": "A", "new": "B"},
  {"op": "unreplace","booklet": "B1", "old": "A"},
  {"op": "publish",  "version": "V1"}
]}
```

- `register`：`slot` 省略则隐式分配最小未用槽；`approved` 缺省 `false`
  （未审核不公开正文）；`title/body/attachments` 可空。
- `replace`：覆盖旧目标（同批撤旧设新的简写）；`unreplace`：删边。
- `publish`：批内最后执行（先过整批终态校验），版次号省略则自动 `V1,V2,…`。
- 批次结果：`{"ok": true, "batch_id": …, "applied": N, "published": [...]}`；
  失败：`{"ok": false, "error": {"code": …, "batch_id": …, "op_index": …}}`，
  整批回滚。`op_index` 缺失表示失败发生在批末图校验（如成环）。

### 场景文件（可复跑验收）

```json
{"format": "slot-catalog-scenario/1", "name": "…", "steps": [
  {"batch":  {"batch_id": "…", "ops": [ … ]}},
  {"publish": "V1"},
  {"query_slot": {"version": "V1", "booklet": "B1", "slot": 0}},
  {"search_export": {"booklet": "B1"}}
]}
```

每步产出一个结果对象；失败的批次/查询以 `{"ok": false, "error": …}` 记录，
场景继续执行。输出文档 `format: "slot-catalog-results/1"`。

### 状态文件（持久化，`save`/`load` 往返逐字节一致）

```json
{"format": "slot-catalog-state/1",
 "booklets": {"B1": {"slot_width": 4,
   "slots": {"0": {"state": "occupied", "part": "A"}},
   "parts": {"A": {"slot": 0, "title": "…", "body": "…",
                   "attachments": [], "withdrawn": false, "approved": true}},
   "edges": {"A": "B"}}},
 "versions": {"V1": {"seq": 1,
   "records": {"B1": {"0": {"part": "A", "title": "…"}}}}},
 "batches": [{"seq": 1, "batch_id": "SETUP", "ops": 5, "ok": true,
              "published": ["V1"], "error": null}],
 "counters": {"batch_seq": 1, "version_seq": 1}}
```

### 错误码

`invalid_op` `unknown_booklet` `booklet_exists` `unknown_version`
`duplicate_version` `slot_not_printed` `slot_out_of_range` `slot_occupied`
`slot_not_reserved` `part_exists` `part_not_found` `already_withdrawn`
`edge_not_found` `replacement_cycle` `capacity_exceeded`（批次失败时包在
`batch_failed` 内，`error.code` 取因由码）。

离线合稿另有：`invalid_branch_file` `base_mismatch` `unsupported_op`
`branch_invalid` `part_conflict` `slot_conflict` `edge_conflict`
`output_exists`（见下节）。

同步通道另有：`invalid_package`（损坏/未收齐/校验失败）、
`checkpoint_mismatch`（包与当前检查点不匹配，含首包非全量）、
`wrong_source`（其他源的包）、`unknown_checkpoint`（源端不认识的起点）、
`entry_not_found`（镜像索引无活入口）。

## 离线合稿 `merge`

纸册发出后，几组编目员可拿同一份目录底稿各自离线登记更正；`merge` 把
按优先级排列的分支文件（命令行先列者优先级最高）合成一份可继续编目
的状态——`apply`、`query`、`export` 均可直接接着用。

```sh
python3 -m slotcatalog merge base.json alpha.json beta.json --out merged.json
```

- 合稿状态写入 `--out`（已存在的目标一律拒绝覆盖，失败时不写出半成品）；
  stdout 打印合稿报告（`slot-catalog-merge-report/1`），列出发生搬槽的
  分支、册、件号和新旧槽号。
- 保留底稿的已发版次与原有批次记录；各分支的局部批次不当作已进主目录，
  合稿不发布新纸版。相同输入必得相同结果。

### 分支文件（`slot-catalog-branch/1`）

```json
{"format": "slot-catalog-branch/1",
 "branch": "alpha",
 "base_sha256": "<底稿文件原始字节的 SHA-256>",
 "batches": [{"batch_id": "JIA-1", "ops": [ … ]}]}
```

每个分支先在底稿副本上按原批次边界独立重放：底稿哈希不符
（`base_mismatch`）、分支无效（`branch_invalid`，给出分支、批次、
`op_index` 与因由）或含不合之操作（`unsupported_op`：发版、新建册、
按数量预留）都拒绝合稿——分支不能靠别的分支补齐它引用的件。

### 合稿规则

- 底稿已有件一律留在原位，墓碑仍占槽；底稿预留只能由显式选槽的登记占用。
- 不同分支登记同册同件号即冲突（`part_conflict`，即使内容相同）。
- 显式预留与显式登记落点固定：同槽空预留合并；一个显式登记认领则由它
  占用；不同件认领同一槽拒绝（`slot_conflict`）。
- 未指定槽号的登记只把分支内落点当建议：能保留的原位保留（不让位给待
  搬件），撞到固定槽须搬；几个建议撞同一自由槽时优先级最高的分支留在
  原位。待搬件按分支优先级、分支内登记先后取本册最小剩余自由槽，不吃
  空预留；容量不足整份失败（`capacity_exceeded`，指明用尽的册）。
- 分支内已撤行的新件仍按自己的登记规则占用墓碑槽。册之间互不影响。
- 底稿件的补写按分支优先级、分支内顺序接在原附件之后，同名附件不去重。
- 审核、撤行、每件出边按分支终态相对底稿的净变化合入：回到原值不算改
  动，多个相同改动只算一次；同一出边有不同改动（含删边与改指不同目
  标）拒绝（`edge_conflict`）。
- 合并后的替代图须满足目录原有规则；分支各自无环、合起来成环也拒绝
  （`replacement_cycle`，指明成环的册和链）。

### 可复跑示例

`acceptance/merge_example/` 存有完整示例（底稿 + 两分支 + 期望报告与
期望状态）：

```sh
python3 acceptance/merge_example/run_merge_example.py   # 全流程校验
python3 acceptance/merge_example/gen_example.py         # 重新生成示例文件
```

手工复跑：

```sh
cd acceptance/merge_example
python3 -m slotcatalog merge base.json branch-alpha.json branch-beta.json \
    --out /tmp/merged.json          # 报告打印在 stdout
python3 -m slotcatalog query /tmp/merged.json V1 B1 0001
python3 -m slotcatalog export /tmp/merged.json --booklet B1
```

## 离线同步通道（源端 `sync-*` / 镜像 `mirror-*`）

公示站的搜索镜像不保存编目底稿，也不每次重收全部公开正文：它在文件里收
**包**。镜像装好首包后，哪怕漏收几次更正，也能凭自己检查点申请到的一份
增量包直接追到源端当前版本。单写入源；不改动离线合稿；不接入网络。

### 检查点

```json
{"source": "<开通时状态正文的 SHA-256>", "seq": 5}
```

- `source` 区分同步源：开通时由初始状态内容哈希确定（确定性，非随机、非
  时间戳），重开同一状态得到同一源标识。
- `seq` 是该源上的批次位置：开通后**成功**批次的计数。每个成功批次对应新
  检查点（即使公开数据未变）；失败的批次不推进检查点。用户填写的
  `batch_id` 从不充当位置。
- 源端状态（目录 + 同步日志）写在同一份文件里，重开后已接受的更正与
  检查点天然一致；CLI 每条命令都是一次"重启"，示例全程在验证这一点。

### 镜像数据集（两表）

| 表 | 键 | 值 |
|---|---|---|
| 文档 docs | (册, 件号) | 搜索导出条目（公开终点的正文与全部补写附件） |
| 入口 entries | (纸版, 册, 逻辑槽号) | 当前指向的公开终点键 `{"booklet", "part"}` |

- 正式印过的槽才可能有入口；终点不公开就没有入口，入口绝不退回链上旧件。
- 一个终点的正文只存在文档表一次，入口再多也不复制。
- 内容以源检查点上的 `export` 与已印槽的 `query` 为准；镜像的安装与读取
  只依赖包文件和自己的镜像文件，不依赖源底稿。

### 包（`slot-catalog-sync-package/1`）

- **首包**（`kind: "full"`）：某个检查点上的完整两表。
- **增量包**（`kind: "delta"`）：从本源既有检查点追更时生成，只放相对于
  该检查点的**净**增删改行，另带起止检查点。删除只带键，新增和替换只带
  目标值；被中间批次公开、到目标检查点已不可公开的正文和附件不夹带；
  改了又改回的行不发。只为已公开终点补写附件时只更新该文档，不重发指向
  它的入口；旧入口改指不公开终点必须删掉入口，但原终点自己仍公开就保留
  它的文档。净变化为空的包也合法，镜像照装并跟进检查点。
- 每个包带内容摘要（SHA-256，覆盖除摘要外的全部字段）；损坏或未收齐的
  包在校验阶段即被拒绝。

### 镜像安装与读取

- 两表与检查点**一起**换到目标状态：先整体校验（含"入口不得指向缺失
  文档"），再原子替换；不会出现混装。
- 重试刚装好的同一份包是空操作（`installed: false`），不重复修改。
- 不匹配当前检查点的包（`checkpoint_mismatch`）、其他源的包
  （`wrong_source`）、损坏或未收齐的包（`invalid_package`）一律拒绝，
  已安装数据原样保留；`--out` 目标已存在时拒绝覆盖（`output_exists`）。
- 镜像重启（重新加载自己的镜像文件）后仍能从自己的检查点继续追更。
- `mirror-query` 用历版纸册的索引（版次、册、槽号/印刷槽标）查现行件：
  入口活着就返回终点键与当前公开文档；入口已失效（或从未印过）返回
  `entry_not_found`，绝不指向缓存里的旧正文。`mirror-export` 给出与源端
  `export` 一致的搜索视图。

### 源端的增量维护（性能形状）

- 允许开通时建索引（一次性全量推导两表 + 反向索引）；新发纸版时处理该版
  全部新增索引。
- 其余追更计算全是增量：每个成功批次只重算它点名的件（文档行）和经由
  反向边索引、已印槽索引能碰到的链（入口行），不逐一重算未受区间更正
  影响的历版入口，不扫描无关件；这些工作发生在 `apply_batch` 之外的
  同步层，目录的批次写路径不背任何"重写历版入口"的活。`SyncSource.stats`
  记录每批重算行数，有测试断言。
- 现有目录的加载、保存与校验不纳入该限制（未改动）。

### 状态与文件格式

- 源端：`slot-catalog-sync-source/1` = 目录状态（`slot-catalog-state/1`）
  + 同步段（源标识、检查点序号、逐检查点变更日志）。
- 镜像：`slot-catalog-mirror/1` = 检查点 + 两表。
- `query`/`export` 也可直接读源端文件（取其内层目录），方便源端操作员
  对照镜像视图。

### 可复跑示例

`acceptance/sync_example/` 存有完整示例（批次文件 + CLI 验证器）：

```sh
python3 acceptance/sync_example/run_sync_example.py   # 全流程校验
```

示例故事线：登记 A..D 并发 V1 → 开通同步源 → 两份镜像装首包 → 链式替代
A→B→C → 补写 C → 一个无公开变化的批次（空增量包照样推进检查点）→ 一个
成环批次被拒（检查点不动）→ 链改指未审核的 D 并登记新件 → 撤行一件并发
V2。随后进度不同的两份镜像（一份跟到检查点 3，一份停在 0）各凭自己的
检查点拿到增量包，追到同一份逐字节一致的结果。手工复跑：

```sh
cd acceptance/sync_example
python3 -m slotcatalog init --out /tmp/s0.json
python3 -m slotcatalog apply /tmp/s0.json batches/00-setup.json --out /tmp/s1.json
python3 -m slotcatalog sync-open /tmp/s1.json --out /tmp/src.json
python3 -m slotcatalog sync-package /tmp/src.json --out /tmp/pkg-full.json
python3 -m slotcatalog mirror-install /tmp/pkg-full.json --out /tmp/mirror.json
python3 -m slotcatalog sync-apply /tmp/src.json batches/01-chain.json --out /tmp/src2.json
python3 -m slotcatalog sync-package /tmp/src2.json --from-seq 0 --out /tmp/pkg-delta.json
python3 -m slotcatalog mirror-install /tmp/pkg-delta.json --mirror /tmp/mirror.json --out /tmp/mirror2.json
python3 -m slotcatalog mirror-query /tmp/mirror2.json V1 B1 0000
```

## 验收场景与需求对照

| 场景 / 测试 | 覆盖需求 |
|---|---|
| `acceptance/01_basic_lifecycle` | 预留与显式选槽、登记、补写不变槽、撤行墓碑、未知版次/预留未印/撤下校验、墓碑槽不可占 |
| `acceptance/02_chain_switch` | A→B→C 链、切到不可见 D 后 A/B 不再透露 C、C 作为合格终点独立存在、导出唯一性 |
| `acceptance/03_cycle_rejected` | 环与自环整批不生效（回滚后可再登记同件号）、预留空槽进不了替代链 |
| `acceptance/04_long_suffix`（`gen_long_suffix.py` 生成） | 2000 件长链 + 1000 汇入口共有长后缀；改一条尾边全体重指、历史版次表不变；中间断链只影响上游 |
| `acceptance/merge_example`（`run_merge_example.py`） | 合稿 CLI 全流程：报告与状态 golden、合稿后 query/export/apply 可用、确定性、哈希不符与目标已存在的失败行为 |
| `acceptance/sync_example`（`run_sync_example.py`） | 同步通道 CLI 全流程：首包/增量包、空增量推进检查点、失败批次不推进、漏批镜像追到同一结果、失效入口删除且旧终点文档保留、补写不重发入口、中途公开内容不夹带、重试空操作、错点/外源/损坏包拒绝、重开一致 |
| `tests/test_catalog.py`（31 例） | 上述全部 + 跨册拒绝、版本表不可变、状态持久化往返、场景确定性、解析线性度（`follow_steps` 仪表） |
| `tests/test_merge.py`（36 例） | 合稿：槽位固定/建议/搬让规则、空预留合并、容量失败、同件/同槽冲突、补写顺序与去重、净变化合入、出边冲突、合并成环、分支校验、CLI 行为 |
| `tests/test_sync.py`（50 例） | 同步：检查点推进与失败回滚、重开一致、净增量（改回不发/中途公开不夹带/补写不动入口/失效删入口留文档）、两镜像汇合、包装载拒绝与原子性、镜像读视图、增量维护行数仪表、CLI 全流程 |

## 设计要点（为何满足"局部维护"）

- 替代边存为每册一张 `old → new` 映射（索引）；终点查询时惰性解析并按册
  备忘，边变更仅丢弃该册备忘。改/删一条边的存储代价 O(1)，绝不重写"以被
  改边为共同后缀"的历史槽或版次表（有测试断言版次表 JSON 前后逐字节一致）。
- 纸版表只在 `publish` 时追加，永不修改；正文不复制进版次表。
- 所有变更经 `apply_batch` 原子提交：逐操作应用 + 批末图校验 + 失败回滚；
  没有"整册重生成"式更正路径。
- 搜索导出是显式命令，只有发起时才遍历当前可见终点。

## 目录结构

```
slotcatalog/        # 包：catalog.py(核心) merge.py(离线合稿) sync.py(同步通道) scenario.py(场景) __main__.py(CLI) errors.py
tests/              # 单元测试（python3 -m unittest discover -s tests -t . -v）
acceptance/         # 场景 + golden + 运行器 + 长后缀场景生成器
acceptance/merge_example/  # 离线合稿示例：底稿/分支/golden + 生成器 + CLI 验证器
acceptance/sync_example/   # 同步通道示例：批次文件 + CLI 验证器
run_all.sh          # 一键跑全部测试与验收
```
