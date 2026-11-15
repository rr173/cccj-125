# 未结项核销加工示例（settle）

本目录用可复跑的端到端场景演示 `copybook settle` / `copybook trace`。所有
期望值都在 `expected_results.py` 中**手工**按业务叙述逐条写出，不来自处理器
本身；运行器只调真实 CLI 并逐字段对照。

## 运行

```bash
python3 build_events.py      # 在 run/ 下生成多文件事件数据与字段配置
python3 run_scenarios.py     # 跑 settle/trace 并对照手工期望
```

`build_events.py` 每次先清空再重建 `run/`，可反复执行。运行器在
`run/delivery/` 下为每个场景生成独立结果目录；失败时以非零码退出并逐条列出
不符之处。

## 场景与手工期望来源

| 场景 | 覆盖点 |
|---|---|
| `hand` | 题目手工账：A/B 开账、P/Q 收款、撤销 P、开 C 收 R、撤销 B、撤销 Q；逐事件活动边、解除原因、双方余额、两条守恒关系 |
| `hand` 的 checkpoints | 同一输入在三个更早 cutoff 下重放，核对每个时点的活动边 |
| `hand_shuffled` | 同样事件拆两个文件并打乱物理次序；核销去向必须与 `hand` 逐边一致 |
| `early_receipt` | 先收款、账单数天后才出现，提前到的钱保留并自动抵账 |
| `same_instant` | 四个事件同一时刻，按 event_id 的 UTF-8 字节序决胜 |
| `offsets` | `+08:00` 与 `+03:00` 偏移归一化到同一 UTC 时刻后排序 |
| `cutoff_equal` | cutoff 正好等于事件时刻，事件仍被应用（闭区间） |
| `aging` | 到期日临界值覆盖未逾期（含当日与未来）、1–30、31–60、61–90、91+ 全部桶 |
| `unallocated` | 收款多于账单，未分配金额留在收款余额表并通过收款守恒 |
| `huge` | 30 位整数金额（远超 64 位）以整数分精确保存，无 float |
| `bad_group` | 好组 GOOD 正常结算；坏组 BAD 末事件双重撤销，坏组无任何余额/核销，错误可定位 |
| `fatal_corrupt` / `fatal_duplicate` | 坏 JSON、重复 event_id 使整次无法处理（exit 2），不产生结果目录 |

运行器另外固定校验：`copybook trace` 仅凭交付目录即可重建收款 Q 的完整履历
（B/A/C 三张账单与 e04/e08/e09 三个事件），以及向已存在目录重复交付被拒绝
（exit 2）且原目录不被改动。

## 数据形状

每条事件保持解码器 JSONL 的 `meta` / `fields` 包装；字段名由
`run/config/settle_config.json` 的字符串数组路径声明（event_id、account、
currency、at、kind、object_id、amount、due_on、target_id 九条路由）。
