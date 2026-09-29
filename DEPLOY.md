# linux.do 每日活跃 —— GitHub Actions 部署手册

在 GitHub 云端定时运行，**不需要你的电脑开机，也不需要服务器**。

---

## 为什么这条路能绕开之前所有的坑

| 之前的死结 | Actions 如何解决 |
|---|---|
| 阿里云连不上 linux.do（GFW 污染+封锁） | runner 在 Azure 海外机房，不受墙影响 |
| 本机方案要求电脑开机 | 云端 7×24，与你无关 |
| 没装 docker compose | 不需要 docker |

---

## 前置准备（约 5 分钟）

### 第 1 步：导出 cookie

在**已登录 linux.do 的本机**上运行：

```powershell
cd E:\AI\ubuntunixiang\baoliao\linuxdo-actions
python export_cookies.py
```

它会输出一段 **base64 字符串**（很长，约 900 字符）。**复制整段**，下一步要用。

> 如果提示「找不到 Cookies」或「缺少 _t」，说明指定的 profile 没登录过 linux.do。
> 用 `--profile <路径>` 指定你日常登录用的那个 profile，例如：
> ```powershell
> python export_cookies.py --profile "C:\Users\shen_ovo\AppData\Local\Google\Chrome\User Data"
> ```
> （注意：指定日常 profile 时需要先关掉 Chrome，否则数据库被占用。）

### 第 2 步：创建 GitHub 仓库

**方式 A：网页建（推荐，无需装工具）**

1. 打开 <https://github.com/new>
2. Repository name 填 `linuxdo-daily`（随意）
3. **选 Private**（因为要存 Secret，私有不影响 Actions 免费额度）
4. 不要勾选 "Add a README file"
5. 点 Create repository

**方式 B：装 gh CLI 后用命令建**

```powershell
winget install --id GitHub.cli
gh auth login
gh repo create linuxdo-daily --private --source . --push
```

---

## 第 3 步：推送代码

在 `linuxdo-actions` 目录里执行（把 `<你的用户名>` 换成实际值）：

```powershell
cd E:\AI\ubuntunixiang\baoliao\linuxdo-actions

git init
git add .
git commit -m "linux.do daily activity via GitHub Actions"
git branch -M main
git remote add origin https://github.com/<你的用户名>/linuxdo-daily.git
git push -u origin main
```

> 首次 push 会要求登录 GitHub。建议用 **Personal Access Token** 当密码
> （GitHub 已不支持账号密码推送）：Settings → Developer settings →
> Personal access tokens → Fine-grained tokens，勾 `Contents: Read and write`。

---

## 第 4 步：配置 Secret（关键）

1. 打开你的仓库 → **Settings**
2. 左侧 **Secrets and variables** → **Actions**
3. 点 **New repository secret**
4. **Name** 填：`LINUXDO_COOKIES`
5. **Secret** 粘贴第 1 步导出的那整段 base64 字符串
6. 点 **Add secret**

> ⚠️ 这个 Secret 等同于你的登录凭证。**不要**把它写进代码、issue、或截图发人。
> 想作废：在 linux.do 设置里「登出所有设备」，然后重新导出即可。

---

## 第 5 步：首次手动验证

1. 打开仓库 → **Actions** 标签
2. 左侧选 **linux.do Daily**
3. 右侧 **Run workflow** → 可以先把 `dry_run` 勾上试一次
4. 等约 3–5 分钟，点进运行记录看日志

**重点看这一步的输出：**

```
[preflight] 直连模式，linux.do 可达
模式: 云端（注入 cookie）
已注入 6 个 cookie
1/5 打开 linux.do
    标题: LINUX DO - 新的理想型社区     ← 关键！不是「请稍候…」就说明过了 CF
2/5 确认登录态
    ✅ <你的用户名>  TL?
```

**两种结果：**

| 现象 | 含义 | 处理 |
|---|---|---|
| 标题是 `LINUX DO...` 且显示用户名 | ✅ 成功 | 等定时任务自动跑即可 |
| 标题是 `请稍候…` 或 `登录态查询失败 403` | ❌ CF 挑战没过 | 见下方「故障排查」 |

---

## 日常使用

**查看运行历史**：仓库 → Actions → 点任意一次运行
**下载结果**：运行页面底部 **Artifacts** → `linuxdo-result-<编号>`
**手动跑一次**：Actions → linux.do Daily → Run workflow
**改参数**：编辑 `.github/workflows/linuxdo-daily.yml` 里的 cron，或改脚本里的默认值

**默认时间表**（UTC，北京时间 = UTC+8）：

| cron | UTC | 北京时间 |
|---|---|---|
| `0 1 * * *` | 01:00 | **09:00** |
| `0 13 * * *` | 13:00 | **21:00** |

一天跑两次是为了防止偶发失败导致当天没记录。

**停止**：Actions 页面右上角 **⋯** → **Disable workflow**

---

## 重要限制（务必知道）

### ⚠️ GitHub 会在 60 天无活动后自动停用定时任务

如果你的仓库 60 天没有任何提交/操作，GitHub 会**自动禁用 schedule 触发器**，
任务就静默停止了。

**两个应对办法：**

1. **每月手动点一次** Actions → Run workflow（最简单）
2. 加一个保活工作流（参考 doveppp 项目的 `immortality.yml`，
   用 `PhrozenByte/gh-workflow-immortality` + 一个 PAT）

### ⚠️ 免费额度

Private 仓库每月 **2000 分钟** Actions 额度。本任务单次约 3–5 分钟，
每天两次 ≈ 每月 300 分钟，**远在额度内**。

---

## 故障排查

### CF 挑战过不去（最常见的失败）

日志里标题停在 `请稍候…`。原因：GitHub runner 是 Azure 机房 IP，
Cloudflare 风控较严。**这是本方案最大的不确定因素。**

按顺序尝试：

1. **换 cookie 重新导出**。`cf_clearance` 是 IP 绑定的，在本机有效不代表在
   runner 上有效 —— 但也可能反而更好（clean IP 更容易过）。
2. **检查是否有 `_t`**。日志里「已注入 N 个 cookie」如果没列出 `_t`，
   说明 Secret 内容不对。
3. **重跑几次**。CF 的判定有随机性，有时重试就过。
4. **改用账号密码方式**（风险更高，不推荐）：需要改脚本实现登录流程
   并处理 hCaptcha，工作量大。

### 「未登录（profile 里没有有效会话）」

cookie 过期了。重新执行第 1 步导出，更新 Secret。

### 想看更详细的日志

在 workflow 里把 `LINUXDO_HEADLESS` 改成 `'0'`（但 runner 上没有显示器，
需要有头运行还得配 xvfb，一般不需要）。

---

## 合规提醒

本脚本**只做浏览**：打开帖子、滚动、上报阅读时长，**不产生任何内容**
（不发帖、不回帖、不点赞以外的写入）。

它的前身是社区里 329 star 的
[doveppp/linuxdo-checkin](https://github.com/doveppp/linuxdo-checkin)，
该项目的作者在 README 里明确写了：

> 请不要魔改本项目多线程刷论坛，本项目的初衷只是为了给个人账号刷一下访问天数的，
> 不是给号商养号用的。

**请遵守同样的克制：不要改成多账号、不要加自动回复、不要加大频率。**
linux.do 社区禁止无意义内容和养号行为，违规会被封号。

---

## 文件清单

| 文件 | 说明 |
|---|---|
| `.github/workflows/linuxdo-daily.yml` | 工作流定义（定时、环境、步骤） |
| `linuxdo_daily.py` | 主脚本（浏览 + 上报阅读时长） |
| `export_cookies.py` | cookie 导出工具（本地运行，不上传） |
| `requirements.txt` | 依赖（只有 playwright） |
