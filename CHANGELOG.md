# 更新日志

## v1.1.0

- 新增 QQ 官方机器人（`qq_official`）支持，自动走内核文件消息段、按被动回复发送
- 修复：`context.send_message` 在 qq_official 上不可用，改为优先 `event.send()`、失败再退主动发送
- `current_target` 支持从 `unified_msg_origin` 兜底解析群号 / 用户 openid
- 失败回执里补充 QQ 官方机器人的权限排查提示

## v1.0.0

- 首个版本：取文件、看目录、打包、跨会话转发、收件箱
- 四条发送通道自动兜底：OneBot 扩展接口 → 内核文件消息段 → base64 内联 → 直连 OneBot HTTP
- 路径白名单、敏感文件拦截、大小与频率限制
