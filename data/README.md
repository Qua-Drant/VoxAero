# 武大信息学部场景数据

- 建筑轮廓、路网和地标取自 OpenStreetMap。衍生地理数据标注 © OpenStreetMap contributors，按 ODbL 1.0 提供。每个建筑或参考地物带 `osm_ref`，文件头记录原始提取范围和 SHA-256。[OpenStreetMap 许可](https://www.openstreetmap.org/copyright)
- 地形取自 Mapzen Terrain Tiles 的 `N30E114` Skadi 瓦片，使用 NASA SRTM 约 30 m 高程。经纬度为 WGS84，源高程为 EGM96。场景 `alt` 是相对于裁切网格最低高程 27 m 的模型米值，航层和围栏使用同一基准。[AWS Terrain Tiles](https://registry.opendata.aws/terrain-tiles/) · [Mapzen 数据来源](https://github.com/tilezen/joerd/blob/master/docs/attribution.md)
- `osm_height` 表示 OSM 高度标签，`osm_levels_estimate` 表示按 OSM 楼层估算，`footprint_estimate` 表示仅供显示的轮廓估高。缺少高度或楼层依据的建筑在碰撞规划中封闭至航高上限。`base_alt` 和 `height` 共同定义模型屋面，`collision_top_alt` 可扩大包络。校内外类别无法确认时使用 `other`。
- `VP01` 至 `VP06` 是依据地标放置的仿真起降坪。`VP03` 屋面高度取 4 层估算值，地面坪使用插值地形高度。三处电子围栏是按地物定义的仿真规则。建筑和地形可用于数字孪生演示，现场使用需补充现场测量与授权信息。

## 动态选址和空域核验

- 地名从已加载场景匹配。可输入「从信息学部附近找一个可起飞的地面点，去图书馆西侧巡检，绕开静谧区」，也可输入 `[经度, 纬度]` 或在地图点选。地图点选和明确坐标按原位置校验；区域描述会生成附近的地面候选。
- 从候选中选择一组后，服务端复核其路线。临时起降点和用户圈定的避让区绑定到任务会话。候选窗口为 10 分钟，确认后的应急上下文保留 4 小时，服务重启会清除会话。最近任务回放保留当次飞行的点位和围栏。
- 可用「核验图书馆静谧区是否禁飞」查询已加载规则，也可先点选位置再输入「核验这里的空域」。报告区分场景围栏、任务避让和外部快照。地面候选使用数字孪生地形进行仿真筛选，屋顶选址及运行授权需要独立可靠的数据。

### 导入外部空域快照

将有来源依据的规则转为 `airspace_rules.geojson`，放在当前场景数据目录并重启服务。文件是 WGS84 的 `FeatureCollection`，每个围栏使用 `Polygon` 与现有 `id/name/min_alt/max_alt/reason` 字段。根节点的 `rule_source` 包含：

| 字段 | 要求 |
| --- | --- |
| `name`、`url`、`version` | 数据来源名称、原始链接、快照版本 |
| `horizontal_datum` | `EPSG:4326` |
| `vertical_datum` | `EGM96` 或 `scene_relative`。后者需要匹配场景的 `reference_elevation_m` |
| `coverage_bbox` | `[西, 南, 东, 北]` |
| `valid_from`、`valid_until` | 带时区的 ISO 8601 有效期 |

围栏可另填 `effective_from` 和 `effective_until`，默认采用快照时段。导入时会将 EGM96 高度换算到场景基准。为让暂停、改航和回放使用同一几何，导入围栏会作为当前仿真的保守限制。查询会说明规则登记时段，路线仅在数据覆盖整个航迹和预计飞行时段时显示「外部空域快照已复核」。数据缺失、格式不符、越界或过期时显示「外部空域待核验」。

## 重建

从资产的 `source` 地址取得源 OSM XML 和压缩 HGT。先写到临时目录并验收，再替换正式场景文件。地形和建筑的缓存会在服务启动时加载。

```powershell
conda run -n VoxAero python -m scripts.build_whu_scene --osm third_party/whu_sources/whu.osm --dem third_party/whu_sources/N30E114.hgt.gz --output third_party/whu_sources/candidate
```

## 对照 Mock 场景

从项目根目录启动另一端口，访问 `http://127.0.0.1:8001`。主预览读取 `data/` 的默认实景。

```powershell
$env:VOXAERO_SCENE_DIR = 'data/mock_backup'
conda run -n VoxAero uvicorn backend.main:app --port 8001
```

停止对照服务后，用 `Remove-Item Env:VOXAERO_SCENE_DIR` 恢复该终端的默认场景设置。
