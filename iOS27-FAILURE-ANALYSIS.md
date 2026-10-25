# Omega (Omega27) 在 iOS 27 上失效的原因与修复

> 依据固件差异报告 `iPhone18-2-iOS-26.6.2-to-27.0-diff`
> （iOS 26.6.2 / 23G90 → iOS 27.0 / 24A435，iPhone18,2）
> 以及本仓库代码（`omega.py` / `protective.py` / `sparserestore/`）。

## 1. 这个工具在做什么

Omega 是一个基于 **sparserestore**（稀疏恢复）的“解除 App 吊销 / 跳过设置”工具。它把一组备份记录写成本地临时备份目录，再通过 `Mobilebackup2Service.restore(..., system=True, copy=False)` 注入到设备。它主要写入两类东西：

1. **跳过设置（skip-setup）** —— 仍然有效的部分：
   - `SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles/Library/ConfigurationProfiles/CloudConfigurationDetails.plist`
   - `ManagedPreferencesDomain/mobile/com.apple.purplebuddy.plist`
2. **解除吊销 / 清证书有效性库（unrevoke）** —— 在 iOS 27 上已失效的部分：
   - 用“目录占位符”指向 `ProtectedDomain/trustd/valid.sqlite3`（及其 `-shm`/`-wal`）
   - 用“目录占位符”指向 `DatabaseDomain/MobileIdentityData/Rejections.plist`、`AuthListBannedUpps.plist`、`AuthListBannedCdHashes.plist`

绕过技巧（`omega.py:62-74` 注释）在于：把一个**文件路径**声明成 `Directory` 记录，让恢复流程“弄乱对这些文件的写入”，从而清空吊销库。

所有记录都由 `backup.py` 写成 `flags=4` + `owner=0/group=0`。

## 2. iOS 27 改了什么（差异报告证据）

### 2.1 trustd：自我修复权限 + 新的吊销策略检查（核心原因）
`MACHOS/filesystem/usr/libexec/trustd.md` 在 iOS 27 的 diff 中**新增**了：

- `_SecFixTrustdFilePermissions` —— trustd 现在会在启动时**主动修复自己的文件权限**；
- `_kSecPolicyCheckRevocationDbIgnoredForAppleCodeSignChain` —— 针对 Apple 代码签名链新增了“吊销库被忽略”的策略检查；
- `_kSecPolicyCheckUTCTimeNoSeconds`、`_SecCertificateHasNoSecondsUTCValidityDate` —— 更严格的证书有效期校验；
- 一整组 `_kSecTrustInfoRevocation*` / CRLite / OCSP 相关符号。
- 同时新增 `_access`（说明开始直接触碰磁盘权限）。

**结论**：即使 sparserestore “弄乱”了 `valid.sqlite3` 的写入，iOS 27 的 trustd 也会在下次启动时从受保护的存储重新生成该库，并按新的吊销策略重新判定 —— 所以“清空吊销库”不再生效。这正是 unrevoke 失效的直接原因。

### 2.2 BackupAgent2：放弃账号/网络校验路径 + 恢复时强化属主
`MACHOS/filesystem/usr/libexec/BackupAgent2.md`：

- **删除**了 `NSURLConnection`、`AKAppleIDSession`、`NSURLAuthenticationMethodClientCertificate/ServerTrust`、`PBCodable`（整套网络/账号在线校验路径）；
- **新增** `_fchownat`、`_fcopyfile`（恢复阶段对文件属主/拷贝做更严格的本地处理）。

**结论**：恢复时对受保护域文件的属主/保护类校验被加固；`protective.py` 自己的 `_SKIP_FILES` 注释也提到 iOS 验证保护类。但实测表明 **iOS 27 并不会对这些条目返回 `ErrorCode 205`**（205 是另一上下文：Nugget 的 PosterBoard/AppDomain）。所以 v2/omega 的“目录占位符”在 iOS 27 上**不是被拒，而是被静默忽略**：重做后的 `RestorePostProcess`（`MBAtomicBool`/`MBAtomicULong`）不再被“把文件路径声明成 Directory”这种把戏搞乱写入，占位符落地但**不清空目标文件**，因而 unrevoke 无效果。

### 2.3 misagent：安装/恢复引入在线鉴权度量
`MACHOS/filesystem/usr/libexec/misagent.md` 新增 `CoreAnalytics`、`OnlineAuthMetrics`、`CFPreferences`。sparserestore 借道的安装/恢复链路现在带在线鉴权度量，进一步收紧。

### 2.4 内核/其他
- 新 kext `com.apple.security.Image4`，移除 `com.apple.filesystems.hfs.kext`（HFS 彻底移除）；`AppleMobileFileIntegrity` 有更新。
- `restoreserviced` 整体重编（新 ARC ABI：`_objc_retain_xN`、`_objc_claimAutoreleasedReturnValue`），内部实现改变。

## 3. 失效链路

1. 工具的 unrevoke 机制 = 指向 `ProtectedDomain/trustd/valid.sqlite3` 与 `DatabaseDomain/MobileIdentityData/*` 的目录占位符。
2. iOS 27 的 trustd 现在自我修复权限（`_SecFixTrustdFilePermissions`）并用新策略重判吊销 → 即使写入被“弄乱”，库也会被重建，unrevoke 不再生效。
3. iOS 27 **不会**对这些条目返回 `ErrorCode 205`（恢复协议层被接受）。真正让 unrevoke 失效的是：重做后的 `RestorePostProcess` 不再被“文件声明成 Directory”的占位符把戏清空目标文件（占位符被静默忽略），而即使文件被清空，trustd 也会自修复并重校验（见第 2 节）。所以修复后整轮流程不会因 205 中止，但 unrevoke 效果不持久。
4. **代码层缺口**（让整轮流程崩掉）：`omega.py` 在恢复重启后用一个**盲目的 `asyncio.sleep(5)`** 再 `create_using_usbmux()` 重建 lockdown，而 `perform_restore` **没有任何瞬时错误重试**。于是恢复后第一次服务调用（`_restore_protective_backup`）常常打到一个还没就绪的服务，抛出 `MissingValueError`（典型的 stale-client 症状），整个流程中止。

## 4. 已做的修复

| 文件 | 修复 |
| :--- | :--- |
| `sparserestore/__init__.py` | `perform_restore` 增加瞬时错误重试（服务启动慢、重启掉线、`ErrorCode 205`），并把恢复后的 `DiagnosticsService.restart()` 改为 best-effort。 |
| `omega.py` | 使用 `_wait_for_device` 已等待/已配对的客户端；用就绪探测 `_await_service_ready` 取代盲目 `sleep(5)`；增加 iOS 27 检测与**诚实告警**（unrevoke 路径已被 defeat，仅 skip-setup 仍生效）；用 try/except 包裹各阶段，避免单条被拒就中止。 |
| `protective.py` | Phase 1 `mb.backup` 增加瞬时错误重试，与 Phase 3 行为一致。 |

## 5. 残留限制（必须说明）

- **unrevoke 在 iOS 27 上结构性失效**：并非协议层被拒（iOS 27 无 205），而是 (a) 目录占位符 trick 被重做的 `RestorePostProcess` 静默忽略、不清空文件，且 (b) trustd 自修复 + 重校验（OCSP/CRLite）让即使清空的库也立即重建。属于机制层失效，非简单代码 bug。在找到新的可利用向量之前，只有 **skip-setup**（configurationprofiles / purplebuddy，非受保护域）可靠生效。
- 本仓库代码当前已正确使用 `async with`（上游 Nugget 的“同步 `with` 误用异步服务”问题在此工作区不存在），因此未对此做改动。
- 修复后的代码会**明确提示** unrevoke 可能不生效，而不是静默失败；skip-setup 仍按原路径执行。

## 6. 验证方式

需要真机（iPhone18,2，iOS 27.0 24A435）+ 已 `pip install pymobiledevice3` + 关闭 Find My。本环境无设备、未装 `pymobiledevice3`，故未执行运行验证，仅做静态分析与代码加固。

## 7. “清除”方法（v1.0）复现研究 ← 用户要求

用户指出：早期提交（v1.0，`b9a8f62`，`unblacklist.py`）**不是把 `.sqlite` 变成目录，而是“清除”（truncate / 写空）**，并要求在 iOS 27 上研究这条路。已对照 git 核实：

### 7.1 v1.0 实际做法（git `b9a8f62`）
```python
plist_contents = plistlib.dumps({})          # 空字典 = 清空封禁列表
back = backup.Backup(files=[
    backup.Directory("", "SysContainerDomain-../../../../../../../../var/db/MobileIdentityData/"),
    backup.ConcreteFile("", ".../MobileIdentityData/Rejections.plist",          contents=plist_contents),
    backup.ConcreteFile("", ".../MobileIdentityData/AuthListBannedUpps.plist",  contents=plist_contents),
    backup.ConcreteFile("", ".../MobileIdentityData/AuthListBannedCdHashes.plist", contents=plist_contents),
    backup.Directory("", "SysContainerDomain-../../../../../../../../var/protected/trustd/private/"),
    backup.ConcreteFile("", ".../ocspcache.sqlite3",     contents=b''),   # 截断 = 清除
    backup.ConcreteFile("", ".../ocspcache.sqlite3-shm", contents=b''),
    backup.ConcreteFile("", ".../ocspcache.sqlite3-wal", contents=b''),
])
perform_restore(back, reboot=False)
```
要点：
- 用 **`ConcreteFile` 写“被清空的内容”**（空字典 / `b''`）去覆盖目标文件 = 截断 / 清除，而不是 `Directory` 占位符。
- 走 `SysContainerDomain` 路径穿越到达 `/var/db/MobileIdentityData` 与 `/var/protected/trustd/private/ocspcache.sqlite3`。
- 同样使用 `flags=4`（ConcreteFile 默认值）。

### 7.2 v1.0 与 v2/omega 的真正区别
| 维度 | v1.0 (b9a8f62) | v2 / omega.py |
| :--- | :--- | :--- |
| unrevoke 手段 | `ConcreteFile` 写**清空内容**（truncate） | `Directory` **占位符**（把文件路径声明成目录） |
| trustd 目标 | `ocspcache.sqlite3`（iOS ≤26 的名字） | `valid.sqlite3`（iOS 27 改名后的名字） |
| 域 | `SysContainerDomain` 路径穿越 | `DatabaseDomain` / `ProtectedDomain`（干净） |
| `flags` | `4`（默认） | `4`（默认） |

**关键结论：`flags=4` 不是 v1.0/v2 的区别**——两者都用 `flags=4`。区别仅在“**写清空内容**”vs“**目录占位符**”。而占位符在 iOS 27 被重做的 `RestorePostProcess` 静默忽略（第 2.2 节）；`ConcreteFile` 写的是真实文件（即便为空/损坏），机制上更可能真正落地。

### 7.3 用户的另一处纠正：iOS 27 没有 205
用户确认“iOS 27 没有 205”。结合上文：iOS 27 **不会**在协议层拒绝这些受保护域条目（205 是 Nugget PosterBoard/AppDomain 上下文）。因此 v1.0 的 `ConcreteFile` 清除条目**至少会被协议层接受**，不会被拒。真正的未知数是：**trustd 自修复 / 重校验（第 2.1 节 `_SecFixTrustdFilePermissions` + OCSP/CRLite）会不会让“清除”效果立即失效？** 这需要真机验证——这正是诊断工具的用途。

### 7.4 诊断工具：`omega_clear_diag.py`（已建，独立入口）
把 v1.0 的“清除”方法套到 iOS 27 的真实目标上，真机验证窗口期。**2026-10-25 更新：已改为使用 GoldenNugget 的 `src.restore` 实现**（`/Users/jason/GoldenNugget` 项目根加入 `sys.path` → `from src.restore import backup, perform_restore`；若 `goldennugget` 包已安装则优先），不再依赖 Omega27 本地 `sparserestore`。**关键收益**：GoldenNugget 的 `Backup` 默认 `manifest_ios27=True`，自动生成 iOS 27 现代 sqlite `Manifest.db` 格式（payload 在 `<xx>/<fileID>` 下、`Manifest.plist` v10/24.0、`SystemDomainsVersion 24.0`）；而 Omega27 本地 `sparserestore` 只生成旧版 `Manifest.mbdb`（v2.4/v9.1），本身就不匹配 iOS 27 恢复格式——很可能是 v2/omega 在 iOS 27 失效的额外因素。
- **默认（modern）：走 `restore_files` 六段式（`_files`）恢复**。构造 6 个 `FileToRestore` 对象（3 个 `DatabaseDomain/MobileIdentityData/*.plist` 空字典 + 3 个 `ProtectedDomain/trustd/valid.sqlite3*` 清空内容），交给 `src.restore.restore.restore_files(files, skip_protective_backup=True, skip_setup=False)`。该入口在 iOS 27 上内部走 `_restore_ios27` 多阶段流程；`skip_protective_backup=True` 时回退为单次原始 sparse 恢复（与旧 `perform_restore` 行为一致），但 `Backup`/`Manifest.db` 由它内部按设备版本自动构建——即“六段式 `_files` 恢复”而非手动 6 次 `perform_restore`。（`owner=0/group=0`、`mode=None`→`DEFAULT 0o755`、`flags=4`，与 `ConcreteFile` 默认逐字节等价。）
- **`--legacy`**：100% 复现 v1.0（`SysContainerDomain` 穿越 + `ocspcache.sqlite3`），用来测“旧路径穿越在 iOS 27 是否还可用”。注意 `restore_files` 会**丢弃** `domain=""` 的空域穿越文件（iOS 26.2+ 已无法投递该利用），故 legacy 仍保留在原始 `perform_restore(Backup(...))` 路径上以忠实复现 v1.0，不并入 `restore_files`。
- 旋钮：`--sqlite-payload {zero,space,nullbyte}`（默认 `zero`=截断；`space`/`nullbyte` 用于排除“0 字节被跳过”）；`--reboot`（发重启，强制 trustd 重开库）；`--skip-confirm`（脚本化）。`--ios26-format` 已移除——`restore_files` 按连接设备的系统版本自动选择 iOS 26(MBDB)/iOS 27(sqlite) 清单格式。
- 安全：不直接动 `omega.py`/三阶段流程；无设备时只打印将要发送的 `FileToRestore` 清单（`restore_files` 内部据此构建清单）供审阅；连上设备后需输入 `CLEAR` 才发送；默认不重启以便即时观测窗口。

**已做的离线校验**（沙箱无 `pymobiledevice3`/PySide6，`pymobiledevice3` stub 之，走 GoldenNugget `src.restore`/`src.utils.file_to_restore` 真实代码）：
- 导入解析：`goldennugget` 未安装 → 回退到 `/Users/jason/GoldenNugget` 根的 `src.restore` + `src.restore.restore`（`restore_files`）+ `src.utils.file_to_restore`（`FileToRestore`），`py_compile` 通过。
- modern 构建 → 6 个 `FileToRestore`（`owner=0/gid=0`、`mode=None`）；镜像 `concat_regular_file` 最终落盘逻辑后，`Backup(manifest_ios27=True).write_to_directory()` 写出合法 `Manifest.db`（sqlite，`Files` 表 10 行 = 2 域目录 + 2 路径目录 + 6 文件，`fileID` = sha1(`domain-relativePath`) 校验一致），6 条预期目标路径**全部存在**，`Manifest.plist` `Version=10.0` / `SystemDomainsVersion=24.0`。confirm `restore_files` 会产出 iOS 27 现代格式。
- legacy 构建 → 8 条记录（2 目录 + 6 文件），`Backup` 构造无异常，与 v1.0 一一对应。
- 注：`restore_files` 实际调用需真机 + PySide6/设备栈（沙箱缺 PySide6，无法在离线跑该入口）；构建与清单生成逻辑已离线验证等价。

### 7.5 真机验证步骤（待你执行）
1. 关闭 Find My，连设备，跑 `python3 omega_clear_diag.py`（先只看预览清单，确认 6 条记录无误）。
2. 再跑 `python3 omega_clear_diag.py` 并输入 `CLEAR` 发送。
3. 若恢复**无异常返回** → 确认“iOS 27 无 205、条目被接受”（协议层结论成立）。
4. 立刻打开一个此前被吊销的侧载 App；在 T+5m / +30m / +1h / +24h 复测。
   - 若 T0 生效、之后失效 → 确认“清除可临时生效”，是一条**可重复执行的临时手段**（非永久修复）。
   - 若**从不生效** → trustd 自修复瞬发重建（从受保护源重建库），清除在 iOS 27 上彻底失效。
5. 可选：`--legacy` 验证旧穿越路径是否还能写进去；`--sqlite-payload space` 排除 0 字节被跳过的可能。

### 7.6 用户实测：`ocspcache.sqlite3` 在 iOS 27 已失效（2026-10-25）
- 用户确认 **`ocspcache.sqlite3` 在 iOS 27 上失效**（改名/不再存在）。这与第 1/7.2 节的推断一致：v2 (`555dba0`) 把 trustd 目标从 `ocspcache.sqlite3` 换成 `valid.sqlite3` 正是因为旧文件在 iOS 27 不再存在。
- **结论**：诊断工具的 `--legacy` 模式（`SysContainerDomain` 穿越 + `ocspcache.sqlite3`）写到一个**不存在的路径**，无任何效果。已在 `omega_clear_diag.py` 中标 `CONFIRMED DEAD` 横幅，仅保留用于演示/复验。
- **研究收口到唯一活假设**：默认（modern）模式，目标 `ProtectedDomain/trustd/valid.sqlite3`。但需注意——`valid.sqlite3` 看起来是**权威有效性库**（而非单纯的 OCSP 缓存），iOS 27 的 `_SecFixTrustdFilePermissions` + 吊销策略重判很可能**主动重建**它（见第 2.1 节）。因此 modern 模式的真实未知数只剩一个：**清除 `valid.sqlite3` 后，trustd 自修复/重校验前是否存在一个可用的“临时窗口”**。这只能由真机按 7.5 节步骤测量（T0 / +5m / +30m / +1h / +24h 复测被吊销侧载 App）。
- 若 modern 也确认无效 → 整个“清有效性库”路线在 iOS 27 结构性死亡，仅剩 skip-setup（第 5 节）。届时需另寻向量（例如 misagent 注入、配置描述文件、或证书链层面的绕过），不在此诊断工具范围内。
