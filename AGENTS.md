# AGENTS.md —— 每周增量更新流程

本文件记录「大凤林赛事数据 → 雀魂牌谱 → 天凤 JSON → 本地数据库」的固定流程。
项目结构与实现细节见 `CLAUDE.md` 和 `README.md`，这里只写操作顺序与易错点。

> 所有命令在项目根目录执行。
> 建议用 `.venv\Scripts\python.exe`（Windows）；没有虚拟环境时用 `py -3`。
> 输出到文件时加 `PYTHONIOENCODING=utf-8`。

---

## 0. 一句话流程

```powershell
# 每周新比赛出来后（幂等，可反复跑）
.\.venv\Scripts\python.exe main.py update --mode local
.\.venv\Scripts\python.exe main.py process
```

`update` 抓赛程/牌谱；`process` 按本地 JSON 规则（不入库）生成处理后 JSON。
如果本地账号不可用，用 `main.py update` 走公开中转（较慢）。

`update` 会：

1. 调大凤林接口，找出 `cid=220` 中队伍「立石」参加的所有轮次/对阵；
2. 把每轮 10 个半庄（先锋1/2、次锋1/2、中坚1/2、副将1/2、大将1/2）的元数据写入
   `data/paipu.db`；
3. 对还没抓到的半庄，走 ninklang.tech 公开中转服务，抓取雀魂牌谱并转成
   tenhou.net/6 自定义 JSON，存到 `data/tenhou_json/<uuid>.json`；
4. 从 JSON 里回填四家姓名、座位、开局/终局点数、得失、名次。

已抓取的会跳过；失败的下次会重试。新比赛通常只需要 10 个新半庄（1 轮）。

---

## 1. 一次性准备

```powershell
# 已存在 .venv 可跳过
powershell -ExecutionPolicy Bypass -File .\setup_env.ps1
```

relay 模式只用标准库；`ms-api` / `tensoul` 只在 `--mode local` 时需要。

可选配置：复制 `config.local.example.json` 为 `config.local.json`，按需修改。
**不要**把真实账号密码提交到仓库（`.gitignore` 已忽略该文件）。

---

## 2. 每周更新（推荐流程）

### 2.1 先看赛事接口有没有新数据

```powershell
.\.venv\Scripts\python.exe main.py update --no-fetch
```

输出示例：

```
第 1 轮：10 个半庄已入库（共 4 场对阵，目标队伍参加 1 场）
第 2 轮：10 个半庄已入库（共 4 场对阵，目标队伍参加 1 场）
第 4 轮：暂无数据
...
半庄总数 40，已抓取 30，失败 0，待抓取 10
```

- 「暂无数据」说明该轮还没开赛或接口未更新，正常。
- 如果轮次数和赛程对不上，先查 `https://www.000.mk/?cid=220#!ranking` 的页面，
  再对照 `https://cdn.r-mj.com/api/data.php?t=admin&cid=220` 的 `c_round`。

### 2.2 抓新牌谱（relay 默认）

```powershell
.\.venv\Scripts\python.exe main.py update
```

只想先抓几个测试：

```powershell
.\.venv\Scripts\python.exe main.py fetch --limit 3
```

**限流**：公开中转服务按 IP 限流，连续请求会 429；雀魂侧还可能 540。
默认每个请求间隔 75 秒；429 时按 `Retry-After` 或 90 秒退避重试。
顺利时 10 个新半庄约 15~30 分钟；遇到 540 可能要等 1 小时。
建议放后台跑：

```powershell
Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "main.py","fetch" -NoNewWindow
```

如果长时间 429：

- 等 10 分钟再跑（限额按时间窗口恢复）；
- 或改用本地账号模式（见第 4 节）；
- 不要并发开多个进程，会更快触发限额。

如果看到 `UPSTREAM_RATE_LIMITED ... 雀魂返回 540 ... 北京时间 HH:MM 后可手动再试`：
这是雀魂侧的拉取限额，不是本项目的错误。脚本会停止整批并在 `fetch_error`
里保留提示；**等到提示的北京时间之后再跑 `main.py fetch`**，不要反复提交。

### 2.3 检查结果

```powershell
.\.venv\Scripts\python.exe main.py status
.\.venv\Scripts\python.exe main.py report --round 4
.\.venv\Scripts\python.exe main.py report --round 4 --summary
.\.venv\Scripts\python.exe main.py export        # data/games.csv，Excel 可直接开
```

`report` 每家一行：`座<seat> 姓名 开局 → 终局 (得失) 名次 队伍`，
目标队伍「立石」前面有 `*`。

### 2.4 抽查数据

每周至少抽查 1 场：

```powershell
# 数据库里四家点数/得失是否自洽
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/paipu.db'); \
print(c.execute('''select g.position_label, p.seat, p.name, p.start_score, p.end_score, p.delta, p.rank \
from games g join game_players p on p.game_id=g.id where g.round_no=4 order by g.rowi, p.seat limit 20''').fetchall())"
```

自检项：

- 每轮**第 1 个半庄**四家 `start_score` 均为 `100000`；后续半庄的开局点
  等于上一个半庄的终局点（连续赛程，不是每局重置）；
- 同一半庄四家 `start_score` 之和、`end_score` 之和都 = `400000`；
- 同一半庄四家 `delta` 之和 = 0；
- `name` 与 `000.mk` 页面上的四家一致；
- `position_label` 是「先锋1/先锋2/…/大将2」之一。

最省事的检查：

```powershell
.\.venv\Scripts\python.exe main.py verify --round <轮次>
```

它会逐场比对 JSON、数据库、000.mk 缓存里的 `(姓名, 终局, 得失)`。
`verify` 依赖 `data/league_cache/`；如果缓存被删，先跑
`main.py update --no-fetch` 重新生成。

如果某条 `fetch_status='error'`：

```powershell
.\.venv\Scripts\python.exe main.py status
.\.venv\Scripts\python.exe main.py fetch --limit 1     # 重试最早的一条
```

---

## 3. 生成「处理后 JSON」与填写 NAGA 链接

### 3.1 按本地 JSON 规则自动处理

```powershell
.\.venv\Scripts\python.exe main.py process --dry-run   # 先看计划
.\.venv\Scripts\python.exe main.py process             # 写入 data/processed/
.\.venv\Scripts\python.exe main.py verify-processed    # 校验 30 场
```

规则（`process_pairs.py` 实现）：

- **X1 = 先锋1 / 次锋1 / 中坚1 / 副将1**：每一小局开局四家都重置为 100000。
- **X2 = 次锋2 / 中坚2 / 副将2**：首局 = 100000 + 对应 X1 的得失；
  之后各局保持原来的正常滚动（实现为整份 X2 的每局开局点加同一个偏移）。
- **先锋2**：不调整（原始数据已经是 100000 + 先锋1 得失）。
- **大将1、大将2**：完全不调整。
- **供托（立直棒）**：MD 只提到四家点数，所以 `seed[2]` 保持原样。
  X1 重置后某局可能仍有供托，这是按 MD 严格实现的结果；如果需要连供托也
  清零，要先改规则再重跑 `process --force`。
- **替补**：X2 某玩家没在 X1 出现（中途换人）时，先按游戏内姓名匹配；
  匹配不到就用“X2 原首局点数 == X1 终局点数”找到他继承的 X1 座位，再用该座位
  的得失。`processing_report.csv` 里 `rule` 列会标 `X2_offset:name / score / seat`。

输出：

- `data/processed/<uuid>.json`：处理后的天凤 JSON；
- `data/processed/processing_report.csv`：每个座位一行，含 X1 得失、
  X2 原首局/新首局、映射方式；
- `games.processed_json_path` 自动指向新文件。

原始文件 `data/tenhou_json/<uuid>.json` 不变。处理后 JSON 的顶层 `sc`
也保持不变（保留真实终局），只改 `log[i][1]` 的每局开局点；NAGA/Mortal
使用 `log`，不使用 `sc`。

### 3.2 手工替换处理后 JSON / 填写 NAGA 链接

如果之后要换成自己清洗的 JSON：

```powershell
.\.venv\Scripts\python.exe main.py set-processed --id 31 --path data/processed/261114-xxxx.json
.\.venv\Scripts\python.exe main.py set-naga --id 31 --url "https://naga.dmv.nico/..."
.\.venv\Scripts\python.exe main.py set-naga --uuid 261114-a22 --url "..."
```

清空：

```powershell
.\.venv\Scripts\python.exe main.py set-naga --id 31 --clear
.\.venv\Scripts\python.exe main.py set-processed --id 31 --clear
```

**注意**：`update` 每次会刷新赛事元数据（姓名/点数/位置），但**不会**覆盖
`local_json_path`、`processed_json_path`、`naga_url`、`fetch_status`。
手工填的两列是安全的。`process` 默认跳过已有处理文件；要重算用 `--force`。

### 3.3 通过 ricochet 投递 NAGA（自动回填 naga_url）

ricochet token 是登录凭证，等价于密码。放到
`data/ricochet_token.txt`（已 gitignore），或用 `--token-file` /
`RICOCHET_TOKEN` 环境变量。

```powershell
# 投递单场（默认用 processed_json_path，2,4 = ニシキ+カガシ）
.\.venv\Scripts\python.exe main.py naga --id 1

# 投原始 JSON（不推荐）
.\.venv\Scripts\python.exe main.py naga --id 1 --raw

# 换 NAGA 类型（索引见下）
.\.venv\Scripts\python.exe main.py naga --id 1 --player-types 2,4

# 已有 naga_url 也重新投
.\.venv\Scripts\python.exe main.py naga --id 1 --force

# 批量投递整轮（示例：第3轮，2+4+6，先 dry-run 看 NP）
.\.venv\Scripts\python.exe main.py naga-batch --round 3 --player-types 2,4,6 --dry-run
.\.venv\Scripts\python.exe main.py naga-batch --round 3 --player-types 2,4,6
```

`naga-batch` 会跳过已有 `naga_url` 的场次；`--force` 重投，`--limit` 限数量。

**NAGA 易错点**：

- **四人全不听的流局**：tensoul 输出 `["流局", [0,0,0,0]]`，NAGA 用
  `["全員不聴", [0,0,0,0]]` 兼容；`normalize_for_naga()` 自动改，不改文件。
- **NAGA 的起点要求（已实测）**：
  - X1（处理后每局 100000）：直接投，成功。
  - X2/大将：用 **`--starts shift75`**（默认/推荐）——整份牌谱每人减 75000，
    首局和 = 100000（`25000 + X1 得失`），后续按实际得失滚动。实测成功，
    是唯一同时保留 X1 累积得失和局间点数流的模式。
  - `--starts shift25`：首局平移到标准 **25000**，后续按实际得失滚动。
    实测成功，但会丢掉 X1 累积得失。
  - `--starts all`（每局都 100000）也可用，但忽略局间得失。
  - 以下均实测 `405 jsonが間違っています`：
    `--starts first`、`--starts shift`（平移 100000）、首局 100000+结果修正、
    单局非均匀开局（东1/东2/南1 都试过）。
- **官方 NAGA 自定义解析（已验证：同样拒绝非均匀开局）**：
  - 订单页 `https://naga.dmv.nico/naga_report/order_form/` 需要 NICO/Google
    登录；登录后 cookie 是 **`csrftoken` + `naga-report-session-id`**
    （不是 `TOKEN`/`SESSIONID`，也不是 ricochet token）。
  - 原生接口：`POST /naga_report/api/custom_haihu_analyze/`，字段
    `csrfmiddlewaretoken`、`json_data`（每局一个编辑器格式 JSON 的数组）、
    `seat`、`player_types`、`game_type`（0=半荘，1=東風）。
  - **实测把处理后 X2 的非均匀开局放进 `json_data`，官方接口仍返回
    `405 jsonが間違っています`。** 所以官方入口也不能表达 X1 累积得失；
    只有全部局 100000 能过。
  - cookie 放 `data/naga_cookie.txt`（gitignore）。官方接口的 `json_data`
    可直接用 `naga_client.per_round_payloads()` 生成。
- **X1/X2 四家姓名相同**：先锋1/2、次锋1/2 等同四名选手，不能只按姓名找旧报告。
  批次按“提交前的报告集合”只认新报告；如果批次中断、报告已生成但链接没写入，
  用 `naga-link` 从已有报告补：
  ```powershell
  .\.venv\Scripts\python.exe main.py naga-link --id 25 `
    --player-types 2,4,6 --report-id <report_id>
  ```

NAGA 类型索引：

```
0 オメガ(副露)   1 ガンマ(守备)   2 ニシキ(标准)
3 ヒバカリ(超门前) 4 カガシ(超副露) 5 アナンタ(超避四)
6 ウロボロス(争一位)
```

消耗：默认按 `10 NP × JSON 局数` 左右计费（2 种类型时实测 11 局消耗 110 NP）。
命令会轮询报告列表，按四家姓名找到新报告，把 ricochet 代理链接写入
`games.naga_url`。如果之前已经投过，会复用已有报告（后端提示“已被解析过”时）。

---

## 4. 本地雀魂账号模式（relay 限流时的备选）

前提：一个**国服**雀魂账号（`game.maj-soul.com`，账号密码登录）。

1. 复制配置：

   ```powershell
   Copy-Item config.local.example.json config.local.json
   ```

2. 在 `config.local.json` 填：

   ```json
   {
     "MAJSOUL_ACCOUNT": "your@account",
     "MAJSOUL_PASSWORD": "your-password",
     "MAJSOUL_PACKAGE_VERSION": "",
     "MAJSOUL_RESOURCE_VERSION": ""
   }
   ```

   - 留空会自动探测：
     - `MAJSOUL_PACKAGE_VERSION` 从雀魂网页 `productVersion: "4.0.51"` 取；
     - `MAJSOUL_RESOURCE_VERSION` 从官方资源包
       `assetbundles/ASTC/.../docs_version/version.json` 取
       （例如 2026-11-17 是 `0.16.290`）。
   - **`/1/version.json` 的 `0.11.252.w` 是协议/配置版本，不是资源版本。**
     拿它当 `MAJSOUL_RESOURCE_VERSION` 会登录 `151`。
   - 资源版本解析依赖 `UnityPy`（`requirements.txt` 已包含）。解析失败时
     会在 stderr 打 WARN，并退回 `version.json` 的值；此时应手动填写。

3. 跑：

   ```powershell
   .\.venv\Scripts\python.exe main.py fetch --mode local
   ```

错误码：

- `151`：客户端版本串被拒。更新 `ms-api`，或调整上面两个版本号；
- `540`：牌谱拉取次数超限，等一段时间；
- `1004`：登录态失效/未登录，重新跑；
- `1002`：账号或密码错误（本地模式会直接显示为登录失败）。

本地模式成功抓到后，JSON 和数据库的处理与 relay 模式完全一致。
账号密码只从 `config.local.json` / 环境变量读取，不写日志、不入库。

---

## 5. 位置与得分语义（改代码前必读）

### 5.1 位置映射

大凤林 `admin.t_type` 是 10 行：

```
先鋒 先鋒 次鋒 次鋒 中堅 中堅 副將 副將 大將 大將
```

`c_data` 的 `rowi` 0..9 直接对应这 10 行。因此：

| rowi | position_label |
|------|----------------|
| 0    | 先锋1 |
| 1    | 先锋2 |
| 2    | 次锋1 |
| 3    | 次锋2 |
| 4    | 中坚1 |
| 5    | 中坚2 |
| 6    | 副将1 |
| 7    | 副将2 |
| 8    | 大将1 |
| 9    | 大将2 |

序数 = 同一位置当日第几个半庄。代码里 `league_client.position_label()` 负责。
不要按「第几局」理解；`rowi` 是位置槽位，不是半庄内的局数。

### 5.2 分数（重要：10 个半庄是连续赛程）

- 每轮的 10 个半庄是**一场连续赛程**：第 1 个半庄（通常是先锋1）从
  **100000 点**开始；第 2 个半庄的开局点数 = 第 1 个半庄的终局点数
  （座位轮转后），以此类推。**不是每个半庄都重置到 100000。**
- 因此同一个轮次里，`game_players.start_score` 会变化；但每个半庄四家
  总和始终是 400000，`delta` 总和始终是 0。
- `c_data` 的 `num1..num4` 是该半庄的终局点数，`pint1..pint4` 是该半庄的得失。
  由 `start = num - pint` 得到开局点数。
- 天凤 JSON 顶层 `sc` 是 `[终局点数, 得失/1000] × 4`（例如 `[89700,-10.3,...]`），
  **不是**天凤原生 `[开局, 终局]`；不要拿它当开局点数。
- 每个半庄的开局点数取 `log[0][1]`（该半庄第一局开始时的四家点数）。
- 手算终局不要把所有局的 `delta` 直接累加：未取走的立直棒会让总和多出
  1000 的整数倍。优先用 `sc`，其次用 `c_data` 的 `num`。
- `games.start_points` 记录的是**赛事规则的开局点数**（100000），
  不是某一家的开局点；四家各自的 `start_score` 在 `game_players`。
- 还没抓到 JSON 的半庄：`game_players` 已有姓名/终局/得失（来自 c_data），
  但 `seat` 为 NULL，`name` 是 000.mk 名；抓到 JSON 后才回填座位和游戏内昵称。

### 5.3 姓名别名

`game_players` 有两列姓名：

- `name`：牌谱 JSON 里的**游戏内昵称**（最终以它为准）；
- `league_name`：大凤林 c_data 里的名字。

两者不同时（例如 000.mk 写「電腦」、游戏内叫「AI」），`report` 会显示
`AI [000:電腦]`，`verify` 只报 `WARN`，不算错误。不要为了统一而改掉
`name`，否则无法和牌谱对局内容对应。

### 5.4 UUID / 链接

- `paipu_url` 是 `https://game.maj-soul.com/1/?paipu=<uuid>`；
- `paipu_uuid` 是 `?paipu=` 后面的整串（本项目的数据里不带 `_a<数字>`）；
- 本地 JSON 文件名 = `<paipu_uuid>.json`；
- 数据库存相对路径 `data/tenhou_json/<uuid>.json`，换机器可整体搬走。

---

## 6. 数据库速查

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/paipu.db'); \
print(c.execute('select round_no, count(*), sum(fetch_status=\"fetched\") from games group by round_no').fetchall())"
```

表：

- `competitions`：赛事 id、名称、轮数、开局点数；
- `teams`：队伍 id、队名、正选/替补名单（原样保存换行文本）；
- `matches`：每轮每场对阵（rid、class、四队 tid）；
- `games`：一个半庄一行（位置、日期、链接、JSON 路径、处理后路径、NAGA 链接、状态）；
- `game_players`：一个半庄四家（座位、姓名、队伍、开局/终局/得失/名次）。

`games` 唯一键：`(cid, rid, rowi)`；`game_players` 唯一键：`(game_id, player_order)`。

---

## 7. 常见问题

| 现象 | 原因 / 处理 |
|------|-------------|
| 第 N 轮「暂无数据」 | 还没开赛或接口未更新；不要手动造数据 |
| 目标队伍 0 场 | `TEAM_NAME` 或赛事 `CID` 变了；查 team 接口的 `t_name` |
| 姓名对不上 | 玩家改名/替补；`report` 会 WARN，按 JSON 姓名回填，队伍信息仍按 tid 保留 |
| 429 RATE_LIMITED | 中转限流；等 10 分钟，或 `--mode local`，不要并发 |
| UPSTREAM_RATE_LIMITED / 540 | 雀魂侧牌谱拉取限额；服务会给出下次可尝试时间。脚本会**停止整批**，到点再 `fetch`，不要连续提交 |
| 中转任务 `failed` | 牌谱链接失效/服务暂不可用；稍后重试，或本地模式 |
| `jp`/`en` 不可用 | ninklang 目前只开国服；本项目链接是 `game.maj-soul.com`（国服），正常 |
| 登录 151 | 客户端版本串过期；更新 ms-api 或版本号 |
| 数据库 is locked | 同时开了两个 `main.py`；等前一个结束 |
| 中文乱码 | 加 `PYTHONIOENCODING=utf-8`；Windows 控制台默认 GBK |

---

## 8. 文件位置

```
data/paipu.db                  SQLite 数据库
data/tenhou_json/<uuid>.json   转换后的天凤 JSON（原始比赛信息）
data/league_cache/             大凤林 c_data 缓存（排查用）
data/raw/                      预留：原始 protobuf / 手工文件
data/logs/                     预留：日志
data/processed/                按本地 JSON 规则生成的处理后 JSON + processing_report.csv
```

`main.py process` 会自动写 `data/processed/<uuid>.json` 并回填
`games.processed_json_path`；原始文件在 `data/tenhou_json/` 不动。
如果换成自己清洗的文件，再用 `set-processed` 覆盖路径即可。

---

## 9. 改代码后

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe main.py verify --round <轮次>
```

`tests/test_core.py` 覆盖位置映射、日期/UUID、`sc` 解析、连续赛程得分、
`league_name` 保留；`tests/test_processing.py` 覆盖 X1 重置、X2 偏移、
替补映射。改 `db.py` / `tenhou_meta.py` / `league_client.py` /
`process_pairs.py` 后务必跑。

处理后再跑：

```powershell
.\.venv\Scripts\python.exe main.py verify-processed
```
