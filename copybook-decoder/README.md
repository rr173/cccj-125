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
  __main__.py        CLI
examples/            示例 Copybook、清单、数据生成器
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

## 测试

```bash
python3 -m unittest discover -s tests -t . -v
```

覆盖：字节宽度 vs 字符宽度（COMP-3 打包、非 UTF-8 字节）、EBCDIC 编解码
与 codec 切换、符号穿孔与隐式小数（zoned + COMP-3）、REDEFINES 三种选择
策略与视图容错、按日期切换布局版本、截断/超长行、坏行隔离后的行号稳定
性、绝对字节偏移、按偏移定位与隔离导出。
