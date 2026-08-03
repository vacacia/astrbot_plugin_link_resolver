# Link Resolver

AcaBot runtime plugin，用来监听群内的 **B站 / 抖音 / 小红书 / 微博** 链接，并自动解析、下载、发送视频或图集内容。

## 当前形态

这个目录现在是 **AcaBot 插件本体**，不是 AstrBot 插件包镜像。

运行入口和配置以这些文件为准：

- `plugin.yaml`：AcaBot 插件 manifest 与默认配置
- `__init__.py`：AcaBot runtime plugin 接线
- `main.py` + `core/**`：迁移后的解析核心
- `compat/**`：把旧宿主接口收敛到 AcaBot runtime 的兼容层

## 支持的平台

- B站
- 抖音
- 小红书
- 微博

## 配置来源

运行时配置以 AcaBot plugin spec 为准：

- 包默认值：`extensions/plugins/link_resolver/plugin.yaml`
- 运行时覆盖：`runtime_config/plugins/link_resolver/plugin.yaml`

常用配置包括：

- `enable_platforms`
- `bili_settings.*`
- `douyin_settings.*`
- `xhs_settings.*`
- `weibo_settings.*`
- `general_settings.*`

插件数据目录位于：

- `runtime_data/plugins/link_resolver/data/`

其中会生成：

- `cache/`
- `cookies/`
- `fonts/`

## 测试

在 AcaBot 仓库根目录运行：

```bash
.venv/bin/python -m pytest extensions/plugins/link_resolver/tests -q
```

## 说明

- 这份插件已经按 AcaBot runtime plugin 结构接入
- 目录里不再保留 AstrBot 插件市场元信息或 AstrBot 专用配置文件
- 兼容层仍然保留 `astrbot.api.*` 这套命名，只用于承接迁移来的旧代码接口
