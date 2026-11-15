# 布局变更影响报告示例

`build_scenarios.py` 构造十组"旧清单 vs 新清单"场景（只含清单与
Copybook，无业务数据文件）；`expected_results.py` 把每个字段差异、日期
区间和结论**手工**写在前面；`run_scenarios.py` 重建场景、逐个调用真实的
`copybook impact` CLI，并把程序输出与手工期望逐条核对。

```bash
python3 examples/impact/run_scenarios.py
# 输出 PASS/FAIL 逐场景结果；生成的清单/copybook 在 run/scenarios/，
# 报告在 run/<场景>/report/（impact_report.txt + impact_report.json）
```

| 场景 | 演示点 | 退出码 |
|---|---|---|
| `main` | 尾部字段因前方插入 CURRENCY 而位移 +3；BALANCE 改小数位（字节数不变）；OLD-STATUS 移除；新增 BALANCE-TEXT 重定义视图；BANKB 全时段不受影响 | 1 |
| `compatible` | 新字段落在旧 FILLER 字节内 + 新增视图，记录长度不变 → 兼容 | 0 |
| `regroup` | STATUS 同名但换分组：按完整路径判为移除+新增，不算同一字段 | 1 |
| `conflict` | 新清单内 v1/v2 生效区间重叠（闭区间 2024-08-15..08-31） | 2 |
| `gap` | 新清单 2024 年 9 月无任何布局（空洞） | 2 |
| `nobaseline` | 新清单接入旧清单没有的 BANKC/CARD（缺少旧版本基线） | 2 |
| `missing_copybook` | 新清单引用的 ACCT-9.cpy 不存在 | 2 |
| `duplicate_path` | 新 v2 中 GRP-A 声明两次，字段路径重复 | 2 |
| `version_reused` | 两侧都叫 version 1 但布局文件不同 | 2 |
| `existing_outdir` | 输出目录已存在：拒绝覆盖，不写报告 | 2 |
