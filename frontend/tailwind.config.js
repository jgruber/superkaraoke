/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: [
    './index.html',
    './screen.html',
    './library.html',
    './src/**/*.{js,ts}',
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50:  '#fdf4ff',
          100: '#fae8ff',
          200: '#f5d0fe',
          300: '#f0abfc',
          400: '#e879f9',
          500: '#d946ef',
          600: '#c026d3',
          700: '#a21caf',
          800: '#86198f',
          900: '#701a75',
          950: '#4a044e',
        },
      },
    },
  },
  plugins: [
    require('@tailwindcss/forms'),
  ],
}
