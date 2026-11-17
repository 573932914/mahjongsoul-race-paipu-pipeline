# 立直麻将比赛牌谱处理 · Riichi Paipu Pipeline

**大凤林（000.mk）赛程 → 雀魂牌谱 → 天凤 tenhou.net/6 JSON → SQLite → NAGA 自动分析**

A small toolkit that crawls a league's matches from 大凤林 (000.mk), fetches the
corresponding Mahjong Soul (雀魂) replays, converts them to Tenhou `/6` custom
JSON, stores per-half-game results in SQLite, applies configurable score
corrections, and submits records to NAGA for analysis.

> 本项目最初为「第十届全国立直麻将网络联赛 · 超级」的队伍复盘搭建，但赛事 ID、
> 队伍名、接口地址都在 `config.py` / `config.local.json` 里，可复用于其他
> 大凤林赛事。

## 功能

- **赛程抓取**：读取大凤林 `admin / team / class / c_data` 接口，按轮次生成
  10 个半庄（先锋1 … 大将2）。
- **牌谱获取**：通过公开中转服务（无需雀魂账号）或本地雀魂账号
  （ms-api + tensoul）把每个半庄转成 Tenhou `/6` JSON。
- **本地数据库**：记录每个半庄四家的游戏内昵称、000.mk 姓名、开局/终局/得失、
  名次、位置标签（先锋1/次锋2/…）、本地 JSON 路径、处理后 JSON 路径、NAGA 链接。
- **点数修正**：按本地 JSON 转换规则（不入库）生成 X1/X2 对应版本
  （X1 每局重置 100000，X2 继承 X1 得失，先锋2/大将不调整）。
- **NAGA 投递**：经 ricochet 或官方 NAGA 接口提交，自动轮询报告并回填链接；
  内置多种「开局点数兼容模式」，解决 NAGA 对非均匀开局返回 405 的问题。
- **校验与导出**：`verify` / `verify-processed` / `export` 检查数据一致性并导出 CSV。

## 快速开始

```powershell
git clone https://github.com/<you>/<repo>.git
cd <repo>

# 建立虚拟环境（relay 模式其实只需标准库；local 模式需要 requirements.txt）
powershell -ExecutionPolicy Bypass -File .\setup_env.ps1

# 每周更新：抓赛程 + 抓新牌谱 + 入库
.\.venv\Scripts\python.exe main.py update

# 查看状态 / 明细
.\.venv\Scripts\python.exe main.py status
.\.venv\Scripts\python.exe main.py report --round 3
```

Windows 下 `python` 可能是旧版本，统一用 `.\.venv\Scripts\python.exe` 或 `py -3`；
重定向输出时加 `PYTHONIOENCODING=utf-8`。Linux/macOS 用 `python3 main.py ...`。

## 常用命令

```powershell
python main.py update                  # 抓赛程 + 抓新牌谱 + 入库（默认命令）
python main.py update --no-fetch       # 只刷新赛事元数据
python main.py update --round 3        # 只处理第 3 轮
python main.py update --round 3 --limit 2
python main.py fetch --limit 5         # 只抓待抓取
python main.py fetch --mode local      # 用本地雀魂账号抓
python main.py status                  # 概览 / 错误列表
python main.py report --round 3        # 四家明细
python main.py export                  # 导出 data/games.csv
python main.py process                 # 按本地 JSON 规则生成 data/processed/
python main.py verify-processed        # 校验处理后 JSON
python main.py naga --id 12            # 投递 NAGA 并回填链接
python main.py naga-batch --round 3 --player-types 2,4,6   # 整轮批量投递
python main.py naga-batch --round 3 --dry-run              # 只估算 NP
python main.py naga-link --id 12 --report-id <id>          # 从已有报告补链接
python main.py set-naga --id 12 --url "<NAGA链接>"
python main.py set-processed --id 12 --path data/processed/xxx.json
python main.py service-info            # 中转服务健康状态
python main.py convert-raw --input x.pb --output x.json    # protobuf 转 JSON

# 单元测试
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 配置

默认配置在 `config.py`（CID=220、队伍名、接口、限流参数）。个人设置放
`config.local.json`（**已 gitignore，不会上传**）：

```powershell
Copy-Item config.local.example.json config.local.json
```

可覆盖的键：

| 键 | 说明 |
|----|------|
| `CID` / `TEAM_NAME` | 大凤林赛事 ID 与目标队伍名（精确匹配） |
| `MAJSOUL_ACCOUNT` / `MAJSOUL_PASSWORD` | 本地雀魂账号（`--mode local` 时使用） |
| `MAJSOUL_PACKAGE_VERSION` / `MAJSOUL_RESOURCE_VERSION` | 留空自动探测 |
| `FETCH_DELAY_SECONDS` | 公开中转限流间隔（默认 75 秒） |

**凭证文件（均不入库）**

- `data/ricochet_token.txt` 或环境变量 `RICOCHET_TOKEN`：ricochet NAGA token；
- `data/naga_cookie.txt`：官方 NAGA 会话 cookie（`csrftoken` +
  `naga-report-session-id`）；
- `config.local.json`：雀魂账号密码。

请不要把上述内容提交到仓库；`.gitignore` 已经覆盖。

## 数据流

```
000.mk api/data.php
  ├─ admin  -> 赛事名、轮数、开局点数、10 个位置名
  ├─ team   -> 队伍 / 选手名单
  ├─ class  -> 每轮每场对阵（rid、四队 tid）
  └─ c_data -> {rid: [10 个半庄]}：rowi 0..9 = 先锋1..大将2
        │
        ▼
   data/paipu.db  (games + game_players)
        │  缺牌谱时
        ▼
  公开中转 / 本地雀魂账号（ms-api + tensoul）
        │  雀魂 protobuf -> 天凤 /6 JSON
        ▼
  data/tenhou_json/<uuid>.json
        │  本地 JSON 规则
        ▼
  data/processed/<uuid>.json  ──► NAGA（ricochet / 官方）
```

## 数据库

`data/paipu.db`（不入库，运行时自动创建）：

| 表 | 作用 |
|----|------|
| `competitions` | 赛事（cid、名称、轮数、开局点数） |
| `teams` | 队伍与选手名单 |
| `matches` | 每轮每场对阵（rid、class、四队） |
| `games` | 一个半庄一行：轮次、位置、日期、雀魂链接、本地 JSON、处理后 JSON、NAGA 链接、抓取状态 |
| `game_players` | 一个半庄四家：座位、游戏内姓名、000.mk 姓名、队伍、开局、终局、得失、名次 |

`games` 关键列：`round_no`、`position_label`（如 `先锋1`）、`paipu_url`、
`paipu_uuid`、`local_json_path`、`processed_json_path`、`naga_url`、
`fetch_status`（`pending` / `fetched` / `error`）。

**分数语义**：每轮 10 个半庄是**连续赛程**。第 1 个半庄从 100000 点开始，
之后每个半庄的开局 = 上一个半庄的终局（座位轮转），不是每局重置。
`start_score` / `end_score` / `delta` 来自牌谱（开局 `log[0][1]`，终局优先取
顶层 `sc`），并与大凤林 `num` / `pint` 交叉校验。

## 处理后 JSON（本地规则，文档不入库）

`main.py process` 按同位置 X1/X2 两个半庄修正开局点：

- **X1**（先锋1/次锋1/中坚1/副将1）：每一小局开局都重置为 100000；
- **X2**（次锋2/中坚2/副将2）：首局 = 100000 + 对应 X1 的得失，整份加同一偏移；
- **先锋2、大将1、大将2**：不调整；
- 替补选手按「姓名 → 原首局点数 == X1 终局」映射到继承的座位；
- 立直供托 `seed[2]` 保持原样，只改四家开局点；顶层 `sc` 保持真实终局。

输出 `data/processed/<uuid>.json`、`data/processed/processing_report.csv`，
并回填 `games.processed_json_path`；`verify-processed` 检查是否符合规则。

## NAGA 投递

- 默认使用 `processed_json_path`；`--raw` 投原始 JSON；
- 默认类型 `2,4`（2=ニシキ，4=カガシ），可 `--player-types 2,4,6` 追加
  ウロボロス（6）；
- 费用约 `(max(2, 类型数) + 8) NP × 局数`，`--dry-run` 先估算；
- 投递后轮询报告列表，按四家姓名匹配新报告，把链接写入 `games.naga_url`；
- 全不听的流局自动转成 `["全員不聴",[0,0,0,0]]`（不改磁盘文件）。

**开局兼容模式（`--starts`）**：NAGA 拒绝非均匀开局（405）。实测结果：

| 模式 | 行为 | 结果 |
|------|------|------|
| `shift75`（默认） | 每人减 75000，首局和 100000（25000 + X1 得失） | ✅ 保留 X1 得失与局间点数流 |
| `shift25` | 首局平移到 25000，后续按实际得失滚动 | ✅ 但丢掉 X1 得失 |
| `all` | 每局都是 100000 | ✅ 但忽略局间得失 |
| `first` / `shift` / `none` | 只改首局 / 平移到 100000 / 不调整 | ❌ 405 |

> 官方 NAGA「カスタム牌譜解析」同样拒绝非均匀开局；`per_round_payloads()`
> 可生成官方接口所需的 `json_data`（每局一个编辑器格式 JSON）。

## 项目结构

| 文件 | 职责 |
|------|------|
| `config.py` | 路径、CID、队伍名、接口、限流参数（`config.local.json` 可覆盖） |
| `league_client.py` | 大凤林 admin/team/class/c_data，位置映射，日期/UUID 解析 |
| `relay_client.py` | 公开中转：创建任务、轮询、下载 Tenhou JSON |
| `majsoul_local.py` | 可选：ms-api 直连雀魂 + tensoul 转换 |
| `tenhou_meta.py` | 从 Tenhou JSON 提取四家姓名/开局/终局/得失/名次 |
| `db.py` | SQLite 建表、upsert、状态查询 |
| `pipeline.py` | 编排：赛程 → 入库 → 抓取 → 回填 |
| `process_pairs.py` | 按本地 JSON 规则生成 X1/X2 点数修正后的 JSON |
| `naga_client.py` | ricochet / 官方 NAGA 投递与链接回填 |
| `main.py` | CLI |
| `AGENTS.md` | 每周增量更新流程与排错 |
| `tests/` | 不联网的单元测试 |

## 数据与隐私

- `data/` 下的牌谱、数据库、CSV、日志、缓存**全部不入库**（见 `.gitignore`）；
- 账号密码、ricochet token、NAGA cookie 只从本地文件/环境变量读取，
  不写日志、不入库；
- 牌谱 JSON 由第三方接口生成，仅供复盘分析，请遵守雀魂、大凤林与 NAGA 的
  服务条款，避免高频抓取或公开分发选手数据。

## 第三方组件

见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## License

尚未指定。发布前请根据你的意图补充 `LICENSE`（例如 MIT / Apache-2.0）；
注意 `THIRD_PARTY_NOTICES.md` 中列出的参考项目许可。
