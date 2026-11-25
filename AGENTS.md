# Role & Project Objective
你是一位精通 GIS/三维可视化（CesiumJS）、路径规划算法与 LLM Agent 工程架构的全栈开发工程师。
你的任务是从零构建 **VoxAero（维空智航）**——一套面向武汉大学校园的**体素化低空三维导航与智能体数字孪生调度系统（Voxel-based Low-Altitude Spatial Agent System）**的可运行原型。

系统需实现三大核心闭环：
1. **体素化低空三维导航地图可视化**：展示校园建筑三维白模、垂直空域分层体素网格、起降坪（Vertiport）及动态禁飞电子围栏。
2. **自然语言 Multi-Agent 智能调度**：用户输入自然语言指令（配送或巡检），Agent 解析意图、检查空域规则、调用三维体素 A* 避障路径规划算法，并在前端展示 Agent 的推理与工具调用链（Thought-Action-Observation）。
3. **无人机三维推演、FPV 第一人称 AR 视角与突发应急重规划**：驱动无人机（呼号 `VOXAERO-01`）沿三维航迹飞行，支持切换至带 HUD 虚实叠加指引的第一人称视角，并支持在飞行途中注入“突发阵风/临时禁飞区”或“低电量告警”，触发 Agent 实时动态绕飞或自动备降。

---

# Brand & UI Identity（品牌与界面标识规范）
- **项目名称**：`VoxAero`（中文副标：`维空智航 · 低空三维导航与智能体调度平台`）
- **网页标题（`<title>`）**：`VoxAero — 低空三维数字孪生与智能体调度平台`
- **顶栏 Logo**：几何标志后横向排列 `VoxAero` 与 `维空智航`，顶栏品牌区保留这两个名称。
- **文字层级**：标题、正文、控件、说明和数字读数沿用 `frontend/css/style.css` 的共享字号与字重。实时读数使用等宽数字。
- **无人机标识 / HUD 呼号**：`VOXAERO-01`
- **后端服务元数据**：FastAPI 初始化设置 `title="VoxAero API"`、`version="1.0.0"`，`/api/health` 返回字段包含 `"service": "VoxAero"`。

---

# Tech Stack & Hard Constraints（技术栈与强约束）
为确保单命令稳定启动、彻底规避前端构建工具与 Cesium 静态资源打包报错，必须严格遵守以下技术选型：

0. **Python 运行环境（Conda 强约束）**：
   - 必须使用本机 `conda` 创建并使用名为 `VoxAero` 的独立环境（Python 3.11，例如 `conda create -n VoxAero python=3.11 -y`）；若 `conda env list` 显示 `VoxAero` 已存在则直接复用，不要删除重建。
   - **禁止**在 `base` 或系统默认 Python 环境中安装依赖或运行测试。
   - 由于终端可能为非交互式 Shell，所有依赖安装、测试与服务启动命令请优先使用 `conda run -n VoxAero <command>`（或该环境对应的绝对路径 `python -m ...`），例如 `conda run -n VoxAero pip install -r requirements.txt` 与 `conda run -n VoxAero pytest`。
   - 若因权限或网络受限无法创建 Conda 环境，请立即停止并提示用户手动执行创建命令，不要静默回退到系统 Python。

1. **后端**：Python 3.10+、`FastAPI`、`uvicorn`、`pydantic`、`numpy`、`shapely`、`openai`（用于兼容 OpenAI / DeepSeek / Qwen 等标准 Function Calling 接口）。
2. **前端**：采用**零构建（No-Build）原生模块化架构**（HTML5 + ES6 Modules + 原生 CSS Design Tokens + CesiumJS 1.119 官方 CDN），由 FastAPI 直接通过 `StaticFiles` 托管于 `/` 根路径。
   - **禁止**引入 Webpack、Vite、React/Vue 脚手架或 `vite-plugin-cesium`。
   - **零 Token 地图底图约束**：禁止强依赖 Cesium Ion Token。初始化 `Cesium.Viewer` 时，必须默认使用免费的深色底图（如 OSM 暗色滤镜）与本地高程/椭球地形，确保在无外网 Token 环境下 100% 正常渲染三维白模。
3. **LLM 双模式兜底约束**：
   - 支持通过环境变量 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 调用真实大模型执行 Function Calling。
   - **必须内置 Deterministic Fallback（确定性规则兜底引擎）**：当未配置 `LLM_API_KEY` 或网络请求超时失败时，自动降级到本地关键词+正则意图解析器，完整输出与真实 Agent 相同格式的推理步骤（`thought_steps`）和工具调用结果，确保演示永不崩溃。
4. **Git 版本管理规范**：
   - 使用本地 Git 管理代码（当前主开发分支 `codex/tactical-obsidian`），遵循 Conventional Commits 风格（如 `feat:`、`fix:`、`docs:`、`chore:`）。
   - 每轮修改完成并验证通过后，自动在当前分支执行本地 `git commit`，附上清晰的改动说明，方便查看和回滚。
   - **严禁**提交 `.env`、个人 API Key、缓存文件（`__pycache__/`、`.pytest_cache/`）或未授权执行 `git push`、`git reset --hard`。

---

# Scope Boundaries（严格处理边界）

### In-Scope（必须高质量实现）
- 完整的局部 ENU 坐标系转换与 **三维体素网格（Voxel Grid）A* 避障算法**（支持高度层约束、建筑包络体碰撞检测、禁飞区回避与航点平滑）。
- 完整的 **Agent 工具编排流程**（意图解析 -> 空域合规校验 -> 3D 航路规划 -> 异常重规划），向前端返回结构化的思考链日志。
- **高颜值深色科技风数字孪生 UI**：顶部 `VoxAero` 品牌栏、视角切换栏（全局上帝视角 / 无人机跟随视角 / **FPV 第一人称 AR-HUD 视角**）与“触发突发应急”按钮；左侧图层控制与状态看板；中间全屏 Cesium 三维场景；右侧 Agent 对话与思维链面板。
- 预置一套地理坐标精准落在**武汉大学信息学部及周边（约 114.355°E, 30.528°N）**的高质量 Mock GeoJSON 数据，开箱即用。

### Out-of-Scope（严禁过度设计）
- **不要**实现用户注册、登录、权限管理（JWT/OAuth）。
- **不要**引入 MySQL、PostgreSQL/PostGIS、Redis、Docker 等外部数据库或中间件；所有空间数据直接从 `data/` 目录读取 GeoJSON 文件。
- **不要**接入真实无人机硬件 SDK（如 MAVLink/DJI SDK），飞行过程通过前端 Cesium `SampledPositionProperty` 插值模拟。
- **不要**在代码中硬编码任何个人 API Key 或外部付费地图 Token。

---

# Repository Directory Structure（目录结构规范）
请严格按以下目录组织代码：

    VoxAero/
    ├── AGENTS.md                     # 本开发规范文档
    ├── README.md                     # VoxAero 启动说明、环境变量配置与数据替换指南
    ├── Test.md                       # 自动化回归与浏览器交互验收清单
    ├── requirements.txt              # Python 依赖清单
    ├── .env.example                  # LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 示例
    ├── data/                         # 武大信息学部实景空间数据目录（支持独立目录替换）
    │   ├── README.md                 # 数据来源、高程基准与外部空域快照规范
    │   ├── whu_buildings.geojson     # OSM 建筑轮廓与高度/包络属性
    │   ├── vertiports.geojson        # 起降坪与应急备降点 POI
    │   ├── nofly_zones.geojson       # 静态禁飞/限高电子围栏
    │   ├── whu_roads.geojson         # OSM 道路矢量
    │   ├── whu_places.geojson        # OSM 校门、广场、运动场、道路与水体地标
    │   └── whu_terrain.json          # SRTM 局部高程网格
    ├── backend/
    │   ├── __init__.py
    │   ├── main.py                   # FastAPI 入口、静态文件托管与 REST API 路由
    │   ├── config.py                 # 配置加载与空域高度层/能耗常量定义
    │   ├── schemas.py                # Pydantic 请求与响应数据模型
    │   └── core/
    │       ├── __init__.py
    │       ├── airspace.py           # GeoJSON 加载、ENU 坐标投影、空域体素化与碰撞检测
    │       ├── terrain.py            # 双线性地形插值与连续航段地形净空检查
    │       ├── planner.py            # 3D 体素网格 A* 算法与三维航迹平滑器
    │       ├── intent.py             # 本地分类器与巡航高度硬/软约束解析
    │       ├── rules.py              # 外部空域快照高程基准换算与时效核验
    │       ├── agent.py              # LLM Function Calling Agent + 确定性降级与应急改航
    │       └── spatial_dispatch.py   # OSM 地标消歧、动态地面选址、有序途经与任务会话
    ├── frontend/
    │   ├── index.html                # VoxAero 战术黑曜石航电主页面（原生 ES Modules + CesiumJS CDN）
    │   ├── assets/                   # 本地 WOFF2 界面字体与 VOXAERO-01 四旋翼 GLB 模型
    │   ├── css/
    │   │   ├── tokens.css            # 全局色彩、字体与尺寸语义变量
    │   │   └── style.css             # 航电座舱面板、响应式布局与 FPV 叠层样式
    │   └── js/
    │       ├── app.js                # 应用主入口、状态机与应急联动
    │       ├── map3d.js              # Cesium 场景、白模、空域网格、防重叠标签与航迹渲染
    │       ├── drone_sim.js          # 无人机运动插值、能耗模拟、多视角控制与动态应急
    │       ├── ar_hud.js             # FPV 第一人称 AR-HUD Canvas 姿态与遥测叠层
    │       ├── flight_profile.js     # 2D 离地高度（AGL）垂直飞行剖面 SVG 组件
    │       ├── flight_replay.js      # 累计仿真时间轴与应急事件回放引擎
    │       ├── battery_display.js    # 电量数值与分段进度条语义状态共享组件
    │       ├── agent_ui.js           # 右侧任务调度台、校验链摘要/详情与预设指令
    │       └── spatial_ui.js         # 动态候选起降点/航线对比与空域核验回读表单
    ├── scripts/
    │   ├── build_whu_scene.py        # OSM + Skadi HGT 实景数据离线构建脚本
    │   ├── build_drone_model.py      # VOXAERO-01 glTF 2.0 模型生成脚本
    │   └── evaluate_dispatch.py      # 真实模型与本地规则 30 用例对照评测脚本
    └── tests/
        ├── fixtures/mock_scene/      # Phase 1 受控几何测试夹具
        ├── test_planner_and_api.py   # 算法避障、禁飞区绕行与核心 API 契约测试
        ├── test_real_scene.py        # 武大实景几何同源、起降坪贴地/屋顶与性能测试
        └── test_spatial_dispatch.py  # 多轮消歧、动态选址、有序飞越与应急预算测试

---

# Data Contracts（数据契约规范）
`data/` 目录下的三个 GeoJSON 文件既是系统初始 Mock 数据，也是后续与其他组员对接的标准接口，必须严格遵守字段定义与 WGS84（EPSG:4326）坐标系：

1. `data/whu_buildings.geojson`（FeatureCollection，Geometry 为 Polygon）
   - 必需属性：`id` (str), `name` (str), `height` (float, 单位米，范围 12~65m), `category` (str: "teaching" | "lab" | "dorm" | "canteen" | "library")
   - Mock 要求：在 `[114.349, 30.524]` 至 `[114.362, 30.533]`（武大信息学部及周边）范围内生成至少 25 栋布局合理、互不重叠的校园建筑多边形（如：测绘国重实验室、信息学部图书馆、星湖教学楼、学生食堂、菜鸟驿站、青楼/宿舍群等）。

2. `data/vertiports.geojson`（FeatureCollection，Geometry 为 Point）
   - 必需属性：`id` (str), `name` (str), `type` (str: "logistics" | "inspection" | "emergency"), `altitude` (float, 起降坪所在高度/楼顶高程，单位米)
   - Mock 要求：至少包含 6 个起降点（如 VP01 信息学部食堂起降坪、VP02 菜鸟驿站物流站、VP03 测国重楼顶停机坪、VP04 图书馆西侧巡检点、VP05 星湖应急备降坪、VP06 操场东侧应急备降坪）。

3. `data/nofly_zones.geojson`（FeatureCollection，Geometry 为 Polygon）
   - 必需属性：`id` (str), `name` (str), `min_alt` (float), `max_alt` (float), `reason` (str)
   - Mock 要求：至少包含 3 个静态禁飞区（如 NFZ01 图书馆静谧区 `0-120m`、NFZ02 高压变电设施区 `0-100m`、NFZ03 密集教学楼限飞区 `0-45m`）。

---

# Core Modules Detailed Specification（核心模块详细规格）

## Module 1: 空域体素建模与 3D A* 避障引擎 (`backend/core/airspace.py`, `planner.py`)
1. **垂直空域分层规范**：
   - `0m – 30m`：**地面干扰/树冠缓冲区**（仅允许在起降坪垂直起降阶段穿越，巡航阶段禁止水平飞行）。
   - `30m – 60m`：**物流配送主航道层（Delivery Corridor）**，默认外卖/快递任务优先在此高度层巡航。
   - `60m – 90m`：**安防巡检与高速通道层（Inspection Corridor）**，默认安防巡检或跨建筑高速通行任务优先在此层巡航。
2. **三维体素（Voxel）A* 算法实现**：
   - 将研究区域投影至以区域中心为原点的局部平面直角坐标系（ENU，单位米），构建三维体素网格（建议水平分辨率 `15m~20m`，垂直分辨率 `10m`，高度 `0~100m`）。
   - 障碍物判定：建筑多边形向外扩展 `5m` 安全水平缓冲区（`height + 5m` 垂直缓冲区）；所有静态禁飞区及动态注入的临时禁飞区（圆形/圆柱体）均标记为不可通行体素。
   - 代价函数：综合三维欧氏距离 + 偏离推荐高度层惩罚（保证物流任务稳在 30–60m、巡检稳在 60–90m）+ 靠近障碍物边缘的软安全惩罚。
   - **路径平滑与垂直起降段**：自动在起点和终点补全垂直爬升（Takeoff）与垂直降落（Landing）航段；对巡航段执行 Line-of-Sight 航点抽稀，避免锯齿状折线。
   - **性能要求**：单次三维路径规划耗时需控制在 `500ms` 以内。

## Module 2: Agent 智能调度引擎 (`backend/core/agent.py`)
定义以下 3 个核心 Tool 供 Agent 编排调用：
1. `parse_dispatch_intent(user_query)`：提取任务类型（`delivery` / `inspection`）、起点名称、终点名称、额外约束（如“避开图书馆”、“加急”、“优先高空”）。
2. `plan_3d_route(start_id, end_id, mission_type, extra_nofly_zones)`：调用 `planner.py` 计算无碰撞三维航迹，返回航迹经纬度高程序列、总航程、预计耗时、对比地面配送节省时间百分比、最小净空安全距离及合规状态。
3. `replan_emergency(current_pos, target_id, emergency_type, dynamic_hazard)`：
   - 当 `emergency_type == "wind_gust"` 或 `"temp_nofly"`：将 `dynamic_hazard`（中心经纬度、半径、高度范围）加入临时禁飞区，从无人机**当前实时三维坐标**重新规划绕飞至原终点的安全航线。
   - 当 `emergency_type == "low_battery"`：自动检索距离当前坐标最近的 `type == "emergency"` 或任意可达起降坪，规划最短安全备降航线。

**输出格式要求**：每次调度或重规划均返回完整的 `thought_steps: list[{step, role, content}]`，供前端逐条渲染 Agent 思考与工具调用过程。

## Module 3: REST API 接口 (`backend/main.py`)
- `GET /api/layers/all`：一次性返回 `buildings`、`vertiports`、`nofly_zones` 三个 GeoJSON 以及空域分层定义。
- `POST /api/agent/dispatch`：接收 `{"query": "..."}`，返回 Agent 思考链、解析后的任务参数、规划好的三维航迹（含每个航点的 `lon, lat, alt, phase`）及 KPI 统计指标。
- `POST /api/agent/replan`：接收当前无人机实时位置、原目的地、应急事件类型（`temp_nofly` / `low_battery`），返回新生成的临时禁飞区几何信息、Agent 应急决策链及新航迹。
- `GET /api/health`：健康检查，返回 `service: "VoxAero"` 及当前 LLM 模式状态（`live_llm` 或 `fallback_mock`）。

## Module 4: CesiumJS 数字孪生前端与 FPV AR-HUD (`frontend/`)
1. **三维场景与图层开关 (`map3d.js`)**：
   - 将建筑白模拉伸渲染为半透明科技蓝/银灰色材质（顶部高亮描边），鼠标悬停显示建筑名与高度。
   - 空域分层可视化：通过半透明水平网格面展示 `30m` 与 `60m` 高度分界层（支持一键开关）。
   - 禁飞区渲染为半透明红色发光立体柱体；起降坪渲染为带发光底座和文字标签的 3D 标识。
   - 航迹渲染：发光三维管道/虚线，起飞段、巡航段、降落段及应急重规划段使用不同高亮颜色区分。
2. **无人机仿真与 FPV AR-HUD 叠层 (`drone_sim.js`, `ar_hud.js`)**：
   - 支持播放/暂停、倍速调节（1x / 2x / 4x）、重置。
   - 提供三种相机视角一键切换：`全局总览`、`第三人称跟随`、`FPV 第一人称 AR 视角`。
   - **FPV AR 亮点功能**：切换至 FPV 时，相机锁定于无人机机头前方视角，前端在三维场景中动态渲染**前方虚拟三维绿色航道引导框（AR Corridor Gates）**与终点起降坪的 **AR 降落十字靶标**；同时启用屏幕顶层的透明 `<canvas>` HUD，显示呼号 `VOXAERO-01`，实时绘制地平线姿态标尺、实时高度（ALT）、速度（SPD）、剩余电量（BAT）、距最近建筑/障碍物净空安全距离（CLEARANCE，接近阈值时变红闪烁告警）。
3. **Agent 交互与一键演示快捷按钮 (`agent_ui.js`)**：
   - 右侧提供对话输入框，并内置 3 个一键预设演示按钮（方便录制 3 分钟演示视频）：
     - 按钮 1：*「中午高峰配送：将餐食从信息学部食堂送至测国重楼顶，避开图书馆静谧区」*
     - 按钮 2：*「夜间高空安防巡检：从信息学部起飞，沿 60–90m 巡检层前往图书馆西侧巡检点」*
     - 按钮 3（飞行中可点击）：*「⚡ 模拟突发阵风/临时禁飞区」* 与 *「🔋 模拟低电量强制备降」*
   - 左侧/底部提供实时数据看板：当前任务状态、航线总长、预计耗时、对比地面配送耗时节省率、空域合规率（100%）。

---

# Phased Execution Plan（请严格按阶段执行并自测）
请不要一次性盲目写完所有代码，按以下 4 个阶段逐步推进，每完成一个阶段运行相应验证：

- **Phase 1（环境、数据底座与三维体素算法）**：检查并创建本机 Conda 环境 `VoxAero`（Python 3.11），安装 `requirements.txt` 依赖；创建 `vox-aero/` 目录结构、生成武大信息学部精准 Mock GeoJSON 数据，完成 `airspace.py` 与 `planner.py`，在 `VoxAero` 环境中运行 `pytest` 验证三维航迹不穿过任何建筑和禁飞区、高度层约束生效且单次计算 <500ms。
- **Phase 2（Agent 调度引擎与 FastAPI 后端）**：实现 `agent.py`（含真实 OpenAI 兼容接口 + 本地确定性兜底）与 `main.py` 所有路由，使用 `TestClient` 验证 `/api/agent/dispatch` 与 `/api/agent/replan` 返回数据格式完全符合契约。
- **Phase 3（CesiumJS 前端与 FPV AR-HUD）**：完成 `VoxAero` 品牌顶栏、图层开关、三维白模/空域/航迹渲染、无人机动画及带 `VOXAERO-01` 标识的 FPV 第一人称 AR 引导框与 HUD Canvas 绘制。
- **Phase 4（端到端联调与文档）**：在 `VoxAero` 环境中启动 `uvicorn backend.main:app --reload`，检查无浏览器控制台报错与 404 资源缺失，补全 `README.md`（包含 Conda 环境 `VoxAero` 的创建/激活命令、单命令启动方式、`LLM_API_KEY` 配置方法、以及替换 `data/*.geojson` 的操作步骤）。
