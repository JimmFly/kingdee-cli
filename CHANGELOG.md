# 更新日志

## 0.1.0

第一个公开版本。

- 覆盖金蝶云星空全部读取类 WebAPI：ExecuteBillQuery、BillQuery、View、QueryBusinessInfo、QueryGroupInfo、GetSysReportData、AttachmentDownLoad、GetDataCenterList。
- 写接口在客户端拒绝。
- 表单可用中文名；不写字段时返回全部表头字段；服务器不支持的字段自动剔除（包括二分定位 SQL 列错误）。
- 报表自动使用已保存过滤方案中的列名。
- agent 模式：非终端时默认 JSON 输出，结构化错误和退出码。
- 自动节流和限流重试（服务器每分钟 500 次），会话过期自动重登。
- `scan` / `check` / `export` 用于盘点和批量导出。
- 隐私：`-v` 不打印服务地址和请求/响应正文；错误信息中的服务地址替换为 `<server>`；密码输入不回显。
- Windows 下管道输出统一用 UTF-8。
