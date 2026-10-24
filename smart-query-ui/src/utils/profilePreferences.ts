export type ThemeMode = 'light' | 'dark' | 'auto'
export type AnswerDetail = 'concise' | 'standard' | 'detailed'
export type NumberFormat = 'wan' | 'yi' | 'raw'
export type AnalysisFocus = 'profitability' | 'growth' | 'risk' | 'balanced'
export type FontSize = 'compact' | 'standard' | 'comfortable'

export interface ProfilePreferences {
  nickname: string
  avatarEmoji: string
  roleLabel: string
  signature: string
  favoritePrompts: string[]
  theme: ThemeMode
  fontSize: FontSize
  answerDetail: AnswerDetail
  numberFormat: NumberFormat
  analysisFocus: AnalysisFocus
}

export const PROFILE_AVATAR_OPTIONS = ['😎', '🤓', '🧑‍💻', '🚀', '🎯', '💎', '🔥', '⭐']

export const THEME_OPTIONS: { value: ThemeMode; label: string }[] = [
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
  { value: 'auto', label: '跟随系统' },
]

export const FONT_SIZE_OPTIONS: { value: FontSize; label: string }[] = [
  { value: 'compact', label: '紧凑' },
  { value: 'standard', label: '标准' },
  { value: 'comfortable', label: '舒适' },
]

export const ANSWER_DETAIL_OPTIONS: { value: AnswerDetail; label: string }[] = [
  { value: 'concise', label: '简洁摘要' },
  { value: 'standard', label: '标准分析' },
  { value: 'detailed', label: '深度研报' },
]

export const NUMBER_FORMAT_OPTIONS: { value: NumberFormat; label: string }[] = [
  { value: 'wan', label: '万元' },
  { value: 'yi', label: '亿元' },
  { value: 'raw', label: '原始值' },
]

export const ANALYSIS_FOCUS_OPTIONS: { value: AnalysisFocus; label: string }[] = [
  { value: 'balanced', label: '均衡分析' },
  { value: 'profitability', label: '盈利能力优先' },
  { value: 'growth', label: '成长性优先' },
  { value: 'risk', label: '风险提示优先' },
]

export const ROLE_LABEL_OPTIONS = [
  '投资研究助理',
  '财务分析师',
  '基金经理',
  '风控专员',
  '行业研究员',
  '数据分析师',
]

export const DEFAULT_PROFILE_PREFERENCES: ProfilePreferences = {
  nickname: '分析师用户',
  avatarEmoji: '😎',
  roleLabel: '投资研究助理',
  signature: '偏好结合数据库与知识库进行交叉验证，快速完成财务分析与结论归纳。',
  favoritePrompts: [
    '最近一个完整年度营收前十的企业有哪些？',
    '某公司近三年净利润趋势如何？',
    '结合知识库解释公司盈利能力变化原因'
  ],
  theme: 'light',
  fontSize: 'standard',
  answerDetail: 'standard',
  numberFormat: 'wan',
  analysisFocus: 'balanced',
}

const STORAGE_KEY = 'smart-query-ui-profile-preferences'

const normalizeFavoritePrompts = (value: unknown): string[] => {
  if (!Array.isArray(value)) return [...DEFAULT_PROFILE_PREFERENCES.favoritePrompts]
  const normalized = value
    .map(item => String(item || '').trim())
    .filter(Boolean)
    .slice(0, 8)
  return normalized.length ? normalized : [...DEFAULT_PROFILE_PREFERENCES.favoritePrompts]
}

export const loadProfilePreferences = (): ProfilePreferences => {
  if (typeof window === 'undefined') {
    return {
      ...DEFAULT_PROFILE_PREFERENCES,
      favoritePrompts: [...DEFAULT_PROFILE_PREFERENCES.favoritePrompts]
    }
  }
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) {
      return {
        ...DEFAULT_PROFILE_PREFERENCES,
        favoritePrompts: [...DEFAULT_PROFILE_PREFERENCES.favoritePrompts]
      }
    }
    const parsed = JSON.parse(raw) as Partial<ProfilePreferences>
    return {
      nickname: String(parsed.nickname || '').trim() || DEFAULT_PROFILE_PREFERENCES.nickname,
      avatarEmoji: PROFILE_AVATAR_OPTIONS.includes(String(parsed.avatarEmoji || '').trim())
        ? String(parsed.avatarEmoji).trim()
        : DEFAULT_PROFILE_PREFERENCES.avatarEmoji,
      roleLabel: String(parsed.roleLabel || '').trim() || DEFAULT_PROFILE_PREFERENCES.roleLabel,
      signature: String(parsed.signature || '').trim() || DEFAULT_PROFILE_PREFERENCES.signature,
      favoritePrompts: normalizeFavoritePrompts(parsed.favoritePrompts),
      theme: (['light', 'dark', 'auto'] as ThemeMode[]).includes(parsed.theme as ThemeMode) ? parsed.theme as ThemeMode : DEFAULT_PROFILE_PREFERENCES.theme,
      fontSize: (['compact', 'standard', 'comfortable'] as FontSize[]).includes(parsed.fontSize as FontSize) ? parsed.fontSize as FontSize : DEFAULT_PROFILE_PREFERENCES.fontSize,
      answerDetail: (['concise', 'standard', 'detailed'] as AnswerDetail[]).includes(parsed.answerDetail as AnswerDetail) ? parsed.answerDetail as AnswerDetail : DEFAULT_PROFILE_PREFERENCES.answerDetail,
      numberFormat: (['wan', 'yi', 'raw'] as NumberFormat[]).includes(parsed.numberFormat as NumberFormat) ? parsed.numberFormat as NumberFormat : DEFAULT_PROFILE_PREFERENCES.numberFormat,
      analysisFocus: (['profitability', 'growth', 'risk', 'balanced'] as AnalysisFocus[]).includes(parsed.analysisFocus as AnalysisFocus) ? parsed.analysisFocus as AnalysisFocus : DEFAULT_PROFILE_PREFERENCES.analysisFocus,
    }
  } catch {
    return {
      ...DEFAULT_PROFILE_PREFERENCES,
      favoritePrompts: [...DEFAULT_PROFILE_PREFERENCES.favoritePrompts]
    }
  }
}

export const saveProfilePreferences = (prefs: ProfilePreferences): void => {
  if (typeof window === 'undefined') return
  const payload: ProfilePreferences = {
    nickname: String(prefs.nickname || '').trim() || DEFAULT_PROFILE_PREFERENCES.nickname,
    avatarEmoji: PROFILE_AVATAR_OPTIONS.includes(String(prefs.avatarEmoji || '').trim())
      ? String(prefs.avatarEmoji).trim()
      : DEFAULT_PROFILE_PREFERENCES.avatarEmoji,
    roleLabel: String(prefs.roleLabel || '').trim() || DEFAULT_PROFILE_PREFERENCES.roleLabel,
    signature: String(prefs.signature || '').trim() || DEFAULT_PROFILE_PREFERENCES.signature,
    favoritePrompts: normalizeFavoritePrompts(prefs.favoritePrompts),
    theme: prefs.theme || DEFAULT_PROFILE_PREFERENCES.theme,
    fontSize: prefs.fontSize || DEFAULT_PROFILE_PREFERENCES.fontSize,
    answerDetail: prefs.answerDetail || DEFAULT_PROFILE_PREFERENCES.answerDetail,
    numberFormat: prefs.numberFormat || DEFAULT_PROFILE_PREFERENCES.numberFormat,
    analysisFocus: prefs.analysisFocus || DEFAULT_PROFILE_PREFERENCES.analysisFocus,
  }
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
}
