# Copybook Decoder

面向遗留固定宽度数据交换的解码器：用带版本号的 Copybook 布局定义，把 EBCDIC
定长数据文件解码为结构化记录。每条记录都携带源文件、物理行号、字节起止偏移
和实际采用的布局版本；格式错误的行被单独隔离，不影响同文件后续行。

仅依赖 Python 标准库（≥ 3.9）。

```
copybook/            解码器包
  parser.py          Copybook 子集解析（布局树 + 字节偏移计算）
  decoder.py         记录解码（zoned / COMP-3 / REDEFINES / OCCURS）
  manifest.py        接收清单：按 (发送方, 业务日期) 选择布局版本
  registry.py        布局注册表（按 名称+版本 缓存解析结果）
  pipeline.py        文件级流水线：逐行解码、隔离、导出
  audit/             批次交接校验：FHEAD/BHEAD/DETAIL/BTAIL/FTAIL 跨记录核对
    config.py        字段路径映射 + 有限类别表 + 精确千分单位数量解析
    evidence.py      正常记录与隔离索引的流式归并（物理行号严格覆盖）
    engine.py        磁盘暂存的状态机（SQLite），逐行校验、逐行交付
  impact/            布局变更影响报告：比较两份清单及其 Copybook
    flatten.py       布局展开：完整分组路径、字节区间、结构问题检测
    diff.py          字段级对照：同义/位移/解释变化/增删/视图簇
    engine.py        容错清单加载、闭区间日期分段、报告装配与输出
  mask/              脱敏样本生成：解码 JSONL + 字段策略 -> 脱敏 JSONL + 报告
    policy.py        策略解析与校验（完整字段路径 -> 类别 + 动作）
    mapping.py       密钥加载与确定性脱敏原语（HMAC 计数器模式）
    engine.py        全量扫描验证 -> 映射构建 -> 分块交付（SQLite 暂存）
  settle/            未结项核销加工：账单/收款/撤销事件 -> 核销履历与账龄
    config.py        九条业务字段路由（fields 字符串数组路径）
    values.py        严格两位小数金额（整数分，支持超 64 位）、日期与带偏移时间戳
    events.py        多文件 JSONL 读取、路由校验、UTC+event_id 字节序排序
    ledger.py        单账号币种账本：开立/撤销/自动核销/释放再分配/守恒与账龄
    engine.py        分组重放、截止时点装配、结果目录交付（拒绝覆盖）
    trace.py         仅凭结果目录追查单个账单/收款的事件与全部核销履历
  gitkit/            离线 Git 交接资料接入（bundle v2 / PACK / delta / 提交图）
    bundle.py        bundle 头尾校验、PACK 框架与每个 zlib 流边界
    pack.py          对象 ID 重算、OFS/REF_DELTA 链、暂存与资源上限
    objects.py       commit/tree 严格解析（原始头、续行、原始路径字节）
    graph.py         全 ref 可达提交图与拓扑序、tree 遍历
    diff.py          按完整路径对每个 parent 的变化
    handoff.py       事实目录交付、单 blob 导出与导出清单
  relation/          多表交接的关系准入加工：最大相互支持保留集 + 拒收证明
    config.py        引用契约（表/字段/候选键/引用）与带类型键元组编码
    loading.py       流式 JSONL 载入、meta/身份/重复成员校验、第 0 层局部诊断
    engine.py        半朴素批量固定点消除（SQLite 落盘、有界批次、证明与见证）
    query.py         仅凭交付目录解释一行、读证明节点、逐表读取获准行
  rewrite/           原件定点回写：更正单 -> 只改指定字段字节的新原件 + 凭据
    strictjson.py    拒绝重复成员/NaN/Infinity/float 的严格 JSON 加载
    order.py         更正单模式、坐标与记录长度校验、布局目录编译
    pathres.py       路径解析（OCCURS 索引、REDEFINES 视图、禁写组/FILLER）
    encoder.py       按 PIC 容量的规范字节编码（60+ 位精确、cp037/cp500）
    engine.py        三遍流式：摘要核对 -> 字节要求/冲突 -> 分块回写
  __main__.py        CLI（decode / audit / impact / mask / settle / trace /
                     admit / admit-explain / admit-table /
                     git-ingest / git-export / git-query / rewrite）
examples/            示例 Copybook、清单、数据生成器、交接场景、影响报告场景、
                     脱敏场景、Git 交接演示与压测
tests/               unittest 测试套件与字节构造夹具
```

## 快速开始

```bash
cd copybook-decoder
python3 examples/make_data.py          # 生成示例 EBCDIC 数据
python3 -m copybook decode examples/data/BANKA-20240315.dat \
    --manifest examples/manifest.json --sender BANKA --date 2024-03-15 \
    --out records.jsonl --quarantine-dir quarantine/
python3 -m unittest discover -s tests -t .   # 运行测试
```

库用法：

```python
from copybook import Manifest, decode_file

manifest = Manifest.load("examples/manifest.json")
result = decode_file("examples/data/BANKA-20240315.dat",
                     sender="BANKA", business_date="2024-03-15",
                     manifest=manifest)
for rec in result.records:          # DecodedRecord
    print(rec.line_number, rec.byte_start, rec.byte_end,
          rec.layout_version, rec.fields)
for q in result.quarantined:        # QuarantinedLine
    print(q.line_number, q.reason, q.error_offset)
result.export_quarantine("quarantine/")
result.write_jsonl("records.jsonl")
```

## 多表关系准入（admit）

`relation` 子包回答一次离线交接的质量问题：给定几张已解码表（标准
`meta`/`fields` JSONL，一表可有多份文件）和引用契约，哪些记录彼此仍有
足够引用支持可以入仓，其余记录为何最终失去资格。结果是“满足全部引用的
最大保留集”：闭合引用环整体保留、不挑锚点、不为消环任意删行；先剔除局部
错误行（第 0 层），再按同步轮次反复剔除失去父候选的行，并为每条连锁拒收
保存可独立检查、层数严格递减、节点共享（不指数展开）的证明。

```bash
python3 examples/relation/run_scenarios.py     # 真实接到 decode 产物的完整场景
python3 -m copybook admit --contract contract.json \
    --out-dir delivery/ --temp-dir staging/
python3 -m copybook admit-explain delivery/ transactions T003   # 见证或拒收证明
python3 -m copybook admit-table delivery/ transactions          # 逐表读获准行
```

退出码：`0` 全部准入；`1` 完成质量检查但有拒收（交付目录与 `COMPLETE.json`
仍生成）；`2` 身份/契约/坏 JSON 等结构错误，无法处理且不留下可被当成完成的
交付。所有记录、业务键、引用与证明边都落在交付目录内的 SQLite，重开进程无需
原输入文件；见 `examples/relation/README.md`。

## 支持的 Copybook 子集

本解码器**不**实现全部 COBOL 语法，仅支持下列子集；遇到子集以外的
语法会在解析期抛出 `CopybookSyntaxError`，而不是静默误解布局。

```
<stmt> := <level> <name> [REDEFINES <name>] [PIC <picture>]
          [OCCURS <n> TIMES] [COMP-3 | DISPLAY] [SIGN TRAILING] "."
```

| 特性 | 支持情况 |
|---|---|
| 层号 01–49、嵌套分组 | ✅ 组长度 = 子项之和 |
| `PIC X(n)` / `A(n)` | ✅ 字母数字，按字节定长 |
| `PIC 9(n)` / `S9(n)` | ✅ 区十进制（zoned），尾随符号穿孔 |
| `PIC ...V9(m)` | ✅ 隐式小数点，解码为 `Decimal` |
| `COMP-3` / `PACKED-DECIMAL` | ✅ 压缩十进制，符号在末位半字节 |
| `REDEFINES` | ✅ 重定义已声明的同级字段（见下文） |
| `OCCURS n TIMES`（定长） | ✅ 组或基本项，输出为列表 |
| `FILLER` | ✅ 占位（自动唯一命名，不进输出） |
| 注释：整行 `*`、行内 `*>` | ✅ |
| 语句跨行 | ✅ 以 `.` 为语句结束符 |
| 66/77/88 层、`OCCURS DEPENDING ON`、`COMP`/`BINARY`、`VALUE`、`JUST`、`SIGN LEADING/SEPARATE`、编辑型 PIC（`Z`,`,` 等） | ❌ 解析期报错 |

语义约定：

- **字节而非字符**：所有长度与偏移都是字节。记录先按字节偏移切片，再做
  EBCDIC 解码；COMP-3 每字节存两位数字（`S9(7)V99` 占 5 字节而非 9）。
- **编码**：DISPLAY 字段按布局声明的 EBCDIC codec 解码（默认 `cp037`，
  可在清单条目中用 `"codec"` 覆盖，如 `cp500`）。`X` 字段去除尾部空格。
- **符号**：zoned 数字的符号在**最后**一个字节的区位的穿孔上——EBCDIC
  解码后 `{A–I` 为正、`}J–R` 为负、纯数字为正；无符号 PIC 容忍穿孔并忽略
  符号。COMP-3 符号半字节 `C/A/E/F` 为正、`B/D` 为负。
- **隐式小数**：`V` 不占字节；带 `V` 的字段解码为 `decimal.Decimal`
  （JSON 导出时序列化为字符串，避免浮点误差）。
- **REDEFINES**：重定义字段是其目标字节区间的*另一种解释*。默认解码全部
  视图；视图解释失败（如对文本字节套数值视图）不会隔离整行——该视图值
  为 `None`，错误记入记录的 `field_errors`。可用 `RedefinesPolicy` 选择
  视图：`include=(...)` 白名单，或 `discriminator=字段, cases={值: 视图},
  default=视图` 按判别字段逐记录选择。

## 版本选择（接收清单）

同一目录可混放多个布局版本的数据文件。版本**只**由清单决定，绝不从文件
内容或文件名猜测：

```json
{"layouts": [
  {"sender": "BANKA", "copybook": "ACCT", "version": "1",
   "effective_from": "2024-01-01", "effective_to": "2024-08-31",
   "file": "copybooks/ACCT-1.cpy", "codec": "cp037"},
  {"sender": "BANKA", "copybook": "ACCT", "version": "2",
   "effective_from": "2024-09-01", "file": "copybooks/ACCT-2.cpy"}
]}
```

- 解析键为 `(sender, business_date)`；`effective_from/to` 均为闭区间，
  `effective_to` 可缺省（开放区间）。
- 清单加载时即校验：同一 `(sender, copybook)` 的生效区间重叠 →
  `ManifestError`；缺键、日期非法、未知 codec 同样报错。
- 解析时：无匹配条目、缺少 sender/日期、或多个候选无法区分（同一发送方
  多个 copybook）→ `ManifestError`，**不猜测**。多 copybook 场景可显式传
  `copybook=` 消歧。

## 行级隔离与字节偏移

- 每行期望长度 = 布局记录长度。过短 → `truncated`，过长 → `overlong`，
  字段解码失败 → `decode`，均进入隔离列表；解码从下一行继续，物理行号
  （1 起）保持稳定。
- `DecodedRecord` / `QuarantinedLine` 都带 `byte_start`/`byte_end`
  （文件内绝对偏移，半开区间，不含换行符）；字段级错误还带
  `error_offset`（出错字段的绝对偏移）。
- `result.locate(offset)` 按原始字节偏移定位到记录或隔离行。
- `result.export_quarantine(dir)` 导出 `<文件名>.quarantine.bin`
  （隔离行原始字节）与 `<文件名>.quarantine.jsonl`（索引：行号、偏移、
  原因、原始字节 hex）。
- 行以 `\n` 分隔；容忍 CRLF。文件末尾无换行的短行按截断处理。

## 批次交接校验（audit）

解码器只保证“每一行能被解释”，不保证发送方没有少传明细、批尾控制数没有
被全文件累计值顶替。`copybook audit` 在**已经解码的证据**之上做跨多行的
控制记录校验：它不解码原始字段、不扩展 Copybook 语法，只读 `write_jsonl`
产出的正常记录和 `export_quarantine` 产出的隔离索引，按字符串数组路径访问
`fields`，用 SQLite 做磁盘暂存与“批号是否出现过”的检查。

### 输入证据与完整性

两份输入各自按源文件物理行号递增，调用者另外提供 `--expected-source` 与
`--expected-lines`。校验器先以两指针流式归并它们，证明物理行
`1..expected_lines` 被**恰好覆盖一次**：隔离行不在正常结果里，但绝不能把
后面行的编号顺延。出现下列任一情况，整次判定为**无法校验（exit 2）**，
不写交付目录：

- 行号重复、缺口、越界（不在 `1..expected_lines`）；
- 同一物理行同时出现在两份输入里；
- `source_file` 与 `expected_source` 不一致；
- 字节范围逆序（后一行起点早于前一行区间结束）、负偏移；
- 正常记录携带 `raw_hex`，或隔离索引携带 `fields`——隔离行不是可再次解析
  的业务记录，也不读取隔离 `.bin`；
- JSON 损坏、`expected_lines` 非法。

### 交接协议与校验规则

固定五种记录类型：`FHEAD`、`BHEAD`、`DETAIL`、`BTAIL`、`FTAIL`。

- 文件必须先有且仅有一个 FHEAD，随后是若干完整批次，最后由唯一 FTAIL
  结束；FTAIL 之后不允许任何行。FHEAD 与 FTAIL 携带相同 `file_id`。
- BHEAD 开启一批并携带 `batch_id`；DETAIL、BTAIL 必须指向当前 `batch_id`。
  批号在文件内不可重复（由 SQLite 主键核查）；空批次合法。
- DETAIL 的 `seq` 在**每批内**从 1 连续递增；`category` 必须落在配置预列的
  有限类别内；`quantity` 是带符号十进制字符串、最多三位小数，按精确的
  **千分单位整数（milli-units）**计算，全程无 float、不丢尾部小数。
- BTAIL 给出明细总行数与每个类别的数量合计；未出现类别按零核对。数量合计
  按批内明细实算，不能用全文件累计值蒙混。
- **批内可局限的错误**（seq 错误、batch_id 不合、类别/数量非法、批内出现
  隔离行）只拒收当前批次；仍读到它的 BTAIL 并继续后续批次。批内隔离行算
  一条“已收到的明细位置”，参与 BTAIL 行数核对；因其业务值未知，类别数量
  必须标记为**不可判定（undecidable）**，既不当零也不假装相符。正常
  DETAIL 即使业务字段非法也计入收到行数。
- **无法局限到单批的错误**使整个文件不可入仓，且不会“搜索下一个头再声称
  恢复”：批未结束又遇 BHEAD、批外 DETAIL 或隔离行、未知类型、批尾/批头
  缺失、重复批号、file_id 不合、FTAIL 后还有行。
- FTAIL 的 `batch_count` 核对实际见到的 BHEAD 数；`detail_count` 核对所有
  批内 DETAIL 与隔离行（**含最终被拒收的批次**）。不符同样使全文件不可入
  仓——最后一行才暴露的文件级问题会撤销此前批次的准入资格，可入仓明细清空。
- 所有诊断都保留涉及的全部源行号与字节范围 `[byte_start, byte_end)`，
  不只记最后一处。

### 内存与交付约定

- 逐行读取、逐行落 SQLite：内存只依赖当前一行、固定类别表和一个有界的
  写入缓冲（默认每 2000 行提交一次）；明细、隔离位置、诊断、批次行全部
  在磁盘上，超大批次或大量小批次都不会把明细堆进内存。
- 输出目录必须是**不存在的新目录**（`exist_ok=False`），处理完成后可同时
  读到本次结论 `audit_report.json` 与 `accepted_details.jsonl`，绝不覆盖
  已有交付。临时目录由调用者指定，校验器只在其下创建唯一暂存子目录，
  完成或异常后只清理自己创建的部分。
- 输入缺口/损坏（无法校验）时不留下貌似完成的结果；文件级拒收是一次完成
  的校验，会留下完整拒收报告，只是 `accepted_details.jsonl` 为空。
- 可入仓记录保留原 `meta`、`fields` 原样，仅在顶层附加 `file_id` 与
  `batch_id`，不覆盖任何原字段。文件级拒收时可入仓明细必为空。

### 用法

```bash
python3 -m copybook audit records.jsonl quarantine.jsonl \
    --config examples/audit/audit_config.json \
    --expected-source /landing/feed.dat --expected-lines 9 \
    --out-dir delivery/2024-03-15/ --temp-dir /var/tmp/audit-tmp/
```

Python 接口：

```python
from copybook.audit import AuditConfig, run_audit

config = AuditConfig.load("examples/audit/audit_config.json")
summary = run_audit(
    "records.jsonl", "quarantine.jsonl", config=config,
    expected_source="/landing/feed.dat", expected_lines=9,
    output_dir="delivery/2024-03-15/", temp_dir="/var/tmp/audit-tmp/")
# 完整结论在 output_dir/audit_report.json；可入仓明细在
# output_dir/accepted_details.jsonl。summary 只回计数与路径等有界信息。
```

配置文件把五个类型码和业务项映射到 `fields` 的字符串数组路径，
`btail_totals` 必须恰好覆盖全部类别：

```json
{
  "categories": ["AAA", "BBB"],
  "paths": {
    "record_type": ["REC-TYPE"],
    "file_id": ["FILE-ID"],
    "batch_id": ["BATCH-ID"],
    "seq": ["SEQ-NUM"],
    "category": ["CATEGORY"],
    "quantity": ["QUANTITY"],
    "btail_detail_count": ["DETAIL-COUNT"],
    "btail_totals": {"AAA": ["TOTAL-AAA"], "BBB": ["TOTAL-BBB"]},
    "ftail_batch_count": ["BATCH-COUNT"],
    "ftail_detail_count": ["DETAIL-COUNT"]
  }
}
```

### 退出码与报告结构

| 退出码 | 含义 |
|---|---|
| 0 | 结构有效且所有批次通过（`conclusion = pass`） |
| 1 | 完成校验但存在业务拒收（批次或文件级，`business_rejected`） |
| 2 | 输入证据或配置无法校验（`unverifiable`），不产生交付目录 |

`audit_report.json` 含总体结论、文件级控制数（file_id、batch_count、
detail_count 的声明值/实算值/是否相符）、每批状态与控制数、按源顺序流式
写出的可入仓行号数组、全部隔离位置（含字节范围与所属批 ordinal）及逐项
拒收诊断。类别合计在不可判定时给出 `undecidable` 与原因，不提供伪实算值。

### 交接故事与场景

`examples/audit/` 用真实的 EBCDIC 解码链路（先 `decode` 再 `audit`）交付一
组场景，期望值在 `expected_results.py` 中**手工**按协议逐条写出，不来自校验
器本身：

```bash
python3 examples/audit/build_scenarios.py     # 生成各类 .dat
python3 examples/audit/run_scenarios.py       # 解码→audit→对照手工期望
python3 examples/audit/run_performance.py     # 单超大批次 + 大量小批次压测
```

覆盖：首批含一条坏行/第二批完全正确（首批拒收、第二批可入仓、FTAIL 总数
含坏行）；仅篡改 FTAIL 总数后第二批也被撤销；以及空批、负数量精确抵消、
三位小数、跨批 seq 重新从 1 开始、重复批号、孤立隔离行、FTAIL 后追加记录、
行号缺口（无法校验）。

## 布局变更影响报告（impact）

在接收清单里加入一个新 Copybook 版本之前，维护者需要回答两个问题：**哪些
已经接入的发送方和日期会选到它**，以及**同一字段在新旧布局下是否还表示
相同的字节**。`copybook impact` 只读取两份清单（旧 = 生产在用，新 = 候选）
及各自引用的 Copybook 给出答案——它不读取业务数据文件，不执行逐行解码，
也不做批次交接校验。

```bash
python3 -m copybook impact \
    --old-manifest examples/impact/run/scenarios/main/old/manifest.json \
    --new-manifest examples/impact/run/scenarios/main/new/manifest.json \
    --out-dir review/2024-09/
```

Python 接口：

```python
from copybook.impact import run_impact_report

summary = run_impact_report("old/manifest.json", "new/manifest.json",
                            "review/2024-09/")
# summary 只回结论与计数；完整结果在 review/2024-09/ 下：
#   impact_report.txt   可读报告（字段对照、日期区间、问题列表）
#   impact_report.json  机器可读的字段差异与全部结论
```

输出目录必须是**不存在的新目录**；已存在时拒绝覆盖（退出码 2），绝不往
里面写报告。

### 字段对照口径

- 字段用**完整分组路径**识别（如 `ACCT-RECORD/BALANCE`、
  `REC/GRP-A/CODE`），绝不按裸字段名判等：相同名字出现在不同分组时是
  不同字段，按移除 + 新增处理并在备注中说明。
- 每个叶子字段报告绝对字节区间 `[start, end)`（半开）、PIC、符号、
  小数位、usage、codec（仅 DISPLAY 字段参与比较，COMP-3 不经过文本
  codec）和 OCCURS 次数。
- 变更分类：**原位置同义**（字节与解释均不变）、**位置移动**（自身
  PIC/缩放未变，仅因前方字节增减而位移，与自身解释变化分开说明）、
  **同名但解释变化**（PIC 类别/长度、符号、小数位、usage、OCCURS、
  codec、REDEFINES 目标任一变化）、**新增**、**移除**。
- REDEFINES 视图是同一字节区间的**互斥解释**，不额外占用存储：新增视图
  不视为字段增占字节，视图簇（目标 + 全部视图）在新旧两侧单独对照，
  记录长度计算也不会把视图重复计入。
- 总记录长度变化时，逐项列出构成（新增/移除/自身变长的字段及 FILLER
  增减），并校验合计与长度差一致。
- 新增字段只有落在旧布局 FILLER 字节内、或作为 REDEFINES 视图时才算
  兼容；任何移除、位移、解释变化或记录长度变化都判 **incompatible**。

### 日期区间口径

- 生效区间按**闭区间**处理，`effective_to` 缺省为开放区间；报告列出每个
  发送方**实际受影响**的日期区间（新旧清单解析出不同版本的区间）与不受
  影响的区间。
- 下列情况**不会**被挑一个版本继续生成看似完整的报告，而是记入问题列表
  （指出发送方、日期范围、清单来源），并使总体结论为 `unverifiable`：
  同一清单内生效区间**重叠**；新清单出现**空洞**（旧清单覆盖的日期失去
  布局）；**缺少旧版本**（新清单覆盖的日期在旧清单中没有基线）；清单引用
  的 **Copybook 不存在**或解析失败；同一版本号在两侧指向**不同布局**；
  布局内**字段路径重复**（该布局的字段对照整体跳过，不猜）。

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 报告已生成，所有受影响区间均兼容（`compatible`） |
| 1 | 报告已生成，存在破坏兼容的变更（`incompatible`） |
| 2 | 存在问题使比较无法完整（`unverifiable`），或输出目录已存在 |

### 场景演示

`examples/impact/` 用人工构造的两版布局演示全部口径，期望值在
`expected_results.py` 中**手工**逐字段、逐日期区间写出，不来自程序输出：

```bash
python3 examples/impact/run_scenarios.py   # 构造场景 -> 跑 CLI -> 对照手工期望
```

覆盖：尾部字段因前方插入字节而位移、同名字段改小数位（字节数不变）、字段
移除、新增 REDEFINES 视图、另一发送方生效区间完全不受影响、仅填充 FILLER
+ 新视图的兼容演进、同名字段换分组（按移除+新增处理）、清单区间重叠、
区间空洞、缺少旧版本、Copybook 缺失、字段路径重复、版本号复用，以及输出
目录已存在时拒绝覆盖。

## 脱敏样本生成（mask）

下游团队需要用真实的固定宽度账户流水验证报表和联调程序，但客户姓名、
账号和余额不能直接复制到测试环境。`copybook mask` 在**已经解码的
JSONL**（`write_jsonl` 的产出）之上按字段策略生成脱敏样本：它不重新
读取原始 EBCDIC 文件，不做字段布局比较，也不判断批次能否入仓。

```bash
python3 -m copybook mask accounts.jsonl txns.jsonl related.jsonl \
    --policy examples/mask/mask_policy.json \
    --key-env MASK_KEY --out-dir delivery/masked-2024-03/
```

Python 接口：

```python
from copybook.mask import MaskKey, MaskPolicy, run_mask

policy = MaskPolicy.load("examples/mask/mask_policy.json")
key = MaskKey.from_env("MASK_KEY")          # 或 MaskKey.from_file(path)
summary = run_mask(["accounts.jsonl", "txns.jsonl", "related.jsonl"],
                   policy=policy, key=key,
                   output_dir="delivery/masked-2024-03/")
# summary 只回计数与路径等有界信息；完整结果在输出目录下：
#   <输入名>.masked.jsonl   每个输入文件一份脱敏记录
#   mask_report.json        策略摘要、逐文件计数、拒绝原因、边界说明
#   mask_report.txt         可读报告
#   record_report.jsonl     每条输入记录一行：如何处理（不含原值）
```

### 字段策略

策略按**完整字段路径**（字符串数组；遇到 OCCURS 列表时对每个元素生效）
声明字段类别和动作；未列入策略的字段原样保留：

```json
{
  "key_fingerprint": "sha256:aba28c86450ca4e6",
  "fields": [
    {"path": ["ACCT-NO"], "action": "pseudonym", "kind": "account",
     "group": "acct", "on_empty": "reject"},
    {"path": ["CUST-NAME"], "action": "synthesize", "kind": "name",
     "group": "person"},
    {"path": ["BALANCE"], "action": "bucketize", "kind": "amount",
     "boundaries": ["1000.00", "10000.00", "100000.00"]},
    {"path": ["TXN-AMOUNT"], "action": "shift", "kind": "amount",
     "max_pct": "5"},
    {"path": ["OPEN-DATE"], "action": "shift", "kind": "date",
     "format": "%Y-%m-%d", "max_days": 30, "group": "bizdate"},
    {"path": ["HOLDER-SINCE"], "action": "bucketize", "kind": "date",
     "granularity": "month"}
  ]
}
```

四种动作：

- **pseudonym**（账号等需要跨文件关联的字段）：映射成稳定的假值。
  同一 `group` 内同一原值在任意输入文件得到同一结果，因此同一主体
  在账号、明细和关联表中的键仍可连接；不同原值**不能合并**——交付前
  对全量不同值做冲突检查，任何碰撞都使整次运行失败，绝不静默合并。
  输出保留原字段的类型和格式约束：数字位仍是数字位、大小写保持、
  分隔符原样、整数不会得到前导零。同一关联组内字段的 JSON 类型
  （字符串/整数）必须一致，否则无法保证稳定关联，运行拒绝。
- **synthesize**（姓名等自由文本）：替换为合成值，保证不含原文片段
  （原文的每个长度 ≥ 2 的词元及整体都不出现在合成值中，否则重掷）。
- **shift**（余额/日期）：确定性偏移。金额按 `max_pct`（百分比）或
  `max_abs`（绝对值）界内做 HMAC 派生抖动，小数位与原值一致，全部
  用精确有理数运算（与 decimal 上下文无关）；日期按 `max_days` 界内
  逐值偏移，同一原日期在同一 `group` 的各文件中偏移一致，偏移后仍
  可连接同一主体。
- **bucketize**（区间泛化）：金额落入声明边界得到 `<b0`、`lo-hi`
  （半开区间）、`>=bn` 标签；日期泛化到 `month`/`quarter`/`year`。
  不依赖密钥。

**空值与缺字段分别处理**：字段值为 JSON `null` 时按 `on_null`
（`keep` 保留 / `reject` 拒绝该记录）处理；字段在记录中整个缺失时
直接跳过并单独计数。空字符串按 `on_empty` 同理处理——空账号不会被
映射成一个假值而造成虚假关联。策略指向不存在的路径（全输入无一命
中）、路径指向容器、字段类型与策略不符、重复路径、未知参数、以及
可能破坏唯一性的规则，全部报错，绝不静默跳过。

### 密钥与边界

- 映射密钥由调用者提供：`--key-env <变量名>` 或 `--key-file <路径>`，
  至少 16 字节。策略可声明 `key_fingerprint`（`sha256:` + 16 位十六
  进制），密钥不符即失败。密钥只存在于内存，**绝不**写进输出、诊断、
  日志或报告；报告中只出现其单向指纹。
- **稳定映射的输入范围**：算法版本 `copybook-mask/v1`、动作、关联组
  （未声明组时为完整字段路径）、规范化后的原值、映射密钥，五者共同
  决定输出。同一密钥与策略下，更换输入文件顺序和分块大小
  （`--chunk-size`）结果逐字节一致；不同关联组之间即使原值相同也互
  不可关联。
- **密钥轮换边界**：轮换密钥后 pseudonym/synthesize/shift 的全部
  结果都会改变——新交付无法与旧密钥产出的脱敏数据关联；同一次运行
  内部仍可关联。bucketize 不依赖密钥，轮换前后一致。
- 脱敏输出和报告不含原始敏感值，也不保存可逆映射表；扫描期的
  SQLite 暂存库在运行结束后删除。

### 扫描先行与交付安全

先流式扫描并验证**所有**输入和策略（JSON 结构、记录形状、类型匹配、
收集各关联组的不同原值到磁盘暂存），完成映射构建与冲突检查后，才
创建输出目录并分块写出。任何输入损坏、密钥不可用、映射冲突或输出
路径已存在，都不会留下貌似完整的数据集；交付中途失败会删除已创建
的半成品目录。输出目录必须是**不存在的新目录**，既有目录保持原样。

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 脱敏数据集已交付（`conclusion = masked`；被拒记录计入报告） |
| 2 | 输入/策略/密钥/冲突/输出路径失败，未留下交付目录 |

### 场景演示

`examples/mask/` 用人工构造的三个文件（账号主表、交易明细、关联持有
人表）演示全部行为，期望值在 `expected_results.py` 中手工写出——计数、
拒绝原因、区间标签、空值/缺字段、连接关系为字面量，HMAC 决定的脱敏值
由一份**独立参考实现**（只按文档化算法规范用标准库重写，不 import
被测包）给出，运行器逐字段对照：

```bash
python3 examples/mask/run_scenarios.py
```

覆盖：同一账号跨文件稳定替换、不同账号不碰撞、姓名不泄漏原文、空值与
缺字段的不同处理、金额区间化与偏移、日期偏移后仍可连接同一主体、策略
路径/类型错误、错误密钥、输入 JSON 损坏、既有输出目录，以及打乱文件
次序并改变分块大小后输出逐字节不变。

## 未结项核销加工（settle）

收款明细解码出来后，仅按“账号合计收款 − 合计账单”会让总额看似正确、账龄却落在
错误的账单上。`copybook settle` 在已解码的账单、收款与撤销事件之上，按事件
**实际发生顺序**重放核销去向，给出指定截止时点的未结余额、未分配收款和可逐条
追查的核销履历。它加工的是离线业务事实，不接支付系统，也不重新解码原始字节。

### 事件与字段配置

输入沿用现有 JSONL 的 `meta` / `fields` 包装，可来自多个文件。字段配置是相对
`fields` 的**字符串数组路径**，必须声明全部九条路由：

```json
{"paths": {
  "event_id": ["ID"], "account": ["ACCT"], "currency": ["CCY"],
  "at": ["AT"], "kind": ["KIND"], "object_id": ["OID"],
  "amount": ["AMT"], "due_on": ["DUE"], "target_id": ["TID"]}}
```

- `event_id` 是全输入唯一的非空字符串（含 cutoff 之后的事件也参与唯一性检查）；
  `account`、`currency` 及开立类事件的 `object_id` 同样是非空字符串，编号
  `007` 不会被转成 `7`。
- `at` 是带明确时区偏移的 ISO 8601 秒级时间戳，统一转 UTC 后排序；同一时刻按
  `event_id` 的 **UTF-8 字节序**决胜。文件排列与分块不改变结果。
- `currency` 限 `CNY`、`USD`，两者均以整数分计价且**绝不混算**，天然分成不同
  账号币种组。`BILL`、`RECEIPT` 的 `amount` 是严格两位小数的正十进制字符串，
  全程整数分运算、无 float；指数、`NaN`、隐式舍入和零一律拒绝，金额支持超过
  64 位整数。
- 四类事件 `BILL`、`RECEIPT`、`CANCEL_BILL`、`CANCEL_RECEIPT`。账单与收款两个
  命名空间分开：同一账号币种内同类对象不可重号，**即使前一个已撤销也不能复用**。
  撤销经 `target_id` 引用同账号同币种、排序后更早开立且尚未撤销的同类对象；撤销
  没有金额、不允许部分撤销、也不以负数伪装。各类别不用的路由可以缺字段。

### 核销与撤销语义

每应用一个事件，先完成它自身的开立或撤销，再在该账号币种内做一次自动核销：

- 按开立事件的 UTC 时刻与 event_id 字节序，选**最早仍有余额的账单**，再用同样
  顺序下**最早仍有可用金额的收款**抵它，取两者余额的较小者，直到一边没有可用项。
  到期日只用于账龄，不决定匹配次序；提前收到的钱保留，待后续账单出现时再用。
- 一笔收款可抵多张账单，一张账单也可由多笔收款抵完；每次实际抵扣都保存金额与
  双方对象，绝不只留净额。
- **撤销收款**：只解除当时仍由该收款支持的核销，把金额还回各账单未结余额，并移除
  该收款尚未分配的金额；其他收款的既有核销保持原去向。
- **撤销账单**：解除该账单全部活动核销，把钱退回各自原收款的可用余额后移除账单，
  再按当前未结项次序重新核销。重新分配时收款**保留原开立顺序**，不会因刚释放就
  变成最新。
- 已解除的历史核销不能再次解除；同一对对象解除后重新匹配会生成**全新的核销边**，
  不覆盖旧边。后来再撤销原收款，解除的是它重新形成的活动核销。

### 截止时点与账龄

调用者给出带时区的 `cutoff`，只应用 `at <= cutoff`（UTC，含相等时刻）的事件；
之后的撤销不能提前影响结果，但其 JSON 结构、路由字段与全输入 event_id 唯一性仍被
检查。账龄基准日取 cutoff 的 UTC 日历日，未结账单按 `基准日 − due_on` 的整数天
分为未逾期（≤0）、1–30、31–60、61–90、91 天以上五档；**桶金额只算账单当前未结
部分**，不用原额或账号净额。每个有效组都同时核对两条守恒关系：

```
未撤销账单原额合计 = 未结额 + 活动核销额
未撤销收款原额合计 = 未分配额 + 同一活动核销额
```

### 错误分级与交付安全

- **整次无法处理（exit 2，不写结果目录）**：坏 JSON、缺少有效
  event_id/account/currency/at/kind、全输入 event_id 重复、无效字段配置、cutoff
  不可解析、输出目录已存在。
- **组内业务错误（该组标 invalid，exit 1）**：非法金额/日期、对象重号、错误类别、
  跨账号币种或未来引用、再次撤销。坏组不交付余额、账龄或活动核销，只保留原事件与
  带文件/行号/路由的错误；其他组照常结算。即使错误出现在组内末尾，也不会把之前的
  部分结果说成完整余额。
- cutoff 之后的业务引用暂不执行、不影响该时点；非法业务字段只在事件真正应用时才
  使组失效。

### 用法

```bash
python3 -m copybook settle events1.jsonl events2.jsonl \
    --config examples/settle/run/config/settle_config.json \
    --cutoff 2026-01-10T00:00:00Z --out-dir delivery/settle-2026-01-10/
# 仅凭交付目录追查一笔收款（或账单）的完整去向，无需任何原始输入文件
python3 -m copybook trace delivery/settle-2026-01-10/ Q --kind RECEIPT
```

Python 接口：

```python
from copybook.settle import SettleConfig, run_settlement, trace_object

config = SettleConfig.load("settle_config.json")
summary = run_settlement(["events.jsonl"], config=config,
                         cutoff="2026-01-10T00:00:00+00:00",
                         output_dir="delivery/settle-2026-01-10/")
trace = trace_object("delivery/settle-2026-01-10/", "Q", kind="RECEIPT")
```

结果目录必须是**不存在的新目录**，已存在时拒绝覆盖（exit 2）。目录内容自解释，
新进程只读这些文件即可解释每笔钱的去向：

| 文件 | 内容 |
|---|---|
| `settle_report.json` | cutoff 与基准日、配置路由、每组状态/守恒/账龄桶、按币种合计、错误与文件清单 |
| `events.jsonl` | 全部事件（规范序）、原 `meta`/`fields`、来源文件/行号、是否应用、应用效果（开立/解除了哪些核销边） |
| `objects.jsonl` | 每张账单、每笔收款的原额、当前余额、状态、开立/撤销事件、账单到期日 |
| `allocations.jsonl` | 每条核销边的双方对象、金额、开立事件与状态；解除边带解除事件、原因并指回具体边 |
| `invalid_groups.jsonl` | 仅在存在坏组时出现，记录每组的局部业务错误 |

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 全部组有效，截止时点结果已交付（`conclusion = settled`） |
| 1 | 已完成加工但存在无效组（`business_rejected`）；好组结果完整，坏组无余额 |
| 2 | 输入/配置/cutoff 无法处理或输出目录已存在，不产生正式结果 |

### 场景演示

`examples/settle/` 的期望值在 `expected_results.py` 中**手工**按业务叙述逐条写出，
不来自处理器本身；`run_scenarios.py` 调真实 CLI 逐场景对照：

```bash
python3 examples/settle/build_events.py     # 构造多文件事件数据
python3 examples/settle/run_scenarios.py    # 跑 settle/trace 并对照手工期望
```

覆盖题目给出的手工账（逐事件活动边、解除原因、双方余额、两条守恒关系）、早收款后
开账、同刻字节序决胜、非 UTC 偏移、cutoff 正好等于事件时刻、到期日临界的全部账龄
桶、未分配资金、超 64 位金额，以及双重撤销、向后引用、组末业务错（好组仍正确、坏组
无部分余额）、输入重排不改去向、坏 JSON/重复事件阻止整次交付、既有目录不被覆盖。

## 离线 Git 交接资料接入（git-ingest / git-export / git-query）

供应商把每日原始流水和配套布局放进 Git 仓库，隔离网只能收到一个自包含
bundle。`copybook git-ingest` 把交接包转换成**可查询的提交、目录和文件版本
事实**；`copybook git-export` 按指定提交和精确路径取出一个原始 blob 写成
新文件，供已有 `decode` 入口使用。它不解释文件里的业务字段，不接远程代码
托管，也**不调用** `git`、libgit2、Dulwich 等实现——bundle、PACK、delta 和
对象负载全部由本包用 Python 标准库自行解析。

```bash
# 校验并落盘事实（输出目录必须不存在）
python3 -m copybook git-ingest handoff.bundle \
    --out-dir delivery/handoff-2024-06-15/ --temp-dir /var/tmp/copybook-git/
# 只看父图/清单/差异，不落盘
python3 -m copybook git-query handoff.bundle --temp-dir /var/tmp/copybook-git/ --log
python3 -m copybook git-query handoff.bundle --temp-dir /var/tmp/copybook-git/ \
    --commit <40-hex> --files
python3 -m copybook git-query handoff.bundle --temp-dir /var/tmp/copybook-git/ \
    --commit <40-hex> --diff
# 取出一个普通/可执行文件（路径为无损 base64 分量，见下）
python3 -m copybook git-export handoff.bundle <40-hex> \
    --path ZXhhbXBsZXM=/ZGF0YQ==/QkFOS0EtMjAyNDA2MTUuZGF0 \
    --out delivery/feeds/BANKA-20240615.dat --temp-dir /var/tmp/copybook-git/
```

Python 接口：

```python
from copybook.gitkit import (inspect_handoff, ingest_handoff, export_blob,
                             encode_path)

# 事实目录：handoff_report.json + commits.jsonl + commits/<id>.*.jsonl
summary = ingest_handoff("handoff.bundle", output_dir="delivery/facts/",
                         temp_dir="/var/tmp/copybook-git/")
report = inspect_handoff("handoff.bundle", temp_dir="/var/tmp/copybook-git/")
manifest = export_blob("handoff.bundle", commit_id="...",
                       path=encode_path([b"examples", b"data",
                                         b"BANKA-20240615.dat"]),
                       destination="delivery/BANKA-20240615.dat",
                       temp_dir="/var/tmp/copybook-git/")
```

### 接收范围与容器校验

- **只接受自包含 Git bundle v2**：首行必须是 `# v2 git bundle`；`v3`、
  `@object-format=sha256` 等能力行、64 位十六进制对象 ID、任何
  `-<hex>` prerequisite 行都在处理第一个对象**之前**拒绝。ref 必须是
  完整的 `refs/...`（或 `HEAD`）且指向 commit；指向 tree/blob/tag 的 ref
  拒绝。内部 PACK 必须用版本 2。
- 校验分四层，任何一层失败都不返回部分结果：bundle 头；PACK 签名/版本/
  对象数字段；**每个 zlib 压缩流的边界和声明大小**（流必须恰好在下一个
  对象处结束，解压长度必须等于声明值）；包尾 SHA-1（bundle 末尾 20 字节
  是 PACK 自身校验和，覆盖 PACK 数据区；**包尾之后多一个字节即判不完整**）。
- 对象 ID 按 Git 规范 `sha1("<type> <真实长度>\0" + 内容)` **重新计算**，
  压缩块的摘要从不当作对象 ID。同一对象在包中出现两次但类型/长度不同是
  损坏。
- 支持 commit、tree、blob 与 `OFS_DELTA`、`REF_DELTA`。REF_DELTA 的基对象
  可以在它**之后**；基对象本身也可以是 delta；REF/OFS 可混合成多层链；
  同一基对象被多份版本共享，解析某个目标**不会**改写或消耗基对象。缺失
  基对象、delta 环、复制/插入越界、源/目标长度不符都单独诊断。

### 提交父图

- 从每个 ref 遍历**所有**可达提交。提交身份、**完整且有序的 parents**、
  根 tree、作者/提交者原始头（头部块本身和逐头 base64 原值）、正文都保留；
  多行头（续行以空格开头，如 `gpgsig`）按 Git 规则解释，未知头原样保留。
- 父子关系**只来自 `parent` 头**，绝不按提交时间推断：演示里子提交时间
  早于父提交，输出顺序仍是父先于子。
- 输出顺序是拓扑序，就绪节点按**对象 ID 字节序**；作者时间不参与排序。
- 一个提交从多个 ref 到达只列一次，`refs` 字段列出全部到达来源。提交和
  tree 引用必须存在且类型吻合；父图有环、重复 parent、ref 指向非 commit
  都拒绝。

### 无损路径与文件版本

- tree 内按**原始名称字节**识别路径。报告中的 `path` 是
  `{"b64": "...", "components_b64": [...], "display": "..."}`：
  `b64` 为每段原始字节各自 base64 后以 `/` 连接（base64 字母表不含 `/`，
  因此无歧义，也不会被换行拆开）；`display` 只是方便阅读的安全转义名
  （非 UTF-8 名称显示为 `b64:<...>`），**不可**用于回填。
- 重复名称、空名称、含 `/` 的名称、`.`/`..` 分量、非法模式、tree 与对象
  类型不符、tree 引用缺失都拒绝。普通文件 `100644`、可执行 `100755`、
  符号链接 `120000`、gitlink `160000` 分别标记；**gitlink 只是外部提交
  ID**，包内不必存在该对象，也绝不沿它进入子仓库；符号链接的 blob 是链接
  文本，绝不在宿主机上跟随读取。根 tree 本身永远不是一条业务文件。
- 同一路径引用同一个 blob 仍是两份文件事实；不同提交共享 tree/blob 时，
  缓存按对象 ID 建立，路径前缀由调用方逐层传入，不会串到别的挂载位置。

### 文件变化

- 按**完整路径**对照，不做重命名猜测。新增、删除、内容变化、仅模式变化
  分别可识别；两者同时变化时 `content_changed` 与 `mode_changed` 都为真。
- 根提交相对空目录列出全部新增。合并提交**各自**对照每个 parent，保留
  `parent_index` 与父 ID，绝不只选第一父，也绝不先合成一个虚构父目录。
- 一边是文件、另一边是同名目录时，表现为旧叶子删除 + 新目录下叶子新增。

### 导出

- 只允许导出一个已存在 commit 中某条**精确路径**上的普通/可执行 blob。
  链接与 gitlink 可查询、不可导出；根 tree 不可导出。
- 不把仓库目录树铺到磁盘，不执行文件。目标必须不存在；内容先写入同目录
  临时文件、fsync 后原子改名，失败不留下半个正式文件，既有目标字节不变。
- 导出旁写 `<目标名>.export_manifest.json`：bundle 路径/SHA-1/大小/包尾、
  commit、tree 链（根 tree 到叶子）、blob ID/长度/sha256、原始路径
  （b64 与分量）、模式、类型。用新进程读回导出文件应与该提交的 Git 原件
  逐字节相同。
- 提交不在任何 ref 可达范围、路径不存在、目标是链接/gitlink/目录、目标
  已存在、源包损坏均非零失败。

### 资源上限与暂存

- 每个操作在 `--temp-dir` 下建立**本次独有**的暂存目录，对象与 delta 负载
  落盘（`objects/<前两位>/<id>`、`deltas/`），不把整个 bundle 或全部解压
  对象常驻内存；结束后只删除自己创建的目录，旁边的文件原样保留。
- 明确上限（均可通过 CLI/Python 覆盖）：
  `--max-object-bytes`（单对象解压，默认 64 MiB）、
  `--max-total-bytes`（累计解压/暂存，默认 1 GiB）、
  `--max-objects`（对象数，默认 1,000,000）、
  `--max-delta-depth`（delta 链深度，默认 64）。达到上限是硬拒绝，绝不
  截短成一个看似有效的对象。
- 输出目录必须是**不存在的新目录**；不存在“报告已写但某个文件已取出”的
  混合状态：`git-ingest` 报告只在整包校验完成后由临时目录整体改名生成，
  `git-export` 的完成状态只在目标文件与清单都落盘后返回。损坏包不会留下
  成功报告或半个导出文件。

### 报告字段与退出码

`handoff_report.json` 顶层字段：

| 字段 | 内容 |
|---|---|
| `format` | `copybook-git-handoff/v1` |
| `bundle` | 路径、整包 SHA-1、大小、头长度、PACK 字节数、对象数、包尾、refs |
| `limits` | 本次生效的四项上限 |
| `counts` | ref/可达提交/列出提交/对象/delta 对象数、最大 delta 深度 |
| `resources` | PACK 字节数、暂存字节数、暂存对象数 |
| `order` | 固定顺序说明（父先于子，就绪节点按对象 ID 字节序） |
| `commit_order` | 拓扑序提交 ID 数组 |
| `refs` | 每个有 ref 到达的提交及其全部来源 ref |
| `commits` | 提交身份、有序 parents、根 tree、原始作者/提交者、全部原始头、正文 |
| `file_listings` | 每个提交的完整文件清单（路径/模式/类型/对象 ID/长度/sha256） |
| `diffs` | 每个提交对**每个** parent 的变化行（父序号、父 ID、变化项） |
| `extraction` | `performed: false`——事实接入不代表任何文件已取出；只有 `git-export` 的清单才声明导出 |

`commits.jsonl` 每行一个提交；`commits/<id>.files.jsonl` 每行一个文件事实；
`commits/<id>.diff-p<序号>-<父ID>.jsonl`（根提交为 `.diff-root.jsonl`）每行
一个变化。

| 退出码 | 含义 |
|---|---|
| 0 | bundle 校验通过、事实已交付 / 导出完成 |
| 2 | 包格式、对象、图、路径、上限、目标已存在或输出目录已存在；不产生正式结果 |

### 交接演示与夹具

```bash
python3 examples/git/build_handoff.py      # 用 git 生成双支线仓库与自包含 bundle
python3 examples/git/run_scenarios.py      # 接入、对照手工父图/差异、导出并 decode
python3 examples/git/run_edge_cases.py     # 手工 PACK：REF/OFS 链、共享基对象、各种路径
python3 examples/git/run_corruptions.py    # 复制件损坏（重算外层校验）逐项拒绝
python3 examples/git/run_performance.py    # 大包峰值 RSS、暂存量与上限清理
```

`examples/git/README.md` 记录演示仓库的构造步骤、手工期望值位置与各脚本覆盖
的边界。

- 真实演示仓库含现有样例数据与布局；`data` 支线改数据、`layout` 支线改布局，
  数据与布局子提交的时间都**早于**父提交；随后两次合并并打出 `--all`
  自包含 bundle。`expected_handoff.py` 中的父图、每支线文件版本、每次合并
  对每个父的预期差异与解码字段**全部手工写出**，运行器逐项比对；两份数据
  从不同提交导出，其中一份交给 `decode` 核对字段与源位置。核对对象只来自
  bundle；生成仓库的工作树仅在事后作为独立旁证。
- `run_edge_cases.py` 用手写 PACK 让 REF_DELTA 先于基对象、REF/OFS 混合
  链共享同一基对象，覆盖两 refs 共享祖先、merge 每父差异、模式变化、
  删除、文件变目录、含换行/非 UTF-8 名称、链接与 gitlink。
- `run_corruptions.py` 在复制件中破坏 delta 复制范围、缺基对象、声明长度、
  tree 引用与类型、包尾与额外尾字节，并适当重算外层校验，让内部检查真正
  被执行。
- `run_performance.py` 用约 96 MiB 解压量、2000 个无关 blob 的包记录实际
  峰值 RSS 与暂存量（远低于解压总量），并验证上限触发后不留下报告或暂存。

## 原件定点回写（rewrite）

接收人员确认的少量更正需要送回供应商时，`copybook rewrite` 把更正**按字节定点写回
原件**，而不是重新编码整条记录：只改每个被确认字段占据的字节，填充区、未选中的
REDEFINES 视图、非标准但合法的符号写法都原样保留。它读取原始二进制和一份 UTF-8
JSON 更正单，产出一个新目录，内含与原件等长的新原件和机器可读的字节变更凭据。

```bash
python3 -m copybook rewrite order.json ORIGINAL.bin \
    --out-dir delivery/2026-11-15/ --temp-dir /var/tmp/rewrite-tmp/ \
    --copybook-base copybooks/
```

Python 接口：

```python
from copybook.rewrite import rewrite_original

evidence = rewrite_original(
    "order.json", source_path="ORIGINAL.bin",
    output_dir="delivery/2026-11-15/", temp_dir="/var/tmp/rewrite-tmp/",
    copybook_base="copybooks/")
# 新原件在 output_dir/original.rewritten.bin，凭据在
# output_dir/rewrite_evidence.json；evidence 即写出的凭据对象。
```

### 更正单与坐标

更正单顶层给原件完整 `source_sha256`、具名 `layouts` 目录（每条给 Copybook 路径、
布局名/版本、`codec`，仅支持 `cp037`/`cp500`，同一原件可混合布局）和 `records`
列表。每条记录给从文件起点算的 `byte_start/byte_end` 半开区间、布局条目、该跨度的
`record_sha256` 和 `edits`。坐标是非负整数（bool 不算整数），区间长度必须等于解析
所得 `record_length`，不能越界，记录区间互不重叠或重复；列表可乱序。区间之间的头、
换行、填充等字节直接保留，不靠换行找边界，COMP-3 中恰好为 `0A` 的字节也不会切开
记录。原件可有未列出的记录，`records`/`edits` 均可为空。顶层与各记录摘要都对实际
字节重新计算核对，绝不拿声明值代替计算值。

每个 edit 给 `path`、`expected_hex`、`value`。`path` 从记录根组的孩子起算：命名层
用大写字段名，固定 `OCCURS > 1` 的数组层用从 0 开始的整数（嵌套 OCCURS 每层都要
指定，不能凭末级同名字段猜位置）；只能定位到非 FILLER 基本字段，组、整数组、匿名
填充项不可写；REDEFINES 基字段与任一替代视图都可显式指定，不从判别字段或内容猜选。
`expected_hex` 必须恰好覆盖该字段原字节并对未修改的原件逐条核对，前一项更正不会
成为后一项的“原值”。路径不存在/不唯一、类型错误、旧值不符、未知布局、缺成员都拒绝
整次作业；JSON 重复成员、`NaN`/`Infinity`、浮点数同样拒绝。

### 严格编码与重叠合同

新值按现有 PIC 的字节容量严格编码。`PIC X/A` 只接受字符串，按单字节编码转换后右补
该编码空格，不截断、不替换不可编码字符、不隐式转字符串；`PIC 9/S9`（可含 `V`）只
接受 `-?[0-9]+(?:\.[0-9]+)?` 十进制字符串，小数位可少于 scale 右补零、超出 scale
（哪怕多的是零）拒绝，精确缩放后绝对值必须小于 `10**总位数`，无符号 PIC 拒绝任何
负值（含 `-0`），有符号负零按非负零写。运算只用整数/字符串，不依赖 Decimal 的
28 位精度，可处理 60 位以上数字。DISPLAY 带符号末位用正 C/负 D overpunch，无符号
用普通 F 区位；COMP-3 前置填充半字节写 0，末半字节有符号 C/D、无符号 F。

同记录的更正不是顺序覆盖：每个 edit 是对完整字段跨度的字节要求，两个跨度重叠时仅当
公共位置要求的新字节逐一相同才一起生效，一致重叠合法、不一致拒绝整次作业，重复同一
路径同此规则；该合同覆盖基字段与替代视图、长度不同的视图和重复组内重定义。

### 凭据、边界与退出码

凭据含实际输入/输出 SHA-256 与长度、布局文件 SHA-256 及名称版本、每个 edit 的完整
路径/记录坐标/绝对字段范围/旧字节/要求新字节，以及按绝对坐标排序、互不交叠、相邻
合并的实际变化区间（夹着未变字节会分段，只含真变化字节）和请求字段数、覆盖字节数
（重叠只计一次）、真正改变字节数；不保存整份原件或每个未改字节。输出目录必须不存在，
输入和布局只读，暂存由调用者给位置且只清理自己创建的子目录；原件按块流式处理、不
整份常驻，单记录上限默认 16 MiB 且在分配前检查。任何摘要不合、越界、冲突或写出失败
都返回非零（退出码 `2`），不留下可当成完成的交付；成功为 `0`。

```bash
python3 examples/rewrite/run_demo.py --workdir /tmp/rewrite-demo
python3 tests/rewrite/run_performance.py --size-mib 270 --workdir /tmp/rewrite-perf
```

演示先手算目标字段偏移与新旧十六进制再实际回写，覆盖一个未改符号字段字节保持原样、
金额视图与文本视图公共字节一致时成功而冲突时整次失败、两项 `expected_hex` 都基于
原件且交换 edit 顺序结果不变，并对 60 位数、定点小数、cp500 文本做独立字节核对，
最后把一段结果交给现有 `Decoder` 复核业务值。压测实跑大于 256 MiB 的原件、稀疏更正
首中尾记录，报告实际大小、峰值内存与耗时，并独立逐字节比较差异集合与凭据区间。

## 测试

```bash
python3 -m unittest discover -s tests -t . -v
```

覆盖：字节宽度 vs 字符宽度（COMP-3 打包、非 UTF-8 字节）、EBCDIC 编解码
与 codec 切换、符号穿孔与隐式小数（zoned + COMP-3）、REDEFINES 三种选择
策略与视图容错、按日期切换布局版本、截断/超长行、坏行隔离后的行号稳定
性、绝对字节偏移、按偏移定位与隔离导出；交接校验的证据归并完整性、
五种控制记录状态机、批内/文件级拒收边界、不可判定数量、SQLite 磁盘暂存与
有界内存、交付目录安全、CLI 三档退出码；影响报告的布局展开（分组
路径、视图簇、重复路径检测）、字段分类（位移 vs 解释变化、FILLER 填充、
codec 仅影响 DISPLAY）、日期分段（闭区间边界、多次切换、开放区间）、
各类清单问题（重叠/空洞/缺旧版本/缺 Copybook/路径重复/版本复用）与
输出目录防覆盖；以及脱敏生成的策略解析（路径/动作/参数/指纹校验）、
密钥加载（环境/文件/长度/指纹核对）、脱敏原语（格式保持假值、无片段
合成名、精确金额抖动、日期偏移、区间标签）、引擎行为（跨文件连接、
空值/缺字段、策略拒绝、OCCURS 通配、整数账号、混合类型组拒绝、映射
冲突中止、损坏输入、交付目录安全、顺序与分块不变性、密钥轮换）与
CLI 两档退出码；以及未结项核销的严格金额/日期/时间戳解析与超 64 位
精确运算、UTC+event_id 字节序排序与多文件重排不变性、手工账逐事件
活动边/解除原因/双方余额核对、收款撤销与账单撤销的释放再分配、历史
核销边不覆盖、cutoff 边界与五档账龄、两条守恒关系、坏组隔离无部分
余额、路由级致命错误零交付、结果目录防覆盖、仅凭结果目录的对象追查
与 settle/trace CLI 三档退出码；以及离线 Git 交接的 bundle 头/包尾/多余
尾字节拒绝、PACK 签名/版本/对象数/流边界/声明大小、REF 先于基对象的 delta、
REF/OFS 混合多层链与共享基对象、对象 ID 重算、缺失基对象与坏复制范围、
tree 重复/空/斜杠/点分量与类型不符、父图环与重复 parent、父先于子的拓扑序
与多 ref 来源、合并对每父差异、模式/内容/删除/文件变目录、换行与非 UTF-8
路径、链接与 gitlink 不导出、导出原子性与既有目标保护、上限硬拒绝与暂存
清理、CLI 两档退出码。
