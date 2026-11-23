# VoxAero · 维空智航

VoxAero 是武汉大学信息学部的低空三维数字孪生与无人机调度仿真。输入配送或巡检任务后，可确认地标与起降点，预览按当前场景计算的三维航线，再执行飞行、应急改航和任务回放。

## 快速启动

需要 Conda 和支持 WebGL 的浏览器。在仓库根目录运行下列命令。已有 VoxAero 环境时，直接安装依赖和启动。

```powershell
conda create -n VoxAero python=3.11 -y
conda run -n VoxAero pip install -r requirements.txt
conda run -n VoxAero uvicorn backend.main:app --reload
```

打开 [http://127.0.0.1:8000/](http://127.0.0.1:8000/)。如果 8000 被占用，可用下列命令改用 [8001](http://127.0.0.1:8001/)。

```powershell
conda run -n VoxAero uvicorn backend.main:app --reload --port 8001
```

也可以先用 `conda activate VoxAero` 激活环境再执行 `uvicorn backend.main:app --reload`，用 `Ctrl+C` 停止服务。FastAPI 同时提供页面和 API，前端使用原生 JS，无需构建。校园模型和地形由本机服务读取，地图瓦片和 Cesium CDN 由浏览器加载，无需 Cesium Ion Token。

## 可选模型配置

默认可用本地规则完成规划和演示。在项目根目录将 `.env.example` 复制为 `.env`。已有 `.env` 时直接编辑该文件。

```powershell
if (!(Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
```

| 变量 | 配置内容 |
| --- | --- |
| `LLM_API_KEY` | 你的模型服务密钥，填入本机 `.env` |
| `LLM_BASE_URL` | 服务商的 OpenAI 兼容基础地址，例如 `https://api.openai.com/v1` |
| `LLM_MODEL` | 支持函数调用的模型标识，例如账户中可用的百炼 `qwen-flash` |

百炼的基础地址从控制台复制，区域需和密钥一致。配置变更后重启服务。系统环境变量优先于 `.env` 中同名的值。`.env` 已被 Git 忽略，仓库中的 `.env.example` 只提供配置模板。密钥由后端读取。 [百炼 OpenAI 兼容接口](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions)

访问 `GET /api/health` 查看模型配置模式。`live_llm` 表示服务已配置模型，具体请求是否完成模型调用可在调度结果中核对。模型超时或返回无效结果时，当次调度使用本地规则。`fallback_mock` 模式也使用当前已加载的场景计算航线。

## 浏览器验收

1. 首次打开页面，关闭指南后核对校园白模、起降坪、空域网格和地图署名。切换一个图层，确认地图立即更新。
2. 输入「从珞瑜门到青年宿舍楼」并回车。选择东侧或西侧入口，核对地标、实际落点及其距离。点击「采用候选」和「确认并开始仿真」。
3. 巡航中切换全局、跟随与第一人称视角，核对航迹、遥测和 HUD。需要复现正常抵达时，可在起飞前关闭「随机应急演练」。
4. 另起巡检任务，输入「从星湖南路出发，依次途经星湖、星空广场，最后到信息学部操场巡检，巡航高度60到80米」。巡航时在「突发应急」中触发临时禁飞，核对从当前机位生成的新航线及剩余途经点。再触发低电量，核对备降点与预计落地电量。
5. 停机后展开「最近任务记录」并打开回放，通过底部时间轴回看飞行与应急事件。

更多操作和失败场景见 [Test.md](Test.md)。完整测试在仓库根目录运行。

```powershell
conda run --no-capture-output -n VoxAero pytest -q -s
```

## 替换场景数据

默认从 `data/` 加载武大信息学部实景。建议先把该目录复制到一个单独的场景目录，对照下表替换文件并保持相同文件名。

| 文件 | 必需字段与坐标 |
| --- | --- |
| `whu_buildings.geojson` | `Polygon` 和 `id`、`name`、`category`、`height`。自定义基准高程时核对 `base_alt`、`collision_top_alt` |
| `vertiports.geojson` | `Point` 和 `id`、`name`、`type`、`altitude`。地面坪位于可起降地面，屋顶坪贴合对应屋面 |
| `nofly_zones.geojson` | `Polygon` 和 `id`、`name`、`min_alt`、`max_alt`、`reason` |

三个文件均为 WGS84 的 `FeatureCollection`，坐标顺序为 `[经度, 纬度]`。高度单位为米，建筑、起降坪、地形和围栏须使用一致的场景高程基准。校验范围由 `backend/config.py` 中的 `STUDY_BOUNDS` 等设置确定。地标、路网和地形文件应和新的数据范围一致。外部空域快照规范、地形高程基准及数据重建方式见 [数据说明](data/README.md)。

用独立端口加载候选数据进行对比。将路径换成你的场景目录。

```powershell
$env:VOXAERO_SCENE_DIR = 'D:\scenes\whu-candidate'
conda run -n VoxAero uvicorn backend.main:app --port 8002
```

在 [8002](http://127.0.0.1:8002/) 检查白模、起降点和至少一条配送、一条巡检航线后，再替换正式场景。停止对照服务，在同一个终端执行 `Remove-Item Env:VOXAERO_SCENE_DIR` 可恢复默认数据。场景数据在服务启动时载入，修改文件后需重启。

## API 与数据来源

- `GET /api/health` 返回服务状态和模型配置模式。
- `GET /api/layers/all` 返回场景图层、航层和场景版本。
- `POST /api/agent/dispatch` 接收自然语言指令，返回补答、候选路线或规划结果。
- `POST /api/agent/confirm-candidate` 复核所选候选。`POST /api/agent/replan` 从当前机位计算应急航线。
- 建筑轮廓、路网和地标来自 OpenStreetMap，地形由 Mapzen 的 NASA SRTM 高程数据构建。数据来源、许可与估高口径见 [数据说明](data/README.md)。空域审核结果适用于当前仿真场景。
