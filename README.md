# bilikards

KARDS 社区向 AstrBot 插件。它把 KARDS 官方 B 站动态、相关视频、卡组导入码解析
和手动组卡集中到 QQ 聊天与 AstrBot 仪表盘里。

- 插件名：`bilikards`（目录名 `astrbot_plugin_bili_kards`，两者都能用）
- 作者：Fma
- 当前版本：`v2.0.0`
- 运行环境：AstrBot `v4.x`、Python `3.10+`

## 功能一览

| 功能 | 入口 | 说明 |
| --- | --- | --- |
| 官方动态 | `kards news` | 拉取 KARDS 官方 B 站时间线，文字和配图一起发送 |
| 视频搜索 | `kards now 关键词` | 走 B 站 wbi 搜索接口，按播放量展示前 3 条 |
| 卡组码出图 | `kards deck <卡组码>` | 解析导入码，生成「卡图墙 + 信息图」两张图片 |
| 手动卡组 | 插件页面 | 逐张加卡、减卡、设置主国盟国，生成同样的两张图（聊天中不提供组建卡组指令） |
| 卡牌搜索 | 插件页面 / `kards s` | 按名称、别名、关键字、阵营、类型、稀有度、费用/攻防范围筛选；QQ 返回卡图拼图 |
| 别名库 | 插件页面 / `kards alias` | 给卡牌起常用别名，聊天和页面搜索都能用 |
| 卡牌更新 | 插件页面 / `kards import` | 上传卡图自动识别，或导入官方新卡 JSON；不带参数会生成二位临时短码，识别后需“同意”才同步正式卡表 |
| 本地卡表 | `deck_cards.json` | 1646 张卡的离线数据，含描述与关键字，解析卡组码不依赖网络 |
| DIY 卡编辑器 | 插件页面 `diy_editor` / `kards diy` | 用真实 KARDS 模板出图：卡名/阵营/类型/稀有度/费用/攻防/描述/关键词/卡图，实时预览并保存设计；聊天端 `kards diy` 进入连续出图模式，单独发「退出」/`exit`/`结束` 或 `kards diy exit` 退出；`kards diy last` 继续最近一张、`kards diy new` 强制新建；不写入官方卡表 |
| 管理员审核 | `kards review` / `kards approve` / `kards reject` | `import`、`confirm`、`cancel`、`fix`、`setid` 只有管理员可直接执行；其他人提交后进入审核队列，卡表不改动，由管理员批准或驳回 |

## 安装

把整个目录放到 AstrBot 的插件目录：

```text
AStrBot/
└── data/
    └── plugins/
        └── astrbot_plugin_bili_kards/
            ├── main.py
            ├── metadata.yaml
            ├── _conf_schema.json
            └── ...
```

重启 AstrBot，日志出现下面两行即加载成功：

```text
Plugin bilikards (v2.0.0) by Fma: ...
[bilikards] Manual deck Web API routes registered
```

依赖：

- `aiohttp`：AstrBot 已自带。
- `Pillow`：卡组图片和动态长图需要。AstrBot 环境通常已安装；缺失时卡组码会
  自动退回文字卡表，动态配图会跳过拼接。
- 中文字体：自动检测系统字体。Windows 使用黑体，macOS 使用 PingFang，
  Linux 使用文泉驿正黑。找不到时回退到 Pillow 默认字体，中文可能显示为方块。
  Linux 如需更好支持可安装 `fonts-wqy-zenhei` 或 `fonts-wqy-microhei`。
- DIY 卡字体：由视觉模型分析现有卡图后生成，随插件附带，无需系统字体：
  - `fonts/kards_card_font.ttf`（内部名称 `KARDS Card Font`，中文标题/正文）
  - `fonts/kards_card_number.ttf`（内部名称 `KARDS Card Number`，费用/攻防数字）
  - `fonts/font_profile.json` 记录模型、分析结论、推荐字体与生成参数。
  系统字体仍作为兜底，方便外部工具或临时替换。
- DIY 真实模板素材：`assets/kards_builder/`（55 个文件，约 2.6 MB），来自非官方粉丝项目
  `KARDS-Card-Builder`（Babaika328，基于 1939 Games Community Content Policy，非商业个人使用）。
  含单位/指令/反制模板、国家横幅、稀有度标记与配套字体；`card_template.py` 按需单张加载，
  用完即释放，字体缓存上限 128 条，避免常驻高内存。素材缺失时自动回退到自绘卡面。
  卡名字号按上游 `index.html` 校准：单位 `47`（最大宽度 255，自动缩到 6），
  指令/反制 `40`（最大宽度 410，深色 `#44474d`），中文用自带字体渲染。
  指令/反制卡名再上移 10 px（最终出图像素口径）；效果描述字号按文字量自适应，
  短文本最大 `20`、长文本最小 `11`，始终排在卡框内，超出时末行以 `…` 截断。
   描述绘制使用默认基线，中文标点 `，。：、！？` 留在行底，不再被 `anchor="lt"` 顶到字上方
   而看起来像单引号。

### DIY 卡聊天用法

```text
kards diy                              新建一张卡并进入模式（每次修改自动保存）
名=闪电部队 阵营=德国 类型=坦克 费=3 攻=4 防=2 描述=闪击   ← 模式内直接发，只修改这张卡
描述=闪击，部署时抽一张牌                                      ← 继续补字段，之前的字段保留
插画=1，9~1.2                                                   ← 紧凑写法：左移 1px、上移 9px、放大 1.2 倍
插画x=1 插画y=9 缩放=1.2                                       ← 等价的独立字段写法，二者可混用
退出 / exit / 结束 / quit / bye         ← 模式内单独发即可退出并保存（#、/ 前缀也识别）
kards diy exit                         退出模式（同上，卡片保留）
kards diy last                         继续编辑最近保存的一张
kards diy new                          强制新建一张（不用上次的）
kards diy help                         查看用法
kards diy list                         列出已保存的 DIY 卡
kards diy show <ID>                    渲染并继续编辑这张已保存的卡
kards diy load <卡名/ID>               读取本地卡表 JSON 与卡图，存为新 DIY 卡并进入模式
kards diy save 字段=值 ...             保存并渲染，如 save 名=闪电部队 阵营=德国 费=3
kards diy 字段=值 ...                  新建一张卡，保存并渲染后进入模式
kards diy {"name":"测试卡","faction":"USA","cost":2}
```

模式说明：一次进入只编一张卡，模式内每条普通消息都是**增量修改**（只写本次提到的字段，
其他字段保留），并立即写回 `diy_cards.json`；保存提示文字与卡图在**同一条消息**里发出；
附图会作为这张卡的卡图；
退出时再做一次防御性保存，回执写明「卡片 ID + 卡名 + 是否含卡图 + 继续编辑命令」，
避免看起来像没保存。重新进入时 `kards diy` 仍新建空白卡，要接着编辑请用
`kards diy last` 或 `kards diy show <ID>`。
单独发「退出」「exit」「结束」「quit」「bye」「退出diy」也能退出（`kards diy` 前缀不需要）；
`kards diy help/list/show/last/new/exit` 等命令不会被当成字段。按会话 + 发送者区分，群聊里多人互不干扰。
模式本身保存在进程内存中，插件重载后自动清空，已保存的卡不受影响。

读取本地卡表原有卡牌：聊天端用 `kards diy load <卡名/ID>`，网页编辑器点「读取 JSON + 卡图」。
两者都会把卡表 13 字段（卡名、阵营、类型、稀有度、费用/行动费、攻防、描述、关键词、标签）
完整转成 DIY 字段，并从本地 `deck_card_thumbs` / `deck_card_cache` 读取卡图转成 data URL；
不联网、不改官方卡表。载入后存为新的 `DIY-NN`，可继续增量编辑。
聊天端会先把原始 JSON 完整发出来，再发「JSON + 卡图」同条预览。
本地卡图是 400×562 的整卡图，插画缩放沿用 `art_scale` / `插画=~z`，比例可按需要调整。

插画存储位置：
- **DIY 卡**（`kards diy` 建的卡）：附图会以 `data:image/png;base64,...` 或
  `data:image/jpeg;base64,...` 的 data URL 直接写进
  `C:\Users\Administrator\Documents\默认项目\data\plugin_data\bilikards\diy_cards.json`
  的 `art` 字段，不单独落图片文件；渲染时由 `web_api._diy_art()` 解码。
  同一条消息附图、或模式内只发一张图都会保存；`插画x/插画y/缩放` 只是位置参数，
  不替代图片本身。
- **截图导入的卡**（`kards import`，含 `DIY-` 临时 ID）：卡面 JPEG 缩略图写到
  `C:\Users\Administrator\Documents\默认项目\data\plugin_data\bilikards\deck_card_thumbs\<card_id>.jpg`，
  卡牌字段写进 `deck_cards.json` / `manual_decks.sqlite3`；这部分属于卡表，不是 `diy_cards.json`。

字段可用中英文：`名/name`、`英文名/name_en`、`阵营/faction`、`类型/card_type`（别名 `炮兵` → `artillery`，未知类型回退步兵而不再误落到反制）、
`稀有度/rarity`、`费/cost`、`攻/attack`、`防/defense`、`描述/description`、
`关键词/keywords`、`id`。同一条消息附图、模式内直接发图，或先发命令再发图，都会作为卡图使用。
插画位置与放缩：位移单位是**最终输出图 500×702 的像素**，正值 `x` 左移、`y` 上移，
负值反向；`~z` 的 `z` 是缩放倍数，`1` 为默认铺满，>1 放大居中裁切，<1 缩小并用图片平均色补边；
位移限制 ±200 px，缩放限制 0.2–5.0。三种写法等价，可混用，也可只写其中一部分（未写的保持不变）：

```text
插画=x，y~z     例：插画=1，9~1.2    左移 1 px、上移 9 px、放大 1.2 倍
插画=x，y       例：插画=-1，-9      右移 1 px、下移 9 px，缩放不变
插画=~z         例：插画=~1.5       只把缩放设为 1.5 倍，位移不变
```

紧凑写法别名：`插画`、`插画位置`、`位移`、`位置`、`art_pos`、`artpos`、`artxy`、`artoffset`。
独立字段写法：`插画x`（别名 `左移`、`x`）、`插画y`（别名 `上移`、`y`）、
`插画缩放`（别名 `缩放`、`放缩`、`zoom`），例如 `插画x=1 插画y=9 缩放=1.2`。
三个字段与卡图一起增量保存，后续修改不会覆盖。逗号支持半角 `,` 与全角 `，`；若格式不合法会提示
`插画格式应为 插画=x，y 或 插画=~z 或 插画=x，y~z`，且不会写入错误值。
分隔符兼容 `=`、`＝`（全角）、`:`、`：`（中文冒号），两边可有空格，例如
`名 ＝ 闪电部队　阵营：德国　费: 3`。值可加引号以包含空格，不加引号时也会
读到下一个字段或行尾：`描述=闪击 部署时抽一张牌 阵营=德国`。
保存的设计在 `data/plugin_data/bilikards/diy_cards.json`，与官方卡表隔离。

- 卡图下载：采用三层fallback策略
  1. 优先尝试官网直连 (www.kards.com)
  2. 失败则使用 wsrv.nl 中转
  3. 仍失败则使用 weserv.nl 中转
  4. 最后尝试 kardscm API

## 命令

所有子命令大小写不敏感。私聊直接发送；群聊需要 @机器人或加唤醒前缀。

### 动态与视频

```text
kards news
kards dyn
kards dynamic
kards 动态
kards 刷新动态   # 跳过内存缓存，立即重新拉取
kards refresh

kards news 3       # 只看倒数第 3 条动态（1-20）
kards news list    # 列出最新 20 条动态的标题和序号
kards 动态列表
kards newslist
kards dynlist
```

拉取官方最新动态，按「标题 / 正文段落 / 链接」的原始顺序输出。一条动态会拆成
多段，每段单独发一条「文字 + 图片」消息，第 N 段配第 N 张图；正文不再做字数
截断，短段落会合并到同一段，单段最多 200 字，超长段落按句末标点自然拆开。

`kards news <n>` 表示「拉取倒数第 n 条动态」：插件会拉最新 n 条，只发出最后
一条，编号仍标为 n，方便和列表对应。`n` 取值范围 1-20，超出会提示范围。
`kards news list`（或 `kards 动态列表` / `kards newslist` / `kards dynlist`）
列出最新 20 条动态的标题、类型和发布时间，再用 `kards news <序号>` 读取对应
动态。

长动态（专栏 / opus）在 feed 里只有带 `...` 的摘要，插件会自动再请求
`opus/detail` 补全全文；视频动态会取完整的视频简介；转发动态会补全被转发
原文。所有类型都会先尝试拉取完整原文，拿不到时才用 feed 自带内容，因此不会
因为摘要截断而丢正文。

配图不足时，后面的段会自动补一张 64×64 纯白占位图（`data/plugin_data/bilikards/picture/blank.png`，缺失
时自动生成），所以开启配图时不会出现纯文本段。`max_pics_per_item` 控制单条
动态最多取几张图，超出的图片不下载、对应段用占位图。关闭 `send_pics` 或把
`max_pics_per_item` 设为 0 时只发分段文字，不补占位图。

配图会先按动态 ID 归档到本地，同一张图只下载一次；不再把多张图拼成长图，避免
打乱图文对应关系。

```text
kards now 虎式
kards now kards
```

搜索 B 站视频，输出前 3 条的标题、UP 主和播放量。

### 卡组码

```text
kards deck %%53|5ucCbn;5W6LctbjdicAbO;6Q7qcEd7ddd4;cy
kards 卡组 <卡组码>
kards 卡组代码 <卡组码>
kards 卡组码 <卡组码>
kards code <卡组码>
kards deckcode <卡组码>
kardsdeck <卡组码>
```

主国与卡表之间的竖直分隔符 `|` 也可写成 `～`（例如 `%%53～5ucCbn;...`）。

生成两张图片：

1. **卡图墙**：固定 8 × 5 = 40 个卡位，卡图边贴边铺满。39 张主卡组卡牌 +
   1 个总部占位；不足 40 张时空位保留，方便直观看到缺卡。
2. **信息图**：主国/盟国、完整卡表、费用曲线、单位/指令/反制数量、平均费用、
   稀有度统计、校验提示和完整卡组码。

发送顺序是「说明文字 + 卡图墙 + 信息图」。QQ 官方接口一条消息只能带一个媒体，
所以实际到达顺序是「文字 + 卡图墙」一条、「信息图」一条。图片生成失败时自动
退回纯文字卡表；把配置项 `deck_image` 设为 `false` 则始终只发文字。

### 卡牌搜索、别名与 ID（聊天）

```text
kards s 虎式                   # 搜索卡牌（也可用 search/搜索/查卡）
kards s 德国 坦克 6K2/5/4 闪击  # 多条件搜索：费用6 行动2 攻5 防4
kards alias 虎式坦克 E 型 ～ 虎E  # 添加别名
kards alias list               # 查看别名库
kards alias del 虎E            # 删除别名
kards import                   # 生成二位临时短码，发卡图后先出草稿
kards import <ID/JSON/文件名>  # 按指定 ID 导入，或私聊发卡图识别并导入
kards confirm / 回复“同意”     # 把草稿同步到正式卡表
kards cancel                   # 放弃待确认草稿
kards fix <字段> <新值>        # 修正刚导入卡牌的识别错误（如 kards fix 攻击 6）
kards fix <卡牌> <字段> <新值> # 修正指定卡牌字段；kards fix <卡牌> 查看当前字段
kards setid 临时ID/卡名 永久ID  # 将 DIY 临时 ID 设为永久 ID（2 位字母/数字）
kards add 虎式 [x2]            # 手动卡组加卡（衍生牌不可加入）
kards del 虎式 [x2]            # 手动卡组删卡
kards list / kards show        # 查看手动卡组文字 / 图片
kards clear                    # 清空手动卡组
kards setmain 德国 / setally 日本  # 设置手动卡组主国 / 盟国
```

搜索结果超过 15 张时会自动分页，每张卡图拼图最多 15 张卡，并提示发送 `#` 查看下一页（也可用「下一页」或 `next`）。搜索状态保留 5 分钟。

搜索结果列表带序号；发送 `#第N个`（群聊可发 `/#第N个` 或 `##第N个`）会单独发送第 N 张卡的卡图和完整 JSON 信息，优先按当前页序号，超出本页时按全部结果序号。搜索结果只有 1 张时不发拼图，直接发送该卡的单独卡图和完整 JSON 信息。

说明：

- 搜索支持名称、别名、描述、词条、标签及属性条件；不同关键词用空格分隔，费用支持 `nK` / `nKm`，攻防支持 `5/4` 或 `攻5 防4` 等写法。卡名匹配忽略空格，`MONSOONRAIN` 与 `MONSOON RAIN`、`第16步兵旅` 与 `第 16 步兵旅` 等价。
- 手动卡组按用户隔离，保存在 `data/plugin_data/bilikards/manual_decks.sqlite3`：聊天按平台+发送者区分，网页按仪表盘账号区分；同一用户在聊天与网页看到同一份卡组，修改即时生效。衍生牌（Token）不可加入卡组。
- 卡牌筛选、字段修正和网页组卡也可在 AstrBot 仪表盘插件页面「手动卡组」操作。
- 卡图识别不带 ID 时会生成二位 base62 临时短码，识别结果先存草稿；确认后才写入卡表。也可用 `kards setid <短码> <正式ID>` 在确认前改号。
- 通过卡图识别导入时会尝试使用视觉模型生成标签文本并一并写入。
- 卡图识别字段有误时，除聊天 `kards fix` 外，也可在网页「手动卡组」卡牌库里点该卡的「修」按钮直接改字段并保存。

### 别名库（聊天）

别名库保存在 `data/plugin_data/bilikards/card_aliases.json`，聊天指令和插件页面共用，别名全局唯一。

```text
kards alias 虎式坦克 E 型 ～ 虎E     # 添加别名
kards alias list                    # 查看别名库
kards alias del 虎E                 # 删除别名
kards 别名 虎式坦克 E 型 ～ 虎E
```

- 用全角波浪号 `～`（半角 `~` 也可以）分隔卡名和别名，两边顺序不限，写卡名或写别名都可以，只要其中一边能唯一匹配到卡牌。
- 添加已属于其他卡牌的别名时，会自动从原卡牌移走，避免歧义。
- 搜索时精确别名优先，其次是前缀和包含匹配。

### 卡牌更新（聊天）

```text
kards import                         # 生成二位临时短码，进入“发卡图 → 草稿 → 同意”流程
kards import new_cards.json          # 读取数据目录或插件目录内的文件
kards import [{"importId":"Zz",...}] # 直接粘贴 JSON
```

- 要导入旧的 `new_cards.json`，请显式写文件名 `kards import new_cards.json`。

- 支持 Fards 的 `cards.json` 数组、`{"cards": [...]}`，以及简化字段的单个对象。
- 导入前自动备份为 `data/plugin_data/bilikards/deck_cards.json.bak.YYYYmmdd_HHMMSS`。
- 已有卡牌按 `cardId` / 名称匹配后更新；只写部分字段时，未提供的字段保留原值。
- 新卡缺少 `importId` 时会自动生成一个不冲突的两字符短码。
- 导入后立刻出现在卡牌库、搜索和别名库中，无需重启。

### 卡图识别导入（插件页面）

「卡牌更新」支持直接上传新卡卡图：

1. 选择一张或多张卡图（JPG / PNG / WebP）。
2. 为每张图填写卡牌 ID（官方图片文件名，如 `101_infantry_regiment`）。
3. 上传图片会先本地裁掉卡图外的边框、文字或界面内容；如果一张图里有多张卡，
   会自动切成单张卡图并逐张识别。第一张用填写的 ID，其余使用 `DIY-` 临时 ID，
   之后可用 `kards setid` 改成永久 ID。
4. 视觉模型会读取中文名、英文名、阵营、类型、稀有度、费用、行动费用、攻击、
   防御、描述和关键字。
5. 每张卡图会保存为 `data/plugin_data/bilikards/deck_card_thumbs/<卡牌 ID>.jpg`，卡组图片和搜索拼图
   会直接使用它。

识别使用的视觉模型由插件配置 `vision_provider_id` 指定，留空时自动选择一个
支持图片输入的 provider。页面顶部下拉框可以临时切换模型。

### 卡图识别导入（QQ 私聊）

在同一个会话里发送：

```text
kards import
（1 分钟内发送卡图，识别后回复“同意”）
```

或指定 ID 直接写入：

```text
kards import 101_infantry_regiment
（1 分钟内发送卡图）
```

1. 不带参数会随机生成二位 base62 短码，识别结果先存草稿；回复“同意”或 `kards confirm` 后才同步到正式卡表，`kards cancel` 放弃。指定 ID 时仍直接写入。以上命令均受下方「管理员权限与审核流程」约束。
2. 机器人在当前会话记录该 ID，有效期 **1 分钟**；期间只有发命令的同一个发送者有效。
3. 这 1 分钟内在该会话发送的卡图都会按该 ID 识别；每次识别会先裁掉卡图外的内容，
   若一张图里有多张卡则逐张识别，第一张用指令里的 ID，其余使用 `DIY-` 临时 ID。
   识别结果会回复名称、阵营、类型、稀有度、费用、攻防和关键字；不带参数时进草稿，确认后才写入卡表。
4. 卡图同样保存为 `data/plugin_data/bilikards/deck_card_thumbs/<卡牌 ID>.jpg`。

如果一条消息里同时带指令和卡图，会直接识别，并继续为该会话保持 1 分钟识别窗口。

没有执行 `kards import <卡牌 ID>` 时，只有图片说明文字或文件名是**卡表里已有的卡牌 ID / 导入 ID**
才会触发识别（例如 `101_infantry_regiment`、`01`）。随机文件名和机器人生成的拼图名会被忽略。
全新卡牌 ID 请先用 `kards import <卡牌 ID>` 显式声明。

### 管理员权限与审核流程

会改动本地卡表或卡牌草稿的调用只有管理员能直接执行：

| 受管控调用 | 聊天命令 | LLM 工具 |
| --- | --- | --- |
| 卡牌导入 | `kards import` | `kards_import` |
| 确认写入卡表 | `kards confirm`、纯文本「同意」 | `kards_confirm` |
| 放弃草稿 | `kards cancel`、纯文本「取消」 | `kards_cancel` |
| 修改卡牌字段 | `kards fix <字段> <新值>` | `kards_fix` |
| 设置永久 ID | `kards setid <临时ID> <永久ID>` | `kards_setid` |

管理员判定同时看两处：AstrBot 事件的 `event.is_admin()`，以及 `data/cmd_config.json`
里的 `admins_id`。两者任一命中即可直接执行。

其他人执行上述调用时**不会写入卡表**，而是收到一个 `RV-` 开头的审核编号，
请求进入 `data/plugin_data/bilikards/admin_reviews.json`。管理员按下面流程处理：

```text
kards review                 # 查看待审核列表
kards approve RV-a1b2c3      # 批准：按原提交者身份执行该操作
kards reject RV-a1b2c3       # 驳回：不改卡表、不改草稿
kards approve RV-a1b2c3      # 对应 LLM 工具：kards_approve / kards_reject / kards_review
```

补充规则：

- 附图导入在提交时就已完成识别并保存为草稿条目，批准后直接写入，管理员不需要重发图片。
- `kards fix <卡牌>` 只查看字段，属于只读操作，所有人可用；只有带新值的修改才需要审核。
- 审核记录保留 `pending` / `approved` / `rejected` 状态与处理人，重复批准或驳回会被拒绝。
- 审核回放使用原提交者的 sender id，因此草稿确认、最近导入卡定位都按原提交者上下文生效。
- 插件页面的 `manual/cards/import` 与 `manual/cards/import-image` 同样会写卡表，
  但它们走 AstrBot 仪表盘登录鉴权（本身只有管理员能进仪表盘），不走 QQ 审核队列。

### 帮助

```text
kards
kards help
```

无参数或无法识别的子命令会输出内置帮助。

## 手动卡组插件页面

打开 AstrBot 仪表盘，进入「插件」→「bilikards」→「manual_deck」，或直接访问：

```text
http://<AstrBot 地址>:6185/plugin-page/bilikards/manual_deck
```

页面能力：

- 左侧卡牌库：名称（中文/英文/卡牌 ID）、别名、关键字/描述三个独立搜索框；阵营、类型、稀有度筛选；费用/攻击/防御范围筛选；点 `+` 加一张。没有设置任何筛选时直接列出全部卡牌。
- 卡牌行会显示描述和关键字标签，点右侧「别」展开别名编辑器，可增删该卡的别名。
- 「别名库」面板集中查看和删除所有别名，并显示总数。
- 右侧当前卡组：查看张数、种类、校验提示；点 `−` / `+` 调整数量；设置主国/盟国。
- 「生成图片」：调用与聊天命令相同的渲染器，页面内预览卡图墙和信息图，并显示卡组码。
- 「复制卡组码」：复制生成的 KARDS 导入码，可直接粘回游戏。
- 底部「卡牌更新」：上传卡图并填写卡牌 ID 自动识别导入；也可展开「高级」粘贴官方卡牌 JSON 或上传 `.json` 文件。导入后自动刷新卡牌库。
- 网页和聊天按用户共享各自的卡组，保存在 `data/plugin_data/bilikards/manual_decks.sqlite3`；别名库 `data/plugin_data/bilikards/card_aliases.json` 仍是全局共享。同一用户任一侧修改后刷新即可看到。

页面通过 AstrBot 的插件页面桥接 SDK 调用插件 Web API，不需要额外端口。后端路由：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/v1/plugins/extensions/bilikards/manual/catalog` | 全部卡牌目录，含阵营/类型/稀有度/关键字选项 |
| GET | `/api/v1/plugins/extensions/bilikards/manual/aliases` | 别名库列表 |
| POST | `/api/v1/plugins/extensions/bilikards/manual/alias/add` | 添加别名，body `{"id":"3P","alias":"101团"}` |
| POST | `/api/v1/plugins/extensions/bilikards/manual/alias/remove` | 删除别名，body 同上 |
| POST | `/api/v1/plugins/extensions/bilikards/manual/cards/import` | 导入卡牌，body `{"cards":[...]}` 或直接数组 |
| POST | `/api/v1/plugins/extensions/bilikards/manual/cards/import-image` | 卡图识别导入，body `{"id":"101_infantry_regiment","image":"data:image/jpeg;base64,...","provider":"..."}` |
| GET | `/api/v1/plugins/extensions/bilikards/manual/vision/providers` | 可用的图片识别模型列表与默认值 |
| GET | `/api/v1/plugins/extensions/bilikards/manual/deck` | 当前手动卡组 |
| POST | `/api/v1/plugins/extensions/bilikards/manual/add` | 加卡，body `{"id":"3P","count":1}` |
| POST | `/api/v1/plugins/extensions/bilikards/manual/remove` | 减卡，body 同上 |
| POST | `/api/v1/plugins/extensions/bilikards/manual/set` | 设置阵营，body `{"main":"德国","ally":"美国"}` |
| POST | `/api/v1/plugins/extensions/bilikards/manual/clear` | 清空卡组 |
| POST | `/api/v1/plugins/extensions/bilikards/manual/render` | 生成两张图片，返回 data URL 和卡组码 |
| GET | `/api/v1/plugins/extensions/bilikards/manual/thumb?card_id=...` | 读取内置卡图缩略图 |

`astrbot_plugin_bili_kards` 和 `bilikards` 两种插件名前缀都已注册。

## 配置项

配置文件：`data/config/astrbot_plugin_bili_kards_config.json`（由 `_conf_schema.json` 生成）。

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `kards_uid` | string | `2127596945` | KARDS 官方 B 站 UID |
| `max_items` | int | `5` | 每次展示的动态条数 |
| `cache_ttl` | int | `300` | 动态缓存秒数，`0` 表示不缓存 |
| `send_pics` | bool | `true` | 是否下载并发送动态配图 |
| `max_pics_per_item` | int | `9` | 每条动态最多发送的图片数，`0` 表示不发图 |
| `deck_image` | bool | `true` | 卡组码是否生成图片，`false` 时只发文字 |
| `vision_provider_id` | string | `openai-responses/Sage-S2.1-Flash` | 卡图识别使用的视觉模型，留空自动选择；失败时自动回退到其他图片模型 |

## 动态是怎么刷新的

- **按需拉取**：只有执行 `kards news` / `kards 动态` 时才请求 B 站，没有后台轮询，
  不执行命令就不产生流量。
- **内存缓存**：同一进程内按 `cache_ttl`（默认 300 秒）复用上一次结果。TTL 内
  重复发送命令会直接返回缓存，不会再打 B 站接口。
- **失败回退**：拉取失败时如果有旧缓存，会继续使用旧数据并在日志里记录警告；
  没有缓存则回复「官方动态拉取失败，请稍后再试」。
- **重启清空**：缓存只在内存里，AstrBot 重启后第一次查询一定重新拉取。
- **配图缓存**：图片按动态 ID 永久归档在 `data/plugin_data/bilikards/picture/<动态ID>/`，
  `data/plugin_data/bilikards/picture/index.json` 记录 URL 哈希，同一张图不会重复下载；目前没有自动清理。

想强制刷新时，发送 `kards 刷新动态`（或 `kards refresh`）跳过内存缓存；也可以把
`cache_ttl` 设为 `0`，或重启 AstrBot。

## 冷却与限流

- 所有 `kards` 网络命令按发送者统一 3 秒冷却；手动卡组的增删改查不触发冷却。
- 动态和视频请求都带 wbi 签名；游客身份下会先访问首页激活 `buvid3`，降低 412 风险。
- 视频搜索使用 `Semaphore(3)` 限流，失败会自动清签名重试一次。

## 文件结构

```text
astrbot_plugin_bili_kards/                 只读代码与随包种子资源
├── main.py                命令注册、子命令分发、消息链组装、冷却
├── metadata.yaml          插件元数据（名称、作者、版本、描述）
├── _conf_schema.json      配置项定义
├── data_paths.py          统一持久化路径与旧数据一次性迁移
├── deck_code.py           卡组码归一化、base64/varint 解码、校验、文字渲染
├── deck_cards.json        随包种子卡表；首次运行复制到数据目录，之后只读
├── deck_image.py          卡图墙 + 信息图渲染，卡图下载与本地缓存
├── manual_deck.py         手动卡组读写、阵营推断、别名库、卡表查询与导入、合成导入码
├── web_api.py             手动卡组 Web API 路由（卡组、别名、卡牌导入、DIY 卡渲染/保存/列表）
├── diy_card.py            DIY 卡面渲染入口（优先模板，缺失时回退自绘，500×702）
├── card_template.py       真实 KARDS 模板渲染器（按需加载素材、字体缓存限 128 条）
├── assets/kards_builder/  粉丝项目模板素材（Templates + Fonts，约 2.6 MB，非商业使用）
├── fonts/                 模型生成的自带 TTF 字体与 font_profile.json
├── views/diy_editor/      DIY 卡编辑器插件页面
├── views/manual_deck/     手动卡组插件页面
├── dynamic_fetch.py       动态时间线抓取、wbi 签名、字段解析
├── bili_fetch.py          B 站通用只读接口（搜索、视频、用户等）
├── media_cache.py         动态配图下载、判重、归档、长图拼接
├── news_fetch.py          预留的 Steam 新闻源模块（当前未接入）
├── tools/                 卡表同步等开发工具
└── tests/                 卡组码解析单元测试

AstrBot/data/plugin_data/bilikards/       全部运行时持久化数据
├── deck_cards.json        运行时卡表（新卡导入实际写入这里）
├── manual_decks.sqlite3   手动卡组（按用户隔离）
├── manual_deck.json       旧卡组文件，仅作初始迁移
├── card_aliases.json      卡牌别名库
├── diy_cards.json         DIY 编辑器保存的自绘卡设计（与官方卡表隔离）
├── pending_imports.json   私聊“先指令后图片”待导入状态
├── new_cards.json         可选的待导入官方卡牌文件
├── deck_card_thumbs/      内置/导入卡图缩略图（1662 张）
├── deck_card_cache/       运行时下载的卡图缓存
├── deck_images/           生成的卡组图片缓存
├── picture/               动态配图归档、index.json 与 blank.png
├── monitor/               动态监控状态与归档
├── temp/                  临时文件
└── t2i_templates/         图片模板（如存在）
```

## 卡组码解析细节

1. 归一化输入：容忍缺少 `%%` 前缀、全角符号、空格和异常分号。
2. 校验版本段，base64 解码后按 varint 读取主国段和盟国段。
3. 用导入短码在 `deck_cards.json` 中反查卡名、阵营、类型、稀有度、费用和攻防。
4. 本地卡表没有的短码不报错，标记为「未收录」并写入 `warnings`。
5. 结构校验：总 40 张（含 1 张总部）、主国至少 28 张、盟国最多 12 张；
   同名牌超过稀有度上限也会提示。
6. 手动卡组没有官方导入码，插件会用固定规则合成一个：`%%主国码盟国码|1张卡;2张卡;3张卡;4张卡`，
   这样可以直接复用现有解码器和渲染器。

## 测试

```text
cd AStrBot
.venv/bin/python -m unittest discover -s data/plugins/astrbot_plugin_bili_kards/tests
```

也可以直接跑一次手动卡组渲染：

```text
.venv/bin/python - <<'PY'
import asyncio
from astrbot_plugin_bili_kards import manual_deck, deck_image

deck = manual_deck.load_deck()
decoded = manual_deck.build_decoded(deck)
cards, info = asyncio.run(deck_image.render_deck_images(decoded))
print(cards, info)
PY
```

（需要先把插件目录加入 `PYTHONPATH`，或在插件目录内运行。）

## 常见问题

**图片里中文是方块**

安装中文字体，或把 `deck_image.py` 里的 `FONT_PATH` 改成系统已有字体路径。

**卡图墙有空位**

卡图墙固定 40 格。标准卡组是 39 张主卡 + 1 张总部；手动卡组不足 39 张时，
空位会保留，信息图里会提示还差多少张。

**动态没有配图**

检查 `send_pics` 是否为 `true`、`max_pics_per_item` 是否大于 0；再看日志里
是否有「配图缓存失败」。B 站图片需要外网访问，网络不通时会退化为纯文字。

**卡组码解析失败**

确认复制的是完整导入码（通常以 `%%` 开头）。缺字符、混入聊天文本或版本段
不完整都会失败。

**插件页面打不开**

确认 AstrBot 版本支持 `register_web_api` 和插件页面（`views/` 目录）。日志里
应出现 `Manual deck Web API routes registered`；没有这行说明当前版本不支持。
