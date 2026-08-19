# 部署指南：启动后端 + Cloudflare 隧道

前端已发布在 GitHub Pages：`https://huaian-blip.github.io/ai-resume-engine/`
页面本身是静态的，但「分析 / STAR 优化 / 自我评价」等 AI 功能需要调用后端 API，因此每次使用前需按本指南启动本地后端并开一条公网隧道。

## 每次使用步骤

### 第 1 步 · 启动后端

在项目目录打开 PowerShell：

```powershell
cd "C:\Users\yuan\Documents\Tencent Files\ai-resume-engine"
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

保持窗口运行，不要关闭。

### 第 2 步 · 开公网隧道

另开一个 PowerShell 窗口：

```powershell
cloudflared tunnel --url http://127.0.0.1:8000
```

看到形如 `https://xxx-xxx.trycloudflare.com` 的输出行即成功，**复制该地址**（此窗口也要保持运行）。

### 第 3 步 · 更新前端配置 `docs\config.js`

```js
window.__API_BASE__ = 'https://你的新隧道地址';
window.__API_TOKEN__ = 'rsm-9f2c4e7a1b8d3f60';   // 与 .env 的 API_TOKEN 一致，保持不变
```

> 若 `.env` 里的 `API_TOKEN` 改过，此处必须同步修改。

### 第 4 步 · 推送，GitHub Pages 自动重建

```powershell
git add docs/config.js
git commit -m "chore: update tunnel url"
git push
```

约 1 分钟后访问 `https://huaian-blip.github.io/ai-resume-engine/`，AI 功能即可使用。

## 注意事项

- **隧道地址每次重启都会变**：`trycloudflare` 是临时隧道，每次开隧道都要重做第 3、4 步。
- **两个窗口要一直开着**：后端与隧道进程关闭后，线上 AI 功能随之失效（页面本身不受影响）。
- 首次安装依赖与浏览器（PDF 导出用）：

  ```powershell
  pip install -r requirements.txt
  playwright install chromium
  ```
