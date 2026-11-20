# 武大信息学部场景数据

- 建筑轮廓、路网和地标取自 OpenStreetMap。衍生地理数据标注 © OpenStreetMap contributors，按 ODbL 1.0 提供。每个建筑或参考地物带 `osm_ref`，文件头记录原始提取范围和 SHA-256。[OpenStreetMap 许可](https://www.openstreetmap.org/copyright)
- 地形取自 Mapzen Terrain Tiles 的 `N30E114` Skadi 瓦片，使用 NASA SRTM 约 30 m 高程。经纬度为 WGS84，源高程为 EGM96。场景 `alt` 是相对于裁切网格最低高程 27 m 的模型米值，航层和围栏使用同一基准。[AWS Terrain Tiles](https://registry.opendata.aws/terrain-tiles/) · [Mapzen 数据来源](https://github.com/tilezen/joerd/blob/master/docs/attribution.md)
- `osm_height` 表示 OSM 高度标签，`osm_levels_estimate` 表示按 OSM 楼层估算，`footprint_estimate` 表示仅供显示的轮廓估高。缺少高度或楼层依据的建筑在碰撞规划中封闭至航高上限。`base_alt` 和 `height` 共同定义模型屋面，`collision_top_alt` 可扩大包络。校内外类别无法确认时使用 `other`。
- `VP01` 至 `VP06` 是依据地标放置的仿真起降坪。`VP03` 屋面高度取 4 层估算值，地面坪使用插值地形高度。三处电子围栏是按地物定义的仿真规则。建筑和地形可用于数字孪生演示，现场使用需补充现场测量与授权信息。

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
