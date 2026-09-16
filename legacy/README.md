# legacy/ — 冻结留档的旧代码

此目录保存重写前的原始实现，**只读参考，不参与构建与部署**，计划在 Phase 8 完成后删除。

| 路径 | 说明 |
| --- | --- |
| `backend/` | 原 Flask-AppBuilder 后端（脚手架级） |
| `frontend/` | 原 Vue 2 + Vuetify 2 前端（基于 flatlogic/material-vue 模板） |

## 保留目的

1. **模型语义参考**：`backend/app/model/` 的字段命名与业务含义，是新数据模型的设计输入（但其中的缺陷已在计划 §4.7 列出并修正）。
2. **设计基线**：`frontend/src/styles/_variables.scss`、`_base.scss`、`config.js` 与布局组件是 material-vue 设计语言的权威来源，新版前端按其令牌移植。
3. **数据搬运**：`backend/app.db` 可供 `scripts/import_legacy.py` 一次性导入用户/组/资源数据。

## 归档时已做的清理

| 清理项 | 原因 |
| --- | --- |
| 删除 `backend/app/common/k8s/config/config.yaml`、`dev.yaml` | 含**明文 kubeconfig token**，属泄漏凭证 |
| 删除 `backend/node_modules`、所有 `__pycache__` | 体积与噪音 |
| 删除 `backend/package.json`、`package-lock.json` | 仅含 axios 的无用文件 |
| 删除 `frontend/docs/` | 已构建产物，非源码 |
| `backend/config.py` 的 `SECRET_KEY` 改为环境变量读取 | 原为硬编码密钥 |
| 清除 `backend/app.db` 的提交（由 `.gitignore` 排除） | 本地运行数据 |

> ⚠️ **`config/*.yaml` 中曾存在明文 kubeconfig token（集群 `10.28.0.194`），应在集群侧吊销并轮换。** 归档只移除了工作区中的文件，无法撤销该 token 已暴露的事实。
