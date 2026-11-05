# kd — 金蝶云星空只读查询 CLI

单文件、只依赖 Python 3 标准库。**只调用读取接口**（View / ExecuteBillQuery / BillQuery /
QueryBusinessInfo / QueryGroupInfo / GetSysReportData / AttachmentDownLoad / GetDataCenterList），
其它方法在客户端直接拒绝，不会改动任何数据。

**给 agent 用**：看 [AGENTS.md](AGENTS.md)。被程序调用时默认输出 JSON，错误也是 JSON（`{"error":{"code","message"}}`），
退出码区分错误类别（3 配置/登录、4 业务错误/表单名歧义、5 限流/网络、6 写操作被拒）。`-q` 关闭提示信息。

## 安装与配置

```bash
chmod +x kd.py && ln -s "$PWD/kd.py" /usr/local/bin/kd
KD_URL=https://xxx.ik3cloud.com/k3cloud/ kd dbs   # 查数据中心 ID（无需登录）
kd config init                                    # 填 url / acct_id / 用户名 / 密码（或 AppID+AppSecret）
kd login
```

配置在 `~/.config/kdcli/config.json`（权限 600）；缓存（表单目录、元数据、可用字段、扫描/检查结果）在
`~/.config/kdcli/cache/<profile>/`。多环境：`kd -p prod config init`，`kd config use prod`。

**限流**：服务器限制每分钟 500 次 WebAPI 调用。kd 默认按每分钟 450 次自动节流，被限流时按服务器提示的秒数等待后重试
（`KD_RATE=300 kd ...` 可调低，给其他系统留余量）。会话过期会自动重新登录。

## 查询 —— 任何表单都可以直接查

表单可以写 FormId，也可以写中文名（同名时优先单据，模糊匹配不唯一会列出候选）。

```bash
kd query 销售订单 -n 10                         # 不写 -f = 全部表头字段
kd query 销售订单 -f '*FSaleOrderEntry' -a -o csv > 明细.csv   # 某分录的全部字段、全部行
kd query 物料 -f FNumber,FName,FSpecification -w "FDocumentStatus='C'"
kd query SAL_SaleOrder -f FBillNo,FCustId.FName,FMaterialId.FNumber,FQty -w "FDate>='2025-01-01'" --order "FDate DESC"
kd view 物料 --number MAT0001               # 单条完整数据（含所有分录）
kd groups 物料                                 # 分组树
```

- 引用字段自动输出编码（如客户→客户编码），用户字段输出姓名。
- 服务器不支持的字段会自动跳过（`--show-skipped` 显示是哪些），首次查询后可用字段会缓存，之后直接查。
- `-o table|json|csv`，`-n` 每页行数，`-a` 自动翻页取全部，`-v` 仅打印接口路径和请求/响应大小，不打印服务地址或正文。

## 找表单、看结构

```bash
kd forms --tree                     # 云 > 子系统 > 表单（等同系统「在线测试WebAPI」页左侧目录）
kd forms -k 订单 -t bill            # 搜索；-t bill,base,report
kd fields 销售订单                   # 全部实体和字段（表头、分录、子分录），中文名、引用表单
kd fields 销售订单 --ops             # 支持的操作，标出只读操作
```

## 附件

```bash
kd attachments PUR_PurchaseOrder --number PO0001   # 单据的附件
kd download <附件ID> -O 文件名.pdf                          # 按附件ID或 FileId 下载（分块）
```

测试环境下载会被服务器以「跨目录访问」拦截，正式环境应可用。

## 报表

报表 = 列名（`-f`）+ 过滤条件（`-d '{"Model":{...}}'`）。

```bash
kd report 科目余额表 --schemes                  # 已保存的过滤方案
kd report 科目余额表 -f FBALANCEID,FBALANCENAME,FENDDEBIT,FENDCREDIT -d '{"Model":{"FACCTBOOKID":{"FNumber":"001"},"FCURRENCY":"1","FSTARTYEAR":"2026","FSTARTPERIOD":"1","FENDYEAR":"2026","FENDPERIOD":"8","FBALANCELEVEL":"1","FNOTPOSTVOUCHER":"true"}}'
```

- 只支持金蝶的「简单账表」。
- **列名**：有人在网页里给该报表保存过过滤方案时，kd 会自动读出列名，否则要自己给 `-f`。
- **过滤条件**：在系统「在线测试WebAPI」页面里，该报表「查询报表数据」样例的 `Model` 就是模板。这两样金蝶都不通过 API 提供。

## 盘点所有查询入口

```bash
kd scan            # 扫描全部单据+基础资料：能否读取、各有多少行
kd check           # 对 scan 中有数据的每个表单，逐个实体（表头+各分录）真实查询一次，并缓存可用字段
kd check --include-empty   # 连当前没数据的表单也检查
kd check 销售订单 物料      # 只检查指定表单（结果合并进已有结果）
```

结果在 `~/.config/kdcli/cache/<profile>/scan.json`、`check.json`。

## 敏感信息

密码和 AppSecret 交互输入时不回显。`config show` 会隐藏这两项，但仍包含服务地址、账号和数据中心信息。
查询结果、导出文件、下载的附件及错误信息可能包含业务数据，分享前请检查并脱敏。
本地配置和会话请保存在默认的 `~/.config/kdcli/`，不要提交到 Git；自定义配置或导出目录也应加入忽略规则。
