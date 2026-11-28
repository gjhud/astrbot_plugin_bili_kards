# bilikards 更新日志

## 2026-11-28 管理员权限与审核流程（v2.0.0 增量）

### 新增
1. **管理员门禁**：`import`、`confirm`、`cancel`、`fix`、`setid` 五类会改动卡表或草稿的调用，
   聊天命令与 LLM 工具统一要求管理员权限。管理员判定为 `event.is_admin()` 或
   `data/cmd_config.json` 的 `admins_id` 命中。
2. **审核队列**：非管理员调用不执行，改为在 `data/plugin_data/bilikards/admin_reviews.json`
   生成 `RV-` 编号的待审记录，回执写明编号、提交内容与批准/驳回命令。
3. **审核命令**：`kards review` 看待审列表，`kards approve <编号>` 批准，
   `kards reject <编号>` 驳回；对应 LLM 工具 `kards_review`、`kards_approve`、`kards_reject`。
4. **附图导入审核**：提交时以 `commit=False` 先识别并保存草稿条目，批准后直接写入，
   管理员无需重发卡图。
5. **按原提交者回放**：批准时用 `_ReviewEvent` 保留原 sender id 与会话 id，
   草稿确认、最近导入卡定位都按原提交者上下文生效。
6. **补齐命令分发**：帮助中已有的 `kards confirm` / `kards cancel` 此前没有真正分发，
   现已可用；`kards fix <卡牌>` 查看字段仍为只读，所有人可用。

### 改动文件
- `main.py`：管理员判定、审核队列、门禁、审核命令与 LLM 工具、帮助文本。
- `data_paths.py`：新增 `ADMIN_REVIEW_FILE`。
- `README.md` / `help.txt`：补充权限说明与审核流程。

### 验证
- `py_compile` 通过，AstrBot 热重载通过；回归见本地测试脚本输出。

## 2026-11-28 v2.0.0 正式发布

版本号从 `v1.0.0` 升至 `v2.0.0`，`metadata.yaml`、`main.py` 的 `@register`、`README.md`、
`help.txt` 已统一。本版本包含自 v1.0.0 以来的全部功能增量：

- **DIY 卡编辑器**：网页页面 `diy_editor` 与聊天端 `kards diy`，真实 KARDS 模板渲染，
  自带字体优先、系统字体兜底，模板缺失自动回退自绘。
- **DIY 单卡编辑语义**：一次进入只编一张卡，字段增量修改并每次自动保存；
  `kards diy last` 续编最近一张、`kards diy new` 强制新建、`kards diy show <ID>` 按 ID 续编。
- **DIY 退出**：单独发 `退出` / `exit` / `结束` / `quit` / `bye` 即可退出并保存；
  退出前做防御性保存，回执写明卡名、ID、是否含卡图与续编命令。
- **DIY 读取本地卡表**：`kards diy load <卡名/ID>` 与网页「读取 JSON + 卡图」按钮，
  本地卡 13 字段完整展示并从 `deck_card_thumbs` / `deck_card_cache` 读取卡图，不联网。
- **插画变换**：`插画=x，y~z` 紧凑格式与 `插画x/插画y/缩放` 独立字段，
  坐标按最终输出图 500×702 像素计算，支持位移与 0.2–5.0 缩放。
- **卡牌识别与导入**：`kards import` 支持两位临时短码、指定 ID、JSON/文件与截图识别；
  草稿需「同意」后才写正式卡表，`kards confirm` / `kards cancel` / `kards fix` / `kards setid` 配套。
- **类型与排版修复**：炮兵等别名归一化，未知类型回退步兵；指令卡名上移、描述字号自适应、
  标点基线校准，数字位置按像素质心复核。
- **搜索体验**：`#第N个` 选取、单结果直发卡图与 JSON、搜索 TTL 300 秒、JSON 增加稀有度/ID/国家、
  单卡图无背景、网页双击预览。
- **watchmode 协作**：普通进度白底黑字，`【需答复】` 红底白字；长文本按行自动拆成多张图，
  避免 QQ 缩放导致文字模糊。

仓库说明：公开仓库 `gjhud/astrbot_plugin_bili_kards` 不含真实 KARDS 模板素材与衍生字体，
克隆后自动回退到自绘模板与系统字体；本地个人使用可自行放置这些素材。

## 2026-11-28 DIY 载入本地卡表原有卡牌（JSON + 卡图）

### 新增
1. **聊天端 `kards diy load <卡名/ID>`**（别名 `读取` / `载入` / `加载` / `读卡` / `from`）
   - 用 `manual_deck.find_cards()` 按卡名/短 ID/别名查找，找不到再按 `card_id` 精确匹配。
   - 先发完整原始 JSON，再把提示与卡图放在同一条 `chain_result` 里；保存为新的 `DIY-NN`
     并自动进入 DIY 模式，后续消息照常增量编辑。
   - 原始字段保留在 `source_card_id` / `source_import_id` / `operation_cost`，方便溯源。
2. **网页编辑器「读取 JSON + 卡图」**
   - 新增只读 `card-json` 框展示原始 JSON，载入后自动填表并渲染预览。
   - 新增 Web 路由 `POST /api/plug/bilikards/manual/diy/load`。
3. **共用的本地卡图读取**
   - `web_api._local_card_art_url()` 复用 `card_image_data` 的查找顺序：
     `deck_card_thumbs/<id>.jpg` → `deck_card_cache/<id>.webp` / `.jpg` / `.png`，
     只读本地文件、不联网。`card_image_data` 也改为调用该函数。

### 验证
- `verify_localcard.py`：`1005th_rifles` → `步兵第 1005 团`（`9y`），`has_art=True`，
  卡图 `data:image/webp;base64,...` 34771 字符；中文名 `V-1 飞行炸弹` 也命中；
  不存在的名字返回明确错误。聊天 load 回 2 条消息并存为 `DIY-01`，后续改 `防=9` 正常落盘。
- `test_diy_load_route.py`：Web 路由返回 `ok=true` 与完整 14 个 JSON 字段。
- `py_compile` 通过，热重载 `200 ok`；测试数据已还原，用户原有 `飞虎队 / DIY-01` 保留。

### 待办
- 插画调整比例：沿用现有 `art_scale` / `插画=~z`，收到具体比例后设为载入时的默认值。

### 备份
- `C:\\Users\\Administrator\\Desktop\\备份\\diy_localcard_20261128_225616`：
  含改动前的 `main.py`、`web_api.py`、`views/diy_editor/index.html`、`README.md`、
  `CHANGELOG.md`、`diy_cards.json`。

## 2026-11-28 DIY 退出与插画存储说明

### 改动
1. **退出后看起来没保存**
   - 字段修改本来就每次都写 `diy_cards.json`，但旧退出回执只有「卡片 DIY-01 已保存」，
     没写卡名与继续编辑方法；重新发 `kards diy` 又是空白新卡，容易造成误解。
   - `main.py` 新增 `_diy_exit()`：退出前再做一次防御性保存，回执改为
     「卡片 ID + 卡名 + 是否含卡图 + 继续编辑命令」。
   - 新增 `kards diy last`（别名 `最近` / `继续` / `resume` / `上次`）继续最近一张；
     `kards diy new`（别名 `新建` / `create`）强制新建；`kards diy` 保持新建语义不变。
2. **退出要单独发「退出」「exit」「结束」**
   - `diy_mode_message()` 在字段解析前识别 `DIY_MODE_EXIT_ALIASES`：
     `退出` / `exit` / `结束` / `quit` / `bye` / `退出diy` / `bye diy`，并兼容 `#`、`/`、`?` 唤醒前缀
     与全角空格。实测 8 种写法均成功退出且只回一条消息。
   - `DIY_MODE_TIP`、`HELP`、`DIY_HELP_TEXT`、`README.md` 同步说明。
3. **插画存储机制说明**（行为未改，仅文档化）
   - DIY 卡：附图转 `data:image/png;base64,...` / `data:image/jpeg;base64,...` data URL，
     直接写进 `diy_cards.json` 的 `art` 字段，不单独落图片文件；渲染时 `web_api._diy_art()` 解码。
   - 截图导入卡（`kards import`，含 `DIY-` 临时 ID）：JPEG 缩略图写到
     `deck_card_thumbs/<card_id>.jpg`，字段写进 `deck_cards.json` / `manual_decks.sqlite3`。

### 验证
- `verify_diy_exit.py`：8 种退出写法 `active=False`；编辑后退出保存
  `飞虎队 / fighter / 描述=测试，标点。`；`kards diy last` 恢复后写入 `defense=5` 成功。
- `verify_art_roundtrip.py`：data URL 经 `web_api._diy_art()` 解码为 `(12, 8)` 图像。
- `py_compile` 通过；测试数据已还原，`diy_cards.json` 保留用户原有的 `飞虎队 / DIY-01`。

### 备份
- `C:\\Users\\Administrator\\Desktop\\备份\\diy_exit_art_20261128_224515`：
  含改动前的 `main.py`、`README.md`、`CHANGELOG.md`、`diy_cards.json`。

## 2026-11-28 四项修复：炮兵类型 / 提示与卡图同发 / 描述字号复核 / 标点基线

### 修复
1. **类型「炮兵」被渲染成反制**
   - `main.py`：新增 `_TYPE_VALUE_ALIASES`，`炮兵` / `炮` / `火炮` / `火箭炮` / `自行火炮` /
     `反坦克炮` / `榴弹炮` 统一解析成 `artillery`；`歼击机` → `fighter`，`轰炸` → `bomber`，
     `反制牌` → `countermeasure`，`指令牌` → `order`。
   - `card_template.py`：`CARD_TYPE_ALIASES` 兜底，未知类型不再静默落到反制模板，而是回退步兵。
   - 实测：`类型=炮兵` 解析为 `artillery`，`_template_path()` 得到 `artillery.png`，保存值也是 `artillery`。
2. **保存提示单独发一条**
   - `_diy_render()` 新增 `caption` 参数，成功时把 `Plain(caption)` 与 `Image` 放进同一个 `chain_result`。
   - 模式内每次修改、命令新建、`save`、`show` 都改为「提示 + 卡图」一条消息，不再先发纯文本再发图。
3. **效果描述字号（复核）**
   - 上一轮的自适应字号正常：单位 4/28/81/224 字选 `20/20/18/12`，指令选 `20/20/16/11`，
     底边最大 687 px，未越框。本轮未改动该逻辑。
4. **效果标点看起来全是单引号**
   - 根因：`_draw_body()` 使用 `anchor="lt"`，PIL 会按**墨迹顶部**对齐而不是按字体基线对齐，
     导致 `，。：、！？` 被顶到每行最上方，视觉上就是一排单引号。
   - 修复：去掉 `anchor="lt"`，恢复默认 ascender 基线。实测「中」墨迹从 y=604 起，
     「，」从 y=619 起、「：」从 y=614 起，标点回到字下方正确位置。

### 验证
- `py_compile`：`main.py`、`card_template.py` 通过。
- `verify_4issues.py`：4 项全部通过；模式内每次修改只回 1 条消息，`parts` 为 `Plain` + `Image`。
- `verify_desc_all.py`：8 组描述渲染全部合规，底边最大 687 px < 692.4 px 上限。
- `unittest discover`：7 项 `OK`；聊天回归测试通过并清理测试数据；热重载 `200 ok`；
  Web 路由短/长描述出图 `215053` / `231973` 字节且内容不同。

### 备份
- `C:\\Users\\Administrator\\Desktop\\备份\\four_issues_20261128_222052`：含改动前的
  `main.py`、`card_template.py`、`deck_code.py`、`README.md`、`CHANGELOG.md`。

## 2026-11-28 指令卡名上移 + 效果描述字号自适应

### 改动
- `card_template.py`：指令/反制卡名中心由模板 `y=486` 改为
  `486 - 10 / OUT_PER_TEMPLATE_Y`，即按最终输出图口径精确上移 10 px；
  单位卡名位置不变。
- `card_template.py`：效果描述从固定字号 `15` 改为自适应。新增常量
  `BODY_MIN_SIZE=11`、`BODY_MAX_SIZE=20`、`BODY_BOTTOM=613`，以及
  `_adaptive_body_size()`：从最大字号逐档下调，取「按 360 px 宽度换行后的
  行数 × 字号 × 1.2 不超过剩余高度」的最大字号；`_draw_body()` 按同一高度
  公式计算最大行数，极端长文本在末行追加 `…`，保证不越出卡框。
  - 单位（无关键词起始 y=504 / 有关键词 y=534）与指令（y=536）各自按剩余高度计算。
  - 字号只影响描述，卡名、关键词、费用、攻防数值均不受影响。

### 验证
- `py_compile`：`card_template.py` 通过。
- 指令卡名位移：模板坐标减少 `10 / OUT_PER_TEMPLATE_Y = 8.447`，折算输出图正好 10 px；
  暗色像素质心在收紧区域 `x=100..400, y=505..575` 实测 `DY=-9.75`，与 10 px 一致（差值来自栅格化）。
- 描述自适应 `verify_desc_all.py`：单位 4/28/81/224 字分别选 `20/20/18/12`，
  指令分别选 `20/20/16/11`；8 组全部渲染成功，描述底边最大 687 px，低于 692.4 px 上限，
  未越框；超长文本自动截断且仍可读。
- `unittest discover`、`py_compile`、热重载与 Web 路由回归见下方验证命令。

### 备份
- `C:\\Users\\Administrator\\Desktop\\备份\\ordername_descsize_20261128_221214`：含改动前的
  `card_template.py`、`README.md`、`CHANGELOG.md`。

## 2026-11-28 插画位置紧凑格式兼容（x，y / ~z）

### 新增
- `main.py`：`DIY_FIELD_ALIASES` 新增 `art_pos` 及别名 `插画`、`插画位置`、`位移`、`位置`、
  `art_pos`、`artpos`、`artxy`、`artoffset`，支持三种紧凑写法（逗号支持半角 `,` 与全角 `，`）：
  - `插画=x，y~z`：一次设置左移 x px、上移 y px、缩放 z 倍
  - `插画=x，y`：只设位移，缩放不变
  - `插画=~z`：只设缩放，位移不变
  - x/y 支持正负小数；`~z` 的 `z` 即缩放倍数，沿用 0.2–5.0 限制与 ±200 px 位移限制
- `main.py`：新增 `_ART_COMPACT_RE` 与 `_diy_parse_art_pos()`，在 `_diy_parse_fields()` 中把紧凑
  写法展开为 `art_x` / `art_y` / `art_scale`，与独立字段混用时以最后出现的字段为准；
  格式非法时返回明确提示且不写入错误值。紧凑写法只写入显式给出的子字段，未给的部分保留旧值，
  与「一次进模式只编一张卡、增量保存」语义一致。

### 改动
- `DIY_HELP_TEXT`、`README.md`：补充紧凑格式、别名、三种写法示例与错误提示。

### 验证
- `py_compile`：`main.py` 通过。
- 字段测试 `test_art_compact.py`：全角/半角逗号、负数、`~z` 单独、仅 x/y、与其他字段混用、
  `位移` 别名、旧独立字段兼容全部通过；非法输入 `插画=abc`、`插画=1~1.2` 均返回格式提示且
  不写入错误值；模式内增量保存验证通过，后续修改不会覆盖已设的位移/缩放。
- 测试数据已还原，未污染 `diy_cards.json`。

### 备份
- `C:\\Users\\Administrator\\Desktop\\备份\\art_format_20261128_213938`：含改动前的
  `main.py`、`README.md`、`CHANGELOG.md`。

## 2026-11-28 插画位移与自定义放缩

### 新增
- `card_template.py`：`_paste_art()` 新增 `art_x` / `art_y` / `art_scale`：
  - 位移以最终输出图 500×702 像素为单位，正值 `x` 左移、`y` 上移，负值反向；
    内部按 `OUT_PER_TEMPLATE_X/Y` 换算成模板坐标，保证用户说的 1 px 就是出图上的 1 px。
  - 缩放 `1` 为默认 cover 铺满，`>1` 放大并居中裁切，`<1` 缩小并用图片平均色补边，
    避免位移或缩小时出现空洞。位移限制 ±200 px，缩放限制 0.2–5.0。
- `main.py`：`DIY_FIELD_ALIASES` 新增 `插画x/左移/x`、`插画y/上移/y`、
  `插画缩放/缩放/放缩/zoom`，并把这三个字段按 `float` 解析；它们与卡图一起增量保存。
- `views/diy_editor/index.html`：新增「插画左移 / 上移 / 缩放」三个输入框并写入 spec。

### 改动
- `DIY_HELP_TEXT`、`README.md`：补充字段别名、方向约定、单位与示例。

### 验证
- `py_compile`：`card_template.py`、`main.py` 通过。
- 像素验证 `verify_art_offset.py`：白块质心默认 `(251.5,274.5)`；`art_x=1 art_y=9` 为
  `(-1.0,-9.5)`；`art_x=-1 art_y=-9` 为 `(+1.5,+9.5)`；`缩放=1.4` 白块面积 1.99 倍、
  `缩放=0.7` 为 0.49 倍，方向与幅度符合预期。
- 字段测试 `test_art_fields.py`：中文别名、短别名、JSON、模式内增量保存全部通过。
- Web 路由带 `art` + 位移/缩放参数：`200`、`500×702`、出图字节与普通渲染不同。
- `unittest discover`：7 项 `OK`；聊天回归测试通过并清理测试数据；热重载 `200 ok`。

### 备份
- `C:\\Users\\Administrator\\Desktop\\备份\\art_offset_20261128_211148`：含改动前的
  `card_template.py`、`main.py`、`views/diy_editor/index.html`、`README.md`、`CHANGELOG.md`。

## 2026-11-28 单卡编辑模式 + 每次修改自动保存 + 卡名字号校准

### 新增
- `main.py`：DIY 模式改成「一次进入只编一张卡」：
  - `_diy_mode[key]` 保存 `id/spec/art`，条目的 `id` 在整个模式内固定，后续消息只做增量修改。
  - `_diy_save_card()` 每次修改立即写回 `data/plugin_data/bilikards/diy_cards.json`，
    按 ID 更新且不覆盖其他已保存卡；`_diy_next_id()` 跳过已用编号，避免删除后重号。
  - `kards diy` 无参数新建一张空卡并绑定模式；`kards diy 字段=值` / `save` / `show <ID>`
    分别新建/更新/继续编辑一张卡，出图后都绑定该卡。
  - 模式内每条消息：合并字段到当前卡 → 自动保存 → 回一条「已保存并更新：<ID>」→ 出图。
    纯图片消息更新该卡卡图；`id` 字段在模式内被忽略，防止中途换卡。
  - `kards diy exit` 退出时提示卡片已保存，模式状态仅存内存，插件重载后自动清空。

### 改动
- `card_template.py`：按上游 `KARDS-Card-Builder/index.html` 校准卡名字号与颜色。
  - 单位：`22 → 47`，`maxWidth 255`，`bold=True`，中心 `(332,73)`，自动缩至最小 6px。
  - 指令/反制：`22 → 40`，`maxWidth 410`，`bold=True`，中心 `(325,486)`（等效 top 基线 y=466），
    颜色改为上游深色 `#44474d`（原先误用单位浅色，在羊皮纸上几乎看不见）。
- `HELP_TEXT`、`DIY_HELP_TEXT`、`README.md`：同步单卡模式、自动保存与字号说明。

### 验证
- `py_compile`：`main.py`、`card_template.py` 通过。
- 模拟测试 `test_diy_smart_mode.py`：11 项全部通过，包括进入建卡、两次增量合并、纯图片更新、
  `show` 重绑同一张卡、退出后忽略、重新进入新建第二张卡、`save` 只更新指定卡、
  `help/list` 不绑定模式、字段命令新建第三张卡，测试数据已还原。
- 字号像素对比：单位卡名区域同色像素 526 → 2830（约 5.4 倍）；指令卡名深色像素 0 → 1660。
- `unittest discover`：7 项 `OK`；热重载 `200 ok`；Web 路由 `200`，出图 223183 字节 / `500×702`。

### 备份
- `C:\\Users\\Administrator\\Desktop\\备份\\diy_smart_mode_20261128_205533`：含改动前的
  `main.py`、`card_template.py`、`README.md`、`CHANGELOG.md`。

## 2026-11-28 DIY 连续出图模式

### 新增
- `main.py` 新增 `_diy_mode` 会话状态（按 `session_id + sender_id` 隔离）与 `diy_mode_message` 事件钩子：
  - `kards diy` 无参数进入 DIY 模式，后续普通消息直接当DIY字段渲染，不必重复输入命令。
  - `kards diy 字段=值 ...`、`kards diy save ...`、`kards diy show <ID>` 出图后也自动进入模式。
  - `kards diy exit` 退出；兼容 `quit`、`退出`、`退出diy`、`结束`、`bye`。
  - 模式内附图自动作为卡图；`kards diy help/list/show/exit` 等命令消息不会被钩子重复处理。
  - `kards diy` 无参数提示改为模式说明；`help` 单独查看完整用法。
- 模式为进程内存状态，插件重载后自动清空；不写入磁盘，避免留下卡死状态。

### 改动
- `HELP_TEXT`、`DIY_HELP_TEXT`、`README.md`：补充模式用法、退出方式与隔离说明。
- `import_pending_card_image`：DIY 模式下跳过卡图导入钩子，避免把 DIY 卡图误导入正式卡表。

### 验证
- `py_compile`：`main.py` 通过。
- 模拟事件测试 `test_diy_mode.py`：13 项全部通过，包括进入模式、连续字段、JSON、带 `#` 前缀字段、
  命令跳过、退出后忽略、字段命令自动进模式、`help/list` 不进模式、错误字段提示、纯图片出图。

### 备份
- `C:\\Users\\Administrator\\Desktop\\备份\\diy_mode_20261128_204342`：含改动前的 `main.py`、`README.md`、`CHANGELOG.md`。

## 2026-11-28 模板数字位置上移

### 改动
- `card_template.py`：费用 `(145,76)→(145,72)`、攻击 `(222,468)→(222,464)`、防御 `(427,468)→(427,464)`，三个数字在模板坐标上均上移 4 px，对应 500×702 输出图约 5 px。

### 验证
- 渲染前后像素质心对比：费用上移 3.96 px、攻击 4.61 px、防御 4.91 px（输出图坐标）。
- `py_compile` 通过；`unittest discover` 7 项 `OK`；插件热重载 `200 ok`。

### 备份
- `C:\Users\Administrator\Desktop\备份\num_shift_20261128_203636`：含改动前的 `card_template.py`、`diy_card.py`、`README.md`、`CHANGELOG.md`。

## 2026-11-28 接入真实 KARDS 卡面模板素材

### 新增
- `assets/kards_builder/`：从非官方粉丝项目 `KARDS-Card-Builder`（Babaika328）获取的 55 个模板素材文件，共 2.6 MB：
  - `Templates/Units/`：步兵/坦克/火炮/战斗机/轰炸机卡面模板，含暗色费用变体与国家×兵种横幅。
  - `Templates/Orders_Countermeasures/`：指令/反制模板与各国国家图标。
  - `Templates/Rarity/`：普通/限定/特殊/精英稀有度标记。
  - `Fonts/`：Franklin Gothic 与 Roboto 系列字体。
- `card_template.py`：新的模板渲染器，按原项目 `index.html` 的坐标常量复刻绘制流程：
  - 卡图区域 `ART_X=120, ART_Y=30, ART_W=411, ART_H=460`，按 cover 方式居中裁切后贴入。
  - 卡面模板 `(113,28)-(535,621)` 裁剪后缩放为 500×702，保持与既有编辑器一致的输出尺寸。
  - 单位绘制国家/空地横幅 `(195,39,332,66)`；指令/反制绘制国家图标 `(469,50)`（美/苏微调）。
  - 稀有度标记 y=600；费用 `(145,76)`、攻 `(222,468)`、防 `(427,468)`；卡名、关键词、描述按中英文分别选字体。
- `diy_card.render_card_png()` 优先调用模板渲染器；素材缺失或渲染异常时自动回退到原有自绘逻辑。

### 改动
- `README.md`：依赖与文件结构补充模板素材、说明来源与许可边界。

### 运存控制
- 模板/字体按需单张加载，合成后立即 `close()`，不缓存图片对象。
- 字体对象缓存上限 128 条，满了整体清空，避免长驻进程无限增长。
- 实测 `tracemalloc`：首次渲染（含字体加载）峰值约 38.9 MB，缓存预热后单次渲染峰值约 0.34 MB。

### 许可与风险
- 素材来自非官方粉丝项目，基于 1939 Games Community Content Policy，限个人非商业二创使用；
  卡面名称、美术与商标权利归 1939 Games 所有。若要公开推送到 GitHub 或用于商业用途，
  需要先确认授权或移除这些素材，插件会自动回退到自绘卡面。

### 验证
- `py_compile`：`card_template.py`、`diy_card.py` 通过。
- `unittest discover`：7 项测试全 `OK`。
- 模板渲染实测：无卡图 218843 字节、带卡图 217778 字节，尺寸均 `[500, 702]`。
- Web 路由 `POST /api/plug/bilikards/manual/diy/render`：`200`、`ok=true`、`size=[500,702]`、PNG 218237 字节。
- 聊天命令模拟：帮助/列表/渲染/保存/按 ID 渲染/错误提示全部正常，测试数据已清理。
- 插件热重载：`/api/plugin/reload` 返回 `200 ok`。

### 备份
- `C:\Users\Administrator\Desktop\备份\template_assets_20261128_202228`：含改动前的 `diy_card.py`、`main.py`、`README.md`、`CHANGELOG.md`。

## 2026-11-28 DIY 字段分隔符兼容增强

### 改动
- `main.py` 的 `_diy_parse_fields()` 重写字段解析：
  - 分隔符从仅 `=` 扩展为 `=`、`＝`（全角）、`:`、`：`（中文冒号），允许两侧带空格（含全角空格）。
  - 值支持引号包裹以保留空格；未加引号的值读取到下一个已识别字段或行尾，因此 `描述=闪击 部署时抽一张牌 阵营=德国` 可正确拆成两个字段。
  - 用 `DIY_FIELD_ALIASES` 构造前瞻边界，避免描述里的冒号被误判为字段分隔符。
  - JSON 输入路径不变。
- `README.md`：新增分隔符兼容说明与示例。

### 验证
- `py_compile`：`main.py` 通过。
- 7 组解析实测全部正确，含：`名=闪电部队 阵营=德国 ...`、`名 ＝ 闪电部队　阵营 ＝ 德国　费 ＝ 3`、`名＝闪电部队 阵营：德国 类型：坦克 费：3`、`name: Lightning Forces faction: Germany cost: 3`、带引号与不带引号的空格描述、JSON 输入。
- `unittest discover`：7 项测试全 `OK`。
- 插件热重载：`/api/plugin/reload` 返回 `200 ok`。

### 备份
- `C:\Users\Administrator\Desktop\备份\diy_equal_20261128_200111`：含改动前的 `main.py`、`README.md`、`CHANGELOG.md`。

## 2026-11-28 模型生成的自带 TTF 字体

### 新增
- `fonts/` 目录新增随插件附带的字体文件，DIY 卡面不再依赖系统是否安装黑体：
  - `kards_card_font.ttf`（9,745,640 字节，内部名称 `KARDS Card Font`）——中文标题/正文，派生自模型首选 `simhei.ttf`。
  - `kards_card_number.ttf`（371,408 字节，内部名称 `KARDS Card Number`）——费用/攻防数字，派生自 `bahnschrift.ttf`。
  - `font_profile.json`——记录模型、对现有卡图的分析结论、推荐字体列表与生成参数。
- 字体生成流程：
  1. 用视觉模型（Sage-S2.1-Flash）分析 `data/plugin_data/bilikards/deck_card_cache` 中的真实卡图（如 `155_c_modle_1917.webp`、`101_infantry_regiment.webp`）与缩略图 `aa_barrage.jpg`。
  2. 模型输出：`title_font = Heavy-weight CJK sans-serif`、`number_font = Condensed bold sans-serif digits`，推荐字体依次为 `simhei.ttf`、`msyhbd.ttc`、`Dengb.ttf`。
  3. 复制系统字体并用 `fontTools` 改写 `name` 表（family/full/PostScript/typographic 名称）为独立字体名，得到真正可识别的衍生 TTF。
- `diy_card.py` 新增 `_bundled_fonts()`：从 `font_profile.json` 读取自带字体优先加载，系统字体与 macOS/Linux 字体作为兜底；数字字体单独使用 `number_recommendations`，不再混入中文字体候选。

### 改动
- `diy_card.py`：模块文档、字体常量、`_font()` 候选顺序改为「自带字体 → 模型推荐系统字体 → 跨平台字体 → Pillow 默认」。
- `README.md`：依赖说明新增 DIY 自带字体段落，列出三个新增文件。

### 验证
- `py_compile`：`diy_card.py` 通过。
- `fontTools` 读取：`kards_card_font.ttf` 内部名为 `KARDS Card Font`，`kards_card_number.ttf` 为 `KARDS Card Number`。
- 渲染实测：`_font("cn")` 与 `_font("num")` 均加载自带字体路径；`render_card_png()` 返回 24994 字节、尺寸 `[500, 702]`。
- `unittest discover`：7 项测试全 `OK`。
- 插件热重载：`/api/plugin/reload` 返回 `200 ok`。

### 备份
- `C:\Users\Administrator\Desktop\备份\font_20261128_195004`：含改动前的 `diy_card.py`、`README.md`、`CHANGELOG.md`。

## 2026-11-28 DIY 卡编辑器（首次加入）

### 新增
- DIY 卡编辑器插件页面：入口 `views/diy_editor/index.html`，在 AstrBot 仪表盘插件页面列表可见 `diy_editor`。
  - 可编辑字段：ID、中文名、英文名、阵营、类型、稀有度、费用、攻击、防御、描述、关键词、卡图上传。
  - 阵营/类型/稀有度下拉复用 `deck_code.py` 的 `FACTION_CN`/`TYPE_CN`/`RARITY_CN` 中文标签。
  - 按钮：渲染预览、保存设计、已保存列表；预览为 500×702 卡面 PNG。
  - 保存的设计写入 `data/plugin_data/bilikards/diy_cards.json`，与官方卡表 `deck_cards.json` 完全隔离，不影响卡组码解析和卡牌搜索。
- 新增渲染模块 `diy_card.py`（从独立原型迁移）：
  - `render_card(spec, art)` / `render_card_png(spec, art)`：按 500×702 画布自绘卡面。
  - 版式：顶部费用圆章 + 卡名 + 英文名 + 稀有度菱形宝石；中部卡图画框（保持比例居中裁切）；阵营/类型色条；描述框（自动折行、最多 6 行、超长省略号）；底部攻防圆章；指令/反制类型用「—」代替攻防。
  - 字体沿用 bilikards 既有约定：中文 `C:/Windows/Fonts/simhei.ttf`（黑体），数字 `C:/Windows/Fonts/bahnschrift.ttf`（缺失回退 `arialbd.ttf`）；macOS 用 PingFang/Helvetica，Linux 用文泉驿/DejaVu；全部有 `load_default()` 兜底。
  - 卡图支持 data URL 或纯 base64；无卡图时绘制占位底纹。
- `web_api.py` 新增 3 个路由（前缀 `bilikards` 与 `astrbot_plugin_bili_kards` 双注册）：
  - `POST manual/diy/render`：接收 `{"spec": {...}, "art": "<data URL 或 base64>"}`，返回 `{"ok": true, "image": "data:image/png;base64,...", "size": [500, 702]}`。
  - `POST manual/diy/save`：保存设计到 `diy_cards.json`，自动生成 `DIY-XX` ID，返回 `{"ok": true, "id": ..., "count": ...}`。
  - `GET manual/diy/list`：列出已保存设计（不含卡图 base64）。

### 改动
- `README.md`：功能一览表新增 DIY 卡编辑器行；文件结构新增 `diy_card.py`、`views/diy_editor/`、`diy_cards.json`；卡表数量按当前实际更新为 1646。
- `web_api.py`：导入 `diy_card` 模块，ROUTES 列表追加 3 条 DIY 路由。

## 2026-11-28 DIY 卡聊天命令 `kards diy`

### 新增
- `kards diy` 子命令，聊天端直接调用 DIY 渲染器：
  - `kards diy` / `help`：用法与说明。
  - `kards diy list`：列出已保存设计（`diy_cards.json`）。
  - `kards diy show <ID>`：按 ID 渲染已保存设计并出图。
  - `kards diy 字段=值 ...`：直接渲染，不保存；支持中英文字段名和中文阵营/类型/稀有度标签。
  - `kards diy save 字段=值 ...`：保存并渲染，自动分配 `DIY-XX` ID，同 ID 覆盖。
  - `kards diy {"name":...}`：直接传 JSON 对象。
  - 同一条消息附图，或先发命令再发图，图片会作为卡图（转 data URL 交给渲染器）。
  - `MANUAL_DIY_ALIASES`：`diy`、`diycard`、`diykards`、`自制卡`、`diy卡`；加入 cheap 命令集合，不受冷却限制。
- LLM 工具 `kards_diy`：机器人可直接调用，参数 `spec` 与 `kards diy` 后接内容一致。
- `DIY_HELP_TEXT`、`DIY_FIELD_ALIASES`、`_diy_parse_fields`、`_diy_enum_value`、`_diy_art_image` 辅助函数。
- `BiliKards._diy_render`、`_diy_art_from_event`、`_manual_diy` 方法；图片写入 `data/plugin_data/bilikards/deck_images/diy_<hex>.png` 后以 `Image` 组件发送。

### 改动
- `main.py`：新增 `import base64`，导入 `diy_card` 模块；`HELP_TEXT` 增加 4 行 DIY 用法；命令分发新增 `MANUAL_DIY_ALIASES` 分支；新增 `kards_diy` 工具。
- `README.md`：功能表 DIY 行补充聊天入口，新增「DIY 卡聊天用法」小节。

### 验证
- `py_compile`：`main.py`、`diy_card.py` 通过。
- `unittest discover`：7 项测试全 `OK`。
- 插件热重载：`/api/plugin/reload` 返回 `200 ok`。
- 异步模拟事件实测：`help` 输出帮助、`list` 空列表提示、`save` 输出「已保存 DIY 卡：DIY-01」并出图、`list` 显示 1 张、`show DIY-01` 出图、`show 不存在` 输出未找到；测试保存已清理。
- 字段解析实测：`名=闪电部队 阵营=德国 类型=坦克 稀有度=精英 费=3 攻=4 防=2 关键词=闪击,守护` → `{"name":..., "faction":"Germany", "card_type":"tank", "rarity":"Elite", "cost":3, "attack":4, "defense":2, "keywords":[...]}`；JSON 输入也通过。

### 备份
- `C:\Users\Administrator\Desktop\备份\diy_chat_20261128_193523`：含改动前的 `main.py`、`README.md`、`CHANGELOG.md`。

### 验证
- `py_compile`：`web_api.py`、`diy_card.py` 通过。
- `unittest discover`：7 项测试全 `OK`。
- 插件热重载：`/api/plugin/reload` 返回 `200 ok`。
- 接口实测（路径 `/api/plug/bilikards/...`）：
  - `POST manual/diy/render` → `200`，PNG 24166 字节，尺寸 500×702。
  - `POST manual/diy/save` → `200`，`{"ok": true, "id": "DIY-VERIFY", "count": 1}`。
  - `GET manual/diy/list` → `200`，保存后可见 `DIY-VERIFY`，清理后为空。
- 页面实测：`/api/v1/plugins/bilikards/views/diy_editor` 返回 `200` 且带 `content_path`；加载该 `content_path` 返回 `200`、6197 字节，含 DIY 标题与 3 个新路由名。
- 测试数据 `DIY-VERIFY` 已从 `diy_cards.json` 清除，`diy_cards.json` 为空列表。

### 备份
- `C:\Users\Administrator\Desktop\备份\diy_editor_20261128_193130`：含 `before/`（上游原始 `web_api.py`、`README.md`）与 `after/`（本次改动后的 `web_api.py`、`README.md`、`diy_card.py`、`views/diy_editor/index.html`）。

### 已知限制 / 风险
- 卡面版式按 500×702 比例与既有字体推断设计；当前模型无法直接读取图片像素做逐字比对，字体匹配属于合理假设，未与官方卡图逐像素校准。
- DIY 设计保存在 `diy_cards.json`，仅用于编辑器预览与保存，不会自动进入聊天端 `kards s` 搜索或卡组码解析；需要时可后续单独接入。
- 保存 route 对同一 ID 采用覆盖写入；空 ID 会按当前设计数生成 `DIY-XX`。
