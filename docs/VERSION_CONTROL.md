# GitHub 版本管理

目标仓库为 `https://github.com/chixiaotao-Exm/ai-video-director`。

浏览器的「版本中心」通过 `GET /version-control` 读取当前应用源码所在仓库，显示安装包版本、分支、HEAD、当前标签、最近 15 个提交、最近 30 个本地标签和未提交变更数量。包版本来自已安装的 `avd` 分发元数据；Git 提交和标签来自真实仓库，两者分别显示，不把未发布提交伪装成正式版本。

同步状态只比较已有的本地跟踪引用，不会自动执行 `fetch` 或联网。因此「已同步」表示与上一次获取到的远程引用一致，不能证明 GitHub 此刻没有新增提交。没有上游、没有提交、分离 HEAD、未安装 Git 或 wheel 安装不含 `.git` 都有明确的降级状态，不影响视频工作区。

接口只读，不接受客户端的仓库路径、命令或分支参数，不执行 checkout、reset、pull、push、安装或自动更新。每次 Git 子命令最多运行 3 秒，禁用可选索引锁、fsmonitor、子模块扫描和 partial clone 的懒加载；错误消息不回传本地路径或原始 stderr。Git 必须支持 `--no-lazy-fetch`；过旧版本会显示 Git 不可用，不降级为可能联网的命令。远程地址仅展示去除用户信息、查询参数与片段的 GitHub 仓库链接。变更只提供数量，不返回素材文件名；未跟踪的 `data/`、`.venv/`、`node_modules/` 和真实环境文件不计入列表。

## 日常变更

1. 在功能分支完成一个范围明确的改动，检查 diff，避免带入 `.env`、`data/`、模型权重或未经授权的素材。
2. 在项目虚拟环境运行 `scripts/check.ps1`；前端改动另运行 `npm ci --prefix frontend`、`npm run build --prefix frontend`，检查桌面和手机截图。
3. 测试全绿后提交并推送功能分支，使用仓库的 PR 模板描述行为与验证结果。
4. GitHub Actions 的 **Quality checks / quality** 在 PR、主分支、功能/修复/Codex 分支及版本标签上运行 Python 3.12、Node 24、前端构建、Ruff、mypy、JS 和完整 pytest。依赖安装允许联网，测试使用 fake/stub，不提供生产 API 密钥，不下载模型或访问付费服务。
5. 审查通过后合并。仓库管理员可在 GitHub 分支规则中把该检查设为必需；本地代码不能声称或代替已生效的远程分支保护。

## 发布版本

发布前把 `pyproject.toml` 的版本与发布说明保持一致，在全量检查通过的提交上建立 `vX.Y.Z` 标签，再通过 GitHub Releases 人工发布对应版本和变更说明。仓库没有自动部署、自动收费调用或自动发布工作流。安装时保留自己的 `data/` 和 `.env`，先备份 SQLite 与素材，不把程序的 Git 回退当作数据库或生成版本回退。

## 本地身份与认证

Git 身份和 GitHub 登录由本机的 Git / GitHub CLI 管理；浏览器服务不持有 GitHub token，也不写入系统级凭据。推送需要拥有目标仓库权限的登录身份。远程仓库是否已推送、CI 是否成功、分支保护是否启用，以 GitHub 的真实状态为准。

参考（2026-10-20 核对）：`https://git-scm.com/docs/git-status`、`https://github.com/actions/checkout`、`https://github.com/actions/setup-python`、`https://github.com/actions/setup-node`。
