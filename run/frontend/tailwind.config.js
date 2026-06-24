/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "../hextech/display/web/static/**/*.html",
    "../hextech/display/web/static/**/*.js"
  ],
  theme: {
    extend: {
      colors: {
        hx: {
          bg: '#0d1117',
          surface: 'rgba(18, 24, 32, 0.84)',
          'border-base': 'rgba(139, 148, 158, 0.18)',
          'border-hover': 'rgba(88, 166, 255, 0.38)',
          text: '#c9d1d9',
          'text-strong': '#f0f6fc',
          'text-muted': '#8b949e',
          accent: '#58a6ff',
          'accent-soft': 'rgba(88, 166, 255, 0.16)',
          'tier-gold': '#d4a72c',
          'tier-silver': '#94a3b8',
          'tier-prismatic': '#22d3ee',
          'art-gold': '#c8aa6e',
          'art-bronze': '#785a28',
          'art-ink': 'rgba(5, 10, 16, 0.82)',
          'art-line': 'rgba(200, 170, 110, 0.34)',
        }
      },
      fontFamily: {
        sans: ['Inter', '"Microsoft YaHei UI"', '"Segoe UI"', 'Tahoma', 'Geneva', 'Verdana', 'sans-serif'],
        display: ['Oswald', 'Rajdhani', '"Microsoft YaHei UI"', 'sans-serif']
      }
    },
  },
  plugins: [],
}
