import type { MessageSchema } from "./en-US";

/**
 * Simplified Chinese (zh-CN) message catalog.
 *
 * Typed as `MessageSchema`, so this file cannot drift from `en-US.ts`: a missing
 * key is a compile error, and an unknown key is rejected as an excess property.
 *
 * Machine vocabulary (task states, risks) is translated here, but the *protocol
 * value* is never changed — only what the badge renders. Error codes map to a
 * sentence; the raw code and the runtime's own message stay available as detail.
 */
const zhCN: MessageSchema = {
  common: {
    refresh: "刷新",
    retry: "重试",
    clear: "清除",
    loading: "加载中…",
    somethingWentWrong: "出现了问题",
    unknownError: "未知错误",
    units: {
      bytes: "{value} B",
      kilobytes: "{value} KB",
      megabytes: "{value} MB",
    },
  },

  app: {
    tagline: "本地优先的智能体桌面基础设施",
    runtime: "运行时",
    via: "经由 {source}",
    sections: "分区",
    contactingRuntime: "正在连接本地运行时…",
    runtimeUnavailable: "运行时不可用",
    tabs: {
      overview: "概览",
      tasks: "任务",
      references: "参考图",
      logs: "日志与预览",
      settings: "设置",
    },
  },

  overview: {
    healthTitle: "运行时健康状态",
    healthMeta: "v{version} · 架构版本 {schema}",
    database: "数据库",
    uptime: "运行时长",
    uptimeValue: "{seconds} 秒",
    adaptersTitle: "适配器",
    adaptersRegistered: "已注册 {count} 个",
  },

  adapters: {
    emptyTitle: "尚未注册适配器",
    emptyDescription: "运行时注册适配器后会显示在这里。目前还没有连接任何适配器。",
  },

  tasks: {
    emptyTitle: "还没有工作流任务",
    emptyDescription: "工作流运行创建的任务会列在这里。Phase 0 尚未运行工作流。",
    columnTask: "任务",
    columnStatus: "状态",
    columnRun: "运行",
    columnUpdated: "更新时间",
  },

  logs: {
    title: "执行日志与预览（占位）",
    description: "执行记录与 Blender 预览将显示在这里。Phase 0 尚未实现。",
  },

  settings: {
    runtimeTitle: "运行时",
    runtimeDescription: "这些值由本地运行时提供，Phase 0 中为只读。",
    version: "版本",
    schemaVersion: "架构版本",
    database: "数据库",
    languageTitle: "语言",
    languageDescription: "界面语言。切换会立即生效，并在下次启动应用时保留。",
    languageLabel: "界面语言",
  },

  references: {
    title: "参考图采集",
    intro:
      "浏览器连接器会搜索参考图并在本地保存。在你选择图片并确认之前，不会有任何内容发送给 Blender。",
    searchQuery: "搜索关键词",
    searchPlaceholder: "例如：动漫角色三视图",
    candidatesLabel: "候选数量",
    collect: "开始采集",
    working: "处理中…",
    loadingTasks: "正在加载任务…",
    loadFailed: "无法加载任务",
    emptyTitle: "还没有采集任务",
    emptyDescription:
      "在上方发起一次采集。候选项会先出现在这里供你审阅，确认后才会发送给 Blender。",
    candidateCount: "{count} 个候选",
    updatedAt: "更新于 {timestamp}",
    collecting: "正在采集并下载候选图…",
    recoveryNote: "任务从中断中恢复（{action}）——{detail}",
    failuresHead: "{count} 个候选未能保存",
    selectHint: "选择本次任务要使用的图片。确认后只会为这次选择运行一次 Blender。",
    planeName: "平面名称（可选）",
    planeNamePlaceholder: "ReferencePlane",
    selectedCount: "已选 {count} 张",
    cancelTask: "取消任务",
    confirm: "确认并继续",
    confirming: "确认中…",
    confirmed: "已于 {timestamp} 确认 · {plane}，共 {count} 张图片。",
    planeFallback: "参考平面",
    blendScene: "Blender 场景",
    blendArtifact: "场景产物 {id}",
    createdAt: "创建于 {timestamp}",
    sizeUnknown: "大小未知",
    objectCount: "{count} 个对象：",
    blendNote: "浏览器无法预览 .blend 文件；请在 Blender 中打开它，或在磁盘上定位。",
    showInFolder: "在文件夹中显示",
    openInBlender: "用 Blender 打开",
    missingArtifact: "场景被记录为 {id}，但该产物已不在存储中，无法打开。",
    blendWarning: "场景已生成，但无法登记为产物：{detail}",
    untitledCandidate: "未命名候选",
    select: "选择 {title}",
    deselect: "取消选择 {title}",
    loadingPreview: "正在加载预览…",
    openSourcePage: "打开来源页面",
  },

  status: {
    ok: "正常",
    degraded: "降级",
    error: "错误",
    connected: "已连接",
    connecting: "连接中",
    disconnected: "已断开",
    running: "运行中",
    pending: "等待中",
    waiting_for_confirmation: "等待确认",
    succeeded: "成功",
    failed: "失败",
    cancelled: "已取消",
    unknown: "未知",
    not_configured: "未配置",
    read_only: "只读",
    low: "低",
    moderate: "中",
    high: "高",
    critical: "严重",
  },

  errors: {
    unavailable: "本地运行时不可用。",
    unknown: "未预期的错误：{detail}",
    detail: "（{detail}）",
    codeUnknown: "请求失败，返回了未识别的错误码（{code}）。",
    codes: {
      deskweave_error: "运行时报告了内部错误。",
      config_error: "运行时配置有误。",
      validation_error: "请求被拒绝：请求内容未通过校验。",
      not_found: "请求的对象不存在。",
      conflict: "该任务当前状态下无法修改。",
      lifecycle_error: "当前生命周期状态不允许该操作。",
      persistence_error: "存储操作失败。",
      adapter_error: "适配器失败。",
      adapter_connection_error: "适配器无法连接。",
      adapter_execution_error: "适配器执行时失败。",
      adapter_timeout_error: "适配器操作超时。",
      permission_denied: "没有执行该操作的权限。",
      unsafe_resource: "资源未通过安全检查，已被拒绝。",
      browser_access_denied: "站点拒绝自动访问。请手动打开后重试。",
      not_implemented: "该功能尚未实现。",
      artifact_open_failed: "操作系统无法打开或定位该文件。",
      unauthorized: "运行时拒绝了请求令牌。",
      internal_error: "运行时发生内部错误。",
      preview_failed: "无法加载预览。",
      interrupted: "任务被中断，未能完成。",
      no_candidates: "没有找到可用的候选图片。",
      collection_failed: "采集参考图失败。",
      target_execution_failed: "运行 Blender 失败。",
    },
  },
};

export default zhCN;
