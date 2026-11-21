# 不高山上 · SideStore / AltStore 软件源

为四川大学校园助手 [不高山上（Bugaoshan）](https://github.com/The-Brotherhood-of-SCU/Bugaoshan)
维护的 iOS 侧载源。IPA 全部由 GitHub Actions **从上游 tag 的源码现场编译**（未签名），
发布到本仓库 Releases，并自动写回 AltStore / SideStore 格式的源 JSON。

## 源地址

| 渠道 | 直连源（GitHub） | 国内镜像源（gh-proxy 加速） |
| --- | --- | --- |
| 预览版 | `https://raw.githubusercontent.com/The-Brotherhood-of-SCU/Bugaoshan-sidestore/main/preview.json` | `https://gh-proxy.com/https://raw.githubusercontent.com/The-Brotherhood-of-SCU/Bugaoshan-sidestore/main/preview-mirror.json` |
| 正式版 | `https://raw.githubusercontent.com/The-Brotherhood-of-SCU/Bugaoshan-sidestore/main/stable.json` | `https://gh-proxy.com/https://raw.githubusercontent.com/The-Brotherhood-of-SCU/Bugaoshan-sidestore/main/stable-mirror.json` |

| 渠道 | 跟随的上游 tag | 说明 |
| --- | --- | --- |
| 预览版 | `vX.Y.Z-preview`、`vX.Y.Z-preview.N`、`vX.Y.Z-rcN` | 上游每发一个新 tag 就自动构建 |
| 正式版 | `vX.Y.Z`（正式 Release） | 只跟随正式版 |

**国内镜像源**：按 [gh-proxy.com 的加速方式](https://gh-proxy.com/docs/github-accelerator)，在原始
GitHub 链接前拼接 `https://gh-proxy.com/`。镜像源里的 `sourceURL`、`iconURL`、截图和 IPA
`downloadURL` 全部走镜像前缀，所以中国大陆网络下也能加载图标、下载安装包。镜像 JSON 由 CI
自动从直连源生成（`scripts/make_mirror.py`），版本、大小、SHA-256 与直连源完全一致。

GitHub Pages 落地页（含一键添加按钮）：<https://scubro.dev/Bugaoshan-sidestore/>

点一下就直接添加（iOS 上打开链接会唤起对应 App；AltStore 把 `sidestore://` 换成 `altstore://` 即可）：

- 预览版（直连）：`sidestore://source?url=https%3A%2F%2Fraw.githubusercontent.com%2FThe-Brotherhood-of-SCU%2FBugaoshan-sidestore%2Fmain%2Fpreview.json`
- 预览版（国内镜像）：`sidestore://source?url=https%3A%2F%2Fgh-proxy.com%2Fhttps%3A%2F%2Fraw.githubusercontent.com%2FThe-Brotherhood-of-SCU%2FBugaoshan-sidestore%2Fmain%2Fpreview-mirror.json`
- 正式版（直连）：`sidestore://source?url=https%3A%2F%2Fraw.githubusercontent.com%2FThe-Brotherhood-of-SCU%2FBugaoshan-sidestore%2Fmain%2Fstable.json`
- 正式版（国内镜像）：`sidestore://source?url=https%3A%2F%2Fgh-proxy.com%2Fhttps%3A%2F%2Fraw.githubusercontent.com%2FThe-Brotherhood-of-SCU%2FBugaoshan-sidestore%2Fmain%2Fstable-mirror.json`

> 两个渠道共用同一个 Bundle ID（`io.github.thebrotherhoodofscu.bugaoshan.ios`），
> 同一台设备同一时间只能安装其中一个渠道的版本；每个渠道的直连源与镜像源内容相同，加一个即可。

> 如果你想同时使用梯子和本地回环（SideStore签名），你可以在Clash的TUN规则中增加以下内容：
```
loopback-address:
  - 10.7.0.1
```
> 之后在每次更新时打开Clash并连接即可
## 工作原理

```
上游 repository_dispatch（推荐）/ schedule 兜底 / manual dispatch
        │
        ▼
   plan.py（ubuntu，约 20 秒）
   ├─ git ls-remote 拉上游全部 tag
   ├─ 按渠道挑最新 tag（preview / stable）
   ├─ 对比 preview.json / stable.json 的 versions[0]
   └─ 查本仓库是否已有该 tag 的 Release 资产
        │  只有需要的渠道才继续
        ▼
   build-release.yml（macos-15，约 5–15 分钟；首次拉 Flutter SDK 会更久）
   ├─ 选择 Xcode 26.3（不存在则退回镜像里最新版）
   ├─ git clone --branch <tag> 上游源码
   ├─ Flutter（从被构建 tag 的源码自动检测）+ pub get / build_runner / gen-l10n
   ├─ flutter build ios --release --no-codesign
   ├─ package_ipa.py 打包成 Payload/Runner.app → .ipa
   ├─ 发布 GitHub Release（tag 与上游同名；重建时另开 rebuild-* tag）+ IPA 资产
   ├─ update_source.py 写回直连源 JSON（保留最近 N 个历史版本）
   ├─ make_mirror.py 生成国内镜像源 JSON（所有 GitHub 链接套 gh-proxy 前缀）
   ├─ validate_sources.py 校验直连源 + 镜像源（含下载地址/大小/SHA-256）
   └─ 提交并推送 4 个 JSON（直连/镜像 × 预览/正式；rebase 重试）
```

触发方式：

- **推荐**：上游 release 流水线发布后直接 `repository_dispatch` 本仓库（两个仓库现在同属一个 org，
  可用 org secret 或 GitHub App）；实时、无需轮询。dispatch 的 `client_payload.tag` 会被当作
  `TARGET_TAG`，渠道从 tag 自动推断，因此只会构建这一个 tag；没有 payload 时按各渠道最新处理；
- **兜底**：GitHub `schedule`（实测会漂到几小时一次，只保证最终会跑）；
- **手动**：Actions → *Sync upstream* → Run workflow；
- 早期用 Cloudflare Worker 每 5 分钟轮询，已弃用，仅留档：[docs/cloudflare-worker.md](docs/cloudflare-worker.md)。

几个刻意与上游对齐的细节：

- **版本注入（与上游有意不同）**：SideStore 默认开启「App 版本校验」，安装时要求
  IPA 的 `CFBundleShortVersionString` 与源 JSON 的 `version` **完全一致**，否则会以
  `mismatchedVersion` 失败；同时不同 RC 的版本号必须能排出先后，客户端才知道有新预览版。
  因此预览渠道的版本号就是 tag 的真实版本（如 `2.6.0-rc3`、`2.5.4-preview.48`），稳定渠道是
  `X.Y.Z`；两者的 `CFBundleVersion` 都按上游公式 `major*10000 + minor*100 + patch` 计算。
  另外 Flutter 在 iOS 上会把 `--build-name` 过滤成纯数字（`2.6.0-rc3` → `2.6.03`），
  所以 CI 在构建后用 `package_ipa.py --set-short-version` 回写主 App 与扩展的
  `Info.plist`，保证与源 JSON 完全一致。
- **完整性**：源 JSON 的每个版本都带 IPA 的 `sha256`，SideStore 默认会校验下载内容；
  CI 在提交前会实际下载已发布的 IPA 复核大小与哈希。
- **构建环境**：`PUB_HOSTED_URL=https://pub.flutter-io.cn`、`dart run build_runner`、
  `flutter gen-l10n`，与上游 CI 的步骤相同。
- **Flutter 版本不写死**：每次构建都从**被构建的那个 tag** 里读取上游 pin 的版本
  （`.github/actions/setup/action.yml` → 任意 `.github/workflows/*.yml` 的 `flutter-version`
  → `.metadata` revision 映射），**解析不到会直接让构建失败并触发 Actions 失败通知**，
  便于及时发现上游结构变化；临时钉版本可以设置仓库变量 `FLUTTER_VERSION`。
- **未签名**：产物没有签名、没有描述文件，安装时由 SideStore / AltStore 使用
  你自己的证书重签名；App Group、小组件扩展等由 SideStore 在签名流程里按你的
  证书能力处理（可能提示移除扩展）。
- **Release tag 与上游同名**：本仓库的 `v2.6.0-rc3` 与上游 `v2.6.0-rc3` 一一对应，
  便于追溯；资产名固定为 `Bugaoshan-<version>.ipa`。
- **immutable releases**：org 已开启「不可变 Release」，已发布 release 的资产不能覆盖/删除。
  因此对同一 tag 重建（`force` 或资产缺失）时，产物会发布到
  `rebuild-<上游tag>-<run_id>-<attempt>` 的独立 Release；源 JSON 的 `version` 保持不变，
  `downloadURL` 指向重建后的资产。

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
- 想重打同一个 tag：加 `force=true`（会发布到 `rebuild-*` 独立 Release，并把 JSON 的 `downloadURL` 指过去；
  因为 `version` 不变，SideStore 不会提示更新，需要手动重装一次）。

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
preview.json                 # 预览版直连源（CI 维护，不要手改结构）
stable.json                  # 正式版直连源
preview-mirror.json          # 预览版国内镜像源（由 make_mirror.py 生成）
stable-mirror.json           # 正式版国内镜像源
config/source.json           # 静态元数据：应用信息、源标识、图标截图路径、历史长度
scripts/plan.py              # 解析上游 tag + 决定是否构建
scripts/update_source.py     # 把新版本写回源 JSON
scripts/make_mirror.py       # 直连源 → 国内镜像源（gh-proxy 前缀）
scripts/validate_sources.py  # 直连/镜像源校验（可校验下载地址与 SHA-256）
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

## 小组件（CourseWidget）与 Keychain 桥接补丁

上游小组件通过 App Group `group.io.github.thebrotherhoodofscu.bugaoshan.ios` 与 App 共享 SQLite
数据库和设置。侧载环境拿不到这个 App Group：标识全局唯一、归上游开发者 Team 所有，SideStore 重签时
会追加你的 TeamID（`...ios.<TEAMID>`），而 App / Widget 代码里写死原标识，所以**原版 IPA 的小组件
一定空白**（App Group 也是付费开发者 capability，免费账号更没有）。

本仓库的构建内置了桥接补丁 `patches/widget-bridge.patch`，换掉这条数据通路：

- **App 侧**：在 `updateWidget`（进入首页、改课表、改小组件设置时都会调）以及 App 进入后台时，
  把 `bugaoshan.db` 整库快照写进 **Keychain**（默认 access group，不需要 App Group capability）；
  小组件的显示明天 / 配色 / 密度设置也写入 Keychain；
- **Widget 侧**：先尝试 App Group（正常签名场景）；拿不到时从 Keychain 读快照，落到扩展自己的
  缓存目录，再走**原有 SQLite 查询与 UI**，所以渲染逻辑不变；
- **真机验证**：SideStore + 免费 Apple ID、安装时选 *Keep App Extensions (Use Main Profile)*，
  小组件可正常显示课程。

安装要求：

1. 出现 *App Contains Extensions* 时选 **Keep App Extensions (Use Main Profile)**——这样 App 与扩展
   共享同一个 `application-identifier`，Keychain 默认 group 才一致；
2. 装完先打开一次 App（停留在首页）写入快照，再添加小组件；从旧版升级的话，把旧小组件删掉重新添加；
3. 若安装时移除扩展，或选择 *Register App ID for Each Extension*，小组件不可用；实时活动也需要保留扩展。

已知限制：

- 补丁依赖上游 `ios/Runner/AppDelegate.swift` 与 `ios/CourseWidget/WidgetExtension.swift` 的结构；
  上游改动导致失配时 CI 会先尝试 fuzz，仍失败则**整个构建直接失败并触发 Actions 失败通知**，
  此时更新 `patches/widget-bridge.patch` 后重跑即可（不会发布没有桥接的包）；

## 注意事项

- **这是非官方分发**。上游同时在 App Store 分发正式版；侧载渠道仅作调试/尝鲜用途，
  请优先支持官方渠道。上游代码为 **AGPL-3.0**，本仓库的构建产物同样受该协议约束，
  源码请见上游仓库。
- 预览版可能很"碎"：上游一天可能发好几个 preview tag，CI 由上游 dispatch 触发（schedule 仅兜底），
  只会构建"当前最新"的那个 tag；错过的中间版本不会再补（需要时用 `tag` 输入手动补）。
- 免费 Apple ID 签名的 App 有效期 7 天，SideStore 会自动续签；这是 SideStore 的行为，与本仓库无关。
- 小组件（CourseWidget 扩展）在侧载签名时可能被 SideStore 移除，App 本体不受影响。
- IPA 未签名、未公证；下载后如校验失败，可在 Release 页面核对文件大小并重新下载。
- 本仓库只发布 iOS IPA，不镜像 Android APK / Windows / Linux 产物。
- org 开启了 immutable releases，已发布 Release 无法修改；需要重建时请用 `force`，CI 会自动发到
  `rebuild-*` tag，而不是报错退出。
- 国内镜像依赖第三方服务 [gh-proxy.com](https://gh-proxy.com/docs/github-accelerator)，它只做转发、
  不托管文件；若该服务临时故障，源会暂时加载失败，可换回直连地址或稍后重试。镜像可用性只做告警，
  不会阻断 CI 发布。

## 可用性自检（每次发布前后都会做）

CI 发布前会执行 `scripts/validate_sources.py --check-urls --check-hashes`：结构校验 + 实际下载
最新 IPA 比对大小与 SHA-256。另外本地还按 SideStore 的模型（`Source` / `StoreApp` /
`AppVersion` 的必需字段与 `ISO8601DateFormatter` 日期策略）对两份 JSON 做过解码验证，结论：

| 检查项 | 结果 |
| --- | --- |
| SideStore 模型解码两份 JSON | 通过 |
| IPA 大小 / SHA-256 与 JSON 一致 | 通过（preview 与 stable 均已核对） |
| IPA 内 `CFBundleShortVersionString` 与 JSON `version` 一致 | 通过（`2.6.0-rc3` / `2.6.0`） |
| IPA 未签名（`codesign -dv` → not signed） | 通过，SideStore 安装时重签 |
| 图标 / 3 张截图 / IPA 下载地址 HTTP 200 | 通过 |
| 镜像源结构 / URL 前缀 / 版本与直连一致 | 通过（CI 每次发布都会校验） |
| 镜像站实际可达、IPA 经代理下载后 SHA-256 一致 | 通过（gh-proxy.com，实测字节一致） |
| GitHub Pages 落地页 | 通过（`/preview.json`、`/stable.json` 及镜像源均可用） |
| 小组件 Keychain 桥接（真机） | 通过（SideStore + 免费 ID，Use Main Profile 安装） |
