# CLAUDE.md

本文件供 Claude Code / 其他编码代理在此仓库工作时参考。**操作流程以
`AGENTS.md` 为准**，这里只写项目结构、数据和改代码时的坑。

## 项目概述

- 数据源：大凤林 `https://cdn.r-mj.com/api/data.php?t=...&cid=220`
  （赛事「第十届全国立直麻将网络联赛-超级」，目标队伍「立石」）。
- 每轮比赛 10 个半庄，`c_data` 的 `rowi` 0..9 对应
  `admin.t_type` 的 10 行：先锋1/2、次锋1/2、中坚1/2、副将1/2、大将1/2。
- 每个半庄抓成雀魂牌谱，转 tenhou.net/6 JSON，写入 SQLite。

## 代码结构

| 文件 | 职责 |
|------|------|
| `config.py` | 路径、CID、队伍名、接口、限流参数；`config.local.json` 可覆盖 |
| `league_client.py` | 大凤林 admin/team/class/c_data，位置映射，日期/UUID 解析 |
| `relay_client.py` | ninklang.tech 公开中转：创建任务、轮询、下载 Tenhou JSON |
| `majsoul_local.py` | 可选：ms-api 直连雀魂 + tensoul 转换（本地账号） |
| `tenhou_meta.py` | 从 Tenhou JSON 提取四家姓名/开局/终局/得失/名次 |
| `db.py` | SQLite 建表、upsert、状态查询 |
| `pipeline.py` | 编排：赛程 -> 入库 -> 抓取 -> 回填 |
| `process_pairs.py` | 按本地 JSON 规则生成 X1/X2 点数修正后的 JSON |
| `naga_client.py` | ricochet.cn NAGA 投递（默认 2,4=ニシキ+カガシ）并回填链接 |
| `main.py` | CLI |

## 命令

```powershell
.\.venv\Scripts\python.exe main.py update
.\.venv\Scripts\python.exe main.py update --no-fetch
.\.venv\Scripts\python.exe main.py fetch --limit 3
.\.venv\Scripts\python.exe main.py fetch --mode local
.\.venv\Scripts\python.exe main.py status
.\.venv\Scripts\python.exe main.py report --round 3
.\.venv\Scripts\python.exe main.py process
.\.venv\Scripts\python.exe main.py verify-processed
.\.venv\Scripts\python.exe main.py naga --id 1
.\.venv\Scripts\python.exe main.py naga-batch --round 3 --player-types 2,4,6
```

## 易错点

- **每轮 10 个半庄是连续赛程**：第 1 个半庄开局 100000（`admin.c_s_po`），
  之后每个半庄的开局 = 上一个半庄的终局（座位轮转），**不是每局重置**。
  `log[0][1]` 是该半庄四家开局点；`c_data.pint` 是该半庄得失，
  `start = num - pint`。`games.start_points` 只是规则值 100000。
- **Tenhou JSON 顶层 `sc` 是 `[终局, 得失/1000] × 4`**（中转/tensoul 的输出），
  不是 `[开局, 终局]`。不要拿 `sc` 当前两列算开局。
- **逐局 delta 累加会多出立直棒**：终局优先用 `sc`，其次用 `c_data.num`。
- **`rowi` 是位置槽位，不是半庄内局数**。`position_label()` 生成「先锋1」等。
- **处理后 JSON 规则**（本地规则文档不入库 / `process_pairs.py`）：
  - X1（rowi 0/2/4/6）：每一小局 `log[i][1] = [100000]*4`；
  - X2（rowi 3/5/7）：首局 = 100000 + 对应 X1 原始得失，整份 X2 加同一偏移；
  - 先锋2（rowi 1）、大将1/2（rowi 8/9）：不调整；
  - 供托 `seed[2]` 保持原样；顶层 `sc` 保持原样；
  - 替补按“姓名 → X2 原首局 == X1 终局”映射到继承座位。
- **NAGA 投递**：`naga_client.py` 用 ricochet token（`data/ricochet_token.txt`
  或 `--token-file` / `RICOCHET_TOKEN`）调 `/api/naga/analyze_majsoul_step2`，
  `text=https://tenhou.net/6/#json=<JSON>`，`player_types="2,4"`
  （2=ニシキ, 4=カガシ），`id`=NAGA 账户 id。约 10NP×局数；
  成功后按四家姓名轮询报告并写 `games.naga_url`。token 不写日志。
  `naga-batch --round N` 批量处理整轮并跳过已有 `naga_url` 的场次；
  `--dry-run` 先估算 NP；类型 `2,4,6` = ニシキ+カガシ+ウロボロス。
  **全不聴流局转成 `["全員不聴",[0,0,0,0]]`**。NAGA 起点规则：
  - X1（处理后每局 100000）直接可用；
  - X2/大将用 **`--starts shift75`**（默认/推荐）：整份牌谱每人减 75000，
    首局和 100000（25000 + X1 得失），后续按实际得失滚动；实测成功，
    保留 X1 累积得失；
  - `--starts shift25`：首局平移到标准 **25000**，后续按实际得失滚动；
    实测可用但丢 X1 累积得失；
  - `--starts all`（每局 100000）也可用，但忽略局间得失；
  - `first`、`shift`（100000 平移）、单局非均匀开局都实测 405。
  X1/X2 四家同名，批次只认提交后新出现的报告；中断后用
  `naga-link --report-id` 补链接。官方 NAGA カスタム牌譜解析需要
  NICO/Google 登录（1 局 10 NP）；会话 cookie 是 `csrftoken` +
  `naga-report-session-id`，接口 `/naga_report/api/custom_haihu_analyze/`
  （`json_data` 每局一个 JSON）。官方接口同样可用（shift25/全 100000）。
- **中转限流**：连续请求会 429；雀魂侧还会 540。默认
  `FETCH_DELAY_SECONDS=75`，429 按 `Retry-After` / 90 秒退避；
  `FETCH_PROTECTION`/`UPSTREAM_RATE_LIMITED` 会停止整批，不要并发跑多个
  `main.py fetch`。
- **不要覆盖人工列**：`upsert_game` 只更新赛事元数据；
  `local_json_path` / `processed_json_path` / `naga_url` / `fetch_status`
  只能由抓取流程或 `set-*` 命令改。
- **姓名匹配**：`apply_tenhou` 先按姓名匹配 `game_players`，失败按顺序兜底并 WARN；
  `c_data` 的 name1..4 是**队伍顺序**，不是座位顺序，不要直接当 seat。
- **两个姓名字段**：`name` = 牌谱内昵称，`league_name` = 000.mk 名。
  `upsert_league_players` 只在 `seat IS NULL` 时用赛事名覆盖 `name`，
  已抓取的保留游戏内昵称；别名差只算 WARN（实例：電腦 / AI）。
- **000.mk 未开赛轮次返回 `[]`**：`get_round` 返回空字典，跳过，不要造数据。
- **本地模式版本串**：`client_version_string = WebGL_2022-<resource>`，
  `client_version.package` = 程序版本（如 4.0.51），
  `client_version.resource` = **资源版本**（2026-11-17 为 0.16.290，由
  `assetbundles/ASTC/.../docs_version/version.json` 读取，需 UnityPy）。
  `/1/version.json` 的 `0.11.252.w` 是协议/配置版本，混用会 151。
- **编码**：所有文件读写显式 UTF-8；Windows 控制台重定向时加
  `PYTHONIOENCODING=utf-8`。`main.py` 会尝试把 stdout 设为 UTF-8。

## 数据库

`data/paipu.db`，表 `competitions / teams / matches / games / game_players`。
`games` 唯一键 `(cid, rid, rowi)`，`game_players` 唯一键 `(game_id, player_order)`。
JSON 落盘在 `data/tenhou_json/<uuid>.json`；按本地 JSON 规则处理后的
JSON 在 `data/processed/<uuid>.json`；库中只存相对路径。
