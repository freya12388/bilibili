# B站UP主动态监控 → 微信推送

监控 UP主「我走路带风风风」(UID 3706962053172003) 的图文/文字动态，发现新动态后 1 分钟级推送到微信。

## 组成

- `main.py`：抓取B站动态（buvid 指纹 + WBI 签名绕过风控）→ 对比游标 → PushPlus/Server酱推送
- `.github/workflows/watch.yml`：每分钟触发，运行后把 `state/`（游标+指纹）commit 回仓库
- 只推送 `DYNAMIC_TYPE_DRAW`（图文）和 `DYNAMIC_TYPE_WORD`（文字）；视频投稿等只推进游标不打扰

## 部署步骤

1. 在 GitHub **新建一个 Public 仓库**（公开仓库 Actions 免费不限时长）
2. 把本目录（`main.py`、`.github/`）上传上去
3. 仓库 **Settings → Secrets and variables → Actions → New repository secret**，按所选通道添加：
   - 用 Server酱（推荐）：添加 `SERVERCHAN_SENDKEY` = 你的 SendKey（sct.ftqq.com 微信扫码获取）
   - 用 PushPlus：先到 verify.pushplus.plus 完成实名，再添加 `PUSHPLUS_TOKEN` = 你的 token
   - （可选，更稳）添加 `BILI_SESSDATA` = 任意B站小号 cookie 里的 SESSDATA 值
4. Actions 页面手动触发一次 `bili-dynamic-watch`（workflow_dispatch）：
   - 首次运行只记录基线（不推送历史动态），会自动 commit 出 `state/` 目录
5. 之后每分钟自动运行，UP主发新动态 → 微信收到「【B站】xxx 发了新动态」

## 本地测试

```bash
set SERVERCHAN_SENDKEY=xxx   # 或 PUSHPLUS_TOKEN
python main.py
```

## 注意事项

- GitHub Actions 官方最短调度为 5 分钟，`* * * * *` 每分钟写法大部分时候正常执行，
  但高峰期可能延迟或偶发跳过——若要求严格 1 分钟，请改为自有服务器 cron
- B站接口偶发返回空列表（服务端缓存抖动），脚本设计为"空列表不动游标"，不会漏推也不会重推
- 推送失败时游标停在最后成功处，下一分钟自动重试
- 仓库需保持活跃（bot 的 state commit 会自动保活，防止 60 天不活跃被停用调度）
