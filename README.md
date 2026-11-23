<p align="center">
  <img src="docs/media/voxaero-mark.svg" width="88" height="88" alt="VoxAero 双翼标志">
</p>

<h1 align="center">VoxAero · 维空智航</h1>

<p align="center">低空三维调度与飞行仿真 · 首个示范场景为武汉大学信息学部</p>

<p align="center">
  <img alt="发布阶段 Phase 4" src="https://img.shields.io/badge/Release-Phase%204-263444?style=flat-square">
  <img alt="Python 3.11" src="https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/API-FastAPI-009688?style=flat-square&amp;logo=fastapi&amp;logoColor=white">
  <img alt="CesiumJS，零构建" src="https://img.shields.io/badge/3D-CesiumJS-404F65?style=flat-square">
  <a href="Test.md"><img alt="pytest 回归测试用例" src="https://img.shields.io/badge/Tests-pytest-2F6FB2?style=flat-square&amp;logo=pytest&amp;logoColor=white"></a>
</p>

<p align="center">
  <a href="#体验任务">🎮 体验任务</a> ·
  <a href="#快速开始">🚀 快速开始</a> ·
  <a href="#配置模型">🔧 配置模型</a> ·
  <a href="#技术栈">🧱 技术栈</a> ·
  <a href="#agent-调度实现">🧭 Agent 调度</a> ·
  <a href="#测试与验收">🧪 测试与验收</a>
</p>

输入「从珞瑜门到青年宿舍楼」，调度台会先让你确认同名入口，再找出能起降、能接入航层的地面位置。选好候选后，可以跟随无人机起飞，切到第一人称查看航道引导，并在出现临时禁飞区时观察它从当前机位绕飞。

![VoxAero 主界面，校园白模上的三维航线、左侧飞行遥测和右侧任务调度](docs/media/console-overview.webp)

<details>
<summary>🎬 观看 8 秒仿真动图 · 从指令补答到第一人称飞行</summary>

![VoxAero 操作录像，依次展示输入指令、确认西侧校门、预览路线、起飞和切换第一人称](docs/media/dispatch-demo.gif)

</details>

## 体验任务

| 想验证什么 | 试着输入 |
| --- | --- |
| 同名地点消歧与动态选址 | **从珞瑜门到青年宿舍楼** |
| 多点巡检与高度限制 | **从星湖南路出发，依次途经星湖、星空广场，最后到信息学部操场巡检，巡航高度60到80米** |
| 空域核验 | **核验图书馆静谧区是否禁飞** |

1. 对话有歧义时，按提示选择入口或补充附近地标。地图会区分原地标和可行的实际落点。
2. 点击「采用候选」或「采用航线」，再点击「确认并开始仿真」。全局总览、第三人称跟随和第一人称视角均可切换。
3. 巡航中打开「突发应急」，试一次临时禁飞绕飞或低电量备降。停机后在「最近任务记录」打开回放。

每次规划按当前场景检查地形、建筑包络、静态禁飞区和任务避让条件。配送默认使用 **30–60 米**航层，巡检默认使用 **60–90 米**航层。绕飞保留未完成的途经点，低电量备降会估算航程、爬升与落地余量。结果适用于当前仿真场景。

## 快速开始

需要 Conda 和支持 WebGL 的浏览器。首次使用可克隆仓库，已有工作区可直接进入项目根目录。

```powershell
git clone https://github.com/Qua-Drant/VoxAero.git
cd VoxAero
```

```powershell
conda create -n VoxAero python=3.11 -y
conda run -n VoxAero pip install -r requirements.txt
conda run --no-capture-output -n VoxAero uvicorn backend.main:app --reload
```

打开 [本地调度台](http://127.0.0.1:8000/)。已有 `VoxAero` 环境时，可从安装依赖开始。也可以先运行 `conda activate VoxAero`，再执行 `uvicorn backend.main:app --reload`。若端口被占用，启动时加 `--port 8001` 后访问 [8001](http://127.0.0.1:8001/)。

FastAPI 同时提供页面和 API，前端无需构建。校园模型和地形由本机服务加载，CesiumJS 与地图瓦片由浏览器联网加载，无需 Cesium Ion Token。

## 配置模型

开箱即可使用本地规则完成调度。启用真实模型时，在项目根目录运行以下命令，再编辑本机的 `.env`。

```powershell
if (!(Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
```

| 变量 | 填写内容 |
| --- | --- |
| `LLM_API_KEY` | 你的模型服务密钥，仅填入本机 `.env` |
| `LLM_BASE_URL` | 服务商的 OpenAI 兼容基础地址，例如 `https://api.openai.com/v1` |
| `LLM_MODEL` | 支持函数调用的模型标识，例如账户中可用的百炼 `qwen-flash` |

使用百炼时，从控制台复制基础地址，并选用同区域的密钥。`.env` 与环境文件变体由 Git 忽略，仓库提供 [.env.example](.env.example) 配置模板。系统环境变量优先于 `.env` 中的同名值。 [百炼 OpenAI 兼容接口](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions)

修改配置后重启。访问 [健康接口](http://127.0.0.1:8000/api/health) 查看运行模式。`live_llm` 表示已配置模型，单次调用的实际结果可在调度记录中查看。遇到模型超时或结果无法核对时，当次调度回到本地规则。`fallback_mock` 模式仍会根据当前场景计算三维航线。

## 技术栈

- **浏览器界面**采用 HTML、原生 ES Modules、CSS 和 Tailwind CSS CDN。`app.js` 维护地图、调度表单和任务状态，`agent_ui.js` 渲染消歧选项、候选航线及校验记录。 [界面入口](frontend/index.html) · [调度界面](frontend/js/agent_ui.js)
- **三维场景与仿真**使用 CesiumJS 1.119 和 OpenStreetMap 底图。场景渲染建筑白模、起降坪、禁飞区和三维航迹。飞行器通过 `SampledPositionProperty` 沿航点插值运行，第一人称姿态与遥测由 Canvas HUD 叠加，任务回放沿用同一套飞行数据。 [地图](frontend/js/map3d.js) · [飞行仿真](frontend/js/drone_sim.js) · [HUD](frontend/js/ar_hud.js)
- **服务端**使用 Python 3.11、FastAPI、Uvicorn 和 Pydantic。启动时加载场景，集中提供 REST API、OpenAPI 文档和前端静态文件。Pydantic 校验经纬度、高度、调度参数及响应结构。 [服务入口](backend/main.py) · [数据模型](backend/schemas.py)
- **地理处理与规划**使用 NumPy 和 Shapely。建筑、起降坪和围栏来自本地 GeoJSON，场景的地形与 OSM 地标也从本地文件读取。后端将 WGS84 坐标投影到以米为单位的局部 ENU 坐标系，维护体素占用图和碰撞检测索引，再使用三维 A* 搜索航线。 [空域建模](backend/core/airspace.py) · [路径规划](backend/core/planner.py)
- **模型与验证**通过 OpenAI Python SDK 接入支持函数调用的兼容模型。本地关键词、正则和场景匹配规则可独立解析任务。pytest 和 FastAPI TestClient 覆盖规划、调度和应急接口。 [模型调用](backend/core/agent.py) · [约束解析](backend/core/intent.py) · [测试用例](Test.md)

## Agent 调度实现

Agent 由后端编排。模型负责把自然语言转换成结构化任务参数，场景数据负责核实地名和坐标，三维规划器负责计算并校验航线。未配置模型或遇到调用失败、输出格式错误、地名缺乏依据时，调度流程使用本地解析。

### 从输入到起飞

1. **识别用户请求。** `POST /api/agent/dispatch` 接收自然语言、可选的地图点选和任务避让区。后端先在本地识别使用帮助、无关输入、空域查询和已有任务的补答，避免为这些请求调用模型。
2. **提取意图和约束。** 对已登记的起降坪，模型通过 `parse_dispatch_intent` 提取起止编号、任务类型和偏好。对普通地标，`extract_spatial_task` 从原指令提取起终点短语，后端再按场景目录匹配。显式限高与空域避让进入规划约束，预计耗时用于展示。
3. **确认地点和途经顺序。** 起终点可以是校门、道路、广场、起降坪编号、显式经纬度或地图点选。遇到同名地点或信息不足时，界面给出候选或点选入口。用户补答后，前面确认的地点和约束会随草稿保留。单次任务最多支持 **3 个有序途经点**，途经按空中飞越处理。
4. **筛选能起降、能接入航层的点。** 对非固定地标，先在 **96 米**内查找附近地面点，必要时加密采样。先排除超出场景、落在道路或水面，以及地形起伏超限的点，再验证建筑净空、围栏及航层接入。适合扩大搜索时，用户确认后可扩大至 **168 米**。地图会显示原地标与实际起降点的偏移。
5. **计算并复核三维航迹。** 默认体素水平分辨率为 **18 米**，垂直分辨率为 **10 米**。规划器在允许航层内搜索，综合航段距离、偏离航层中值以及贴近障碍的代价。搜索后用连续线段相交检查做航点抽稀，再接入垂直起降段。提交候选前会按偏好、地标偏移、净空和航程排序。默认航层走不通时，仅在任务允许切层的情况下尝试备选航层。 [候选与约束](backend/core/spatial_dispatch.py) · [搜索与抽稀](backend/core/planner.py)
6. **确认后进入仿真。** 固定起降坪任务可直接返回待确认的规划结果。采用动态候选时，后端复核选中的路线，生成任务上下文。操作者确认后，前端再根据航点、阶段和途经顺序驱动仿真。调度台将 `thought_steps` 整理为意图解析、约束核验、路线计算和结果摘要，并展示航程、预计耗时及最小净空。

航层以场景高程为基准。默认配送航层为 **30–60 米**，巡检航层为 **60–90 米**。HUD 的离地高度根据机位下方的地形计算。 [航层配置](backend/config.py)

### 飞行中如何改航

- **突发阵风或临时禁飞。** 仿真将当前机位、已完成的途经数、活跃围栏和电量写入改航请求。后端将新增围栏加入任务并复用原航层硬约束，从当前三维位置计算至原目的地的新航迹，保留未完成的途经点。
- **低电量备降。** 后端优先评估可达的应急起降坪，必要时评估普通起降坪。带电量数据的仿真请求按飞行时间和累计爬升估算耗电，要求落地至少保留 **2%** 电量余量，再在可行路线中优先选择耗电较低的方案。
- **应急路线无法生成。** 后端返回具体原因，并附上本次已生效的新增围栏。仿真暂停，保留应急信息供查看。重置含临时围栏的任务时，后端为新一轮仿真建立会话。 [应急规划](backend/core/agent.py) · [仿真与回放](frontend/js/app.js)

### 接口与会话

| 接口 | 作用 |
| --- | --- |
| `GET /api/layers/all` | 返回建筑、起降坪、围栏、可选地形及 `scene_version` |
| `POST /api/agent/dispatch` | 返回地点补答、候选航线、已计算的航迹或空域核验结果 |
| `POST /api/agent/confirm-candidate` | 根据 `candidate_token` 和候选编号复核选中方案，返回 `mission_token` |
| `POST /api/agent/replan` | 使用当前机位和任务上下文处理临时禁飞或低电量 |
| `POST /api/agent/restart-mission` | 为重置后的仿真生成新任务上下文 |

浏览器会随调度和确认请求提交 `scene_version`。服务端检测到版本不一致时返回 `409`，提示刷新场景。`clarification_token` 和 `candidate_token` 的有效期各为 **10 分钟**，`mission_token` 为 **4 小时**。这些上下文保存在服务进程内，重启后需重新调度。 [接口实现](backend/main.py) · [会话状态](backend/core/spatial_dispatch.py)

## 测试与验收

[Test.md](Test.md) 记录每组测试的输入、预期结果、浏览器验收步骤和历次性能结果。自动化测试使用本地规则及模型接口替身，安装项目依赖即可运行。

```powershell
conda run --no-capture-output -n VoxAero pytest -q -s
```

按任务只运行相关用例。

| 想检查什么 | 命令 |
| --- | --- |
| 地名补答、动态选址、有序途经与应急预算 | `conda run -n VoxAero pytest -q tests/test_spatial_dispatch.py` |
| 三维规划、API 和静态资源 | `conda run -n VoxAero pytest -q tests/test_planner_and_api.py` |
| 实景 A* 性能 | `conda run --no-capture-output -n VoxAero pytest -q -s tests/test_real_scene.py -k real_scene_performance` |

在浏览器复跑时，可先关闭「随机应急演练」。从 [B03 用例](Test.md#浏览器验收) 验证两处校门起飞和抵达，再用 B07、B08 验证临时禁飞绕飞、低电量备降和重置。需要核对真实模型时，先完成[模型配置](#配置模型)，再查看调度结果中的运行模式。

## 场景与数据

当前默认读取 `data/` 中的武汉大学信息学部场景。服务启动时加载一套场景，可为其他地区配置独立的数据目录和地理范围。先复制整套数据到单独目录，再替换同名 GeoJSON 并用独立端口验收。

| 文件 | 数据契约 |
| --- | --- |
| `whu_buildings.geojson` | `Polygon`，`id`、`name`、`category`、`height`。地形启用时核对 `base_alt` 和 `collision_top_alt` |
| `vertiports.geojson` | `Point`，`id`、`name`、`type`、`altitude`。地面坪应贴地，屋顶坪应贴合对应屋面 |
| `nofly_zones.geojson` | `Polygon`，`id`、`name`、`min_alt`、`max_alt`、`reason` |

三个文件均为 WGS84 的 `FeatureCollection`，坐标按 `[经度, 纬度]` 排列。高度以米计，建筑、起降点、地形和围栏需使用同一场景高程基准。切换地区时，在 `backend/config.py` 中同步调整 `STUDY_BOUNDS` 和局部坐标系原点 `ORIGIN_LON_LAT`。地标、路网和地形也应覆盖同一范围。

```powershell
$env:VOXAERO_SCENE_DIR = 'D:\scenes\whu-candidate'
conda run -n VoxAero uvicorn backend.main:app --port 8002
```

在 [候选场景](http://127.0.0.1:8002/) 核对白模、起降点、配送和巡检路线。停止对照服务后，可在同一终端运行 `Remove-Item Env:VOXAERO_SCENE_DIR` 恢复默认数据。修改数据后需重启服务。 [数据来源、许可与空域快照规范](data/README.md)

开发接口可通过 [FastAPI 文档](http://127.0.0.1:8000/docs) 查看。项目使用 OpenStreetMap 的建筑、道路和地标，以及 Mapzen 的 NASA SRTM 地形高程。
