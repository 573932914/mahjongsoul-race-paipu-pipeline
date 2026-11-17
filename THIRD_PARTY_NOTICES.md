# 第三方组件与数据来源

本项目不复制上述项目的源代码，仅通过 HTTP/WebSocket 协议或 PyPI 依赖使用；
以下注明来源与许可，便于后续维护。

| 组件 | 用途 | 许可 / 说明 |
|------|------|-------------|
| [ninkjin/Majsoul-Mortal-Analyzer](https://github.com/ninkjin/Majsoul-Mortal-Analyzer) | `relay_client.py` 的中转协议（`/api/v1/desktop/requests`、`/api/v1/requests/<id>/result`）参考；`majsoul_local.py` 的登录流程参考 | 仓库 GPL-3.0；仅参考协议字段与调用顺序 |
| [ninklang.tech](https://ninklang.tech/) | 公开雀魂牌谱中转服务（代取 + 转 Tenhou 6 JSON） | 第三方服务，按 IP 限流；结果保留 7 天 |
| [MahjongRepository/mahjong_soul_api](https://github.com/MahjongRepository/mahjong_soul_api)（PyPI `ms-api`） | 雀魂 WebSocket / protobuf 协议 | MIT |
| [Equim-chan/tensoul](https://github.com/Equim-chan/tensoul) / [tensoul-py](https://pypi.org/project/tensoul/) | 雀魂牌谱 → tenhou.net/6 JSON | MIT |
| [ricochet.cn NAGA 助手](https://ricochet.cn/riichi/naga) | NAGA 投递与报告代理 | 第三方服务；需要 ricochet token，按 NP 计费 |
| [大凤林 000.mk](https://www.000.mk/?cid=220#!ranking) | 赛事 / 对阵 / 比分数据接口 | 赛事平台公开接口 |
| [Fat-pig-Cui/misc-code](https://github.com/Fat-pig-Cui/misc-code) | 匿名牌谱 UUID 编解码参考（当前未使用） | 参考 |

`data/` 下的牌谱 JSON 由第三方中转/雀魂接口生成，仅供本队复盘分析使用；
请遵守雀魂与大凤林的服务条款，不要高频抓取或公开分发。
