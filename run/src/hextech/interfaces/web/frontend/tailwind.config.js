// Tailwind 构建配置，扫描 hextech Web 静态资源的类名。
// 调用方: tailwindcss CLI; 关键依赖: interfaces/web/backend/static。
// 色值须与 static/css/hextech-theme.css 的 --hx-* token 保持一致（那边是事实源）。
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "../backend/static/**/*.html",
    "../backend/static/**/*.js"
  ],
  theme: {
    extend: {
      colors: {
        hx: {
          bg: '#0a1119',
          surface: 'rgba(16, 23, 33, 0.88)',
          'border-base': 'rgba(148, 163, 184, 0.16)',
          'border-gold': 'rgba(200, 170, 110, 0.20)',
          'border-hover': 'rgba(200, 170, 110, 0.45)',
          text: '#cbd5e1',
          'text-strong': '#f1f5f9',
          'text-muted': '#94a3b8',
          accent: '#3fa9dc',
          'accent-soft': 'rgba(63, 169, 220, 0.16)',
          win: '#34d399',
          loss: '#f87171',
          'tier-gold': '#d4a72c',
          'tier-silver': '#94a3b8',
          'tier-prismatic': '#22d3ee',
          'art-gold': '#c8aa6e',
          'gold-bright': '#e3c16f',
          'art-bronze': '#785a28',
          'art-ink': 'rgba(5, 10, 16, 0.82)',
          'art-line': 'rgba(200, 170, 110, 0.34)',
        }
      },
      // 系统字体栈：拉丁/数字走 Segoe UI，中文回落雅黑。此前声明的
      // Inter/Oswald/Rajdhani 从未随包分发，属幽灵字体，已移除。
      fontFamily: {
        sans: ['"Segoe UI"', 'system-ui', '"Microsoft YaHei UI"', '"Microsoft YaHei"', 'sans-serif'],
      },
      fontSize: {
        // 11px 微字号刻度：徽章/微标签/头像胜率条，归档散落的任意值字号。
        '2xs': ['11px', { lineHeight: '1.35' }],
      },
      letterSpacing: {
        // 大写小标题统一字距，替换 0.28/0.24/0.2em 任意值全家桶。
        hx: '0.22em',
      },
    },
  },
  plugins: [],
}
