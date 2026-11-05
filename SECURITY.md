# 安全说明

## 报告安全问题

请不要在公开 issue 里描述安全问题。请通过 GitHub 的 [Private vulnerability reporting](https://github.com/JimmFly/kingdee-cli/security/advisories/new) 私下报告。

报告时**不要附带**真实的服务地址、账号、密码、AppSecret、会话 cookie 或业务数据。用占位符说明复现步骤即可。

## 设计上的安全约束

- **只读**：`Client.call` 只放行 `READ_METHODS` 白名单内的接口，其他接口在发出请求前就被拒绝。绕过这条约束的改动都视为安全问题。
- **凭据**：配置文件 `~/.config/kdcli/config.json` 权限为 600；交互输入密码和 AppSecret 时不回显；`config show` 会打码。
- **日志**：`-v` 只打印接口名和请求/响应大小。
- **错误信息**：服务地址（URL 和主机名）会替换成 `<server>`，包括 TLS 证书错误、HTTP 错误正文和非 JSON 响应。服务器返回的业务报错会原样保留，可能包含业务数据。
- **会话**：会话 cookie 保存在 `~/.config/kdcli/session-<profile>-<摘要>.txt`，按「profile + 服务地址 + 账套 + 用户」隔离，原子写入，权限 600（已存在的宽权限文件会被收紧）。文件名只含摘要，不暴露身份信息。
- **重试**：登录只请求一次，避免认证失败被重复提交。

## 使用者需要注意

- kd 返回的是你账号有权限看到的业务数据。把输出交给 agent 或第三方服务之前，请确认符合你所在组织的数据规定。
- 建议给 kd 单独建一个只有查询权限的金蝶账号或第三方应用授权。
