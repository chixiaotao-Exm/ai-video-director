export const DEFAULT_THEME = 'midnight';

export const THEMES = Object.freeze([
  Object.freeze({
    id: 'midnight',
    name: 'Midnight Studio',
    subtitle: '午夜舞台',
    description: '深色创作空间，雾紫与薄荷色点亮节奏。适合长时间剪辑、排练与灵感实验。',
    swatches: ['#0c0e14', '#202438', '#b8a8ff', '#8fddcd'],
    mood: 'CINEMATIC · AFTER HOURS',
  }),
  Object.freeze({
    id: 'glass',
    name: 'Liquid Glass',
    subtitle: 'iOS 27 灵感概念',
    description: '借鉴苹果玻璃质感的概念主题：轻盈的层次、柔和折光和通透的蓝色界面。非官方系统复刻。',
    swatches: ['#eaf0fa', '#ffffff', '#4877e8', '#b4baff'],
    mood: 'AIRY · LIQUID · LIGHT',
  }),
  Object.freeze({
    id: 'acid',
    name: 'Acid Edit',
    subtitle: '酸橙剪辑室',
    description: '利落黑白与一抹酸橙，减少装饰，把注意力留给音乐、动作和你的作品。',
    swatches: ['#f4f4ef', '#ffffff', '#191d14', '#c9ef49'],
    mood: 'BOLD · YOUTH · ENERGY',
  }),
  Object.freeze({
    id: 'editorial',
    name: 'Sunday Editorial',
    subtitle: '周日创作刊',
    description: '纸张暖白、森林绿与衬线标题，像翻开一本只属于你的独立音乐杂志。',
    swatches: ['#f2eee7', '#fffdf8', '#46604d', '#bc8868'],
    mood: 'WARM · ART · CULTURE',
  }),
]);

export function resolveTheme(value) {
  return THEMES.some(theme => theme.id === value) ? value : DEFAULT_THEME;
}
