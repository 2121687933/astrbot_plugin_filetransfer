# 文件传输（astrbot_plugin_filetransfer）

让跑在服务器上的 AstrBot 把服务器里的文件发到 QQ 群或私聊，也能反过来把 QQ 里收到的文件存到服务器。

## 能干什么

- 服务器 → QQ：发单个文件、多个文件、整个目录（自动打 zip）
- 服务器 → 别的群 / 别的 QQ：`/转` 直接转发
- QQ → 服务器：把别人发的文件存进收件箱，按 id 再取出来
- 发之前可以先 `/目录` 看一眼，用序号取文件，不用手打长路径

## 安装

1. 把 `astrbot_plugin_filetransfer` 整个文件夹放进 AstrBot 的 `data/plugins/` 目录。
2. 面板里重启 AstrBot，或点插件页的刷新。
3. 依赖只有 `aiohttp`，AstrBot 自带，一般不用额外装。
4. 打开插件配置，至少填一项 `allow_dirs`，否则什么文件都发不出去。

Windows 上打包上传的话，把文件夹压成 zip 直接丢到面板的插件管理里也能装。

## 命令

| 命令 | 别名 | 作用 |
| --- | --- | --- |
| `/fileget <路径\|序号>` | `/取` | 把服务器上的文件发到当前会话 |
| `/filelist [路径]` | `/目录` | 列目录，带序号 |
| `/filezip <路径…>` | `/打包` | 目录或一堆文件打成 zip 再发 |
| `/filerelay <群号\|@某人> <路径…>` | `/转` | 发到别的群或别人的 QQ |
| `/filestore [新名字]` | `/存` | 把这条消息带的文件存到服务器 |
| `/fileinbox` | `/收件箱` | 看最近存下来的文件 |
| `/fileinfo <路径>` | `/文件信息` | 大小、修改时间、sha256 |
| `/filehelp` | `/文件帮助` | 帮助和当前配置摘要 |

几个用法细节：

```
/取 /root/data/report.pdf          # 绝对路径
/取 ./logs/app.log                 # 相对 allow_dirs 里的目录
/取 3                              # 取上一次 /目录 列出来的第 3 项
/取 "C:/tmp/a b.txt"               # 路径带空格用引号包起来
/目录 /root/data                   # 列目录
/打包 /root/data/logs              # 目录打 zip
/转 123456789 /root/data/report.pdf   # 发到指定群
/转 @张三 /root/data/report.pdf       # 发给被 @ 的人
```

一次最多发 10 个文件。

## 配置说明

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `allow_dirs` | 空 | 允许发送的目录白名单，一行一个 |
| `allow_all_paths` | 关 | 放开整个文件系统（敏感文件仍然拦） |
| `admin_only` | 开 | 只有 AstrBot 管理员能用 |
| `allowed_users` | 空 | 额外放行的 QQ 号 |
| `allowed_groups` | 空 | 额外放行的群号（群里所有人都能用） |
| `send_mode` | auto | 发送通道，见下 |
| `onebot_http_url` | 空 | OneBot 的 HTTP 地址，如 `http://127.0.0.1:3000` |
| `onebot_token` | 空 | OneBot 鉴权 token |
| `force_base64` | 关 | 强制走 base64 内联（上限 20MB） |
| `enable_receive` | 开 | 允许 `/存` 收文件 |
| `max_file_mb` | 100 | 单文件发送上限 |
| `max_save_mb` | 200 | 接收单文件上限 |
| `max_zip_mb` | 200 | 打包后上限 |
| `list_limit` | 60 | `/目录` 最多列几项 |
| `rate_limit_per_min` | 10 | 每分钟最多操作次数，0 = 不限 |
| `keep_files` | 500 | 收件箱保留多少个文件 |

### 发送通道怎么选

- `auto`（默认）：先试 OneBot 的上传接口，不行再试 AstrBot 内核的文件消息段。绝大多数情况用这个就好。
- `onebot`：只走 NapCat / Lagrange 的 `upload_group_file` / `upload_private_file`。最稳，推荐 QQ 用户用。
- `native`：只走 AstrBot 的 `File` 消息段，适合 Telegram 之类。
- `base64`：把文件内容编码塞进消息里。**AstrBot 和 QQ 端不在同一台机器**时用这个，单个文件限 20MB。
- `http`：直接 HTTP 调 OneBot 接口，需要填 `onebot_http_url`。

### NapCat / SnowLuma 双协议端

NapCat 和 SnowLuma 都对外暴露 OneBot v11，AstrBot 都是通过 aiocqhttp 适配器接的，所以插件不用区分，`auto` 或 `onebot` 模式直接就能用，也不用配 `onebot_http_url`。

几个要注意的地方：

- 协议端和 AstrBot 在同一台机器上时，`upload_group_file` 收到的本地路径双方都读得到，最省事。分机部署才需要开 `force_base64`。
- SnowLuma 的群文件默认走群永久文件空间，容量满了它会自动转成临时存储，客户端那边看是「临时文件」，过期会失效。真发失败了回执里会带原因。
- `/转` 用的是**当前这条消息所属的那个 QQ 号**去发。两个协议端挂不同 QQ 的话，目标群得让这个号也在里面，否则发不进去。
- 两个协议端同时在跑的时候，尽量只给其中一个接 AstrBot，避免同一个群收到两遍。

## 安全设计

默认是「什么都不给」：

- 只读 `allow_dirs` 里的目录，插件自己的数据目录除外
- `..` 穿越和符号链接都会先 resolve 再校验，绕不过去
- 敏感文件和目录直接拒绝：`.env`、`*.pem`、`*.key`、`id_rsa`、`authorized_keys`、`*secret*`、`*token*`、`.ssh`、`.git`、`node_modules` 等
- 单个文件有大小上限，防止把机器人拖死
- 有频率限制，防止被刷

想放开的话建议只加目录，不要开 `allow_all_paths`。

## 排错

**发不出去，提示没有可用通道**

看日志里 `deliver` 那几行，会列出每个策略失败的原因。最常见的是：

1. `allow_dirs` 没配，或者路径不在这几个目录里
2. 机器人不是管理员 → 把 QQ 号填进 `allowed_users`，或者关掉 `admin_only`
3. NapCat 端没有文件上传权限 → 群文件需要机器人在群里有上传权限

**能收到文件但打不开 / 是 0 字节**

NapCat 的 `upload_group_file` 需要它能读到那个本地路径。如果 AstrBot 和 NapCat 不在同一台机器，用 `base64` 模式或配 `onebot_http_url`。

**私聊发文件失败**

QQ 私聊发文件需要双方是好友，而且部分协议端对私聊文件支持不好。可以先在群里试，确认整条链路通。

**中文路径乱码**

服务器 locale 不是 UTF-8 时会这样，启动 AstrBot 时设 `LANG=C.UTF-8`。

**回执里写「文件空间不足」或者一堆 retcode**

群文件的容量或者文件数量到上限了。SnowLuma 会自动试一次临时存储，还不成就是真发不了。删点旧群文件，或者换 `base64` 模式走聊天消息。

## 目录结构

```
astrbot_plugin_filetransfer/
├── main.py                 插件入口和命令
├── metadata.yaml           插件元信息
├── _conf_schema.json       面板配置项
├── requirements.txt        依赖
├── selfcheck.py            离线自检（不依赖 AstrBot）
├── smoke_test.py           用桩模块模拟 AstrBot，验证命令注册和收发
└── transfer/
    ├── paths.py            路径校验、打包、列目录、哈希
    ├── store.py            收件箱索引
    ├── senders.py          发送策略
    └── receiver.py         接收文件
```

改完代码可以跑一下测试：

```bash
cd astrbot_plugin_filetransfer
python selfcheck.py     # 路径、打包、索引这些本地逻辑
python smoke_test.py    # 模拟 AstrBot 加载，验证命令和收发
```

两个都不需要装 AstrBot。`smoke_test.py` 会用临时目录造一个假的 AstrBot，把插件真正
import 进来，检查命令有没有注册上、各类发送策略传的参数对不对、收到的文件能不能存下来。
