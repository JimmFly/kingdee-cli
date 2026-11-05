# kd —— 给 agent 用的金蝶云星空只读 CLI

`kd` 只调用金蝶 WebAPI 的读取接口，任何写操作都会在客户端被拒绝，可以放心调用。

## 调用约定

- 被程序调用（stdout 不是终端）时，**结果默认是 JSON**，输出到 stdout。提示信息输出到 stderr，加 `-q` 可关闭。
- 失败时 stdout 输出 `{"error": {"code": ..., "message": ..., ...}}`，退出码如下：

| 退出码 | code | 含义 / 处理方式 |
|---|---|---|
| 0 | — | 成功 |
| 1 | `internal` | kd 自身的缺陷，需要人处理 |
| 2 | — | 命令参数错误（argparse，输出不是 JSON） |
| 3 | `config` / `auth` | 没配置、配置非法、登录失败或 HTTP 401/403/404，需要人处理 |
| 4 | `ambiguous` | 中文名匹配到多个表单；`error.candidates` 里有候选 FormId，选一个重试 |
| 4 | `server` | 金蝶返回业务错误（字段不存在、过滤语法错误、无权限等），按 message 调整 |
| 5 | `rate_limit` / `network` | 限流、网络问题或 HTTP 5xx；kd 已自动重试过，稍后再试 |
| 6 | `readonly` | 尝试调用了写接口，这是被禁止的 |

- 表单可以写 FormId（`SAL_SaleOrder`），也可以写中文名（`销售订单`）。
- 服务器限制每分钟 500 次调用，kd 会自动节流和重试。不要并发跑多个 kd 进程。

## 标准流程：找表单 → 看字段 → 查询

```bash
kd -q forms -k 订单 -t bill             # 1. 找表单：返回 [{"FormId","名称","类型","云","子系统"}]
kd -q fields SAL_SaleOrder              # 2. 看字段：[{"实体","实体类型","字段","名称","引用表单","必填"}]
kd -q query SAL_SaleOrder -f FBillNo,FDate,FCustId.FName -w "FDate>='2025-01-01'" -n 50   # 3. 查询
```

### query 要点

- `-f` 不写 = 全部表头字段（不能查的字段已自动剔除）。`-f '*FSaleOrderEntry'` = 某个分录的全部字段。
- 引用字段用点号取属性：`FCustId.FNumber`、`FCustId.FName`、`FMaterialId.FSpecification`。
- 分录字段直接写字段名，结果每行对应一条分录明细（表头字段会重复）。
- `-w` 是 SQL 风格的过滤条件：`FDocumentStatus='C' and FDate>='2025-01-01' and FCustId.FNumber like 'KH%'`。
  - 单据状态 `FDocumentStatus`：A 创建、B 审核中、C 已审核、D 重新审核、Z 暂存。
  - 禁用状态 `FForbidStatus`：A 正常、B 禁用。
- `--order "FDate DESC"` 排序；`-n` 每页行数（默认 100）；`--start` 偏移；`-a` 自动翻页取全部（注意数据量）。
- 返回 `[{字段: 值}]`。日期是 `2024-12-01T00:00:00` 格式。
- 小数是原样的十进制数字（如 `1234567890.123456789`），做金额计算时请用十进制类型解析，不要转成 float。
- 没写 `--order` 时自动按主键排序，用 `--start` 手动翻页也是稳定的。`-a` 会一直翻到空页；加 `--max-pages N` 限制请求页数。

### 其他读取命令

```bash
kd -q view 物料 --number MAT0001         # 单条完整数据（含所有分录，原始结构）
kd -q groups 物料                          # 分组树 [{"FID","编码","名称","上级FID"}]
kd -q attachments PUR_PurchaseOrder --number PO0001   # 单据的附件列表
kd -q download <附件ID或FileId> -O 文件名   # 下载附件（测试环境被服务器拦截，正式环境可用）
kd -q dbs                                  # 数据中心列表
kd -q call BillQuery -d '{"FormId":"BD_UNIT","FieldKeys":"FNumber,FName"}'   # 直接调只读接口
```

## 报表（GetSysReportData）

报表要两样东西：**列名**（`-f`）和**过滤条件**（`-d '{"Model": {...}}'`）。

- 列名：如果有人在金蝶网页里给这个报表保存过过滤方案，kd 会自动读出列名，`-f` 可以不写。否则必须提供 `-f`。
- 过滤条件：每个报表不同，需要从金蝶「在线测试WebAPI」页面该报表「查询报表数据」样例中的 `Model` 获取。
- `kd report <报表> --schemes` 列出已保存的过滤方案。
- 返回 `[{列: 值}]`，金额是带千分位的字符串。

已验证的示例，科目余额表（`GL_RPT_AccountBalance`）：

```bash
kd -q report 科目余额表 -f FBALANCEID,FBALANCENAME,FBEGINDEBIT,FBEGINCREDIT,FDEBIT,FCREDIT,FENDDEBIT,FENDCREDIT -d '{"Model":{
  "FACCTBOOKID":{"FNumber":"001"}, "FCURRENCY":"1",
  "FSTARTYEAR":"2026","FSTARTPERIOD":"1","FENDYEAR":"2026","FENDPERIOD":"8",
  "FBALANCELEVEL":"1","FNOTPOSTVOUCHER":"true",
  "FSTARTBALANCE":{"FNumber":""},"FENDBALANCE":{"FNumber":""}}}'
```

- 账簿编号（`FACCTBOOKID.FNumber`）：`kd -q query GL_VOUCHER -f FAccountBookID.FNumber,FYEAR,FPERIOD --order "FDATE DESC" -n 1`
- `FCURRENCY` 传 `"1"`（本位币）；传空时金额都是 null。
- 凭证没过账时要加 `FNOTPOSTVOUCHER: "true"`，否则查不出数。

很多报表的数据其实来自单据，也可以直接 `kd query` 对应的单据自己汇总。比如即时库存是 `STK_Inventory`，应收单是 `AR_receivable`，凭证是 `GL_VOUCHER`。

## 常用表单

| 中文 | FormId |
|---|---|
| 物料 / 客户 / 供应商 | `BD_MATERIAL` / `BD_Customer` / `BD_Supplier` |
| 组织 / 部门 / 员工 | `ORG_Organizations` / `BD_Department` / `BD_Empinfo` |
| 销售订单 / 销售出库 / 销售退货 | `SAL_SaleOrder` / `SAL_OUTSTOCK` / `SAL_RETURNSTOCK` |
| 采购订单 / 采购入库 | `PUR_PurchaseOrder` / `STK_InStock` |
| 即时库存 | `STK_Inventory` |
| 应收单 / 应付单 / 收款单 / 付款单 | `AR_receivable` / `AP_Payable` / `AR_RECEIVEBILL` / `AP_PAYBILL` |
| 凭证 / 科目 | `GL_VOUCHER` / `BD_Account` |

找不到时：`kd -q forms -k 关键字`。全部可读表单及行数可用 `kd scan` 生成（注意：`scan` 的行数是查询返回的行数，含明细时一张单据会有多行，不等于单据数）。
