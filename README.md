# Typhoeus - Quaver Music 后端（WIP）

Quaver Music，是一款 QQ 音乐的第三方客户端，其目的是为了让 Linux DE / Wayland WM 用户能够爽用，基于 Electron + Vite 实现

名字取自于八分音符，对应了音乐，“QQ” 的 Q 字母。

这项目是其后端实现的开源，目前正在考虑 C++ / Zig。

> [!CAUTION]
> 真爱音乐，尊重正版，音乐平台不易，该应用**不提供盗版 QQ 音乐服务！**
>
> 与此同时，这个软件目前全权是拷打 QWen 3.8 Flash 诞生的，按原样提供，own ur risk！

# 契机

我一直是 QQ 音乐的用户，也用过 NCM 的第三方客户端，Spotify，Apple Music。

然而 QQ 音乐一直没有什么好用的第三方客户端，而同 TME 系的有 [MoeKoe](https://music.moekoe.cn/) ，而转机是在 [Lyrune](https://github.com/amtoaer/lyrune)，一个挺好用的 Rust Q 音第三方客户端，但可惜 Rust 太重了，而且我 Rust 是真的菜。

所以使用 Electron，对接入 NodeJS 的 API 而言也很方便，开发也很快，也可以避免我孱弱的 Rust 开发，与此同时，后端我也能玩 C++ 这一个我更熟悉的编程语言。故此项目诞生，现正在 Prototype 阶段，逐步新增功能。

# 目标

跟之前的项目 Cipher Tools 一样，这个项目依旧会给每个版本加个代号，后端代号取自《明日方舟：终末地》角色

x：每一个大版本均为 10 个小版本（典型情况），如遇到更改技术栈/本体出现大改情况除外
y：功能更新版本
z：修补版本号

| 版本号    | 开发代号 | 对应前端开发代号 | 隶属开发阶段           |
| ------ | ------- | -------------- | ---------------- |
| v1.0.0 | Laevatain   | Ellen Chisa | Stable |
| v1.0.1 | Laevatain   | Ellen Chisa - Patch1 | Stable - Patch1*     |
| v1.1.0 | Laevatain   | Ellen Cyrene | Stable - FEP1*   |

> PatchX：修复包版本 
> 
> FEP：功能启用包


罗马不是一天建成的，为了防止墙被砌歪，将完成以下工作

- [ ] MPRIS 支持
- [ ] XDG Desktop Portal Inhibit 协议与 Logind 直连睡眠抑制器实现
- [ ] 抽象服务端 API 完善后端功能
- [ ] 完善加密音质播放功能（不提供下载）

并在未来的 FEP 版本中，加入呼声较高的功能，或未完成实现的功能。

# 协议

该项目使用 AGPLv3 及其未来版本协议协议
