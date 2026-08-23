# RAG 智能问答系统 — 前端 UI 实现规格

> **文档用途**：面向大语言模型代码生成。读完本文档后，模型应能生成完整可运行的前端代码，
> 与 `rag_framework_spec.md` 中定义的后端 API 对接，无需额外决策。
>
> **约定**：
> - 技术栈：原生 HTML5 + CSS3 + Vanilla JS (ES2022) + Alpine.js 3.x
> - 无构建步骤，无 npm，所有依赖通过 `<script src>` CDN 引入
> - 后端由 FastAPI 托管静态文件（单体部署），Jinja2 渲染模板
> - 流式输出：SSE（Server-Sent Events）
> - 中文界面，字体：系统默认中文字体栈

---

## 目录

1. [技术栈与依赖](#1-技术栈与依赖)
2. [目录结构](#2-目录结构)
3. [设计系统](#3-设计系统)
4. [页面路由](#4-页面路由)
5. [布局模板](#5-布局模板-basejinja2)
6. [聊天界面](#6-聊天界面)
7. [知识库管理界面](#7-知识库管理界面)
8. [系统配置界面](#8-系统配置界面)
9. [API 对接层](#9-api-对接层)
10. [组件库](#10-组件库)
11. [后端模板路由](#11-后端模板路由fastapi-侧)
12. [实现顺序与自检清单](#12-实现顺序与自检清单)

---

## 1. 技术栈与依赖

### 1.1 核心选型理由

| 技术 | 选择 | 理由 |
|------|------|------|
| 框架 | 无框架（Vanilla JS） | 零构建步骤，现场部署只需复制文件 |
| 响应式状态 | Alpine.js 3.x（15KB） | CDN 引入，处理对话列表/表单开关等响应式逻辑，无需构建 |
| 模板渲染 | Jinja2（服务端） | FastAPI 原生支持，初始数据注入无需额外 API 请求 |
| 流式输出 | SSE + ReadableStream | 浏览器原生支持，无需 WebSocket 复杂度 |
| 样式 | 纯 CSS（CSS 变量 + Flexbox/Grid） | 无需编译，暗色模式用 `prefers-color-scheme` |
| 图标 | Tabler Icons CDN | 5800+ 图标，CDN 引入，`<i class="ti ti-xxx">` 使用 |

### 1.2 CDN 依赖清单

```html
<!-- Alpine.js — 响应式状态管理（必须在 body 末尾或 defer 加载） -->
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js"></script>

<!-- Tabler Icons — 图标字体 -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css">

<!-- marked.js — Markdown 渲染（AI 回答含 markdown） -->
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"></script>

<!-- highlight.js — 代码块高亮 -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github.min.css">
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/core.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/languages/bash.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/languages/python.min.js"></script>
```

### 1.3 浏览器兼容目标

Chrome 90+、Edge 90+、Firefox 88+、Safari 14+（企业内网场景，不需要兼容旧版本）。

---

## 2. 目录结构

```
rag/
└── api/
    ├── static/                    # FastAPI StaticFiles 挂载点
    │   ├── css/
    │   │   ├── base.css           # CSS 变量、Reset、全局样式
    │   │   ├── layout.css         # 三栏布局（侧边栏 + 主区）
    │   │   ├── chat.css           # 聊天气泡、消息列表、输入框
    │   │   ├── knowledge.css      # 知识库管理界面样式
    │   │   └── config.css         # 配置界面样式（表单、开关、资源卡）
    │   ├── js/
    │   │   ├── api.js             # 所有后端 API 调用封装
    │   │   ├── chat.js            # 聊天页面 Alpine 组件
    │   │   ├── knowledge.js       # 知识库页面 Alpine 组件
    │   │   ├── config.js          # 配置页面 Alpine 组件
    │   │   └── utils.js           # 工具函数（markdown 渲染、时间格式等）
    │   └── favicon.ico
    │
    └── templates/                 # Jinja2 模板
        ├── base.html              # 基础布局（侧边栏 + 主区骨架）
        ├── chat.html              # 聊天界面（extends base.html）
        ├── knowledge.html         # 知识库管理界面
        └── config.html            # 系统配置界面
```

---

## 3. 设计系统

### 3.1 CSS 变量定义

**文件**：`static/css/base.css`

```css
/* ── 颜色系统 ──────────────────────────────────────────────── */
:root {
  /* 背景层级（数字越大越亮） */
  --surface-0:  #f5f4f0;   /* 页面底色 */
  --surface-1:  #eeedea;   /* 侧边栏、次级区域 */
  --surface-2:  #ffffff;   /* 卡片、输入框、主内容区 */

  /* 文字 */
  --text-primary:   #1a1a18;
  --text-secondary: #5f5e5a;
  --text-muted:     #888780;

  /* 边框 */
  --border:        rgba(0,0,0,0.10);
  --border-strong: rgba(0,0,0,0.18);

  /* 强调色（蓝） */
  --accent:        #185fa5;
  --accent-bg:     #e6f1fb;
  --accent-text:   #0c447c;
  --accent-border: #b5d4f4;
  --accent-fill:   #185fa5;   /* 按钮填充 */

  /* 语义色 */
  --success-bg:    #eaf3de;
  --success-text:  #3b6d11;
  --success-border:#c0dd97;
  --danger-bg:     #fcebeb;
  --danger-text:   #a32d2d;
  --danger-border: #f7c1c1;
  --warning-bg:    #faeeda;
  --warning-text:  #854f0b;

  /* 间距 */
  --radius: 8px;
  --pad-sm: 8px;
  --pad-md: 16px;
  --pad-lg: 24px;

  /* 字体 */
  --font-sans: -apple-system, "PingFang SC", "Microsoft YaHei", "Hiragino Sans GB",
               BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-mono: "JetBrains Mono", "Fira Code", "Cascadia Code", "Courier New", monospace;
}

/* 暗色模式 */
@media (prefers-color-scheme: dark) {
  :root {
    --surface-0:  #1a1a18;
    --surface-1:  #222220;
    --surface-2:  #2c2c2a;
    --text-primary:   #f0efe9;
    --text-secondary: #a8a79f;
    --text-muted:     #66655e;
    --border:        rgba(255,255,255,0.10);
    --border-strong: rgba(255,255,255,0.18);
    --accent-bg:     #0c2a4a;
    --accent-text:   #85b7eb;
    --accent-border: #185fa5;
    --success-bg:    #17340a;
    --success-text:  #97c459;
    --danger-bg:     #2a0f0f;
    --danger-text:   #f09595;
  }
}

/* ── Reset ───────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; font-family: var(--font-sans); font-size: 14px;
             line-height: 1.6; color: var(--text-primary);
             background: var(--surface-0); }
button { font-family: inherit; cursor: pointer; }
input, select, textarea { font-family: inherit; font-size: 13px; }

/* ── 通用工具类 ──────────────────────────────────────────────── */
.sr-only { position:absolute; width:1px; height:1px; padding:0;
           margin:-1px; overflow:hidden; clip:rect(0,0,0,0);
           white-space:nowrap; border:0; }

/* 徽章 */
.badge {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; border-radius: var(--radius);
  font-size: 11px; font-weight: 500; white-space: nowrap;
}
.badge-success { background:var(--success-bg); color:var(--success-text); }
.badge-danger  { background:var(--danger-bg);  color:var(--danger-text);  }
.badge-neutral { background:var(--surface-1);  color:var(--text-muted);
                 border: 0.5px solid var(--border); }

/* 按钮 */
.btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 14px; border-radius: var(--radius);
  font-size: 13px; border: 0.5px solid var(--border-strong);
  background: transparent; color: var(--text-primary);
  transition: background 0.1s;
}
.btn:hover { background: var(--surface-1); }
.btn-primary {
  background: var(--accent-fill); color: #fff;
  border-color: transparent;
}
.btn-primary:hover { opacity: 0.9; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.btn-icon {
  width: 30px; height: 30px; padding: 0;
  display: inline-flex; align-items: center; justify-content: center;
  border: 0.5px solid var(--border); border-radius: var(--radius);
  background: transparent; color: var(--text-secondary);
}
.btn-icon:hover { background: var(--surface-1); color: var(--text-primary); }

/* 表单元素 */
.field {
  width: 100%; padding: 6px 10px;
  border: 0.5px solid var(--border-strong); border-radius: var(--radius);
  background: var(--surface-2); color: var(--text-primary); font-size: 13px;
  outline: none; transition: border-color 0.15s;
}
.field:focus { border-color: var(--accent); }
.field::placeholder { color: var(--text-muted); }

/* 分隔线 */
.divider { border: none; border-top: 0.5px solid var(--border); margin: 12px 0; }

/* 加载动画 */
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
.typing-dot {
  display: inline-block; width: 6px; height: 6px;
  border-radius: 50%; background: var(--text-muted);
  animation: blink 1.2s infinite;
}
.typing-dot:nth-child(2) { animation-delay: 0.2s; }
.typing-dot:nth-child(3) { animation-delay: 0.4s; }

/* Markdown 渲染区域 */
.md-content { line-height: 1.7; font-size: 13px; }
.md-content p { margin-bottom: 8px; }
.md-content p:last-child { margin-bottom: 0; }
.md-content code {
  font-family: var(--font-mono); font-size: 12px;
  background: var(--surface-1); padding: 1px 5px;
  border-radius: 4px; border: 0.5px solid var(--border);
}
.md-content pre {
  background: var(--surface-0); border: 0.5px solid var(--border);
  border-radius: var(--radius); padding: 12px; overflow-x: auto;
  margin: 8px 0;
}
.md-content pre code { background:none; border:none; padding:0; }
.md-content ul, .md-content ol { padding-left: 20px; margin-bottom: 8px; }
.md-content li { margin-bottom: 3px; }
.md-content h3 { font-size: 14px; font-weight: 500; margin: 12px 0 6px; }
.md-content blockquote {
  border-left: 3px solid var(--border-strong); padding-left: 12px;
  color: var(--text-secondary); margin: 8px 0;
}
.md-content table {
  width: 100%; border-collapse: collapse; font-size: 12px; margin: 8px 0;
}
.md-content th, .md-content td {
  padding: 6px 10px; border: 0.5px solid var(--border); text-align: left;
}
.md-content th { background: var(--surface-1); font-weight: 500; }
```

### 3.2 三栏布局

**文件**：`static/css/layout.css`

```css
/* 整体三区布局：侧边栏 | 主内容 */
.app-shell {
  display: flex; height: 100vh; overflow: hidden;
}

/* ── 侧边栏 ─────────────────────────────────────────────────── */
.sidebar {
  width: 220px; flex-shrink: 0;
  background: var(--surface-1);
  border-right: 0.5px solid var(--border);
  display: flex; flex-direction: column;
  overflow: hidden;
}
.sidebar-header {
  padding: 14px 12px 10px;
  border-bottom: 0.5px solid var(--border);
  flex-shrink: 0;
}
.app-logo {
  font-size: 13px; font-weight: 500;
  color: var(--text-primary); margin-bottom: 10px;
  display: flex; align-items: center; gap: 6px;
}
.app-logo i { color: var(--accent); }

/* 侧边栏导航 */
.sidebar-nav {
  padding: 6px 0;
  border-bottom: 0.5px solid var(--border);
  flex-shrink: 0;
}
.nav-item {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; font-size: 13px;
  color: var(--text-secondary); cursor: pointer;
  border-radius: 0; text-decoration: none;
  transition: background 0.1s;
}
.nav-item:hover { background: var(--surface-2); color: var(--text-primary); }
.nav-item.active { background: var(--surface-2); color: var(--text-primary); }
.nav-item i { font-size: 16px; width: 18px; flex-shrink: 0; }

/* 会话列表区 */
.session-list {
  flex: 1; overflow-y: auto; padding: 6px 0;
}
.session-section-label {
  font-size: 11px; color: var(--text-muted);
  padding: 8px 12px 3px; user-select: none;
}
.session-item {
  display: flex; align-items: center;
  padding: 7px 12px; font-size: 12px;
  color: var(--text-secondary); cursor: pointer;
  gap: 6px; border-radius: 0;
  transition: background 0.1s;
}
.session-item:hover { background: var(--surface-2); }
.session-item:hover .session-actions { opacity: 1; }
.session-item.active {
  background: var(--surface-2); color: var(--text-primary);
}
.session-title {
  flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.session-actions {
  opacity: 0; display: flex; gap: 2px; flex-shrink: 0;
}
.session-action-btn {
  width: 20px; height: 20px; border: none; background: transparent;
  color: var(--text-muted); border-radius: 4px; display: flex;
  align-items: center; justify-content: center; font-size: 13px;
}
.session-action-btn:hover { background: var(--surface-0); color: var(--text-primary); }

/* 侧边栏底部：用户信息 */
.sidebar-footer {
  padding: 10px 12px;
  border-top: 0.5px solid var(--border);
  flex-shrink: 0;
}
.user-info {
  display: flex; align-items: center; gap: 8px;
}
.user-avatar {
  width: 28px; height: 28px; border-radius: 50%;
  background: var(--accent-bg); color: var(--accent-text);
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 500; flex-shrink: 0;
}
.user-name { font-size: 12px; color: var(--text-secondary); }

/* ── 主内容区 ────────────────────────────────────────────────── */
.main-area {
  flex: 1; display: flex; flex-direction: column; overflow: hidden;
  background: var(--surface-2);
}
.main-header {
  padding: 12px 20px;
  border-bottom: 0.5px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
  flex-shrink: 0;
}
.main-title { font-size: 14px; font-weight: 500; }
.main-actions { display: flex; gap: 6px; align-items: center; }
.main-content { flex: 1; overflow: hidden; }
```
## 4. 页面路由

| URL | 模板 | 说明 |
|-----|------|------|
| `/` | 重定向到 `/chat` | |
| `/chat` | `chat.html` | 聊天主界面，无 session 时显示欢迎页 |
| `/chat?session={id}` | `chat.html` | 打开指定会话 |
| `/knowledge` | `knowledge.html` | 知识库管理 |
| `/config` | `config.html` | 系统配置（需 admin 角色） |

---

## 5. 布局模板（base.jinja2）

**文件**：`templates/base.html`

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}智能问答系统{% endblock %}</title>

  <!-- 图标字体 -->
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css">

  <!-- 代码高亮 CSS -->
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github.min.css"
    media="(prefers-color-scheme: light)">
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github-dark.min.css"
    media="(prefers-color-scheme: dark)">

  <!-- 全局样式 -->
  <link rel="stylesheet" href="/static/css/base.css">
  <link rel="stylesheet" href="/static/css/layout.css">
  {% block extra_css %}{% endblock %}
</head>
<body>

<div class="app-shell" id="app">

  <!-- ── 侧边栏 ────────────────────────────────────────────── -->
  <aside class="sidebar" x-data="sidebarComponent()">
    <div class="sidebar-header">
      <div class="app-logo">
        <i class="ti ti-sparkles" aria-hidden="true"></i>
        智能问答系统
      </div>

      <!-- 新建对话按钮（仅在聊天页显示） -->
      {% if active_page == 'chat' %}
      <button class="btn btn-sm" style="width:100%;justify-content:center"
              @click="newChat()">
        <i class="ti ti-plus" aria-hidden="true"></i>
        新建对话
      </button>
      {% endif %}
    </div>

    <!-- 导航 -->
    <nav class="sidebar-nav" aria-label="主导航">
      <a href="/chat"
         class="nav-item {% if active_page == 'chat' %}active{% endif %}">
        <i class="ti ti-message-2" aria-hidden="true"></i>
        对话
      </a>
      <a href="/knowledge"
         class="nav-item {% if active_page == 'knowledge' %}active{% endif %}">
        <i class="ti ti-database" aria-hidden="true"></i>
        知识库
      </a>
      {% if user.is_admin %}
      <a href="/config"
         class="nav-item {% if active_page == 'config' %}active{% endif %}">
        <i class="ti ti-settings" aria-hidden="true"></i>
        系统配置
      </a>
      {% endif %}
    </nav>

    <!-- 会话列表（仅聊天页渲染） -->
    {% if active_page == 'chat' %}
    <div class="session-list" role="list" aria-label="对话列表">
      <!-- 今天 -->
      <template x-if="todaySessions.length > 0">
        <div>
          <div class="session-section-label">今天</div>
          <template x-for="s in todaySessions" :key="s.id">
            <div class="session-item"
                 :class="{ active: s.id === currentSessionId }"
                 role="listitem"
                 @click="openSession(s.id)">
              <i class="ti ti-message" style="font-size:13px;flex-shrink:0"
                 aria-hidden="true"></i>
              <span class="session-title" x-text="s.title"></span>
              <div class="session-actions">
                <button class="session-action-btn"
                        :aria-label="'重命名对话 ' + s.title"
                        @click.stop="renameSession(s)">
                  <i class="ti ti-edit" aria-hidden="true"></i>
                </button>
                <button class="session-action-btn"
                        :aria-label="'删除对话 ' + s.title"
                        @click.stop="deleteSession(s.id)">
                  <i class="ti ti-trash" aria-hidden="true"></i>
                </button>
              </div>
            </div>
          </template>
        </div>
      </template>

      <!-- 更早 -->
      <template x-if="olderSessions.length > 0">
        <div>
          <div class="session-section-label">更早</div>
          <template x-for="s in olderSessions" :key="s.id">
            <div class="session-item"
                 :class="{ active: s.id === currentSessionId }"
                 role="listitem"
                 @click="openSession(s.id)">
              <i class="ti ti-message" style="font-size:13px;flex-shrink:0"
                 aria-hidden="true"></i>
              <span class="session-title" x-text="s.title"></span>
              <div class="session-actions">
                <button class="session-action-btn"
                        :aria-label="'删除对话 ' + s.title"
                        @click.stop="deleteSession(s.id)">
                  <i class="ti ti-trash" aria-hidden="true"></i>
                </button>
              </div>
            </div>
          </template>
        </div>
      </template>

      <!-- 空状态 -->
      <template x-if="sessions.length === 0">
        <div style="padding:20px 12px;text-align:center;color:var(--text-muted);font-size:12px">
          还没有对话记录
        </div>
      </template>
    </div>
    {% endif %}

    <!-- 底部用户信息 -->
    <div class="sidebar-footer">
      <div class="user-info">
        <div class="user-avatar" aria-hidden="true">
          {{ user.name[0] if user.name else 'U' }}
        </div>
        <span class="user-name">{{ user.name }}</span>
        <a href="/logout" class="btn-icon" style="margin-left:auto"
           aria-label="退出登录">
          <i class="ti ti-logout" aria-hidden="true"></i>
        </a>
      </div>
    </div>
  </aside>

  <!-- ── 主内容区 ──────────────────────────────────────────── -->
  <main class="main-area">
    {% block main %}{% endblock %}
  </main>

</div><!-- /app-shell -->

<!-- JS 依赖（body 末尾） -->
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/core.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/languages/bash.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/languages/python.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/languages/javascript.min.js"></script>

<!-- 工具函数 & API 层 -->
<script src="/static/js/utils.js"></script>
<script src="/static/js/api.js"></script>

<!-- 页面特定 JS -->
{% block extra_js %}{% endblock %}

<!-- Alpine.js 最后加载 -->
<script defer
  src="https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js"></script>

</body>
</html>
```

---

## 6. 聊天界面

### 6.1 模板

**文件**：`templates/chat.html`

```html
{% extends "base.html" %}
{% block title %}对话 — 智能问答系统{% endblock %}
{% block extra_css %}
<link rel="stylesheet" href="/static/css/chat.css">
{% endblock %}

{% block main %}
<div x-data="chatPage({{ session_id | tojson }}, {{ user | tojson }})"
     x-init="init()"
     style="display:flex;flex-direction:column;height:100%">

  <!-- ── 顶栏 ────────────────────────────────────────────── -->
  <header class="main-header">
    <h1 class="main-title" style="font-size:14px;font-weight:500">
      <span x-text="currentTitle || '新对话'"></span>
    </h1>
    <div class="main-actions">
      <!-- 引用来源抽屉开关 -->
      <button class="btn-icon"
              :aria-label="showSources ? '隐藏引用来源' : '显示引用来源'"
              @click="showSources = !showSources"
              :style="showSources ? 'color:var(--accent)' : ''">
        <i class="ti ti-files" aria-hidden="true"></i>
      </button>
      <!-- 清空当前对话 -->
      <button class="btn-icon" aria-label="清空对话" @click="clearChat()">
        <i class="ti ti-eraser" aria-hidden="true"></i>
      </button>
    </div>
  </header>

  <!-- ── 内容区（消息列表 + 来源抽屉） ──────────────────── -->
  <div style="flex:1;display:flex;overflow:hidden">

    <!-- 消息列表 -->
    <div class="msg-list" id="msgList" role="log" aria-live="polite"
         aria-label="对话内容">

      <!-- 欢迎屏（无消息时） -->
      <template x-if="messages.length === 0">
        <div class="welcome-screen">
          <i class="ti ti-sparkles welcome-icon" aria-hidden="true"></i>
          <h2 class="welcome-title">有什么可以帮助你？</h2>
          <p class="welcome-sub">基于知识库的智能问答，提问后可查看引用来源</p>
          <div class="welcome-suggestions">
            <template x-for="s in suggestions" :key="s">
              <button class="suggestion-chip" @click="fillInput(s)" x-text="s"></button>
            </template>
          </div>
        </div>
      </template>

      <!-- 消息列表 -->
      <template x-for="(msg, idx) in messages" :key="msg.id">
        <div class="msg-row" :class="'msg-' + msg.role">

          <!-- 头像 -->
          <div class="msg-avatar" :class="'avatar-' + msg.role" aria-hidden="true">
            <template x-if="msg.role === 'assistant'">
              <i class="ti ti-sparkles"></i>
            </template>
            <template x-if="msg.role === 'user'">
              <span x-text="userInitial"></span>
            </template>
          </div>

          <!-- 消息主体 -->
          <div class="msg-body">
            <div class="msg-bubble" :class="'bubble-' + msg.role">

              <!-- 用户消息：纯文本 -->
              <template x-if="msg.role === 'user'">
                <span x-text="msg.content"></span>
              </template>

              <!-- AI 消息：Markdown 渲染 + 打字动画 -->
              <template x-if="msg.role === 'assistant'">
                <div>
                  <!-- 打字中状态 -->
                  <template x-if="msg.typing">
                    <div style="display:flex;gap:4px;align-items:center;padding:2px 0">
                      <span class="typing-dot"></span>
                      <span class="typing-dot"></span>
                      <span class="typing-dot"></span>
                    </div>
                  </template>
                  <!-- 已完成：渲染 Markdown -->
                  <template x-if="!msg.typing">
                    <div class="md-content"
                         x-html="renderMarkdown(msg.content)"></div>
                  </template>
                </div>
              </template>
            </div>

            <!-- 引用来源（AI 消息） -->
            <template x-if="msg.role === 'assistant' && msg.sources && msg.sources.length > 0">
              <div class="msg-sources">
                <template x-for="src in msg.sources" :key="src.ref_id">
                  <button class="source-chip"
                          :aria-label="'查看来源：' + (src.title || src.doc_id)"
                          @click="viewSource(src)">
                    <i class="ti ti-file-text" aria-hidden="true"></i>
                    <span x-text="src.title || src.ref_id"></span>
                    <template x-if="src.page_num">
                      <span class="source-page" x-text="'p.' + src.page_num"></span>
                    </template>
                  </button>
                </template>
              </div>
            </template>

            <!-- 消息操作（hover 时显示） -->
            <template x-if="msg.role === 'assistant' && !msg.typing">
              <div class="msg-actions">
                <button class="msg-action-btn" aria-label="复制回答"
                        @click="copyMessage(msg.content)">
                  <i class="ti ti-copy" aria-hidden="true"></i>
                </button>
                <button class="msg-action-btn" aria-label="重新生成"
                        @click="regenerate(idx)">
                  <i class="ti ti-refresh" aria-hidden="true"></i>
                </button>
              </div>
            </template>

          </div>
        </div>
      </template>

      <!-- 错误提示 -->
      <template x-if="error">
        <div class="error-banner" role="alert">
          <i class="ti ti-alert-circle" aria-hidden="true"></i>
          <span x-text="error"></span>
          <button @click="error = null" aria-label="关闭">
            <i class="ti ti-x" aria-hidden="true"></i>
          </button>
        </div>
      </template>

    </div><!-- /msg-list -->

    <!-- 引用来源侧边抽屉 -->
    <aside class="sources-drawer" :class="{ open: showSources }"
           aria-label="引用来源">
      <div class="drawer-header">
        <span style="font-size:13px;font-weight:500">引用来源</span>
        <button class="btn-icon" @click="showSources = false" aria-label="关闭">
          <i class="ti ti-x" aria-hidden="true"></i>
        </button>
      </div>
      <div class="drawer-body">
        <template x-if="activeSourceMsg">
          <template x-for="src in activeSourceMsg.sources" :key="src.ref_id">
            <div class="source-card">
              <div class="source-card-header">
                <span class="source-ref-id" x-text="'[' + src.ref_id + ']'"></span>
                <span class="source-title" x-text="src.title || src.doc_id"></span>
              </div>
              <template x-if="src.section">
                <div class="source-section" x-text="src.section"></div>
              </template>
              <template x-if="src.page_num">
                <div class="source-meta">第 <span x-text="src.page_num"></span> 页</div>
              </template>
              <template x-if="src.storage_url">
                <a :href="src.storage_url" target="_blank" rel="noopener"
                   class="btn btn-sm" style="margin-top:8px">
                  <i class="ti ti-external-link" aria-hidden="true"></i>
                  查看原文
                </a>
              </template>
            </div>
          </template>
        </template>
        <template x-if="!activeSourceMsg">
          <p style="font-size:12px;color:var(--text-muted);padding:16px">
            点击回答中的来源标签查看详情
          </p>
        </template>
      </div>
    </aside>

  </div><!-- /内容区 -->

  <!-- ── 输入区 ─────────────────────────────────────────── -->
  <div class="input-area">

    <!-- 知识域选择器（用户有多个可访问 collection 时显示） -->
    <template x-if="collections.length > 1">
      <div class="collection-selector">
        <label for="collectionSelect" style="font-size:12px;color:var(--text-muted)">
          知识域
        </label>
        <select id="collectionSelect" class="field"
                style="width:auto;min-width:140px"
                x-model="selectedCollection">
          <template x-for="c in collections" :key="c.id">
            <option :value="c.id" x-text="c.name"></option>
          </template>
        </select>
      </div>
    </template>

    <div class="input-row">
      <div class="input-wrap" :class="{ focused: inputFocused }">
        <textarea id="chatInput"
                  class="chat-textarea"
                  placeholder="输入问题，Shift+Enter 换行，Enter 发送"
                  x-model="inputText"
                  @focus="inputFocused = true"
                  @blur="inputFocused = false"
                  @keydown.enter.prevent="handleEnter($event)"
                  @input="autoResize($event.target)"
                  :disabled="isLoading"
                  rows="1"
                  aria-label="输入问题"></textarea>
      </div>
      <button class="send-btn"
              :disabled="!inputText.trim() || isLoading"
              @click="sendMessage()"
              aria-label="发送">
        <template x-if="!isLoading">
          <i class="ti ti-arrow-up" aria-hidden="true"></i>
        </template>
        <template x-if="isLoading">
          <i class="ti ti-player-stop-filled" aria-hidden="true"></i>
        </template>
      </button>
    </div>

    <div class="input-footer">
      <span style="font-size:11px;color:var(--text-muted)">
        回答基于知识库内容，请以原始文档为准
      </span>
    </div>
  </div>

</div>
{% endblock %}

{% block extra_js %}
<script src="/static/js/chat.js"></script>
{% endblock %}
```

### 6.2 聊天界面样式

**文件**：`static/css/chat.css`

```css
/* ── 消息列表 ───────────────────────────────────────────── */
.msg-list {
  flex: 1; overflow-y: auto; padding: 24px 20px;
  display: flex; flex-direction: column; gap: 20px;
  scroll-behavior: smooth;
}

/* ── 欢迎屏 ─────────────────────────────────────────────── */
.welcome-screen {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 60px 20px; text-align: center;
  gap: 12px; flex: 1;
}
.welcome-icon { font-size: 40px; color: var(--accent); }
.welcome-title { font-size: 20px; font-weight: 500; }
.welcome-sub { font-size: 13px; color: var(--text-secondary); max-width: 360px; }
.welcome-suggestions {
  display: flex; flex-wrap: wrap; gap: 8px;
  justify-content: center; margin-top: 8px; max-width: 500px;
}
.suggestion-chip {
  padding: 7px 14px; border: 0.5px solid var(--border-strong);
  border-radius: 20px; background: var(--surface-1);
  font-size: 12px; color: var(--text-secondary); cursor: pointer;
  transition: all 0.1s;
}
.suggestion-chip:hover {
  background: var(--surface-0); color: var(--text-primary);
  border-color: var(--accent);
}

/* ── 消息行 ─────────────────────────────────────────────── */
.msg-row {
  display: flex; gap: 10px; max-width: 800px; width: 100%;
}
.msg-user {
  align-self: flex-end; flex-direction: row-reverse;
  margin-left: auto;
}
.msg-assistant { align-self: flex-start; }

/* 头像 */
.msg-avatar {
  width: 28px; height: 28px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 500; flex-shrink: 0; margin-top: 2px;
}
.avatar-assistant {
  background: var(--surface-1); border: 0.5px solid var(--border);
  color: var(--text-secondary); font-size: 14px;
}
.avatar-user {
  background: var(--accent-bg); color: var(--accent-text); font-size: 11px;
}

/* 消息主体 */
.msg-body { display: flex; flex-direction: column; gap: 6px; flex: 1; min-width: 0; }

/* 气泡 */
.msg-bubble {
  padding: 10px 14px; border-radius: 12px;
  font-size: 13px; line-height: 1.65; word-break: break-word;
}
.bubble-assistant {
  background: var(--surface-1); border: 0.5px solid var(--border);
  border-top-left-radius: 4px;
}
.bubble-user {
  background: var(--accent-bg); border: 0.5px solid var(--accent-border);
  color: var(--accent-text); border-top-right-radius: 4px;
  display: inline-block; /* 内容短时不撑满 */
}

/* 来源标签 */
.msg-sources {
  display: flex; flex-wrap: wrap; gap: 6px;
}
.source-chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 8px; border: 0.5px solid var(--border);
  border-radius: var(--radius); background: var(--surface-2);
  font-size: 11px; color: var(--text-secondary); cursor: pointer;
  transition: all 0.1s; white-space: nowrap;
}
.source-chip:hover {
  border-color: var(--accent); color: var(--accent-text);
  background: var(--accent-bg);
}
.source-chip i { font-size: 11px; }
.source-page { color: var(--text-muted); margin-left: 2px; }

/* 消息操作（hover 显示） */
.msg-actions {
  display: none; gap: 4px;
}
.msg-row:hover .msg-actions { display: flex; }
.msg-action-btn {
  width: 26px; height: 26px; border: none; background: transparent;
  color: var(--text-muted); border-radius: 6px;
  display: flex; align-items: center; justify-content: center; font-size: 14px;
}
.msg-action-btn:hover { background: var(--surface-1); color: var(--text-primary); }

/* ── 错误提示 ───────────────────────────────────────────── */
.error-banner {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 14px; background: var(--danger-bg);
  border: 0.5px solid var(--danger-border); border-radius: var(--radius);
  color: var(--danger-text); font-size: 13px;
}
.error-banner button {
  margin-left: auto; background: none; border: none;
  color: var(--danger-text); cursor: pointer;
}

/* ── 来源抽屉 ───────────────────────────────────────────── */
.sources-drawer {
  width: 0; overflow: hidden; transition: width 0.2s ease;
  border-left: 0.5px solid var(--border); background: var(--surface-1);
  display: flex; flex-direction: column; flex-shrink: 0;
}
.sources-drawer.open { width: 280px; }
.drawer-header {
  padding: 12px 14px; border-bottom: 0.5px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
  flex-shrink: 0;
}
.drawer-body { padding: 12px; overflow-y: auto; flex: 1; }
.source-card {
  padding: 10px 12px; background: var(--surface-2);
  border: 0.5px solid var(--border); border-radius: var(--radius);
  margin-bottom: 8px;
}
.source-card-header { display: flex; align-items: flex-start; gap: 6px; margin-bottom: 4px; }
.source-ref-id {
  font-size: 11px; font-weight: 500; color: var(--accent-text);
  background: var(--accent-bg); padding: 1px 5px; border-radius: 4px;
  flex-shrink: 0;
}
.source-title { font-size: 12px; font-weight: 500; line-height: 1.4; }
.source-section { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
.source-meta { font-size: 11px; color: var(--text-muted); margin-top: 4px; }

/* ── 输入区 ─────────────────────────────────────────────── */
.input-area {
  padding: 12px 20px 14px; border-top: 0.5px solid var(--border);
  background: var(--surface-2); flex-shrink: 0;
}
.collection-selector {
  display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
}
.input-row { display: flex; gap: 10px; align-items: flex-end; }
.input-wrap {
  flex: 1; border: 0.5px solid var(--border-strong);
  border-radius: var(--radius); padding: 8px 12px;
  background: var(--surface-2); transition: border-color 0.15s;
}
.input-wrap.focused { border-color: var(--accent); }
.chat-textarea {
  width: 100%; border: none; background: transparent;
  font-size: 13px; color: var(--text-primary); outline: none;
  resize: none; line-height: 1.5; max-height: 160px; overflow-y: auto;
}
.chat-textarea::placeholder { color: var(--text-muted); }
.send-btn {
  width: 36px; height: 36px; border-radius: var(--radius);
  background: var(--accent-fill); border: none;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 16px; flex-shrink: 0;
  transition: opacity 0.1s;
}
.send-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.send-btn:not(:disabled):hover { opacity: 0.88; }
.input-footer { margin-top: 6px; text-align: center; }
```

### 6.3 聊天页面 Alpine 组件

**文件**：`static/js/chat.js`

```javascript
/**
 * chatPage — 聊天页面主 Alpine 组件
 * @param {string|null} initialSessionId  — Jinja2 注入的初始 session ID
 * @param {object}      user              — 当前用户信息 {name, roles, ...}
 */
function chatPage(initialSessionId, user) {
  return {
    /* ── 状态 ─────────────────────────────────────────── */
    messages:          [],         // { id, role, content, typing, sources }
    sessions:          [],         // 所有会话列表（侧边栏）
    currentSessionId:  null,
    currentTitle:      '',
    inputText:         '',
    isLoading:         false,
    inputFocused:      false,
    showSources:       false,
    activeSourceMsg:   null,       // 当前查看来源的消息
    error:             null,
    collections:       [],         // 可访问的知识域
    selectedCollection: 'default',
    userInitial:       user.name ? user.name[0] : 'U',
    abortController:   null,       // SSE 中止控制器

    suggestions: [
      '如何申请差旅报销？',
      '产品 X300 支持哪些路由协议？',
      '新员工入职需要准备哪些材料？',
    ],

    /* ── 计算属性 ─────────────────────────────────────── */
    get todaySessions() {
      const today = new Date().toDateString();
      return this.sessions.filter(s =>
        new Date(s.last_active).toDateString() === today
      );
    },
    get olderSessions() {
      const today = new Date().toDateString();
      return this.sessions.filter(s =>
        new Date(s.last_active).toDateString() !== today
      );
    },

    /* ── 初始化 ───────────────────────────────────────── */
    async init() {
      await this.loadSessions();
      await this.loadCollections();
      if (initialSessionId) {
        await this.openSession(initialSessionId);
      }
    },

    /* ── 会话管理 ─────────────────────────────────────── */
    async loadSessions() {
      try {
        const data = await api.getSessions();
        this.sessions = data.sessions || [];
      } catch (e) {
        console.error('加载会话列表失败', e);
      }
    },

    async openSession(sessionId) {
      this.currentSessionId = sessionId;
      this.messages         = [];
      this.error            = null;
      history.replaceState(null, '', `/chat?session=${sessionId}`);

      try {
        const data = await api.getSessionHistory(sessionId);
        this.currentTitle = data.title || '对话';
        this.messages = (data.turns || []).map((t, i) => ({
          id:      i,
          role:    t.role,
          content: t.content,
          typing:  false,
          sources: t.sources || [],
        }));
        this.$nextTick(() => this.scrollToBottom());
      } catch (e) {
        this.error = '加载对话历史失败，请重试';
      }
    },

    newChat() {
      this.currentSessionId = null;
      this.currentTitle     = '';
      this.messages         = [];
      this.error            = null;
      history.replaceState(null, '', '/chat');
    },

    async deleteSession(sessionId) {
      if (!confirm('确定删除这条对话记录？')) return;
      try {
        await api.deleteSession(sessionId);
        this.sessions = this.sessions.filter(s => s.id !== sessionId);
        if (this.currentSessionId === sessionId) this.newChat();
      } catch (e) {
        this.error = '删除失败，请重试';
      }
    },

    async renameSession(session) {
      const newTitle = prompt('重命名对话', session.title);
      if (!newTitle || newTitle === session.title) return;
      try {
        await api.renameSession(session.id, newTitle);
        session.title = newTitle;
        if (this.currentSessionId === session.id) this.currentTitle = newTitle;
      } catch (e) {
        this.error = '重命名失败';
      }
    },

    async loadCollections() {
      try {
        const data = await api.getCollections();
        this.collections = data.collections || [];
        if (this.collections.length > 0) {
          this.selectedCollection = this.collections[0].id;
        }
      } catch (e) {
        console.warn('加载知识域失败，使用默认');
      }
    },

    /* ── 发送消息 ─────────────────────────────────────── */
    handleEnter(event) {
      if (event.shiftKey) return;   // Shift+Enter = 换行
      this.sendMessage();
    },

    async sendMessage() {
      const text = this.inputText.trim();
      if (!text || this.isLoading) return;

      this.inputText = '';
      this.error     = null;
      this.isLoading = true;
      this.$nextTick(() => this.autoResize(
        document.getElementById('chatInput')
      ));

      // 添加用户消息
      const userMsg = { id: Date.now(), role: 'user', content: text,
                        typing: false, sources: [] };
      this.messages.push(userMsg);

      // 添加 AI 占位消息（打字动画）
      const aiMsg = { id: Date.now() + 1, role: 'assistant', content: '',
                      typing: true, sources: [] };
      this.messages.push(aiMsg);
      this.$nextTick(() => this.scrollToBottom());

      try {
        this.abortController = new AbortController();
        await this.streamQuery(text, aiMsg);
      } catch (e) {
        if (e.name !== 'AbortError') {
          aiMsg.typing  = false;
          aiMsg.content = '';
          this.error    = e.message || '请求失败，请检查网络或稍后重试';
        }
      } finally {
        this.isLoading        = false;
        this.abortController  = null;
      }
    },

    /**
     * SSE 流式请求
     * 后端接口：POST /api/v1/query（stream=true）
     * SSE 事件格式：
     *   data: {"type":"delta","content":"文字片段"}
     *   data: {"type":"sources","sources":[{ref_id,title,doc_id,...}]}
     *   data: {"type":"done","session_id":"xxx","title":"对话标题"}
     *   data: {"type":"error","message":"错误信息"}
     */
    async streamQuery(text, aiMsg) {
      const resp = await fetch('/api/v1/query', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          query:      text,
          session_id: this.currentSessionId,
          stream:     true,
          metadata:   { collection: this.selectedCollection },
        }),
        signal:  this.abortController.signal,
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error || `服务器错误 ${resp.status}`);
      }

      const reader  = resp.body.getReader();
      const decoder = new TextDecoder();
      let   buffer  = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();   // 最后一行可能不完整，留到下次

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6).trim();
          if (!raw || raw === '[DONE]') continue;

          let evt;
          try { evt = JSON.parse(raw); }
          catch { continue; }

          if (evt.type === 'delta') {
            aiMsg.typing   = false;
            aiMsg.content += evt.content;
            this.$nextTick(() => this.scrollToBottom());

          } else if (evt.type === 'sources') {
            aiMsg.sources = evt.sources;

          } else if (evt.type === 'done') {
            aiMsg.typing = false;
            // 更新/创建 session
            if (evt.session_id) {
              this.currentSessionId = evt.session_id;
              history.replaceState(null, '', `/chat?session=${evt.session_id}`);
            }
            if (evt.title) {
              this.currentTitle = evt.title;
              // 刷新侧边栏会话列表
              await this.loadSessions();
            }

          } else if (evt.type === 'error') {
            throw new Error(evt.message);
          }
        }
      }
    },

    /* ── 工具方法 ─────────────────────────────────────── */
    stopGeneration() {
      if (this.abortController) this.abortController.abort();
    },

    async regenerate(msgIndex) {
      // 找到该 AI 消息之前的最后一条用户消息
      let userMsg = null;
      for (let i = msgIndex - 1; i >= 0; i--) {
        if (this.messages[i].role === 'user') {
          userMsg = this.messages[i];
          break;
        }
      }
      if (!userMsg) return;
      // 移除该 AI 消息之后的所有消息
      this.messages.splice(msgIndex);
      this.inputText = userMsg.content;
      await this.sendMessage();
    },

    copyMessage(content) {
      navigator.clipboard.writeText(content).catch(() => {
        // 降级方案
        const el = document.createElement('textarea');
        el.value = content;
        document.body.appendChild(el);
        el.select();
        document.execCommand('copy');
        document.body.removeChild(el);
      });
    },

    viewSource(src) {
      // 找到包含此来源的消息
      this.activeSourceMsg = this.messages.find(m =>
        m.sources && m.sources.some(s => s.ref_id === src.ref_id)
      );
      this.showSources = true;
    },

    fillInput(text) {
      this.inputText = text;
      this.$nextTick(() => document.getElementById('chatInput').focus());
    },

    clearChat() {
      if (!confirm('清空当前对话内容？')) return;
      this.messages = [];
    },

    scrollToBottom() {
      const el = document.getElementById('msgList');
      if (el) el.scrollTop = el.scrollHeight;
    },

    autoResize(el) {
      if (!el) return;
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 160) + 'px';
    },

    renderMarkdown(text) {
      if (!text) return '';
      const html = marked.parse(text, { breaks: true, gfm: true });
      // 代码块高亮（异步，不阻塞渲染）
      this.$nextTick(() => {
        document.querySelectorAll('.md-content pre code').forEach(el => {
          if (!el.dataset.highlighted) {
            hljs.highlightElement(el);
            el.dataset.highlighted = '1';
          }
        });
      });
      return html;
    },
  };
}

/* ── 侧边栏组件（base.html 中使用） ───────────────────── */
function sidebarComponent() {
  return {
    sessions:         [],
    currentSessionId: null,
    init() {
      // 从 chatPage 组件同步状态（通过自定义事件）
      window.addEventListener('session-changed', e => {
        this.currentSessionId = e.detail.sessionId;
      });
    },
  };
}
```
## 7. 知识库管理界面

### 7.1 功能范围

知识库管理界面提供以下操作（需 admin 或 knowledge_manager 角色）：

- 查看所有 collection 的文档列表（文档名、大小、状态、入库时间、Chunk 数）
- 上传新文档（拖拽或点击选择文件，支持 PDF/DOCX/TXT/图片）
- 删除文档（同步删除向量库、ES、图谱中的所有 Chunk）
- 查看入库任务进度（轮询 task_id 状态）
- 切换 collection（按知识域分类查看）
- 重新索引文档（触发对已有文档重新入库）

### 7.2 模板

**文件**：`templates/knowledge.html`

```html
{% extends "base.html" %}
{% block title %}知识库 — 智能问答系统{% endblock %}
{% block extra_css %}
<link rel="stylesheet" href="/static/css/knowledge.css">
{% endblock %}

{% block main %}
<div x-data="knowledgePage()" x-init="init()"
     style="display:flex;flex-direction:column;height:100%">

  <!-- 顶栏 -->
  <header class="main-header">
    <h1 class="main-title">知识库管理</h1>
    <div class="main-actions">
      <!-- collection 切换 -->
      <select class="field" style="width:auto;min-width:140px"
              x-model="selectedCollection"
              @change="loadDocuments()">
        <template x-for="c in collections" :key="c.id">
          <option :value="c.id" x-text="c.name"></option>
        </template>
      </select>
      <!-- 上传按钮 -->
      <button class="btn btn-primary btn-sm" @click="openUpload()">
        <i class="ti ti-upload" aria-hidden="true"></i>
        上传文档
      </button>
    </div>
  </header>

  <!-- 统计卡 -->
  <div class="stats-bar">
    <div class="stat-card">
      <div class="stat-label">文档总数</div>
      <div class="stat-value" x-text="stats.doc_count ?? '—'"></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Chunk 总数</div>
      <div class="stat-value" x-text="stats.chunk_count ?? '—'"></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">处理中</div>
      <div class="stat-value" x-text="pendingTasks.length"></div>
    </div>
  </div>

  <!-- 上传拖拽区（显示条件：showUpload） -->
  <template x-if="showUpload">
    <div class="upload-zone"
         @dragover.prevent="dragOver = true"
         @dragleave="dragOver = false"
         @drop.prevent="handleDrop($event)"
         :class="{ 'drag-over': dragOver }">
      <input type="file" id="fileInput" multiple
             accept=".pdf,.docx,.doc,.txt,.png,.jpg,.jpeg"
             style="display:none"
             @change="handleFileSelect($event)">
      <i class="ti ti-cloud-upload upload-icon" aria-hidden="true"></i>
      <p style="font-size:14px;font-weight:500">拖拽文件到此处，或</p>
      <button class="btn btn-sm" @click="$el.closest('.upload-zone').querySelector('#fileInput').click()">
        选择文件
      </button>
      <p style="font-size:11px;color:var(--text-muted);margin-top:6px">
        支持 PDF、Word、TXT、图片，单文件最大 100MB
      </p>
    </div>
  </template>

  <!-- 进行中的任务 -->
  <template x-if="pendingTasks.length > 0">
    <div class="tasks-bar">
      <template x-for="task in pendingTasks" :key="task.id">
        <div class="task-item">
          <i class="ti ti-loader-2 spin" aria-hidden="true"></i>
          <span x-text="task.filename" style="font-size:12px"></span>
          <span class="badge badge-neutral" x-text="task.status"></span>
        </div>
      </template>
    </div>
  </template>

  <!-- 文档列表 -->
  <div class="doc-list-container">
    <!-- 加载态 -->
    <template x-if="isLoading">
      <div style="padding:40px;text-align:center;color:var(--text-muted)">
        <i class="ti ti-loader-2 spin" style="font-size:24px" aria-hidden="true"></i>
      </div>
    </template>

    <!-- 空态 -->
    <template x-if="!isLoading && documents.length === 0">
      <div class="empty-state">
        <i class="ti ti-file-off" aria-hidden="true"></i>
        <p>该知识域还没有文档</p>
        <button class="btn btn-sm" @click="openUpload()">上传第一个文档</button>
      </div>
    </template>

    <!-- 文档表格 -->
    <template x-if="!isLoading && documents.length > 0">
      <table class="doc-table" role="table" aria-label="文档列表">
        <thead>
          <tr>
            <th style="width:36px">
              <input type="checkbox" aria-label="全选"
                     @change="toggleSelectAll($event.target.checked)">
            </th>
            <th>文件名</th>
            <th>状态</th>
            <th>Chunk 数</th>
            <th>大小</th>
            <th>入库时间</th>
            <th style="width:80px">操作</th>
          </tr>
        </thead>
        <tbody>
          <template x-for="doc in documents" :key="doc.doc_id">
            <tr>
              <td>
                <input type="checkbox" :aria-label="'选择 ' + doc.filename"
                       :value="doc.doc_id" x-model="selectedDocs">
              </td>
              <td>
                <div style="display:flex;align-items:center;gap:6px">
                  <i :class="fileIcon(doc.file_type)" style="font-size:16px;color:var(--text-muted)"
                     aria-hidden="true"></i>
                  <span style="font-size:13px" x-text="doc.filename"></span>
                </div>
              </td>
              <td>
                <span class="badge"
                      :class="statusBadgeClass(doc.status)"
                      x-text="statusLabel(doc.status)"></span>
              </td>
              <td style="font-size:13px;color:var(--text-secondary)"
                  x-text="doc.chunk_count ?? '—'"></td>
              <td style="font-size:13px;color:var(--text-secondary)"
                  x-text="formatSize(doc.file_size)"></td>
              <td style="font-size:12px;color:var(--text-muted)"
                  x-text="formatDate(doc.created_at)"></td>
              <td>
                <div style="display:flex;gap:4px">
                  <button class="btn-icon" :aria-label="'重新索引 ' + doc.filename"
                          @click="reindexDoc(doc)">
                    <i class="ti ti-refresh" aria-hidden="true"></i>
                  </button>
                  <button class="btn-icon" :aria-label="'删除 ' + doc.filename"
                          style="color:var(--danger-text)"
                          @click="deleteDoc(doc)">
                    <i class="ti ti-trash" aria-hidden="true"></i>
                  </button>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </template>

    <!-- 批量操作栏 -->
    <template x-if="selectedDocs.length > 0">
      <div class="bulk-bar">
        <span style="font-size:13px">已选 <b x-text="selectedDocs.length"></b> 个文档</span>
        <button class="btn btn-sm" style="color:var(--danger-text)" @click="bulkDelete()">
          <i class="ti ti-trash" aria-hidden="true"></i>
          批量删除
        </button>
      </div>
    </template>
  </div>

  <!-- 错误提示 -->
  <template x-if="error">
    <div class="error-banner" role="alert" style="margin:12px 20px">
      <i class="ti ti-alert-circle" aria-hidden="true"></i>
      <span x-text="error"></span>
      <button @click="error = null"><i class="ti ti-x" aria-hidden="true"></i></button>
    </div>
  </template>

</div>
{% endblock %}

{% block extra_js %}
<script src="/static/js/knowledge.js"></script>
{% endblock %}
```

### 7.3 知识库页 Alpine 组件

**文件**：`static/js/knowledge.js`

```javascript
function knowledgePage() {
  return {
    documents:           [],
    collections:         [],
    selectedCollection:  'default',
    selectedDocs:        [],
    pendingTasks:        [],
    stats:               {},
    isLoading:           false,
    showUpload:          false,
    dragOver:            false,
    error:               null,
    _pollTimer:          null,

    async init() {
      await this.loadCollections();
      await this.loadDocuments();
      await this.loadStats();
    },

    async loadCollections() {
      const data = await api.getCollections();
      this.collections = data.collections || [];
    },

    async loadDocuments() {
      this.isLoading = true;
      try {
        const data = await api.listDocuments(this.selectedCollection);
        this.documents = data.documents || [];
      } catch (e) {
        this.error = '加载文档列表失败';
      } finally {
        this.isLoading = false;
      }
    },

    async loadStats() {
      const data = await api.getKnowledgeStats(this.selectedCollection);
      this.stats = data;
    },

    openUpload() { this.showUpload = !this.showUpload; },

    handleDrop(event) {
      this.dragOver = false;
      const files = Array.from(event.dataTransfer.files);
      this.uploadFiles(files);
    },

    handleFileSelect(event) {
      const files = Array.from(event.target.files);
      this.uploadFiles(files);
      event.target.value = '';
    },

    async uploadFiles(files) {
      const allowed = ['pdf','docx','doc','txt','png','jpg','jpeg'];
      const valid   = files.filter(f => {
        const ext = f.name.split('.').pop().toLowerCase();
        return allowed.includes(ext) && f.size <= 100 * 1024 * 1024;
      });
      if (valid.length < files.length) {
        this.error = `${files.length - valid.length} 个文件不支持或超过 100MB 限制`;
      }
      for (const file of valid) {
        const task = { id: Date.now() + Math.random(), filename: file.name,
                       status: 'uploading' };
        this.pendingTasks.push(task);
        try {
          const url  = await api.uploadFile(file);
          const resp = await api.ingestDocument({
            file_url:   url,
            collection: this.selectedCollection,
          });
          task.id     = resp.task_id;
          task.status = 'processing';
          this._pollTask(resp.task_id, task);
        } catch (e) {
          task.status = 'failed';
          this.error  = `${file.name} 上传失败`;
        }
      }
    },

    _pollTask(taskId, task) {
      const poll = async () => {
        try {
          const data = await api.getTaskStatus(taskId);
          task.status = data.status;
          if (data.status === 'done') {
            this.pendingTasks = this.pendingTasks.filter(t => t.id !== taskId);
            await this.loadDocuments();
            await this.loadStats();
          } else if (data.status === 'failed') {
            this.pendingTasks = this.pendingTasks.filter(t => t.id !== taskId);
            this.error = `${task.filename} 入库失败`;
          } else {
            setTimeout(poll, 3000);   // 3 秒轮询
          }
        } catch (e) {
          setTimeout(poll, 5000);
        }
      };
      setTimeout(poll, 2000);
    },

    async deleteDoc(doc) {
      if (!confirm(`确定删除文档「${doc.filename}」？此操作将同时清除知识库中的相关内容。`)) return;
      try {
        await api.deleteDocument(doc.doc_id);
        this.documents = this.documents.filter(d => d.doc_id !== doc.doc_id);
        await this.loadStats();
      } catch (e) {
        this.error = '删除失败，请重试';
      }
    },

    async reindexDoc(doc) {
      if (!confirm(`重新索引「${doc.filename}」？将覆盖现有 Chunk。`)) return;
      const task = { id: Date.now(), filename: doc.filename, status: 'processing' };
      this.pendingTasks.push(task);
      try {
        const resp = await api.ingestDocument({
          file_url:   doc.storage_url,
          doc_id:     doc.doc_id,
          collection: this.selectedCollection,
        });
        task.id = resp.task_id;
        this._pollTask(resp.task_id, task);
      } catch (e) {
        this.error = '重新索引失败';
      }
    },

    async bulkDelete() {
      if (!confirm(`确定删除选中的 ${this.selectedDocs.length} 个文档？`)) return;
      for (const docId of this.selectedDocs) {
        await api.deleteDocument(docId).catch(() => {});
      }
      this.selectedDocs = [];
      await this.loadDocuments();
    },

    toggleSelectAll(checked) {
      this.selectedDocs = checked ? this.documents.map(d => d.doc_id) : [];
    },

    /* 格式化工具 */
    fileIcon(type) {
      const m = { pdf:'ti-file-type-pdf', docx:'ti-file-type-doc',
                  doc:'ti-file-type-doc', txt:'ti-file-type-txt',
                  png:'ti-file-type-png', jpg:'ti-photo', jpeg:'ti-photo' };
      return 'ti ' + (m[type] || 'ti-file');
    },
    statusLabel(s) {
      return { done:'已入库', processing:'处理中', pending:'等待中',
               failed:'失败' }[s] || s;
    },
    statusBadgeClass(s) {
      return { done:'badge-success', failed:'badge-danger',
               processing:'badge-neutral', pending:'badge-neutral' }[s] || 'badge-neutral';
    },
    formatSize(bytes) {
      if (!bytes) return '—';
      if (bytes < 1024)       return bytes + ' B';
      if (bytes < 1024*1024)  return (bytes/1024).toFixed(1) + ' KB';
      return (bytes/1024/1024).toFixed(1) + ' MB';
    },
    formatDate(iso) {
      if (!iso) return '—';
      return new Date(iso).toLocaleString('zh-CN', {
        month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'
      });
    },
  };
}
```

---

## 8. 系统配置界面

### 8.1 功能范围（仅 admin 角色可访问）

配置界面按 Tab 分为四组：

| Tab | 内容 |
|-----|------|
| 资源连接 | LLM、Embedding、向量库、ES、图谱、对象存储、Redis、业务数据库的连接参数和连通测试 |
| 功能开关 | 各召回路启用/禁用、Pipeline 步骤开关、参数调整（top_k、重排阈值等） |
| 知识域权限 | 角色到 collection 的映射编辑，新增/删除权限条目 |
| 提示词 | System prompt 编辑，意图分类 few-shot 示例编辑 |

### 8.2 模板

**文件**：`templates/config.html`

```html
{% extends "base.html" %}
{% block title %}系统配置 — 智能问答系统{% endblock %}
{% block extra_css %}
<link rel="stylesheet" href="/static/css/config.css">
{% endblock %}

{% block main %}
<div x-data="configPage({{ config | tojson }})" x-init="init()"
     style="display:flex;flex-direction:column;height:100%">

  <!-- 顶栏 -->
  <header class="main-header">
    <h1 class="main-title">系统配置</h1>
    <div class="main-actions">
      <span x-show="isDirty" style="font-size:12px;color:var(--warning-text)">
        <i class="ti ti-point-filled" aria-hidden="true"></i> 有未保存的更改
      </span>
      <button class="btn btn-sm" @click="resetForm()" :disabled="!isDirty">
        撤销更改
      </button>
      <button class="btn btn-primary btn-sm" @click="saveConfig()" :disabled="isSaving">
        <template x-if="isSaving">
          <i class="ti ti-loader-2 spin" aria-hidden="true"></i>
        </template>
        <template x-if="!isSaving">
          <i class="ti ti-check" aria-hidden="true"></i>
        </template>
        保存配置
      </button>
    </div>
  </header>

  <!-- Tab 导航 -->
  <div class="cfg-tabs" role="tablist" aria-label="配置分类">
    <template x-for="tab in tabs" :key="tab.id">
      <button class="cfg-tab"
              :class="{ active: activeTab === tab.id }"
              role="tab"
              :aria-selected="activeTab === tab.id"
              @click="activeTab = tab.id"
              x-text="tab.label">
      </button>
    </template>
  </div>

  <!-- Tab 内容 -->
  <div class="cfg-body" role="tabpanel">

    <!-- ── Tab: 资源连接 ───────────────────────────────── -->
    <div x-show="activeTab === 'resources'">

      <!-- LLM -->
      <div class="cfg-section">
        <div class="cfg-section-title">
          <i class="ti ti-brain" aria-hidden="true"></i>
          语言模型（LLM）
        </div>
        <div class="cfg-form">
          <div class="form-row">
            <label class="form-label">适配器类型</label>
            <select class="field" x-model="form.llm.adapter" @change="setDirty()">
              <option value="openai_compatible">OpenAI 兼容接口</option>
              <option value="vllm">vLLM</option>
            </select>
          </div>
          <div class="form-row">
            <label class="form-label">服务地址</label>
            <input class="field" type="url" x-model="form.llm.base_url"
                   @input="setDirty()" placeholder="https://llm.example.com/v1">
          </div>
          <div class="form-row">
            <label class="form-label">默认模型</label>
            <input class="field" x-model="form.llm.model" @input="setDirty()"
                   placeholder="qwen2.5-72b-instruct">
          </div>
          <div class="form-row">
            <label class="form-label">API Key</label>
            <input class="field" type="password" x-model="form.llm.api_key"
                   @input="setDirty()" placeholder="留空则使用环境变量">
          </div>
          <div class="form-row">
            <label class="form-label">超时（秒）</label>
            <input class="field" type="number" min="5" max="120"
                   x-model.number="form.llm.timeout_seconds" @input="setDirty()"
                   style="width:100px">
          </div>
        </div>
        <div class="test-row">
          <span class="badge"
                :class="testStatus.llm === 'ok' ? 'badge-success' :
                        testStatus.llm === 'err' ? 'badge-danger' : 'badge-neutral'"
                x-text="testLabel(testStatus.llm)">
          </span>
          <button class="btn btn-sm" @click="testConnection('llm')"
                  :disabled="testLoading.llm">
            <template x-if="testLoading.llm">
              <i class="ti ti-loader-2 spin" aria-hidden="true"></i>
            </template>
            测试连接
          </button>
        </div>
      </div>

      <!-- 各存储资源（向量库、ES、图谱、Redis、对象存储、业务库）
           结构与 LLM 相同，按以下规格生成每个 section：

           向量库（vector_store）：
             字段: adapter(select: milvus/qdrant/pgvector)、host、port(number)、dim(number)

           全文检索（full_text_search）：
             字段: adapter(select: elasticsearch/opensearch)、
                   hosts(textarea,每行一个)、api_key

           知识图谱（knowledge_graph）：
             前置开关: enabled(toggle)
             字段（enabled 时显示）:
               adapter(select: neo4j/nebula)
               neo4j 时: uri
               nebula 时: host、port
               username、password

           对象存储（object_storage）：
             字段: adapter(select: minio/local_fs)、
                   endpoint、access_key、secret_key、bucket

           Redis 缓存（cache）：
             字段: host、port(number)、password、
                   session_ttl_minutes(number, min=5 max=1440)

           业务数据库（business_data）：
             前置开关: enabled(toggle)
             字段（enabled 时显示）:
               adapter(select: sqlalchemy)、dsn、
               allowed_tables(tags input，逗号分隔)、
               max_rows_returned(number, min=1 max=1000)

           每个 section 末尾有 测试连接 行，调用 testConnection(key)。
      -->

      <!-- 向量库示例（其余资源 section 结构相同） -->
      <div class="cfg-section">
        <div class="cfg-section-title">
          <i class="ti ti-vector" aria-hidden="true"></i>
          向量数据库
        </div>
        <div class="cfg-form">
          <div class="form-row">
            <label class="form-label">适配器类型</label>
            <select class="field" x-model="form.vector_store.adapter" @change="setDirty()">
              <option value="milvus">Milvus</option>
              <option value="qdrant">Qdrant</option>
              <option value="pgvector">pgvector</option>
            </select>
          </div>
          <div class="form-row">
            <label class="form-label">主机地址</label>
            <input class="field" x-model="form.vector_store.host" @input="setDirty()">
          </div>
          <div class="form-row">
            <label class="form-label">端口</label>
            <input class="field" type="number" x-model.number="form.vector_store.port"
                   @input="setDirty()" style="width:100px">
          </div>
          <div class="form-row">
            <label class="form-label">向量维度</label>
            <input class="field" type="number" x-model.number="form.vector_store.dim"
                   @input="setDirty()" style="width:100px"
                   placeholder="1024">
          </div>
        </div>
        <div class="test-row">
          <span class="badge"
                :class="testStatus.vector_store === 'ok' ? 'badge-success' :
                        testStatus.vector_store === 'err' ? 'badge-danger' : 'badge-neutral'"
                x-text="testLabel(testStatus.vector_store)"></span>
          <button class="btn btn-sm" @click="testConnection('vector_store')"
                  :disabled="testLoading.vector_store">
            测试连接
          </button>
        </div>
      </div>

    </div>

    <!-- ── Tab: 功能开关 ───────────────────────────────── -->
    <div x-show="activeTab === 'features'">
      <div class="cfg-section">
        <div class="cfg-section-title">召回路配置</div>
        <div class="toggle-list">
          <div class="toggle-item">
            <div class="toggle-info">
              <span class="toggle-label">向量语义召回</span>
              <span class="toggle-desc">基于 Embedding 的语义相似度检索</span>
            </div>
            <label class="toggle-switch" :aria-label="'向量语义召回 ' + (form.features.vector_enabled ? '已启用' : '已禁用')">
              <input type="checkbox" x-model="form.features.vector_enabled" @change="setDirty()">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div class="toggle-item">
            <div class="toggle-info">
              <span class="toggle-label">BM25 关键词召回</span>
              <span class="toggle-desc">全文检索，擅长精确术语匹配</span>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" x-model="form.features.bm25_enabled" @change="setDirty()">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div class="toggle-item">
            <div class="toggle-info">
              <span class="toggle-label">知识图谱召回</span>
              <span class="toggle-desc">实体关系推理，需已连接图数据库</span>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" x-model="form.features.graph_enabled" @change="setDirty()"
                     :disabled="!form.knowledge_graph.enabled">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div class="toggle-item">
            <div class="toggle-info">
              <span class="toggle-label">结构化数据查询</span>
              <span class="toggle-desc">NL2SQL，需已连接业务数据库</span>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" x-model="form.features.structured_enabled"
                     @change="setDirty()" :disabled="!form.business_data.enabled">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
      </div>

      <div class="cfg-section">
        <div class="cfg-section-title">Pipeline 步骤</div>
        <div class="toggle-list">
          <div class="toggle-item">
            <div class="toggle-info">
              <span class="toggle-label">图片内容理解</span>
              <span class="toggle-desc">入库时调用 LLM 生成图片文字描述（较慢）</span>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" x-model="form.pipeline.image_caption_enabled" @change="setDirty()">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div class="toggle-item">
            <div class="toggle-info">
              <span class="toggle-label">Cross-Encoder 重排</span>
              <span class="toggle-desc">精排召回结果，提升准确率，增加约 500ms 延迟</span>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" x-model="form.pipeline.rerank_enabled" @change="setDirty()">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div class="toggle-item">
            <div class="toggle-info">
              <span class="toggle-label">忠实度校验</span>
              <span class="toggle-desc">生成后校验答案是否有据可查，增加约 800ms 延迟</span>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" x-model="form.pipeline.faithfulness_check" @change="setDirty()">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
      </div>

      <div class="cfg-section">
        <div class="cfg-section-title">召回参数</div>
        <div class="cfg-form">
          <div class="form-row">
            <label class="form-label">向量召回数 (top_k)</label>
            <input class="field" type="number" min="5" max="50"
                   x-model.number="form.pipeline.vector_top_k" @input="setDirty()"
                   style="width:100px">
          </div>
          <div class="form-row">
            <label class="form-label">重排后保留数</label>
            <input class="field" type="number" min="3" max="20"
                   x-model.number="form.pipeline.rerank_top_k" @input="setDirty()"
                   style="width:100px">
          </div>
          <div class="form-row">
            <label class="form-label">最低相关性分数</label>
            <input class="field" type="number" min="0" max="1" step="0.05"
                   x-model.number="form.pipeline.min_relevance_score" @input="setDirty()"
                   style="width:100px">
          </div>
        </div>
      </div>
    </div>

    <!-- ── Tab: 知识域权限 ─────────────────────────────── -->
    <div x-show="activeTab === 'permissions'">
      <div class="cfg-section">
        <div class="cfg-section-title" style="display:flex;align-items:center;justify-content:space-between">
          角色权限映射
          <button class="btn btn-sm" @click="addPermissionRow()">
            <i class="ti ti-plus" aria-hidden="true"></i> 添加角色
          </button>
        </div>
        <p style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
          配置各角色可访问的知识域（collection），* 表示访问全部
        </p>
        <div class="perm-table">
          <template x-for="(row, idx) in form.permissions" :key="idx">
            <div class="perm-row">
              <input class="field" placeholder="角色名，如 role_rd"
                     x-model="row.role" @input="setDirty()" style="width:180px">
              <input class="field" placeholder="knowledge_docs, api_docs（逗号分隔，* 表示全部）"
                     x-model="row.collections" @input="setDirty()" style="flex:1">
              <button class="btn-icon" :aria-label="'删除角色 ' + row.role"
                      @click="removePermissionRow(idx)">
                <i class="ti ti-trash" aria-hidden="true"></i>
              </button>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- ── Tab: 提示词 ─────────────────────────────────── -->
    <div x-show="activeTab === 'prompts'">
      <div class="cfg-section">
        <div class="cfg-section-title">System Prompt</div>
        <p style="font-size:12px;color:var(--text-muted);margin-bottom:8px">
          定义助手的角色、回答风格和引用格式要求
        </p>
        <textarea class="field" rows="8" x-model="form.prompt.system_template"
                  @input="setDirty()"
                  style="font-family:var(--font-mono);font-size:12px;resize:vertical"></textarea>
      </div>
      <div class="cfg-section">
        <div class="cfg-section-title" style="display:flex;align-items:center;justify-content:space-between">
          意图分类示例（few-shot）
          <button class="btn btn-sm" @click="addIntentExample()">
            <i class="ti ti-plus" aria-hidden="true"></i> 添加示例
          </button>
        </div>
        <p style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
          提供示例帮助模型更准确地分类用户意图，影响召回路路由策略
        </p>
        <template x-for="(ex, idx) in form.prompt.intent_examples" :key="idx">
          <div class="intent-example-row">
            <input class="field" placeholder="示例问题"
                   x-model="ex.query" @input="setDirty()" style="flex:1">
            <select class="field" x-model="ex.intent" @change="setDirty()"
                    style="width:140px">
              <option value="factual">factual（事实）</option>
              <option value="relational">relational（关系）</option>
              <option value="aggregation">aggregation（统计）</option>
              <option value="procedural">procedural（流程）</option>
              <option value="comparative">comparative（对比）</option>
              <option value="chitchat">chitchat（闲聊）</option>
            </select>
            <button class="btn-icon" @click="removeIntentExample(idx)">
              <i class="ti ti-trash" aria-hidden="true"></i>
            </button>
          </div>
        </template>
      </div>
    </div>

  </div><!-- /cfg-body -->

  <!-- 成功提示 -->
  <div x-show="saveSuccess" x-transition
       style="position:fixed;bottom:20px;right:20px;
              background:var(--success-bg);color:var(--success-text);
              border:0.5px solid var(--success-border);
              border-radius:var(--radius);padding:10px 16px;
              font-size:13px;display:flex;align-items:center;gap:6px">
    <i class="ti ti-check" aria-hidden="true"></i>
    配置已保存，重启服务后生效
  </div>

</div>
{% endblock %}

{% block extra_js %}
<script src="/static/js/config.js"></script>
{% endblock %}
```

### 8.3 配置页 Alpine 组件

**文件**：`static/js/config.js`

```javascript
function configPage(initialConfig) {
  return {
    tabs: [
      { id: 'resources',   label: '资源连接' },
      { id: 'features',    label: '功能开关' },
      { id: 'permissions', label: '知识域权限' },
      { id: 'prompts',     label: '提示词' },
    ],
    activeTab:   'resources',
    form:        null,           // 深拷贝的可编辑配置
    original:    null,           // 原始配置（用于撤销）
    isDirty:     false,
    isSaving:    false,
    saveSuccess: false,
    testStatus:  {},             // { llm: 'ok'|'err'|null, vector_store: ..., ... }
    testLoading: {},
    testErrors:  {},

    init() {
      this.original = JSON.parse(JSON.stringify(initialConfig));
      this.form     = JSON.parse(JSON.stringify(initialConfig));
      // 确保 form.permissions 是数组格式（从 dict 转换）
      if (this.form.auth && this.form.auth.permission_mapping) {
        this.form.permissions = Object.entries(
          this.form.auth.permission_mapping
        ).map(([role, collections]) => ({
          role,
          collections: Array.isArray(collections) ? collections.join(', ') : collections,
        }));
      } else {
        this.form.permissions = [];
      }
    },

    setDirty() { this.isDirty = true; },

    resetForm() {
      this.form    = JSON.parse(JSON.stringify(this.original));
      this.isDirty = false;
      // 重新转换 permissions
      if (this.original.auth && this.original.auth.permission_mapping) {
        this.form.permissions = Object.entries(
          this.original.auth.permission_mapping
        ).map(([role, collections]) => ({
          role,
          collections: Array.isArray(collections) ? collections.join(', ') : collections,
        }));
      }
    },

    async saveConfig() {
      this.isSaving = true;
      // 将 permissions 数组转回 dict 格式
      const permMap = {};
      (this.form.permissions || []).forEach(row => {
        if (row.role) {
          permMap[row.role] = row.collections === '*'
            ? ['*']
            : row.collections.split(',').map(s => s.trim()).filter(Boolean);
        }
      });
      const payload = { ...this.form };
      if (payload.auth) payload.auth.permission_mapping = permMap;
      delete payload.permissions;

      try {
        await api.saveConfig(payload);
        this.original   = JSON.parse(JSON.stringify(this.form));
        this.isDirty    = false;
        this.saveSuccess = true;
        setTimeout(() => { this.saveSuccess = false; }, 4000);
      } catch (e) {
        alert('保存失败：' + (e.message || '请检查配置格式'));
      } finally {
        this.isSaving = false;
      }
    },

    async testConnection(key) {
      this.testLoading = { ...this.testLoading, [key]: true };
      this.testStatus  = { ...this.testStatus,  [key]: null  };
      try {
        const result = await api.testConnection(key, this.form[key] || {});
        this.testStatus[key] = result.ok ? 'ok' : 'err';
        if (!result.ok) this.testErrors[key] = result.error;
      } catch (e) {
        this.testStatus[key] = 'err';
        this.testErrors[key] = e.message;
      } finally {
        this.testLoading[key] = false;
      }
    },

    testLabel(status) {
      return { ok:'连接正常', err:'连接失败', null:'未测试' }[status] ?? '未测试';
    },

    addPermissionRow()       { this.form.permissions.push({ role:'', collections:'' }); this.setDirty(); },
    removePermissionRow(idx) { this.form.permissions.splice(idx, 1); this.setDirty(); },
    addIntentExample()       {
      if (!this.form.prompt.intent_examples) this.form.prompt.intent_examples = [];
      this.form.prompt.intent_examples.push({ query:'', intent:'factual' });
      this.setDirty();
    },
    removeIntentExample(idx) {
      this.form.prompt.intent_examples.splice(idx, 1); this.setDirty();
    },
  };
}
```

---

## 9. API 对接层

**文件**：`static/js/api.js`

```javascript
/**
 * api — 所有后端请求的统一封装
 *
 * 所有方法返回 Promise，异常时 reject 一个 Error（message 为中文提示）。
 * 认证 Token 由后端在 Cookie 或 meta 标签中注入，无需 JS 手动管理。
 */
const api = (() => {

  const BASE = '/api/v1';

  async function request(method, path, body, opts = {}) {
    const headers = { 'Content-Type': 'application/json' };
    // CSRF Token（若后端要求）：从 meta 标签读取
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
    if (csrf) headers['X-CSRF-Token'] = csrf;

    const resp = await fetch(BASE + path, {
      method,
      headers,
      body:   body ? JSON.stringify(body) : undefined,
      signal: opts.signal,
    });

    if (!resp.ok) {
      let msg = `请求失败（${resp.status}）`;
      try { const d = await resp.json(); msg = d.error || d.detail || msg; } catch {}
      throw new Error(msg);
    }
    return resp.json();
  }

  const get  = (path, opts)       => request('GET',    path, null, opts);
  const post = (path, body, opts) => request('POST',   path, body, opts);
  const del  = (path, opts)       => request('DELETE', path, null, opts);
  const put  = (path, body, opts) => request('PUT',    path, body, opts);

  return {
    /* ── 问答 ──────────────────────────────────────────── */
    // 注意：流式接口直接用 fetch，不走此封装
    // streamQuery 在 chat.js 中直接调用 fetch('/api/v1/query', ...)

    /* ── 会话管理 ──────────────────────────────────────── */
    getSessions() {
      // GET /api/v1/admin/sessions
      // Response: { sessions: [{id, title, last_active, turn_count}] }
      return get('/admin/sessions');
    },
    getSessionHistory(sessionId) {
      // GET /api/v1/admin/sessions/{sessionId}/history
      // Response: { title, turns: [{role, content, sources}] }
      return get(`/admin/sessions/${sessionId}/history`);
    },
    deleteSession(sessionId) {
      // DELETE /api/v1/admin/sessions/{sessionId}
      return del(`/admin/sessions/${sessionId}`);
    },
    renameSession(sessionId, title) {
      // PUT /api/v1/admin/sessions/{sessionId}
      return put(`/admin/sessions/${sessionId}`, { title });
    },

    /* ── 知识库 ────────────────────────────────────────── */
    getCollections() {
      // GET /api/v1/admin/collections
      // Response: { collections: [{id, name, doc_count, chunk_count}] }
      return get('/admin/collections');
    },
    listDocuments(collection) {
      // GET /api/v1/admin/docs?collection={collection}
      // Response: { documents: [{doc_id, filename, file_type, status, chunk_count, file_size, created_at, storage_url}] }
      return get(`/admin/docs?collection=${encodeURIComponent(collection)}`);
    },
    getKnowledgeStats(collection) {
      // GET /api/v1/admin/stats?collection={collection}
      // Response: { doc_count, chunk_count }
      return get(`/admin/stats?collection=${encodeURIComponent(collection)}`);
    },
    async uploadFile(file) {
      // POST /api/v1/admin/upload（multipart/form-data）
      // Response: { storage_url: "..." }
      const form = new FormData();
      form.append('file', file);
      const resp = await fetch(`${BASE}/admin/upload`, {
        method: 'POST', body: form,
      });
      if (!resp.ok) throw new Error('文件上传失败');
      return (await resp.json()).storage_url;
    },
    ingestDocument(payload) {
      // POST /api/v1/ingest
      // Body: { file_url, doc_id?, collection, allowed_roles? }
      // Response: { doc_id, task_id, status }
      return post('/ingest', payload);
    },
    getTaskStatus(taskId) {
      // GET /api/v1/admin/tasks/{taskId}
      // Response: { task_id, status, chunk_count?, error? }
      return get(`/admin/tasks/${taskId}`);
    },
    deleteDocument(docId) {
      // DELETE /api/v1/admin/docs/{docId}
      return del(`/admin/docs/${docId}`);
    },

    /* ── 系统配置 ──────────────────────────────────────── */
    getConfig() {
      // GET /api/v1/admin/config
      // Response: 当前配置（敏感字段如 api_key 脱敏为 "••••"）
      return get('/admin/config');
    },
    saveConfig(config) {
      // POST /api/v1/admin/config
      // Body: 完整配置对象（空字符串字段保留原值，不覆盖）
      return post('/admin/config', config);
    },
    testConnection(componentKey, componentConfig) {
      // POST /api/v1/admin/health/test
      // Body: { component: "llm", config: {...} }
      // Response: { ok: bool, latency_ms: int, error?: string }
      return post('/admin/health/test', {
        component: componentKey,
        config:    componentConfig,
      });
    },
  };
})();
```

---

## 10. 组件库

**文件**：`static/js/utils.js`

```javascript
/**
 * 工具函数库
 */

/** 日期格式化 */
function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 60)   return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400 && d.toDateString() === now.toDateString())
    return d.toLocaleTimeString('zh-CN', { hour:'2-digit', minute:'2-digit' });
  return d.toLocaleDateString('zh-CN', { month:'short', day:'numeric' });
}

/** 文件大小格式化 */
function formatFileSize(bytes) {
  if (!bytes || bytes === 0) return '0 B';
  const units = ['B','KB','MB','GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return (bytes / Math.pow(1024, i)).toFixed(i ? 1 : 0) + ' ' + units[i];
}

/** 复制到剪贴板（含降级方案） */
async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const el = document.createElement('textarea');
    el.value = text; el.style.position = 'fixed'; el.style.opacity = '0';
    document.body.appendChild(el);
    el.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(el);
    return ok;
  }
}

/** 防抖 */
function debounce(fn, delay) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), delay);
  };
}

/** 配置 marked.js */
marked.setOptions({
  breaks:   true,    // 单个换行变 <br>
  gfm:      true,    // GitHub 风格 Markdown
  sanitize: false,   // 不内置消毒（由 DOMPurify 处理，此处信任后端内容）
});
```

---

## 11. 后端模板路由（FastAPI 侧）

以下代码添加到 `rag/api/app.py` 或独立的 `rag/api/routers/pages.py`：

```python
# rag/api/routers/pages.py
"""
HTML 页面路由。
FastAPI 托管 Jinja2 模板，将用户信息和初始配置注入模板，
前端无需额外 API 请求即可渲染初始状态。
"""
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router    = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory="rag/api/templates")


def _get_user(request: Request) -> dict:
    """从 request.state.user_context（由 AuthMiddleware 注入）提取模板所需用户信息"""
    ctx = getattr(request.state, "user_context", None)
    if not ctx:
        return {"name": "访客", "roles": [], "is_admin": False}
    return {
        "name":     ctx.username,
        "roles":    ctx.roles,
        "is_admin": "role_admin" in ctx.roles,
    }


@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse(url="/chat")


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, session: str | None = None):
    user = _get_user(request)
    return templates.TemplateResponse("chat.html", {
        "request":    request,
        "active_page": "chat",
        "user":       user,
        "session_id": session,   # 注入到 Alpine 组件初始化参数
    })


@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request):
    user = _get_user(request)
    if not (user["is_admin"] or "role_knowledge_manager" in user["roles"]):
        return RedirectResponse(url="/chat")
    return templates.TemplateResponse("knowledge.html", {
        "request":     request,
        "active_page": "knowledge",
        "user":        user,
    })


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    user = _get_user(request)
    if not user["is_admin"]:
        return RedirectResponse(url="/chat")

    # 加载当前配置（敏感字段脱敏）
    from rag.api.app import get_config
    cfg = get_config(request)
    sanitized = _sanitize_config(cfg)

    return templates.TemplateResponse("config.html", {
        "request":     request,
        "active_page": "config",
        "user":        user,
        "config":      sanitized,    # 注入到 configPage() 初始状态
    })


def _sanitize_config(cfg) -> dict:
    """将配置对象序列化为 dict，敏感字段替换为脱敏占位符"""
    import json
    raw = json.loads(cfg.model_dump_json())
    SENSITIVE = ["api_key", "password", "secret_key", "access_key"]
    def mask(obj):
        if isinstance(obj, dict):
            return {k: ("••••" if k in SENSITIVE and v else v if not v else mask(v))
                    for k, v in obj.items()}
        return obj
    return mask(raw)


# 在 app.py 中注册：
# from rag.api.routers import pages
# app.include_router(pages.router)

# 静态文件挂载（在 lifespan 之后）：
# from fastapi.staticfiles import StaticFiles
# app.mount("/static", StaticFiles(directory="rag/api/static"), name="static")
```

### 11.1 新增管理 API（前端需要但规格文档未包含的端点）

在 `rag/api/routers/admin.py` 中补充以下端点：

```python
# 会话列表
GET  /api/v1/admin/sessions
     Response: { sessions: [{id, title, last_active, turn_count}] }

# 会话历史
GET  /api/v1/admin/sessions/{session_id}/history
     Response: { title, turns: [{role, content, sources}] }

# 删除会话
DELETE /api/v1/admin/sessions/{session_id}

# 重命名会话
PUT  /api/v1/admin/sessions/{session_id}
     Body: { title: str }

# collection 列表（按权限过滤）
GET  /api/v1/admin/collections
     Response: { collections: [{id, name, doc_count, chunk_count}] }

# 文档列表
GET  /api/v1/admin/docs?collection={collection}
     Response: { documents: [{doc_id, filename, file_type, status,
                               chunk_count, file_size, created_at, storage_url}] }

# 知识库统计
GET  /api/v1/admin/stats?collection={collection}
     Response: { doc_count, chunk_count }

# 文件上传（返回 storage_url）
POST /api/v1/admin/upload   (multipart/form-data, field: file)
     Response: { storage_url: str }

# 删除文档（级联删除向量库/ES/图谱中的所有 Chunk）
DELETE /api/v1/admin/docs/{doc_id}

# 入库任务状态查询
GET  /api/v1/admin/tasks/{task_id}
     Response: { task_id, status, chunk_count?, error? }

# 获取当前配置（脱敏）
GET  /api/v1/admin/config
     Response: TenantConfig（敏感字段脱敏）

# 保存配置（写入 customer_config.yaml，触发热重载）
POST /api/v1/admin/config
     Body: 部分或完整 TenantConfig（空字段保留原值）

# 连接测试
POST /api/v1/admin/health/test
     Body: { component: str, config: dict }
     Response: { ok: bool, latency_ms: int, error?: str }
```

---

## 12. 实现顺序与自检清单

### 12.1 生成顺序

```
Step 1   static/css/base.css          全局变量、Reset、通用类
Step 2   static/css/layout.css        三栏布局
Step 3   static/css/chat.css          聊天界面样式
Step 4   static/css/knowledge.css     知识库样式（参考 chat.css 风格）
Step 5   static/css/config.css        配置界面样式（toggle、表单、section）
Step 6   static/js/utils.js           工具函数
Step 7   static/js/api.js             API 封装层
Step 8   static/js/chat.js            聊天页 Alpine 组件
Step 9   static/js/knowledge.js       知识库页 Alpine 组件
Step 10  static/js/config.js          配置页 Alpine 组件
Step 11  templates/base.html          基础布局模板
Step 12  templates/chat.html          聊天界面模板
Step 13  templates/knowledge.html     知识库模板
Step 14  templates/config.html        配置界面模板
Step 15  rag/api/routers/pages.py     FastAPI 页面路由
Step 16  补充 rag/api/routers/admin.py 中缺失的端点
```

### 12.2 自检清单

每个文件生成后验证：

- [ ] CSS 变量全部使用 `var(--xxx)`，无硬编码颜色（`#` 或 `rgb()`）
- [ ] 暗色模式下文字可读（`--text-primary`/`--text-secondary` 均有暗色定义）
- [ ] 所有图标使用 `<i class="ti ti-xxx">` Tabler 格式
- [ ] 交互元素有 `aria-label` 或关联 `<label>`
- [ ] Alpine 组件中异步操作（`await`）都有 `try/catch` 和错误提示
- [ ] SSE 流在 `AbortController` 中止时不触发错误提示
- [ ] 配置界面敏感字段（api_key、password）使用 `type="password"` 输入框
- [ ] 保存配置时空字符串字段不覆盖原有值（后端保留原值逻辑）
- [ ] 知识库上传支持拖拽和点击两种方式
- [ ] 消息列表在新消息到来时自动滚动到底部
- [ ] Textarea 输入框随内容自动增高（最大 160px）
- [ ] `Shift+Enter` 在输入框中换行，`Enter` 直接发送

### 12.3 config.css 补充说明（关键样式规格）

```css
/* config.css 需包含以下关键组件 */

/* Tab 导航 */
.cfg-tabs { display:flex; border-bottom:0.5px solid var(--border);
            padding:0 20px; background:var(--surface-1); }
.cfg-tab  { padding:10px 16px; font-size:13px; color:var(--text-secondary);
            cursor:pointer; border-bottom:2px solid transparent;
            margin-bottom:-0.5px; background:none; border-top:none;
            border-left:none; border-right:none; }
.cfg-tab.active { color:var(--accent); border-bottom-color:var(--accent); }

/* 配置区块 */
.cfg-body    { padding:20px; overflow-y:auto; flex:1; }
.cfg-section { margin-bottom:24px; }
.cfg-section-title { font-size:13px; font-weight:500; margin-bottom:12px;
                     display:flex; align-items:center; gap:6px; }
.cfg-section-title i { font-size:16px; color:var(--text-secondary); }

/* 表单行 */
.cfg-form  { display:flex; flex-direction:column; gap:10px; }
.form-row  { display:grid; grid-template-columns:160px 1fr;
             align-items:center; gap:10px; }
.form-label { font-size:12px; color:var(--text-secondary); }

/* 连接测试行 */
.test-row { display:flex; align-items:center; gap:8px;
            margin-top:10px; justify-content:flex-end; }

/* Toggle 开关 */
.toggle-list { display:flex; flex-direction:column; gap:6px; }
.toggle-item { display:grid; grid-template-columns:1fr auto; align-items:center;
               gap:12px; padding:10px 12px; background:var(--surface-1);
               border:0.5px solid var(--border); border-radius:var(--radius); }
.toggle-info  { display:flex; flex-direction:column; gap:2px; }
.toggle-label { font-size:13px; font-weight:500; }
.toggle-desc  { font-size:11px; color:var(--text-muted); }
.toggle-switch { position:relative; display:inline-block; width:36px; height:20px; }
.toggle-switch input { opacity:0; width:0; height:0; }
.toggle-slider { position:absolute; cursor:pointer; inset:0;
                 background:var(--border-strong); border-radius:10px;
                 transition:background 0.15s; }
.toggle-switch input:checked + .toggle-slider { background:var(--accent-fill); }
.toggle-slider::before { content:''; position:absolute; width:14px; height:14px;
                          border-radius:50%; background:#fff; top:3px; left:3px;
                          transition:transform 0.15s; }
.toggle-switch input:checked + .toggle-slider::before { transform:translateX(16px); }
.toggle-switch input:disabled + .toggle-slider { opacity:0.4; cursor:not-allowed; }

/* 权限行 */
.perm-table  { display:flex; flex-direction:column; gap:6px; }
.perm-row    { display:flex; gap:8px; align-items:center; }

/* 意图示例行 */
.intent-example-row { display:flex; gap:8px; align-items:center; margin-bottom:8px; }

/* 加载旋转动画 */
@keyframes spin { to { transform: rotate(360deg); } }
.spin { animation: spin 1s linear infinite; display:inline-block; }
```

---

*文档结束。本规格覆盖：3 个页面、14 个文件、完整 Alpine 组件、API 封装层、FastAPI 页面路由*
*技术栈：HTML + CSS + Vanilla JS + Alpine.js 3.x + Jinja2，无构建步骤*

---

## 附录 A：knowledge.css 完整样式

**文件**：`static/css/knowledge.css`

```css
/* ── 统计卡 ─────────────────────────────────────────────── */
.stats-bar {
  display: flex; gap: 12px; padding: 14px 20px;
  border-bottom: 0.5px solid var(--border); flex-shrink: 0;
  background: var(--surface-1);
}
.stat-card {
  display: flex; flex-direction: column; gap: 2px;
  padding: 8px 16px; background: var(--surface-2);
  border: 0.5px solid var(--border); border-radius: var(--radius);
  min-width: 100px;
}
.stat-label { font-size: 11px; color: var(--text-muted); }
.stat-value { font-size: 20px; font-weight: 600; color: var(--text-primary); }

/* ── 上传区 ─────────────────────────────────────────────── */
.upload-zone {
  margin: 16px 20px; padding: 32px 20px;
  border: 1.5px dashed var(--border-strong);
  border-radius: var(--radius); background: var(--surface-1);
  display: flex; flex-direction: column; align-items: center;
  gap: 10px; text-align: center; transition: all 0.15s;
}
.upload-zone.drag-over {
  border-color: var(--accent); background: var(--accent-bg);
}
.upload-icon { font-size: 32px; color: var(--text-muted); }
.upload-zone.drag-over .upload-icon { color: var(--accent); }

/* ── 进行中任务栏 ────────────────────────────────────────── */
.tasks-bar {
  margin: 0 20px 10px; padding: 10px 14px;
  background: var(--warning-bg); border-radius: var(--radius);
  border: 0.5px solid var(--border); display: flex; flex-direction: column; gap: 6px;
}
.task-item {
  display: flex; align-items: center; gap: 8px; font-size: 12px;
}
.task-item i { color: var(--warning-text); }

/* ── 文档列表 ────────────────────────────────────────────── */
.doc-list-container {
  flex: 1; overflow-y: auto; padding: 0 20px 16px; position: relative;
}
.doc-table {
  width: 100%; border-collapse: collapse; font-size: 13px;
}
.doc-table th {
  padding: 8px 10px; text-align: left; font-size: 11px; font-weight: 500;
  color: var(--text-muted); border-bottom: 0.5px solid var(--border);
  position: sticky; top: 0; background: var(--surface-2); z-index: 1;
}
.doc-table td {
  padding: 9px 10px; border-bottom: 0.5px solid var(--border);
  vertical-align: middle;
}
.doc-table tr:hover td { background: var(--surface-1); }
.doc-table tr:last-child td { border-bottom: none; }

/* ── 空态 ───────────────────────────────────────────────── */
.empty-state {
  display: flex; flex-direction: column; align-items: center;
  gap: 10px; padding: 60px 20px; text-align: center;
  color: var(--text-muted);
}
.empty-state i { font-size: 36px; }
.empty-state p { font-size: 13px; }

/* ── 批量操作栏 ─────────────────────────────────────────── */
.bulk-bar {
  position: sticky; bottom: 0; display: flex; align-items: center;
  justify-content: space-between; padding: 10px 16px;
  background: var(--surface-2); border-top: 0.5px solid var(--border);
  box-shadow: 0 -2px 8px rgba(0,0,0,0.05);
}
```

---

## 附录 B：三包文档集成说明

本文档（`ui_spec.md`）与 `rag_framework_spec.md`、`nebula_addition.md` 共同构成完整系统规格。
AI 生成代码时需理解三者的边界：

| 文档 | 生成内容 | 依赖关系 |
|------|----------|----------|
| `rag_framework_spec.md` | 后端核心：适配器、Pipeline、记忆管理、API 路由（§12） | 独立，先生成 |
| `nebula_addition.md` | NebulaGraph 适配器补丁，插入到 `builtin/knowledge_graph/` | 依赖 framework spec 的接口定义 |
| `ui_spec.md`（本文档） | 前端：HTML/CSS/JS/Jinja2 模板 + FastAPI 页面路由 + 新增管理 API | 依赖 framework spec 的 `/api/v1/` 端点 |

**生成顺序**：framework spec → nebula addition → ui spec

**集成要点**：

前端调用的 `/api/v1/query`（SSE 流式）由 `rag_framework_spec.md` §12 中的 `query_endpoint` 提供，
但 SSE 事件格式需在后端补充实现——原规格返回完整字符串，需改为逐 token 推送三类事件：

```python
# rag/api/routers/query.py — streamQuery 对应的后端 SSE 实现
# 在 query_endpoint 的 stream=True 分支中，将 AsyncIterator 包装为 SSE 格式：

async def _sse_stream(ctx, pipeline, memory, session_id, title):
    import json
    ctx = pipeline.run_context(ctx)   # 非流式步骤先跑完（理解、检索、重排）

    # 发送 sources（检索完成后立即推送，用户看到引用来源）
    if ctx.source_refs:
        yield f"data: {json.dumps({'type':'sources','sources':ctx.source_refs})}\n\n"

    # 流式生成
    async for token in ctx.llm_stream:
        yield f"data: {json.dumps({'type':'delta','content':token})}\n\n"

    # 完成信号
    yield f"data: {json.dumps({'type':'done','session_id':session_id,'title':title})}\n\n"
```

**新增 API 端点**（§11.1 中列出）与 framework spec §13 的运维控制面路由合并到同一文件
`rag/api/routers/admin.py`，按 §11.1 中的接口契约逐一实现。

