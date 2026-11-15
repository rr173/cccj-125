# 关系准入加工（relational admission）示例

本目录演示 copybook-decoder 的 `admit` 子命令：一次离线多表交接里，哪些记录
彼此仍有足够的引用支持可以入仓，哪些记录失去资格，以及每条拒收记录可被另一
个进程独立检查的证明。

## 场景构成

输入不是手工拼的孤立 JSON，而是**真实接到 decode 产物**：

1. `build_scenarios.py` 先调用项目自身的解码器，把 EBCDIC 账号文件
   `examples/data/BANKA-20240315.dat` 解码成标准 `meta` / `fields` JSONL，
   作为 `accounts` 表（保留原始 meta 与物理行号，不重新解码原件）；
2. 再写出同包装形状的 `branches`、`transactions`（一个逻辑表、两份文件）、
   `terminated` 表；
3. `contract.json` 声明每张表的 row_id 路径、字段（string/integer、
   nullable）、具名候选键和具名引用（mandatory / optional）。

预期结论（运行前手写）：

| 表 | 记录 | 处置 | 层 | 依据 |
|---|---|---|---|---|
| branches | BR1, BR2 | 保留 | – | BR1 自引用闭环，BR2→BR1 |
| branches | BR3 | 连锁拒收 | 1 | r_parent 指向不存在的分支 |
| accounts | 0000000001/2 | 保留 | – | r_gone 指向 terminated 中存在的 TERM-1 |
| accounts | 0000000003 | 连锁拒收 | 1 | r_gone 的 GHOST 键无任何候选 |
| transactions | T001, T004 | 保留 | – | T004 的 optional 复合引用全空，显式免匹配 |
| transactions | T002 | 连锁拒收 | 1 | 账号 0000000099 不存在 |
| transactions | T003 | 连锁拒收 | 2 | 其账号 3 是第 1 层拒收，影响传遍依赖链 |

## 可复跑命令

```bash
# 1. 生成输入（含真实 decode 步骤）并运行作业、核对全部手写预期
python3 examples/relation/run_scenarios.py

# 2. 仅重建输入
python3 examples/relation/build_scenarios.py

# 3. 真实终端入口运行作业（业务拒收退出码 1，结构错误退出码 2）
python3 -m copybook admit \
    --contract examples/relation/run/contract.json \
    --out-dir examples/relation/run/delivery \
    --temp-dir examples/relation/run/stage

# 4. 重开进程，仅凭交付目录查询一行的保留见证或拒收证明
python3 -m copybook admit-explain examples/relation/run/delivery transactions T003
python3 -m copybook admit-explain examples/relation/run/delivery branches BR3

# 5. 逐表读取获准行（按 row_id 的 UTF-8 字节序）
python3 -m copybook admit-table examples/relation/run/delivery transactions
```

## 交付目录布局

```
delivery/
  COMPLETE.json             完成标记；没有它的目录一律视为未完成
  admission_report.json     结论、每张表 input/kept/local/cascade 计数、
                            证明节点与边数量、暂存量与峰值 RSS
  admission.sqlite3         自包含溯源库：记录、业务键、引用、证明节点/边、
                            保留见证；重开后只读打开，不再依赖原输入
  tables/0000_<表名>/
      kept.jsonl            获准行（保留原 meta/fields，按 row_id 字节序）
      local_rejected.jsonl  第 0 层局部拒收行（含全部局部诊断）
      cascade_rejected.jsonl 连锁拒收行
```

## 压测：长链连锁与大同键候选组

```bash
# 默认 30000 行链式连锁 + 20000 行同键候选组（可传参调整）
python3 examples/relation/run_performance.py [chain_n] [group_m]
```

消除算法是**半朴素增量传播**：每个目标键元组维护一个“仍存活候选数”计数，
只有上一轮真正被拒收的行（delta 表）才会在本轮减少计数；计数归零的元组才
触发其引用边失效。每条记录至多被拒收、至多减少一次计数，因此全部轮次的总
工作量为 O(引用边 + 键值)，即使引用图是一条需要 N 轮的长链。Python 侧最大
的容器只是单轮 delta 批次，整表、全量键、同键大组和完整依赖图都只存在于
SQLite（见输出中的 proof node/edge 计数与 RSS、暂存量）。
