# OnseiCraft 构建与发版手册

> 本文件是**操作手册**，`MEMORY.md` 只留索引。每次发版照着跑即可。

## 一、版本号（三处，必须一起改）

| 位置 | 变量 |
|---|---|
| `work/app/main.py` | `VERSION = "x.y.z"` |
| `work/app/studio/__init__.py` | `__version__ = "x.y.z"` |
| `work/app/installer.iss` | `#define MyAppVersion "x.y.z"` |

⚠️ `work/pipeline/VERSION`（形如 `v0.1.0-beta.21`）是**管线自己的**版本号，**不是** app 版本。
用户看到的、`update.py` 比较的，是上面三处。

## 二、构建 exe

```bash
PY="C:/Users/zyk/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
$PY -m PyInstaller --noconfirm --distpath dist_new work/app/OnseiCraft.spec
```

⚠️ **别用默认 `dist/`**：PyInstaller 会先删 `dist/OnseiCraft`（2000+ 文件），
会被 `safe-delete` hook 拦下 → 构建静默失败、`dist` 里还是旧版本。
**永远用 `--distpath <新目录>`**，确认 `dist_new/OnseiCraft/OnseiCraft.exe` 的 mtime 是刚才。

## 三、打安装包

`.iss` 的 `BuildDir` 固定指向 `work/tmp/pydist/OnseiCraft`：

```bash
mv dist_new/OnseiCraft work/tmp/pydist/OnseiCraft
"C:/Users/zyk/AppData/Local/Programs/Inno Setup 6/ISCC.exe" work/app/installer.iss
```

⚠️ **Inno Setup 装在 `%LOCALAPPDATA%\Programs`**，只在 `C:\Program Files` 找会误判「没装」。

- 耗时约 **206 秒**，产物 `work/tmp/installer/OnseiCraft-<ver>-Setup.exe`（约 **631MB**）
- 安装包**故意不含 `pipeline.local.json`**（`.iss` 里 `Excludes`）——那是机器相关配置，首次运行才生成。
  所以「全新安装默认用什么模型」必须**单独验**，不能靠「工作区配置里是 v3」推断。

## 四、发布到 GitHub（不需要 gh CLI）

token 就在 git 凭据里（classic PAT，scopes `gist, repo, workflow`）：

```bash
TOK=$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill | grep '^password=' | cut -d= -f2-)
```

1. **建 draft release**（先 draft，避免用户看到没资产的空壳）
   `POST https://api.github.com/repos/1tsuka322/OnseiCraft/releases`
   body: `{"tag_name":"v<ver>","name":"...","body":"<说明>","draft":true}`
2. **传资产**
   `POST https://uploads.github.com/repos/1tsuka322/OnseiCraft/releases/<id>/assets?name=OnseiCraft-<ver>-Setup.exe`
   `-H "Content-Type: application/octet-stream" --data-binary @<安装包>`
   ⚠️ 631MB 约 **87 秒**（7.2MB/s）。**走 API 比浏览器 UI 稳**——UI 传大文件容易断且进度不可观测。
3. **发布** `PATCH .../releases/<id>` body `{"draft": false}`

资产名**必须以 `.exe` 结尾** —— `update.py` 只挑第一个 `.exe` 资产。

## 五、验证（四条都要过，别只看构建成功）

| 项 | 怎么验 |
|---|---|
| 下载链接 | `curl -sIL <browser_download_url>` → 200，`Content-Length` 与本地字节数一致 |
| 应用内检查更新 | 模拟旧版：`update.__version__='0.1.3'` 后调 `update.check()`，应返回新 tag + 资产名/大小/URL |
| 静默安装 | `Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOICONS /DIR="<临时目录>"` → 退出码 0 |
| 装出来的东西 | 目录里有 `OnseiCraft.exe` + `work/scripts`(80+) + `work/presets`(17)；**默认模型解析到 v3**；exe 能启动 |

⚠️ `update.check()` 返回 `None` 是**正确**的（当前已是最新），不是失败。
⚠️ 冒烟测试后**必须确认进程真的退出**，否则覆盖 exe 报 `Device or resource busy`。

### ⚠️⚠️ 静默安装测试会污染用户的正式安装（2026-11-13 真实事故）

Inno Setup 的静默安装**会写注册表卸载项、重建开始菜单快捷方式**——即使你装到临时目录。
测试完如果直接 `rm -rf <临时目录>`，就会留下：

- 注册表 `InstallLocation` 指向**已删除的目录**
- 开始菜单快捷方式指向**已删除的 exe** → **用户点图标打不开**

实测事故：装到 `work/tmp/install_test` 测试 → 删目录 → 用户开始菜单的 OnseiCraft 失效。

**正确做法（二选一）**：

```bash
# A. 测完用安装包自己的卸载程序卸掉（推荐）
"<临时目录>\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES

# B. 测完手工清注册表 + 快捷方式
#    删 HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{9E4C1A62-...}
#    删 %APPDATA%\Microsoft\Windows\Start Menu\Programs\OnseiCraft\
```

**绝不要**只删目录就完事。
另外测试时务必带 `/NOICONS`（不建桌面图标），减少污染面。

## 六、同步便携版安装目录

`D:/aiwork/kikoeru/onseicraft/`（Inno 安装出来的那份）要跟着更新：

```bash
cp -f dist_new/OnseiCraft/OnseiCraft.exe onseicraft/OnseiCraft.exe
mv onseicraft/_internal onseicraft/_internal.old && cp -r dist_new/OnseiCraft/_internal onseicraft/_internal
rm -rf onseicraft/work/scripts onseicraft/work/presets
cp -r dist_new/OnseiCraft/work/scripts onseicraft/work/scripts
cp -r dist_new/OnseiCraft/work/presets onseicraft/work/presets
# pipeline 目录要逐个复制（含 pipeline_lib/ 子目录），跳过 pipeline.local.json
```

⚠️ **`cp -f` 不加 `-r` 会静默跳过目录** → 必须逐个目录核对。
⚠️ `pipeline.local.json` 是机器相关配置，**保留安装版自己那份**，不要覆盖。
校验：exe md5 一致 + `for f in scripts/*.py` 全部 `cmp -s` 通过 + pipeline 关键文件一致。

## 七、清理

```bash
rm -rf work/tmp/pydist work/tmp/install_test   # 各约 1.6G
```

安装包（631MB）可以留着，方便重传。
