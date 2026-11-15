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
  __main__.py        CLI（decode / audit 两个子命令）
examples/            示例 Copybook、清单、数据生成器、交接场景与压测
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

## 测试

```bash
python3 -m unittest discover -s tests -t . -v
```

覆盖：字节宽度 vs 字符宽度（COMP-3 打包、非 UTF-8 字节）、EBCDIC 编解码
与 codec 切换、符号穿孔与隐式小数（zoned + COMP-3）、REDEFINES 三种选择
策略与视图容错、按日期切换布局版本、截断/超长行、坏行隔离后的行号稳定
性、绝对字节偏移、按偏移定位与隔离导出；以及交接校验的证据归并完整性、
五种控制记录状态机、批内/文件级拒收边界、不可判定数量、SQLite 磁盘暂存与
有界内存、交付目录安全、CLI 三档退出码。
