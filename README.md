# Web File Manager

轻量网页版文件浏览器，浏览 `$HOME` 下的文件，支持图片 / 视频 / PDF / Markdown / HTML note / 代码预览，支持上传。
设计上为单用户、依赖 Tailscale 等可信网络层做隔离。

镜像 `~/Git/web-terminal` 的部署模式：单 `web_file_manager.py` 文件，Tornado 后端，内嵌 HTML/JS，无构建步骤。

## 功能

- Cloud Light 亮色双区布局：收藏夹 / 文件列表整合为一个可调宽度、可整体收起的文件侧栏，右侧预览区自适应占满剩余空间
- 目录收藏：文件夹行可点星标收藏；侧栏顶部只保留收藏夹区，默认折叠，点击标题展开 / 收起
- 隐藏文件开关：闭眼表示隐藏文件不可见，睁眼表示正在显示以点号开头的文件
- 移动端使用统一文件侧栏和全屏预览，选择文件后可明确返回列表
- 路径跳转：路径栏可直接粘贴 `$HOME` 下的系统绝对路径，提交时自动转换到文件管理器根路径
- 图片预览：`<img>` 直接显示，左右键 / 屏幕箭头切换同目录下张
- 视频预览：使用浏览器原生 `<video>` 播放器，默认静音（可手动开启声音），支持单段 HTTP Range 加载、拖动进度，以及 `←` / `→` 后退/前进 5 秒；支持 MP4 / WebM / Ogg Video / MOV / MKV（实际可播放编码取决于浏览器）
- PDF 预览：内嵌自托管 PDF.js 查看器（iframe），默认关闭 PDF.js 左侧缩略图栏
- 深链接：`/?open=<path>` 或 HOME 内绝对路径超链接可直接跳到 `$HOME` 下的文件或目录；支持 `%20` 这类 URL 编码，文件会自动预览
- Markdown 预览：支持 `$...$` / `$$...$$` / `\(...\)` / `\[...\]` LaTeX 公式，使用本地 MathJax 排版，可切换源码和渲染视图
- HTML note 预览：`.html` / `.htm` 在右侧以网页形式渲染；同目录相对图片 / CSS 可正常加载，LaTeX 公式通过本地托管 MathJax 渲染，并可切换 Source 查看源码
- 代码 / 文本预览：highlight.js 语法高亮，512 KB 上限，二进制自动检测（可强制以文本打开）
- 拖放上传 + 进度条；多文件并发
- 下载任意文件（带正确的 `Content-Disposition`）
- 路径越界 / 符号链接逃逸 → 403
- 强制 HTTP Basic Auth（未设 `WEBFM_AUTH` 拒绝启动）

## 依赖

- Python 3.8+
- `tornado` （任意 6.x 版本即可）

## 启动

```bash
# 必填：用户名:密码
WEBFM_AUTH="<user>:<strong-password>" python web_file_manager.py
```

环境变量：

| 变量 | 默认 | 说明 |
|------|------|------|
| `WEBFM_AUTH` | （必填） | HTTP Basic 凭证，格式 `user:pass`，未设拒绝启动 |
| `WEBFM_PORT` | `7701` | 监听端口（绑定 `0.0.0.0`） |
| `WEBFM_CONFIG_DIR` | `~/.config/web-file-manager` | 配置目录 |
| `WEBFM_FAVORITES_FILE` | `$WEBFM_CONFIG_DIR/favorites.json` | 文件夹收藏列表 |

根目录硬编码为 `Path.home()`，不可通过环境变量改动。

## Tailscale 访问

服务器绑 `0.0.0.0:7701`。通过 Tailscale magic DNS 从任意设备访问：

```
http://<tailscale-hostname>:7701
```

例如 `http://my-laptop:7701`。

## systemd 用户单元（可选）

新建 `~/.config/systemd/user/webfilemanager.service`：

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

启用：

```bash
systemctl --user daemon-reload
systemctl --user enable --now webfilemanager.service
systemctl --user status webfilemanager.service
```

## API（仅供调试）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | HTML 主页；可带 `?open=`, `?path=`, 或 `?file=` 打开指定文件/目录，也可处理 HOME 内绝对路径深链接 |
| `GET` | `/api/list?path=` | 目录列表 JSON |
| `GET` | `/api/favorites` | 收藏文件夹列表 |
| `POST` | `/api/favorites` | 收藏 / 取消收藏文件夹，JSON: `{"path":"/dir","favorite":true}` |
| `GET` | `/api/file?path=&mode=raw\|text\|download&force=0\|1` | 文件流 / 文本 JSON / 下载；二进制流支持单段 HTTP Range |
| `GET` | `/preview/<path>` | HTML note 预览及其相对资源加载用的鉴权文件流 |
| `GET` | `/mathjax/<path>` | 本地托管 MathJax 静态资源 |
| `POST` | `/api/upload?path=` | multipart 上传到目标目录 |

`/api/list` 响应：

```json
{"path": "/Pictures", "parent": "/",
 "entries": [{"name":"Trips","is_dir":true,"size":4096,"mtime":1714200000,"kind":"dir","favorite":true}]}
```

`kind` ∈ `dir | image | video | pdf | markdown | html | text | other`。目录项会额外带 `favorite` 布尔值。

## 安全说明

- 路径解析使用 `Path.resolve()`，会展开符号链接；任何指向 `$HOME` 之外的链接会被 403 拒绝
- 上传文件名通过 `os.path.basename` 剥掉路径组件，并再次 `resolve` 校验目标路径
- 上传上限 10 GB（`MAX_UPLOAD_BYTES`），由 Tornado 在内存里缓冲；如需上传超大文件请自行调小 / 改成 `@stream_request_body`
- 文本预览上限 512 KB，超出会截断并显示横幅
- HTML note 预览按可信个人笔记处理，不加 iframe sandbox / CSP 限制；HTML 内自带 MathJax 时保留笔记自己的配置并把 CDN 地址改写到本地 `/mathjax/`，否则服务端注入默认本地 MathJax
- **不要把端口暴露在公网**——这个程序只针对 LAN / Tailscale 等可信网络

## 验证清单（端到端手动）

```bash
# a. 拒绝裸跑
unset WEBFM_AUTH; python web_file_manager.py
# → 退出码 2，提示 "WEBFM_AUTH must be set"

# b. 启动 + 鉴权（仅本地测试示例）
WEBFM_AUTH="<user>:<password>" WEBFM_PORT=7799 python web_file_manager.py &
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:7799/        # 401
curl -s -o /dev/null -w "%{http_code}\n" -u "<user>:<password>" http://localhost:7799/  # 200

# c. 浏览 + 预览（浏览器打开 http://localhost:7799）
#    点 ~/Pictures 中的图片，按 → 切下一张
#    点 .mp4 / .webm 视频，右侧出现浏览器原生播放器并可播放
#    点 .py 文件，看到语法高亮
#    点 .html 文件，右侧以网页形式渲染；$...$/$$...$$ LaTeX 公式会渲染，Source 按钮可切源码
#    点 .pdf，iframe 渲染
#    点文件夹行星标加入收藏夹；从左侧导航快速跳转或取消收藏

# d. 路径越界
curl -s -u "<user>:<password>" "http://localhost:7799/api/list?path=../../etc"   # 403

# e. 移动端模拟（Chrome DevTools → 设备模式 → 390px）
#    收藏夹折叠区位于统一文件侧栏顶部；点文件进入全屏预览；返回按钮回到列表

# f. 拖放上传
#    把若干文件拖进左侧列表区，看到进度条

# g. 界面
#    桌面端拖动文件列表和预览区之间的分隔条调整宽度
#    按 B 整体收起 / 展开文件侧栏；所有界面使用 Cloud Light 亮色设计
```

## 文件结构

```
web-file-manager/
├── web_file_manager.py  # 后端 + 内嵌 HTML/JS（单文件）
├── test_web_file_manager.py  # 文件流 / Range 自动测试
├── pdfjs/        # 自托管 PDF.js viewer 静态资源
├── mathjax/      # 自托管 MathJax 静态资源
└── README.md
```
