# Web File Manager

[English](README.md) | 简体中文

轻量级单用户网页文件浏览器。界面根目录固定为运行用户的 `$HOME`，支持文件预览、收藏夹、上传和下载。

## 界面

<img src="docs/screenshots/web-file-manager-demo.png" alt="Web File Manager Cloud Light 界面" width="600">

## 功能

- Cloud Light 亮色界面：左侧为收藏夹和文件列表，右侧为预览区。
- 文件侧栏可调整宽度，也可通过按钮或 `B` 键整体收起。
- 收藏夹默认折叠；目录行的星标用于添加或取消收藏。
- 隐藏文件开关使用闭眼和睁眼图标表示当前状态。
- 路径栏支持粘贴 `$HOME` 内的绝对路径，也支持 `/?open=<path>` 深链接。
- 图片支持预览和同目录左右切换。
- 视频使用浏览器原生播放器，默认静音，支持拖动进度和 `←` / `→` 后退或前进 5 秒。可播放的编码取决于浏览器。
- PDF 使用本地 PDF.js 查看器打开。
- Markdown 支持语法高亮和 `$...$`、`$$...$$`、`\(...\)`、`\[...\]` LaTeX 公式。
- HTML 文件支持网页预览和源码切换；文本与代码文件支持语法高亮。
- 支持拖放上传、多文件上传和文件下载。
- 窄屏下文件列表和预览区分别显示，预览页提供返回列表按钮。

## 运行

依赖 Python 3.8+ 和 Tornado 6.x。

```bash
WEBFM_AUTH="<user>:<strong-password>" python web_file_manager.py
```

默认监听 `0.0.0.0:7701`。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WEBFM_AUTH` | 必填 | HTTP Basic 凭证，格式为 `user:pass` |
| `WEBFM_PORT` | `7701` | 监听端口 |
| `WEBFM_CONFIG_DIR` | `~/.config/web-file-manager` | 配置目录 |
| `WEBFM_FAVORITES_FILE` | `$WEBFM_CONFIG_DIR/favorites.json` | 收藏夹数据文件 |

## systemd 用户服务

`~/.config/systemd/user/webfilemanager.service`：

```ini
[Unit]
Description=Web File Manager
After=network.target

[Service]
Type=simple
Environment="WEBFM_AUTH=<user>:<strong-password>"
Environment=WEBFM_PORT=7701
WorkingDirectory=%h
ExecStart=/path/to/python /path/to/web-file-manager/web_file_manager.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now webfilemanager.service
```

## 运行边界

- 未设置 `WEBFM_AUTH` 时服务拒绝启动。
- 界面只能访问 `$HOME` 内容；越界路径和指向 `$HOME` 外的符号链接返回 403。
- HTML 预览按可信个人文档处理，不使用 iframe sandbox。
- 服务适合可信 LAN 或 Tailscale 网络，不应直接暴露在公网。
