# 批次交接校验（audit）示例

本目录用一条**真实的 EBCDIC 解码链路**演示交接校验：先用现有 `decode`
命令把固定宽度数据解成正常记录 JSONL 与隔离索引，再用 `audit` 命令核对
FHEAD/BHEAD/DETAIL/BTAIL/FTAIL 五种控制记录之间的关系。

## 文件

| 文件 | 作用 |
|---|---|
| `copybooks/HANDOFF-1.cpy` | 73 字节交接记录布局（五种类型共用一个定长记录） |
| `manifest.json` | 解码用接收清单（发送方 BANKX） |
| `audit_config.json` | 审计字段路径映射 + 有限类别表 `AAA`/`BBB` |
| `build_scenarios.py` | 生成全部场景的 EBCDIC `.dat`（含截断行、坏 COMP-3） |
| `expected_results.py` | **手工**写出的每场景批次状态、控制数、可入仓行号 |
| `run_scenarios.py` | 解码 → audit（子进程，验退出码）→ 对照手工期望 |
| `run_performance.py` | 单超大批次 + 大量小批次的流式/内存/暂存压测 |
| `data/`、`run/`、`perf_run/` | 生成物（可删除后重跑） |

数量字段是 `S9(9)V999 COMP-3`，因此所有数量都以**千分单位整数**精确核对，
例如 `2.500` 编码为 milli=2500，`-1.250` 为 -1250，全程不使用 float。

## 交接故事

`handoff_good`（9 个物理行）：

```
1 FHEAD F-2024-001
2 BHEAD BAT-01
3 <截断行，被解码器隔离>          # 一条已收到的明细位置，业务值未知
4 BTAIL BAT-01 dc=1 aaa=0 bbb=0
5 BHEAD BAT-02
6 DETAIL seq=1 AAA  2.500
7 DETAIL seq=2 BBB -1.250
8 BTAIL BAT-02 dc=2 aaa=2.500 bbb=-1.250
9 FTAIL bc=2 dc=3                 # 3 = 1 条隔离 + 2 条真明细
```

- BAT-01 因含隔离行被**拒收**：行数 1=1 相符，但类别数量**不可判定**
  （隐藏值不当零）。
- BAT-02 完全正确，其第 6、7 行明细**可入仓**。
- FTAIL 总数诚实（含坏行），文件本身不被拒，退出码 1（存在业务拒收）。

`handoff_ftail_lied`：物理内容完全相同，只把第 9 行 FTAIL 的 detail_count
从 3 改成 2。校验器实算仍为 3，于是在最后一行触发文件级
`ftail_detail_count_mismatch`：BAT-02 自身检查仍为 accepted，但准入资格被
撤销，`accepted_details.jsonl` 为空，退出码仍是 1。

## 其余小场景（期望均手工列在 expected_results.py）

| 场景 | 关键约定 |
|---|---|
| `empty_batch` | BHEAD 后直接 BTAIL，dc=0、合计为零，空批通过 |
| `negative_offset` | +10.001 与 -10.001 精确抵消为 0.000 |
| `three_decimals` | 0.001+0.002=0.003，三位小数无浮点漂移 |
| `seq_restart` | 第二批 seq 重新从 1 开始，两批都通过 |
| `duplicate_batch` | 同一 batch_id 出现两次 → 全文件拒收，无明细可入仓 |
| `orphan_quarantine` | 批外隔离行 → 全文件拒收（不可局限） |
| `data_after_ftail` | FTAIL 后追加 DETAIL → 全文件拒收 |
| `line_gap` | 证据删掉一行却仍称 expected_lines=9 → 无法校验，exit 2，无交付 |

## 复跑

```bash
python3 examples/audit/build_scenarios.py
python3 examples/audit/run_scenarios.py        # 全部应打印 PASS
GIANT_DETAILS=200000 MANY_BATCHES=5000 MANY_PER_BATCH=4 \
    python3 examples/audit/run_performance.py  # 输出行数/峰值RSS/暂存规模
```

压测输出（参考量级，随机器不同）：20 万条明细的单批次，峰值 RSS 增量约
3 MB、SQLite 暂存约 70 MB、耗时约 9 秒；5000 个小批次（各 4 条）峰值增量
同样约 3 MB，证明内存不随明细数增长。
