# Alltools 🛠️

<div align="center">

**本地优先的办公与开发工具台**

PDF ↔ Word 转换、发票合并、编码调试与取件码文件快递

浏览器直达、无需安装客户端、数据不出域、完全免费

[![License](https://img.shields.io/badge/license-GPLv3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-green.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-teal.svg)](https://fastapi.tiangolo.com/)

</div>

---

## ✨ 特性

- 🚀 **零依赖运行**：基于 Python/FastAPI 构建，核心功能尽量使用系统自带或轻量级依赖
- 🔒 **数据隐私**：所有文件处理在服务端完成，数据不出域，适合企业或个人私有化部署
- 🎨 **现代化界面**：响应式设计，支持深色模式，命令面板（Ctrl+K）快速导航
- 🛡️ **安全可靠**：管理后台认证授权，输入验证，API限流防护
- 📦 **一键部署**：提供 Docker 配置，几分钟内完成部署
- 🔧 **模块化设计**：工具按分类组织，易于扩展和定制
- 📊 **后台管理**：完整的工具管理、分类管理、上传记录查看功能
- 🌐 **PWA支持**：支持安装为桌面应用，离线功能

## 🧩 核心功能

### 📕 PDF 处理
- **PDF 转 Word**：纯文本/表格 PDF 转 Word，支持合并单元格、嵌套样式、图片嵌入、可选 OCR、批量 ZIP
- **Word 转 PDF**：基于 LibreOffice 引擎，支持 Windows 下回退 Microsoft Word
- **发票合并**：两张发票合并到一张 A4 纸，支持页内预览和打印

### 🖼️ 图片处理
- **图片压缩**：高观感压缩 JPEG/PNG/GIF/SVG，显著减小体积
- **格式转换**：7 种格式互转：JPEG/PNG/WebP/GIF/BMP/TIFF/ICO，保留透明和动图
- **图片转 PDF**：多图合成 PDF，支持原图像素/A4 适配，自动校正 EXIF 方向
- **九宫格切图**：一张图切成 N×N 小块打包 ZIP，支持无缝拼接
- **图片加水印**：文字 / Logo 水印，支持透明度、颜色、角度、位置与斜向平铺

### ✏️ 文本处理
- **Base64 编解码**：支持标准/URL-safe、多字符集、文件编码、换行折叠
- **Unicode 还原**：将 \uXXXX、U+XXXX、HTML 实体等转义还原为中文
- **代码格式化**：多语言美化/压缩（JSON/JS/TS/Python/HTML/CSS/XML/SQL/YAML）
- **Markdown 编辑**：左右分栏编辑与实时 HTML 预览，支持表格、代码块
- **时间戳转换**：Unix 时间戳与日期时间互转，三时区显示，自动识别输入
- **正则测试**：正则表达式匹配/捕获/替换测试，支持常用标志
- **人民币大写**：数字金额转财务规范中文大写，支持角分、千分位
- **二维码生成**：为网址、文本、Wi-Fi、邮件生成自定义二维码

### 📦 特色功能
- **文件快递**：上传文件生成 6 位取件码，可设有效期与下载次数，对方输入即可下载

## 🏗️ 技术架构

### 后端技术栈
- **框架**：FastAPI 0.100+
- **异步运行**：Uvicorn ASGI 服务器
- **数据库**：SQLite
- **并发处理**：ProcessPoolExecutor + Redis（可选）
- **限流**：基于 Redis 或内存的 API 限流
- **文件处理**：LibreOffice、Tesseract OCR
- **前端模板**：Jinja2

### 前端技术栈
- **样式**：Tailwind CSS（自定义设计）
- **图标**：Phosphor Icons
- **字体**：Inter（系统字体回退）
- **交互**：原生 JavaScript，无前端框架
- **PWA**：Service Worker + Web Manifest

### 架构设计
```mermaid
graph TB
    A[用户请求] --> B[Uvicorn ASGI服务器]
    B --> C[FastAPI应用]
    C --> D[工具路由层]
    C --> E[管理后台]
    C --> F[核心功能层]
    C --> G[存储层]
    
    D --> H[PDF处理]
    D --> I[图片处理]
    D --> J[文本处理]
    D --> K[文件快递]
    
    E --> L[认证授权]
    E --> M[工具管理]
    E --> N[分类管理]
    
    F --> O[并发控制]
    F --> P[作业队列]
    F --> Q[API限流]
    F --> R[工具目录]
    
    G --> S[SQLite数据库]
    G --> T[文件存储]
    G --> U[配置持久化]
    
    H --> V[LibreOffice]
    H --> W[Tesseract OCR]
    P --> X[Redis可选]

---

## 🧩 插件开发

所有功能工具（含原内置工具）均以**插件**形式位于 `plugins/<名称>/` 目录，启动时自动注册页面、路由、导航、工具开关、sitemap 与健康检查，无需改动主程序代码。原内置工具迁移为插件后，`tools/` 仅保留注册表基础设施与共享辅助（`tools/common.py`、`tools/pipeline.py`），以及 `/tools/json` 遗留 308 重定向。

插件目录约定（见 `plugins/text-lines/` 示例）：

```
plugins/
  <name>/
    __init__.py   # 必需：TOOL 清单 + router
    templates/    # 可选：私有 Jinja2 模板（页面名 tools/<slug>.html）
    static/       # 可选：私有静态资源，挂载于 /plugins/<slug>/static
```

`__init__.py` 最小契约：

```python
from fastapi import APIRouter
from tools.common import templates, with_nav

PLUGIN_VERSION = "1.0.0"                       # 可选
TOOL = {                                       # 与内置工具注册表同构
    "slug": "text-lines",                      # 唯一，^[a-z0-9-]+$
    "name": "文本行处理",
    "category": "text",                        # 栏目 id（内置或自定义）
    "description": "…", "icon": "📋",
    "route": "/tools/text-lines",
    "features": [...], "cta": "开始处理", "accent": "cyan",
    "order": 100,                              # 可选：首页展示顺序（升序，缺省 999）
}
router = APIRouter(prefix="/tools/text-lines", tags=["text-lines"])
```

要点：

- **信任边界**：插件是同进程内任意 Python，拥有与主程序同等权限（数据库、文件、LibreOffice）。只放置你信任的代码；`PLUGINS_DIR` 环境变量可指向受控目录。
- **错误容忍**：导入失败、清单缺失、slug 冲突的插件会被跳过并记录日志，应用照常启动；状态可在管理后台「系统状态 → 插件」查看。
- **启用/停用**：插件注册后自动出现在「功能开关」页，无需改代码。
- **依赖**：插件若需第三方库，将 `requirements.txt` 由运维手动安装；缺依赖时该插件标记不可用而非崩溃。
- **生效方式**：新插件或插件代码修改**无需重启**——管理后台「系统状态 → 插件 → 热重载插件」即可重新扫描并生效（路由、注册表、模板、静态资源、sitemap 一并刷新）；旧插件代码会被重新执行，加载失败的插件自动跳过。
- **自动热插拔（可选）**：设置 `PLUGIN_AUTO_RELOAD=1` 后，进程每 3 秒检测插件目录文件变化并自动重载（默认关闭，适合开发；生产建议手动重载）。
- **限流约定**：公开 POST 接口默认不限流。重操作（大文件、外部进程、第三方调用）请沿用内置工具约定——路径包含 `/convert`、`/compress`、`/send` 等标记即可被 `PublicRateLimitMiddleware` 按 IP 限流；同时插件应自带输入大小上限（参考示例的 `MAX_CHARS`）。

---

## 💾 数据与备份

所有持久化数据都在 `file/` 目录（Docker 中为挂载卷）：

- **SQLite 数据库**：`file/records.db`（上传历史索引）、`file/express/express.db`（取件码包）
- **归档输入文件**：`file/YYYY-MM-DD/`（历史上传，按保留期自动清理）、`file/express/YYYY-MM-DD/`
- **配置**：`file/tool_flags.json`、`file/tool_catalog.json`、`file/donation.json`、`file/donation/qr.png`
- **异步任务产物**：`file/jobs/`（临时，TTL 后自动清理）

**备份建议**（SQLite 使用 WAL 模式，不要直接拷贝运行中的 db 文件）：

```bash
# 在线一致性备份（SQLite 自带 .backup 命令）
sqlite3 file/records.db ".backup 'backup-records.db'"
sqlite3 file/express/express.db ".backup 'backup-express.db'"
# 再拷贝归档文件目录
cp -r file/2026-* backup/
```

恢复时将备份文件放回原路径并重启即可；`records.db` 与归档目录必须一起备份才能保留「记录 ↔ 文件」对应关系。

