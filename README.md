# 不高山上 · SideStore / AltStore 软件源

为四川大学校园助手 [不高山上（Bugaoshan）](https://github.com/The-Brotherhood-of-SCU/Bugaoshan)
维护的 iOS 侧载源。IPA 全部由 GitHub Actions **从上游 tag 的源码现场编译**（未签名），
发布到本仓库 Releases，并自动写回 AltStore / SideStore 格式的源 JSON。

## 源地址

| 渠道 | 源 JSON | 跟随的上游 tag | 说明 |
| --- | --- | --- | --- |
| 预览版 | `https://raw.githubusercontent.com/wjj-8283/Bugaoshan-sidestore/main/preview.json` | `vX.Y.Z-preview`、`vX.Y.Z-preview.N`、`vX.Y.Z-rcN` | 上游每发一个新 tag 就自动构建 |
| 正式版 | `https://raw.githubusercontent.com/wjj-8283/Bugaoshan-sidestore/main/stable.json` | `vX.Y.Z`（正式 Release） | 只跟随正式版 |

GitHub Pages 镜像（适合 raw 访问受限的网络）：

- `https://wjj-8283.github.io/Bugaoshan-sidestore/preview.json`
- `https://wjj-8283.github.io/Bugaoshan-sidestore/stable.json`

点一下就直接添加（iOS 上打开链接会唤起对应 App）：

- SideStore：`sidestore://source?url=https%3A%2F%2Fraw.githubusercontent.com%2Fwjj-8283%2FBugaoshan-sidestore%2Fmain%2Fpreview.json`
- AltStore：`altstore://source?url=https%3A%2F%2Fraw.githubusercontent.com%2Fwjj-8283%2FBugaoshan-sidestore%2Fmain%2Fstable.json`

> 两个源共用同一个 Bundle ID（`io.github.thebrotherhoodofscu.bugaoshan.ios`），
> 同一台设备同一时间只能安装其中一个渠道的版本。

## 工作原理

```
schedule(每 30 分钟) / manual dispatch
        │
        ▼
   plan.py（ubuntu，约 20 秒）
   ├─ git ls-remote 拉上游全部 tag
   ├─ 按渠道挑最新 tag（preview / stable）
   ├─ 对比 preview.json / stable.json 的 versions[0]
   └─ 查本仓库是否已有该 tag 的 Release 资产
        │  只有需要的渠道才继续
        ▼
   build-release.yml（macos-15，约 30–50 分钟）
   ├─ 选择 Xcode 26.3（不存在则退回镜像里最新版）
   ├─ git clone --branch <tag> 上游源码
   ├─ flutter 3.44.9 + pub get（沿用上游镜像/回退脚本）/ build_runner / gen-l10n
   ├─ flutter build ios --release --no-codesign
   ├─ package_ipa.py 打包成 Payload/Runner.app → .ipa
   ├─ 发布 GitHub Release（tag 与上游同名）+ IPA 资产
   ├─ update_source.py 写回源 JSON（保留最近 N 个历史版本）
   ├─ validate_sources.py 校验（含下载地址 HEAD 校验）
   └─ 提交并推送 JSON（rebase 重试，避免并发冲突）
```

几个刻意与上游对齐的细节：

- **版本注入**：预览版 / RC 的 `CFBundleShortVersionString` 取「上一正式版」，
  `CFBundleVersion = major*10000 + minor*100 + patch`，与上游自有发布流水线一致；
  源 JSON 里的 `version` 则使用 tag 的真实版本号（如 `2.6.0-rc3`），SideStore 里能看到预览编号。
- **构建环境**：Flutter `3.44.9`、`PUB_HOSTED_URL=https://pub.flutter-io.cn`、
  `dart run build_runner`、`flutter gen-l10n`，与上游 CI 的步骤相同。
- **未签名**：产物没有签名、没有描述文件，安装时由 SideStore / AltStore 使用
  你自己的证书重签名；App Group、小组件扩展等由 SideStore 在签名流程里按你的
  证书能力处理（可能提示移除扩展）。
- **Release tag 与上游同名**：本仓库的 `v2.6.0-rc3` 与上游 `v2.6.0-rc3` 一一对应，
  便于追溯；资产名固定为 `Bugaoshan-<version>.ipa`。

## 手动操作

GitHub Actions → **Sync upstream** → *Run workflow*：

| 输入 | 说明 |
| --- | --- |
| `channel` | `all`（默认）/ `preview` / `stable` |
| `tag` | 可选，强制构建指定上游 tag（如 `v2.6.0-rc3`），必须与单一渠道配合 |
| `force` | 即使源 JSON 已有该版本也重新构建 |

常见场景：

- 某次构建失败、上游已经又发了新版本：直接 `channel=preview` + `force=false` 就会构建最新；
- 想补一个旧版本：`channel=preview` + `tag=v2.5.4-preview.48`；
- 想重打同一个 tag：加 `force=true`（会覆盖 Release 资产并重写 JSON 条目）。

### 本地跑脚本

```bash
python3 scripts/plan.py --channel all          # 看上游最新 tag 与计划（需要 git 联网）
python3 scripts/update_source.py --channel preview --tag v2.6.0-rc3 \
  --size 12345678 --download-url https://example.com/x.ipa --notes "测试"
python3 scripts/validate_sources.py             # 结构校验
python3 scripts/package_ipa.py --app Runner.app --out Bugaoshan-test.ipa
python3 -m unittest discover -s tests -v        # 单元测试
```

## 目录结构

```
preview.json                 # 预览版源 JSON（CI 维护，不要手改结构）
stable.json                  # 正式版源 JSON（CI 维护）
config/source.json           # 静态元数据：应用信息、源标识、图标截图路径、历史长度
scripts/plan.py              # 解析上游 tag + 决定是否构建
scripts/update_source.py     # 把新版本写回源 JSON
scripts/validate_sources.py  # 源 JSON 校验（可校验下载地址）
scripts/package_ipa.py       # .app → .ipa 打包（保留符号链接/权限位）
scripts/common.py            # tag 解析、版本注入、GitHub API 等公共逻辑
tests/test_scripts.py        # 单元测试（不联网）
site/index.html              # GitHub Pages 落地页（含一键添加深链）
assets/                      # 图标与截图（来自上游）
.github/workflows/sync.yml          # 定时 / 手动同步入口
.github/workflows/build-release.yml # 构建 + 发布 + 更新 JSON（可复用）
.github/workflows/validate.yml      # 单测与 JSON 校验
.github/workflows/pages.yml         # Pages 部署
```

## 注意事项

- **这是非官方分发**。上游同时在 App Store 分发正式版；侧载渠道仅作调试/尝鲜用途，
  请优先支持官方渠道。上游代码为 **AGPL-3.0**，本仓库的构建产物同样受该协议约束，
  源码请见上游仓库。
- 预览版可能很"碎"：上游一天可能发好几个 preview tag，CI 每 30 分钟检查一次，
  只会构建"当前最新"的那个 tag；错过的中间版本不会再补（需要时用 `tag` 输入手动补）。
- 免费 Apple ID 签名的 App 有效期 7 天，SideStore 会自动续签；这是 SideStore 的行为，与本仓库无关。
- 小组件（CourseWidget 扩展）在侧载签名时可能被 SideStore 移除，App 本体不受影响。
- IPA 未签名、未公证；下载后如校验失败，可在 Release 页面核对文件大小并重新下载。
- 本仓库只发布 iOS IPA，不镜像 Android APK / Windows / Linux 产物。
