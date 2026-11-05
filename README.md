# kingdee-cli（`kd`）

[![test](https://github.com/JimmFly/kingdee-cli/actions/workflows/test.yml/badge.svg)](https://github.com/JimmFly/kingdee-cli/actions/workflows/test.yml)

金蝶云星空（K/3 Cloud）WebAPI 的**只读**命令行工具，适合人和 AI agent 调用。

- **只读**：只调用读取类接口，保存、提交、审核、删除等写接口在客户端直接拒绝，不会改动任何数据。
- **覆盖所有读取接口**：单据查询、查看、元数据、分组、简单账表、附件下载、数据中心列表。
- **好查**：表单可以写中文名；不写字段就返回全部表头字段；服务器不支持的字段自动跳过。
- **适合 agent**：被程序调用时默认输出 JSON，错误也是 JSON，并用退出码区分错误类别。
- **零依赖**：单个 Python 文件，只用标准库，支持 Python 3.10 及以上。

> 本项目与金蝶公司无关，不是金蝶官方产品。「金蝶」「金蝶云星空」是其各自所有者的商标。

## 安装

```bash
pipx install git+https://github.com/JimmFly/kingdee-cli.git   # 推荐，装好后有 kd 命令
# 或
pip install git+https://github.com/JimmFly/kingdee-cli.git
```

也可以直接下载 [`kd.py`](kd.py)，用 `python3 kd.py ...` 运行。

## 快速开始

```bash
KD_URL=https://你的租户.ik3cloud.com/k3cloud/ kd dbs   # 1. 列出数据中心，拿到 acct_id（这一步不需要登录）
kd config init                                         # 2. 填服务地址、acct_id、用户名和认证信息
kd login                                               # 3. 登录测试
kd query 销售订单 -n 5                                  # 4. 开始查询
```

## 配置

`kd config init` 会把配置写到 `~/.config/kdcli/config.json`（权限 600）。密码和 AppSecret 输入时不回显。

**认证方式**（二选一）：

| 方式 | 配置项 | 说明 |
|---|---|---|
| 第三方应用授权（推荐） | `app_id` + `app_secret` | 在金蝶「第三方系统登录授权」中申请，调用 `LoginByAppSecret` |
| 账号密码 | `password` | 调用 `ValidateUser` |

**多环境**：`kd -p prod config init` 新建一个 profile，`kd config use prod` 切换默认，`kd config show` 查看（密钥打码）。

**环境变量**（优先级高于配置文件）：

| 变量 | 作用 |
|---|---|
| `KD_URL` `KD_ACCT_ID` `KD_USERNAME` `KD_PASSWORD` `KD_APP_ID` `KD_APP_SECRET` `KD_LCID` | 对应配置项 |
| `KD_PROFILE` | 使用哪个 profile |
| `KD_FORMAT` | 默认输出格式：`table` / `json` / `csv` |
| `KD_RATE` | 每分钟最多调用次数，默认 450 |
| `KD_CONFIG_DIR` | 配置和缓存目录，默认 `~/.config/kdcli` |

## 命令一览

| 命令 | 对应接口 | 作用 |
|---|---|---|
| `kd forms` | ExecuteBillQuery | 列出系统里的所有表单（单据、基础资料、报表），可按云/子系统树状显示 |
| `kd fields <表单>` | QueryBusinessInfo | 表单的实体和字段（中文名、引用表单）；`--ops` 看支持的操作 |
| `kd query <表单>` | ExecuteBillQuery | 查询数据 |
| `kd view <表单>` | View | 按编号或内码查看单条完整数据（含所有分录） |
| `kd groups <表单>` | QueryGroupInfo | 基础资料分组树，如物料分组 |
| `kd report <报表>` | GetSysReportData | 简单账表数据 |
| `kd attachments [表单]` | ExecuteBillQuery | 附件列表 |
| `kd download <附件>` | AttachmentDownLoad | 分块下载附件 |
| `kd dbs` | GetDataCenterList | 数据中心列表 |
| `kd call <接口>` | 白名单内任意读接口 | 直接调用，参数自己传 |
| `kd scan` / `kd check` / `kd export` | — | 盘点所有表单 / 验证所有查询入口 / 批量导出 |

通用参数：`-o table|json|csv` 输出格式，`-q` 不输出提示信息，`-v` 打印接口名和请求/响应大小（不打印地址和正文），`-p` 指定 profile。

## 查询

```bash
kd query 销售订单 -n 10                                    # 不写 -f = 全部表头字段
kd query 销售订单 -f '*FSaleOrderEntry' -a -o csv > 明细.csv  # 某个分录的全部字段、全部行
kd query 物料 -f FNumber,FName,FSpecification -w "FDocumentStatus='C'"
kd query SAL_SaleOrder -f FBillNo,FCustId.FName,FMaterialId.FNumber,FQty \
    -w "FDate>='2025-01-01'" --order "FDate DESC"
kd view 物料 --number MAT0001
kd groups 物料
```

- 表单可以写 FormId，也可以写中文名。同名时优先单据；名字有歧义时会列出候选。
- 引用字段默认输出编码，用户字段输出姓名。也可以自己写 `FCustId.FName` 这样的引用属性。
- 服务器不支持的字段会自动跳过（`--show-skipped` 显示是哪些）。可用字段会缓存，之后直接查。
- `-n` 每页行数（默认 100），`--start` 偏移，`-a` 自动翻页取全部。

## 找表单、看结构

```bash
kd forms --tree                 # 云 > 子系统 > 表单
kd forms -k 订单 -t bill        # 搜索；-t bill,base,report
kd fields 销售订单               # 全部实体和字段（表头、分录、子分录）
kd fields 销售订单 --ops         # 支持的操作，标出只读操作
```

## 报表

报表需要两样东西：**列名**（`-f`）和**过滤条件**（`-d '{"Model":{...}}'`）。

```bash
kd report 科目余额表 --schemes   # 已保存的过滤方案
kd report 科目余额表 -f FBALANCEID,FBALANCENAME,FENDDEBIT,FENDCREDIT -d '{"Model":{
  "FACCTBOOKID":{"FNumber":"001"},"FCURRENCY":"1",
  "FSTARTYEAR":"2026","FSTARTPERIOD":"1","FENDYEAR":"2026","FENDPERIOD":"8",
  "FBALANCELEVEL":"1","FNOTPOSTVOUCHER":"true"}}'
```

- 金蝶只开放「简单账表」通过 WebAPI 查询，其他报表会返回「此接口暂时只支持简单账表」。
- **列名**：如果有人在金蝶网页里给这个报表保存过过滤方案，kd 会从方案里自动读出列名，`-f` 可以省略。
- **过滤条件**：在金蝶「在线测试WebAPI」页面找到该报表，「查询报表数据」样例里的 `Model` 就是模板。

## 附件

```bash
kd attachments PUR_PurchaseOrder --number PO0001   # 单据的附件
kd download <附件ID或FileId> -O 文件名.pdf
```

## 盘点与导出

```bash
kd scan                      # 扫描全部单据和基础资料：能否读取、各有多少行
kd check                     # 对有数据的表单，逐个实体真实查询一次，并缓存可用字段
kd check --include-empty     # 连当前没数据的表单也检查
kd export SAL_SaleOrder -d ./out   # 导出表单全部数据（表头和各分录分文件）
kd export --from-scan -d ./out     # 导出 scan 发现有数据的所有表单
```

结果在 `~/.config/kdcli/cache/<profile>/` 下。整库扫描要调用几千次接口，受限流影响会持续较长时间。

## 给 agent 用

见 [AGENTS.md](AGENTS.md)：调用约定、错误码、标准查询流程、过滤语法、报表示例和常用 FormId。

要点：stdout 不是终端时默认输出 JSON；失败时输出 `{"error": {"code", "message"}}`，退出码 3 为配置/登录问题，4 为业务错误或表单名歧义，5 为限流/网络，6 为写操作被拒。

## 限流与缓存

- 金蝶服务器限制每分钟 500 次 WebAPI 调用。kd 默认每分钟最多 450 次，被限流时按服务器提示的秒数等待后自动重试。
- 会话过期会自动重新登录。
- 表单目录、元数据、可用字段会缓存。系统新增了表单或字段后，用 `kd forms --refresh`、`kd fields <表单> --refresh` 刷新。

## 已知限制

- **报表**：只支持简单账表；列名和过滤条件金蝶都不通过 API 提供（见上文）。
- **附件下载**：部分环境会以「检测到存在跨目录访问场景」拒绝下载，这是服务器端的限制。
- **表单自有的业务操作**（如「下载PDF」「获取开票金额」）要通过 `ExcuteOperation` 执行，这个接口同样能执行写操作，所以不提供。
- 已在金蝶云星空公有云版（9.1）上用账号密码登录验证；AppID + AppSecret 登录按官方协议实现，欢迎反馈。

## 敏感信息

- `config show` 会隐藏密码和 AppSecret，但仍显示服务地址、账号和数据中心信息。
- `-v` 和错误信息不包含服务地址和请求正文。
- 查询结果、导出文件、下载的附件可能包含业务数据，分享前请检查并脱敏。
- 配置和会话保存在 `~/.config/kdcli/`，不要提交到 Git。

发现安全问题请看 [SECURITY.md](SECURITY.md)。

## 开发

```bash
python -m unittest discover -s tests -v   # 离线测试，不需要金蝶服务器
```

所有代码在 [`kd.py`](kd.py) 一个文件里。测试用假的服务器响应覆盖只读白名单、限流重试、会话重登、字段自动修复、名称解析和 agent 输出格式。改动请附带测试。

## 许可证

[MIT](LICENSE)
