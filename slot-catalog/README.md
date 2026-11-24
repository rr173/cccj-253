# stable-slot-catalog 槽位目录

公示系统的**可局部维护槽位目录**与**两种读视图**。不生成纸质文档；纸版只留
"各版曾正式列出的槽与身份"的不可变表。纯 Python 3 标准库，无第三方依赖，
无权限系统、无远程接入。

## 快速开始

```sh
cd slot-catalog
python3 -m unittest discover -s tests -t . -v   # 单元测试（67 个）
python3 acceptance/run_acceptance.py            # 验收：跑场景并比对 golden
python3 acceptance/merge_example/run_merge_example.py  # 离线合稿 CLI 验证
./run_all.sh                                    # 上面三条一起
```

CLI（从仓库根目录运行，或设 `PYTHONPATH` 指向仓库根目录）：

```sh
python3 -m slotcatalog run    SCENARIO.json                  # 执行场景文件
python3 -m slotcatalog apply  STATE.json BATCH.json --out NEW.json  # 局部提交一个批次
python3 -m slotcatalog merge  BASE.json BRANCH1.json BRANCH2.json [...] --out MERGED.json  # 离线合稿
python3 -m slotcatalog query  STATE.json V1 B1 0007          # stable-slot 查询
python3 -m slotcatalog export STATE.json --booklet B1        # 搜索导出
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

## 验收场景与需求对照

| 场景 / 测试 | 覆盖需求 |
|---|---|
| `acceptance/01_basic_lifecycle` | 预留与显式选槽、登记、补写不变槽、撤行墓碑、未知版次/预留未印/撤下校验、墓碑槽不可占 |
| `acceptance/02_chain_switch` | A→B→C 链、切到不可见 D 后 A/B 不再透露 C、C 作为合格终点独立存在、导出唯一性 |
| `acceptance/03_cycle_rejected` | 环与自环整批不生效（回滚后可再登记同件号）、预留空槽进不了替代链 |
| `acceptance/04_long_suffix`（`gen_long_suffix.py` 生成） | 2000 件长链 + 1000 汇入口共有长后缀；改一条尾边全体重指、历史版次表不变；中间断链只影响上游 |
| `acceptance/merge_example`（`run_merge_example.py`） | 合稿 CLI 全流程：报告与状态 golden、合稿后 query/export/apply 可用、确定性、哈希不符与目标已存在的失败行为 |
| `tests/test_catalog.py`（31 例） | 上述全部 + 跨册拒绝、版本表不可变、状态持久化往返、场景确定性、解析线性度（`follow_steps` 仪表） |
| `tests/test_merge.py`（36 例） | 合稿：槽位固定/建议/搬让规则、空预留合并、容量失败、同件/同槽冲突、补写顺序与去重、净变化合入、出边冲突、合并成环、分支校验、CLI 行为 |

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
slotcatalog/        # 包：catalog.py(核心) merge.py(离线合稿) scenario.py(场景) __main__.py(CLI) errors.py
tests/              # 单元测试（python3 -m unittest discover -s tests -t . -v）
acceptance/         # 场景 + golden + 运行器 + 长后缀场景生成器
acceptance/merge_example/  # 离线合稿示例：底稿/分支/golden + 生成器 + CLI 验证器
run_all.sh          # 一键跑全部测试与验收
```
