# 桂航绯想天 AstrBot 插件

对接 [GuatSWR Website](https://github.com/guatswr/GuatSWR_Website) 提供的 `/api/bot/v1/*`，在 QQ 中查询网站内容、绑定身份、报名、审核、订阅通知，并接收网站发起的完整群成员采集任务。

插件使用 **AstrBot 4.5+ 的 OneBot v11（aiocqhttp）适配器**。每份配置固定一个平台实例和一个网站 Bot 凭据。当前为 v0.1.0，已完成本地模拟接口验证，尚未进行真实 QQ / 生产站点联调。

## 目录

- [安装](#安装)
- [配置与接线](#配置与接线)
- [指令](#指令)
- [权限](#权限)
- [通知与故障恢复](#通知与故障恢复)
- [成员同步](#成员同步)
- [开发与验证](#开发与验证)

## 安装

1. 在 AstrBot 插件管理中使用[插件仓库地址](https://github.com/guatswr/astrbot_plugin_guatswr.git)安装。手动安装时，将整个 `astrbot_plugin_guatswr` 文件夹复制到 AstrBot 的 `data/plugins/` 下，保留这个文件夹名。该目录的直接子文件应包含 `main.py`、`metadata.yaml` 和 `_conf_schema.json`。
2. 在 AstrBot 使用的 Python 环境安装 `requirements.txt`，然后重启或重载插件。插件依赖 aiohttp 和 Pillow；不需要启动网站前端，也不需要安装本仓库的 Node 依赖。
3. 在 AstrBot 插件配置中填写下表，最后打开 `enabled` 并重载插件。默认关闭时不监听端口，不请求网站，不发送消息。

在 AstrBot 根目录安装依赖：

```bash
python -m pip install -r data/plugins/astrbot_plugin_guatswr/requirements.txt
```

也可将本仓库压缩后从 AstrBot 管理面板上传安装。本仓库的插件入口直接位于根目录，无需下载配套网站工程。

## 配置与接线

| 配置 | 填写方式 |
| --- | --- |
| `enabled` | 完成配置后设为 `true` |
| `site_url` | 公开网站 origin，默认 `https://guatswr.top`，用于消息链接 |
| `api_origin` | 插件可达的后端 origin，不带 `/api/bot`；留空时使用 `site_url` |
| `api_token` | 网站 `/admin/bot?section=credentials` 创建的 `gwb_…` 服务凭据 |
| `credential_id` | 同一凭据的 UUID；不是 QQ 号 |
| `platform_id` | AstrBot 消息平台的实例 ID，类型须为 `aiocqhttp`；不是机器人 QQ 号 |
| `allowed_groups` | QQ 群号字符串列表；网站凭据也须授权对应群号 |
| `webhook_token` | 与网站环境变量 `ASTRBOT_TOKEN` 相同的独立出站 Token |
| `webhook_host` / `webhook_port` | 默认 `127.0.0.1:9245` |
| `legacy_groups` | 旧版无 `target` 通知的默认接收群，默认空；须属于 `allowed_groups` |
| `asset_origin` | 留空使用 `site_url`；其他图片 origin 须与网站 `BOT_PUBLIC_ASSET_BASE_URL` 相同 |
| `max_broadcast_images` | 历史上的今天最多发送的图片数，默认 6，范围 0–20 |
| `sync_avatars` | 是否匿名下载并回传 QQ 头像，默认开启 |

插件固定接收路径为 **`POST /guatswr/webhook`**，使用 `Authorization: Bearer <webhook_token>`。在网站端配置 `ASTRBOT_WEBHOOK_URL` 指向网站服务器实际可达的这个地址：

```dotenv
# 网站后端与 AstrBot 位于同一宿主网络时的示例
BOT_PLATFORM_ENABLED=true
ASTRBOT_WEBHOOK_URL=http://127.0.0.1:9245/guatswr/webhook
# ASTRBOT_TOKEN 通过私有环境配置填写，与插件 webhook_token 一致
```

容器之间的 `127.0.0.1` 不互通：按实际网络填写 AstrBot 服务名，并将插件 `webhook_host` 配为 `0.0.0.0`。跨主机可通过 HTTPS 反向代理暴露上述固定路径，保留 Authorization 请求头并限制来源。此监听器独立于 AstrBot 管理面板端口。

**配套网站当前 Compose 中，后端 3000 端口仅在容器网络开放，公开 Nuxt BFF 不代理 `/api/bot/v1/*`。** 因此常见配置是让 AstrBot 加入网站应用网络，将 `api_origin` 填为 `http://backend:3000`，`site_url` 保持公开网站地址。外部部署须另行提供受控的 Bot v1 代理地址；只填写公开网站域名通常不能调用 Bot API。图片下载继续使用公开 `asset_origin`，不会向资源服务器携带 API Token。

`api_token`、`webhook_token`、网站 JWT 用途不同。插件不会使用网站 JWT 或创建管理凭据。配置中的两个 Token 在面板中遮蔽，但配置文件仍是私有数据，应与 AstrBot 数据目录一起保护。

后端启用和数据库升级要求见随仓库附带的 [Bot API](docs/BOT-API.md) 和 [成员同步协议](docs/MEMBER-SYNC.md)，文档来源及版本见 [SOURCES](docs/SOURCES.md)。本插件不会执行迁移、创建凭据或修改站长的同步配置。仅查询时仍需配置插件所需的接线字段，但可以只授予 `query:read`，不创建订阅、不启用网站成员采集。

## 指令

以下示例使用 AstrBot 的 `/` 唤醒前缀；其他前缀按 AstrBot 配置替换。输入 `/桂航 帮助` 可查看指令表。含空格的参数使用英文双引号。

| 指令 | 用途 |
| --- | --- |
| `/桂航 活动 [页码] [类型]` | 公开活动列表，例如 `/桂航 活动 1 STG` |
| `/桂航 详情 活动ID` | 活动规则、内容和赛果 |
| `/桂航 文章 [页码]` | 已发布文章摘要 |
| `/桂航 成员 [页码]` | 公开成员资料 |
| `/桂航 群史 [页码]` | 群史摘要 |
| `/桂航 今天 [YYYY-MM-DD] [all或articles或history] [页码]` | 历史上的今天，默认日期由后端按北京时间确定 |
| `/桂航 绑定 绑定码` | 私聊，将当前 QQ 绑定到网站发放绑定码对应的身份 |
| `/桂航 报名 活动ID "常用ID" /uploads/头像路径 "机签" "来源"` | 私聊，报名或修改自己的报名；后两项可省略 |
| `/桂航 我的报名 活动ID` | 私聊，查询自己的报名 |
| `/桂航 取消报名 活动ID` | 私聊，取消自己的报名 |
| `/桂航 待审 [页码]` | 私聊，绑定网站管理员身份后查看待审活动 |
| `/桂航 审核 活动ID approve或reject "理由"` | 私聊，批准或驳回活动，理由必填 |
| `/桂航 订阅 事件名 [活动ID或0] [提前分钟]` | 当前群创建或更新订阅，提前量默认 60 分钟 |
| `/桂航 订阅列表` | 当前群的订阅 |
| `/桂航 退订 订阅UUID` | 删除当前群的指定订阅 |
| `/桂航 投递 [页码]` | 管理员私聊，网站投递记录 |
| `/桂航 重试 投递UUID` | 管理员私聊，重试网站 failed/skipped 投递 |
| `/桂航 队列` | 管理员私聊，本地队列各状态数量 |
| `/桂航 本地失败 [页码]` | 管理员私聊，本地失败投递 ID 与固定错误代码 |
| `/桂航 本地重试 投递ID` | 管理员私聊，重新处理本地失败的群通知 |

查询列表每页 10 条。历史上的今天只播报当前页，有下一页时会提示；图片按独立图片段发送，下载和解码失败时继续其他图片。活动文章使用后端提供的封面，不扫描正文或相册。当前插件按指令播报历史上的今天，未添加每日定时群发。

报名需要先绑定网站身份，并提供网站上已经存在的有效 `/uploads/` 头像。当前 Bot API 没有报名头像上传入口，插件不会把 QQ 头像 URL 当作报名头像，也不会自行申请绑定码。网站的截止时间、活动类型及报名权限继续由后端校验。

## 权限

| 功能 | 网站凭据 scope | 插件侧限制 |
| --- | --- | --- |
| 公开查询、历史上的今天 | `query:read` | 配置的平台；群聊须在允许群中 |
| 身份绑定 | `identity:write` | 仅私聊，QQ 来自消息发送者 |
| 查询自己的报名 | `signup:read` | 仅私聊，QQ 来自消息发送者 |
| 报名、取消 | `signup:write` | 仅私聊，QQ 来自消息发送者 |
| 待审、审核 | `review:read` / `review:write` | 仅私聊，由网站核验当前 QQ 绑定账号的管理员角色 |
| 订阅管理 | `subscriptions:write` | AstrBot 管理员，在当前群操作 |
| 网站投递查询、重试 | `deliveries:read` / `deliveries:retry` | AstrBot 管理员，仅私聊 |
| 网站成员采集命令 | `members:sync` | Webhook 鉴权、凭据 ID 和群号三项校验 |
| 管理员资料私信回执 | `deliveries:ack` | 只发送给网站通知明确指定的私聊目标 |

所有身份类操作都使用消息上下文中的 QQ，不接受额外 QQ 参数。群管理员身份不等于网站管理员，也不等于 AstrBot 管理员。订阅 `event.pending` / `event.rejected` 额外需要 `review:read`；`event.signup` / `event.signup_cancelled` 额外需要 `signup:read`，宜仅订阅到管理群。

事件名沿用后端：[完整列表及类型契约快照](docs/contracts/bot.ts)。例如在目标群执行 `/桂航 订阅 reminder.start 12 60`，订阅活动 12 开始前 60 分钟提醒；使用 0 表示全部活动。插件不自行计算提醒时间，网站统一调度。

## 通知与故障恢复

Webhook 验证通过后，先写入 SQLite 并提交，再返回 202。独立 worker 负责发送，按 `delivery_id` 持久去重；相同 ID 携带不同内容会被拒绝。无 ID 的旧三类公开通知用完整消息摘要兼容去重，只转发到显式配置的 `legacy_groups`。

群通知、管理员私信、成员采集分别处理；采集命令不会发到群里。所有标题、昵称和名片均作为纯文本消息段发送，`[CQ:…]` 不会作为消息控制码执行。私信收到 OneBot 的 `message_id` 后先保存发送检查点，再提交 sent 回执；回执接口暂时不可用时只重试回执。

临时错误最多自动处理 5 次，间隔按指数递增，常规间隔上限 300 秒。失败回执暂时无法回传时继续后台重试回执。重启会恢复未完成任务。已完成任务清除消息正文和成员快照，保留 ID 与摘要用于去重；失败任务保留恢复数据。运行数据位于 AstrBot 的 `data/plugin_data/astrbot_plugin_guatswr/inbox.sqlite3`，请持久挂载并按需备份。当前不自动清理历史 ID，需监控文件大小；同一数据目录只运行一个插件实例。

网站投递 sent 表示 Webhook 已可靠接收。群消息发送失败时，网站可能仍显示 sent，此时使用 `/桂航 本地失败` 和 `/桂航 本地重试 ID`。私信失败由插件提交 failed 回执，随后从网站后台或 `/桂航 重试 ID` 重试，网站会重置回执状态。成员采集失败从网站重新发起新任务。已成功的任务不能通过本地重试重复发送。

平台已发送、但保存检查点前发生进程崩溃，或平台确认在网络中丢失时，仍可能出现重复消息。此插件提供至少一次处理和持久去重，不承诺 QQ 端 exactly-once。

## 成员同步

网站下发 `members.sync.request` 后，插件先读取任务，核对凭据、群号和期限，再通过固定 OneBot 实例调用 `get_group_member_list`。采集前后分别读取群人数，只有两次人数都与列表长度相同、QQ 唯一、群号一致时才视作完整快照。网关必须提供真实、完整的成员列表和人数；不支持分页或截断列表冒充完整结果。

首次取得的快照持久保存，按任务限制分批 PUT，同一批次重试保持相同内容。空群也会上传一个空批次，由网站决定是否需要核验。头像在名单批次成功后上传，仅访问固定 QQ 头像服务；已知 SHA-256 未变化时跳过上传。单头像失败保留网站旧图，接近采集期限时优先提交完整名单。

完成后由网站决定 `preview`、`blocked` 或 `succeeded`。首次导入、人数骤减、旧档案关联、允许保留未绑定记录等仍在网站后台处理，插件不会自动执行人工应用接口。管理员资料提醒只发送到后端明确指定的 QQ 私聊。

## 开发与验证

模块划分：`main.py` 为 AstrBot 入口；`commands.py` 处理指令；`client.py` 处理 API 和匿名图片下载；`service.py` 处理接收和同步；`inbox.py` 保存去重与进度；`onebot.py` 调用 QQ 适配器；`protocol.py` 校验契约。

在本插件仓库根目录运行（文件夹名保持 `astrbot_plugin_guatswr`）：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -t .. -v
python -m compileall -q .
```

测试使用本地 HTTP 服务、临时 SQLite 和模拟 OneBot，覆盖鉴权、Bearer 隔离、重复投递、进度恢复、部分名单拒绝、不可变批次、头像校验、身份防冒用及群订阅权限。不会调用生产 API、升级数据库或向真实群发消息。后端事件名契约测试读取本仓库附带的快照，不依赖网站工程目录；后端接口升级时应同步更新快照。

开发接口参考：[AstrBot 插件开发](https://github.com/AstrBotDevs/AstrBot/tree/master/docs/en/dev/star)、[官方 Context 源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/context.py)、[配套网站 Bot API](docs/BOT-API.md)、[机器契约](docs/bot-openapi.yaml)。正式启用前仍需在测试群完成 OneBot 网关、群列表完整性、好友私信和反向代理的联调。
