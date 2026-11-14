# 接口文档来源

插件于 2026-11-14 从 GuatSWR Website 拆分为独立仓库，随迁移保留配套网站的接口文档和契约快照，便于离线查阅和独立运行测试。

来源仓库：[guatswr/GuatSWR_Website](https://github.com/guatswr/GuatSWR_Website)。来源工作区基线提交：`7c2b6306d2e172400c2e3673c41460149c7f1119`。说明文档包含迁移当日工作区的插件入口说明，拆分后仅调整相对链接及来源提示；API 契约文件和类型文件保持原始内容。

| 本仓库文件 | 网站来源 |
| --- | --- |
| [BOT-API.md](BOT-API.md) | [backend/docs/BOT-API.md](https://github.com/guatswr/GuatSWR_Website/blob/7c2b6306d2e172400c2e3673c41460149c7f1119/backend/docs/BOT-API.md) |
| [MEMBER-SYNC.md](MEMBER-SYNC.md) | [backend/docs/MEMBER-SYNC.md](https://github.com/guatswr/GuatSWR_Website/blob/7c2b6306d2e172400c2e3673c41460149c7f1119/backend/docs/MEMBER-SYNC.md) |
| [bot-openapi.yaml](bot-openapi.yaml) | [backend/docs/bot-openapi.yaml](https://github.com/guatswr/GuatSWR_Website/blob/7c2b6306d2e172400c2e3673c41460149c7f1119/backend/docs/bot-openapi.yaml) |
| [contracts/bot.ts](contracts/bot.ts) | [packages/shared/src/bot.ts](https://github.com/guatswr/GuatSWR_Website/blob/7c2b6306d2e172400c2e3673c41460149c7f1119/packages/shared/src/bot.ts) |

原始契约文件 SHA-256：

```text
bot-openapi.yaml  25e272dec1290f2c508918e45f3257779b33fa3e54735627b61abce34e96e6f4
contracts/bot.ts  428271406eab3070acffd61c3ae7cc32ea8422502d374298e3e701bbcc7ad871
```

`contracts/bot.ts` 是后端 Zod 类型契约的参考快照，插件运行与测试均不执行 TypeScript，也不需要安装 zod。事件名契约测试直接读取该快照，独立检查插件支持的事件集合。

接口升级时，从已核验的网站版本更新上述快照，保留文档中指向配套网站的迁移与证据链接，并在此记录新来源提交和摘要。然后从插件仓库根目录运行：

```bash
python -m unittest discover -s tests -t .. -v
```

后端运行时还提供 `/api/bot/docs/openapi.yaml`；实际启用条件、权限和数据库迁移应以部署版本的后端为准。插件安装说明见 [README](../README.md)。
